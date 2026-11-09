"""A leitura da janela de contexto para o Duo: `GET /api/conversas/{id}/janela`.

Em plugin, quem tem o histórico é o servidor, então o medidor e o painel do Duo só mostram o que vem
daqui. Os números são os do próximo envio, pela mesma conta do turno (`envio.montar`), com o próximo
pedido vazio: assim o último turno já conta como anterior, como vai contar de fato.

O modelo não é chamado: a leitura é frequente e não pode disputar o motor com uma resposta em curso.
Por isso o total é a estimativa corrigida pelo fator gravado no último envio, que `fonte` identifica.
"""

from __future__ import annotations

import math
from typing import Iterator

from . import envio, historico, janela
from .armazenamento import RAIZ
from .backends.base import ErroBackend

# O começo do pedido, para o painel mostrar o que é cada turno.
TAMANHO_ROTULO = 80
ID_PROXIMO_PEDIDO = "proximo-pedido"


def folha_atual(conversa: dict) -> str:
    """O ponto de onde sai o próximo envio: o último nó gravado.

    Um pedido sem resposta (parado ou com erro) fica de fora: o Duo manda o envio seguinte pelo pai
    dele, então ele não vai ao modelo.
    """
    nos = conversa.get("chat_messages") or []
    if not nos:
        return RAIZ
    ultimo = nos[-1]
    return ultimo.get("parent_message_uuid") or RAIZ if ultimo.get("sender") == "human" else ultimo["uuid"]


def _proximo_pedido() -> dict:
    return {"uuid": ID_PROXIMO_PEDIDO, "sender": "human", "text": "", "content": [], "attachments": [], "files": []}


def _rotulo(mensagem: dict | None) -> str:
    if mensagem is None:
        return ""
    return " ".join(mensagem.get("texto_sem_anexos", mensagem.get("texto", "")).split())[:TAMANHO_ROTULO]


def _com_fator(tokens: int, fator: float) -> int:
    return math.ceil(tokens * fator)


def _resumo_publico(resumo: dict, ficou: bool) -> dict:
    """O resumo como o painel o lê. `incluido`: a janela pode tê-lo tirado, e o usuário tem de saber o que o modelo vê."""
    return {"texto": resumo["texto"], "tokens_antes": resumo["tokens_antes"], "tokens_depois": resumo["tokens_depois"],
            "ate": resumo["ate_no"], "modelo": resumo["modelo"], "incluido": ficou,
            **({} if ficou else {"motivo": "corte"})}


def _listados(nos: list[dict]) -> Iterator[tuple[str, list[dict], list[dict]]]:
    """(id, mensagens e resultados) de cada turno que o painel lista.

    Os resultados internos não são itens, pois o usuário nunca os viu. O que o resumo substituiu não aparece,
    porque não há o que decidir sobre ele enquanto o resumo valer, e o resumo vem no campo próprio, com como desfazer.
    """
    for id_turno, turno in zip(historico.ids_dos_turnos(nos), historico.para_turnos(nos, "nativo")):
        if id_turno != historico.ID_RESUMO:
            yield id_turno, turno, [item for m in turno for item in janela.resultados_de(m) if not item.get("interno")]


def ids_visiveis(conversa: dict, resumo: dict | None) -> set[str]:
    """Os ids que o `GET /janela` lista agora; o `PUT` só pode tirar da lista o que o usuário está vendo."""
    nos = historico.com_resumo(historico.ramo(conversa, folha_atual(conversa)), resumo)
    ids: set[str] = set()
    for id_turno, _, resultados in _listados(nos):
        ids.add(id_turno)
        ids.update(item["id"] for item in resultados)
    return ids


def _itens(nos: list[dict], mantidos: dict[str, list[dict]], excluidos: set[str], jan: janela.ResultadoJanela,
           fator: float) -> list[dict]:
    """Os turnos e os resultados do ramo, com o tamanho de cada um e, se está fora, o motivo.

    O ponto de partida são os turnos sem exclusão nenhuma, porque o painel lista também o que o usuário
    tirou, para ele poder pôr de volta. A forma é sempre a do modo nativo, em que cada resultado é uma
    mensagem; o estado de cada item vem do que `janela.ajustar` deixou, nos dois modos. O peso dos resultados
    internos entra no do turno.
    """
    na_janela = {item["id"]: item for turno in jan.turnos for m in turno for item in janela.resultados_de(m)}
    itens: list[dict] = []
    for id_turno, turno, resultados in _listados(nos):
        ids = {item["id"] for item in resultados}
        sem_resultados = [m for m in turno if m.get("id_chamada") not in ids]
        restante = mantidos.get(id_turno)
        retirado = id_turno in excluidos
        cortado = restante == []
        # Sobrou só o pedido: o 1º turno, fixado na janela, perde a resposta e os anexos e continua dentro. A conta é
        # esta, e não "ficou menor", porque nas cercas vários resultados dividem uma mensagem.
        parcial = not retirado and restante is not None and len(restante) == 1 < len(turno)
        peso = sum(map(janela.peso_mensagem, restante if parcial else sem_resultados))
        item_turno = {"id": id_turno, "tipo": "turno", "rotulo": _rotulo(janela.pedido_de(turno)),
                      "tokens": _com_fator(peso, fator), "incluido": not (retirado or cortado)}
        if retirado or cortado:
            item_turno["motivo"] = "usuario" if retirado else "corte"
        elif parcial:
            item_turno["parcial"] = True
        itens.append(item_turno)
        for item in resultados:
            atual = na_janela.get(item["id"])
            fora = atual is None or "omitido" in atual or "excluido" in atual
            pelo_usuario = retirado or item["id"] in excluidos
            # Fora, o tamanho é o que ele voltaria a ocupar; dentro, o que ocupa (pode estar truncado).
            peso = janela.estimar_tokens(item["conteudo"] if fora else atual["conteudo"])
            item_resultado = {"id": item["id"], "tipo": "resultado", "turno": id_turno, "rotulo": janela.nome_de(item),
                              "tokens": _com_fator(peso, fator), "incluido": not fora}
            if fora:
                item_resultado["motivo"] = "usuario" if pelo_usuario else "corte"
            itens.append(item_resultado)
    return itens


def medir(servico, conversa: dict, modelo_pedido: str | None, contexto_pedido: str | None) -> dict:
    """A resposta de `GET /janela`; `modelo` e `contexto` vêm da consulta, e faltando vale o último envio."""
    cfg, armazenamento = servico.cfg, servico.armazenamento
    cid = conversa["uuid"]
    # Antes do estado: a compactação pode terminar no meio desta leitura. Na outra ordem, sairia uma
    # resposta sem compactação em andamento e sem resumo, e o Duo pararia de acompanhar sem o resultado.
    andamento = servico.compactacao_publica(cid)
    modelo = envio.resolver_modelo(cfg, modelo_pedido or conversa.get("model"))
    try:
        info = servico.backend.info_modelo(modelo)
    except ErroBackend:
        info = None  # motor fora do ar: o medidor continua respondendo, e o erro aparece ao conversar
    estado = armazenamento.estado_janela(cid) or {}
    guardado = estado.get("ultimo_envio") or {}
    # O que o Duo mandou é dado externo: um `opcoes` torto vale como nenhum, como no envio.
    opcoes_pedidas = dict(guardado["opcoes"]) if isinstance(guardado.get("opcoes"), dict) else {}
    if contexto_pedido:
        opcoes_pedidas["contexto"] = contexto_pedido
    opcoes = envio.mesclar_opcoes(cfg, opcoes_pedidas)
    preparo = envio.preparar(cfg, servico.sistema_padrao, armazenamento, conversa, guardado, opcoes, modelo, info)
    fator, ultimo_uso = envio.medicao_da_conversa(conversa, estado or None, modelo)
    # O pedido em curso não sai, como no turno.
    excluidos = set(estado.get("excluidos") or []) - {ID_PROXIMO_PEDIDO}
    resumo = estado.get("resumo")

    nos = historico.ramo(conversa, folha_atual(conversa))
    jan, _ = envio.montar(preparo, nos + [_proximo_pedido()], excluidos, resumo, fator)
    com_resumo = historico.com_resumo(nos, resumo)
    # O que ficou de cada turno, pelo id: a janela devolve na ordem em que recebeu, sem dizer de qual é cada um.
    mantidos = dict(zip(historico.ids_dos_turnos(historico.sem_excluidos(com_resumo, excluidos)), jan.turnos))

    usado = _com_fator(jan.estimativa, fator)
    # Para baixo: o resto vai para o histórico, e as seções têm de somar exatamente o total.
    sistema, ferramentas = int(jan.peso_sistema * fator), int(jan.peso_ferramentas * fator)
    if preparo.cercas:
        # Nas cercas o catálogo vai no texto do sistema, mas o usuário quer vê-lo como ferramentas.
        ferramentas = min(sistema, int(janela.estimar_tokens(preparo.catalogo_em_texto(jan.leitura)) * fator))
        sistema -= ferramentas
    return {
        "limite": opcoes.contexto_tokens,
        "reserva": janela.reserva_resposta(opcoes.contexto_tokens),
        "usado": usado,
        "fonte": "real" if ultimo_uso is not None else "estimado",
        "ultimo_uso": ultimo_uso,
        "secoes": {"sistema": sistema, "ferramentas": ferramentas, "historico": usado - sistema - ferramentas},
        "itens": _itens(com_resumo, mantidos, excluidos, jan, fator),
        # A lista gravada: pelos `itens`, um resultado tirado à parte não se distingue do que saiu com o turno.
        "excluidos": list(estado.get("excluidos") or []),
        "compactacao": andamento,
        # Só o que vale neste ramo: mostrar outro diria ao usuário que o modelo vê o que ele não vê.
        "resumo": _resumo_publico(resumo, bool(mantidos.get(historico.ID_RESUMO))) if com_resumo is not nos else None,
        # Antes do 1º envio, o servidor ainda não conhece o `system` nem as ferramentas.
        "provisorio": not guardado,
    }
