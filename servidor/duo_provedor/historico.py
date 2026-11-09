"""Converte a árvore de mensagens armazenada no formato neutro dos backends.

A conversa guarda nós no formato do claude.ai (com blocos `text`, `thinking`, `tool_use`,
`tool_result`); os backends só conhecem o formato neutro descrito em `backends/base.py`.
"""

import base64
import copy
import json
from typing import Callable

from . import emulacao


TEXTO_ORFA = "[a ferramenta não foi executada: o turno foi interrompido]"
MARCA_RETIRADO = "[resultado retirado da janela pelo usuário]"
MARCA_RESUMO = "Resumo da conversa até aqui, gerado pelo modelo; confira nos autos antes de citar:"
# Como começa o texto que a compactação manda resumir; o backend simulado a reconhece por aqui.
ABERTURA_RESUMO = "Trecho a resumir:\n\n"
# O id do turno que o resumo vira: não é um nó de verdade, e o painel não o lista.
ID_RESUMO = "resumo-da-conversa"


def _sem_lembretes(no: dict) -> dict:
    conteudo = no.get("content")
    if not isinstance(conteudo, list) or not any(_eh_lembrete(b) for b in conteudo):
        return no
    return {**no, "content": [b for b in conteudo if not _eh_lembrete(b)]}


def _eh_lembrete(bloco: object) -> bool:
    return isinstance(bloco, dict) and bloco.get("type") == "correcao"


def so_lembretes(no: dict) -> bool:
    """Nó sem nada além do que o servidor falou com o modelo: não há resposta a gravar."""
    return all(_eh_lembrete(b) for b in no.get("content") or [])


def ramo(conversa: dict, folha: str) -> list[dict]:
    """Nós gravados, da raiz até a folha, seguindo `parent_message_uuid`. Folha desconhecida dá [].

    Sem os lembretes do servidor ao modelo: eles valem só para o turno em que nasceram, que os tem no nó ainda em
    curso. No seguinte, "não chame mais ferramentas" e "sua última resposta veio vazia" só o atrapalhariam.
    """
    por_uuid = {n["uuid"]: n for n in conversa.get("chat_messages", [])}
    caminho = []
    visto = set()
    atual = por_uuid.get(folha)
    # `visto` protege contra ciclo em dados corrompidos: sem ele o laço não terminaria.
    while atual is not None and atual["uuid"] not in visto:
        visto.add(atual["uuid"])
        caminho.append(_sem_lembretes(atual))
        atual = por_uuid.get(atual.get("parent_message_uuid"))
    caminho.reverse()
    return caminho


def texto_resultado(content: object) -> str:
    """Texto de um `tool_result`: string direta, ou junção dos `text` de uma lista de blocos."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        # `text` chega do Duo e pode vir como número ou null: str() evita o TypeError do join.
        return "".join(
            str(b.get("text") or "") for b in content if isinstance(b, dict) and b.get("type") == "text"
        )
    return ""


def conversa_visivel(conversa: dict) -> dict:
    """Cópia para a interface: sem `ferramentas_carregadas` e sem blocos internos."""
    copia = copy.deepcopy(conversa)
    copia.pop("ferramentas_carregadas", None)
    for no in copia.get("chat_messages", []):
        if isinstance(no.get("content"), list):
            no["content"] = [b for b in no["content"] if not (isinstance(b, dict) and b.get("interno"))]
    return copia


def ids_dos_turnos(nos: list[dict]) -> list[str]:
    """O uuid do nó que abre cada turno, na mesma ordem de `para_turnos`.

    Cada `human` abre um; um histórico que não começa num pedido tem antes um turno sem ele.
    """
    return [no["uuid"] for posicao, no in enumerate(nos) if no.get("sender") == "human" or posicao == 0]


def _blocos(no: dict) -> list:
    conteudo = no.get("content")
    return conteudo if isinstance(conteudo, list) else []


def resultado_com_erro(bloco: dict) -> bool:
    """Se o resultado voltou com erro. O Duo responde `{"erro": ...}` com `is_error` falso, então olha o conteúdo."""
    if bloco.get("is_error"):
        return True
    try:
        dado = json.loads(texto_resultado(bloco.get("content")))
    except (TypeError, ValueError):
        return False
    return isinstance(dado, dict) and bool(dado.get("erro"))


def _sem_vazias_que_falharam(blocos: list) -> list:
    """Tira a chamada sem argumentos que voltou com erro, com o resultado dela: só o modelo deixa de vê-la.

    Com uma dessas à vista, o modelo a imita (medido: 0 de 12 trazem o argumento), inclusive em conversa gravada antes
    da validação no turno. A árvore e o que o Duo relê não mudam.
    """
    vazias = {b.get("id") for b in blocos
              if b.get("type") == "tool_use" and not b.get("input") and not b.get("interno")}
    if not vazias:
        return blocos
    falhas = {b.get("tool_use_id") for b in blocos
              if b.get("type") == "tool_result" and b.get("tool_use_id") in vazias and resultado_com_erro(b)}
    return [b for b in blocos
            if not (b.get("type") == "tool_use" and b.get("id") in falhas)
            and not (b.get("type") == "tool_result" and b.get("tool_use_id") in falhas)]


def _sem_resultados_retirados(no: dict, retirados: set[str]) -> dict:
    def retirado(bloco: object) -> bool:
        if not isinstance(bloco, dict) or bloco.get("type") != "tool_result":
            return False
        trecho = bloco.get("trecho")
        # Uma leitura feita antes de o usuário tirar o resultado guarda um pedaço dele.
        return bloco.get("tool_use_id") in retirados or (isinstance(trecho, dict) and trecho.get("alvo") in retirados)

    blocos = _blocos(no)
    if not any(map(retirado, blocos)):
        return no
    return {**no, "content": [{**b, "content": MARCA_RETIRADO, "excluido": True} if retirado(b) else b
                              for b in blocos]}


def texto_do_resumo(texto: str) -> str:
    return f"{MARCA_RESUMO}\n\n{texto}"


def com_resumo(nos: list[dict], resumo: dict | None) -> list[dict]:
    """O ramo com o trecho até `ate_no` trocado pelo resumo; os turnos originais continuam na árvore.

    Só vale para o ramo em que ele foi feito: quem volta a um ponto anterior e segue por outro caminho
    conversa sem aquele trecho, e o resumo o traria de volta.
    """
    if resumo:
        for posicao, no in enumerate(nos):
            if no.get("uuid") == resumo["ate_no"]:
                pedido = {"uuid": ID_RESUMO, "sender": "human", "text": texto_do_resumo(resumo["texto"]),
                          "content": [], "attachments": [], "files": [], "resumo": True}
                return [pedido] + nos[posicao + 1:]
    return nos


def sem_excluidos(nos: list[dict], excluidos: set[str]) -> list[dict]:
    """O ramo sem o que o usuário tirou da janela: um id de turno leva o turno, um de chamada, o resultado.

    O resultado vira uma marca, e não some, porque os motores nativos rejeitam chamada sem resposta. A
    árvore gravada não muda: isto é só o que o modelo vê, e o usuário pode pôr de volta.
    """
    if not excluidos:
        return nos
    retirados = set(excluidos)
    mantidos, fora = [], False
    for posicao, no in enumerate(nos):
        if no.get("sender") == "human" or posicao == 0:
            fora = no.get("uuid") in excluidos
        if fora:
            retirados.update(b.get("tool_use_id") for b in _blocos(no)
                             if isinstance(b, dict) and b.get("type") == "tool_result")
        else:
            mantidos.append(no)
    return [_sem_resultados_retirados(no, retirados) for no in mantidos]


def _mensagem_usuario(no: dict, obter_arquivo, visao: bool) -> dict:
    texto = no.get("text") or ""
    anexos = no.get("attachments") or []
    for a in anexos:
        texto += "\n" + emulacao.anexo_inline(a.get("file_name", "anexo"), a.get("extracted_content") or "")
    imagens = []
    if obter_arquivo:
        # `files` guarda os ids devolvidos pelo upload (strings); outros itens são ignorados.
        for fid in no.get("files") or []:
            if not isinstance(fid, str):
                continue
            achado = obter_arquivo(fid)
            if not achado:
                continue
            nome, mime, dados = achado
            if mime.startswith("image/") and visao:
                imagens.append(base64.b64encode(dados).decode())
            else:
                texto += f"\n[anexo {nome} ({mime}) não pode ser lido por este modelo]"
    msg = {"papel": "usuario", "texto": texto}
    # A janela o trata à parte: é o último dos anteriores a sair.
    if no.get("resumo"):
        msg["resumo"] = True
    if anexos:
        msg["texto_sem_anexos"] = no.get("text") or ""
    if imagens:
        msg["imagens"] = imagens
    return msg


def para_turnos(
    nos: list[dict],
    modo: str,
    obter_arquivo: Callable[[str], tuple[str, str, bytes] | None] | None = None,
    visao: bool = False,
) -> list[list[dict]]:
    """Agrupa os nós em turnos (cada nó `human` abre um) de mensagens neutras.

    `modo` é "nativo" (chamadas estruturadas) ou "cercas" (chamadas e resultados em texto). Os
    resultados vão inteiros: quem decide o que cabe é a janela, que enxerga o espaço de verdade.
    """
    turnos: list[list[dict]] = []
    atual: list[dict] | None = None

    def turno() -> list[dict]:
        nonlocal atual
        if atual is None:
            atual = []
            turnos.append(atual)
        return atual

    for no in nos:
        if no.get("sender") == "human":
            atual = [_mensagem_usuario(no, obter_arquivo, visao)]
            turnos.append(atual)
            continue

        saida = turno()
        assistente: dict | None = None
        nomes: dict[str, str] = {}
        pendentes: list[dict] = []  # resultados do modo cercas, ainda não emitidos

        def fechar_resultados():
            nonlocal pendentes
            if pendentes:
                # A marca separa o resultado do pedido humano, que também é `usuario`. Os itens
                # ficam à parte porque aqui vários vão numa mensagem só, e a janela limpa e trunca
                # um por vez e refaz o texto.
                saida.append({"papel": "usuario", "texto": emulacao.mensagem_resultados(pendentes),
                              "resultado": True, "itens": pendentes})
            pendentes = []

        def fechar_assistente():
            nonlocal assistente
            assistente = None

        def corrente() -> dict:
            nonlocal assistente
            if assistente is None:
                assistente = {"papel": "assistente", "texto": ""}
                saida.append(assistente)
            return assistente

        blocos = _sem_vazias_que_falharam(no.get("content") or [])
        com_resultado = {b.get("tool_use_id") for b in blocos if b.get("type") == "tool_result"}
        orfas: list[tuple[str, str]] = []  # tool_use sem tool_result (turno interrompido)

        def emitir_orfas():
            # Backends nativos rejeitam chamada sem resposta: sintetiza o resultado no lugar.
            nonlocal orfas
            if not orfas:
                return
            fechar_assistente()
            for id_, nome in orfas:
                if modo == "nativo":
                    saida.append({"papel": "ferramenta", "id_chamada": id_, "nome": nome,
                                  "texto": TEXTO_ORFA})
                else:
                    pendentes.append({"id": id_, "nome": nome, "conteudo": TEXTO_ORFA, "erro": True})
            orfas = []

        for bloco in blocos:
            tipo = bloco.get("type")
            if tipo != "tool_use":
                emitir_orfas()
            if tipo != "tool_result":
                fechar_resultados()
            if tipo == "text":
                corrente()["texto"] += bloco.get("text", "")
            elif tipo == "tool_use":
                entrada = bloco.get("input") or {}
                nomes[bloco.get("id")] = bloco.get("name")
                if bloco.get("id") not in com_resultado:
                    orfas.append((bloco.get("id"), bloco.get("name")))
                if modo == "nativo":
                    corrente().setdefault("chamadas", []).append(
                        {"id": bloco.get("id"), "nome": bloco.get("name"), "argumentos": entrada}
                    )
                else:
                    a = corrente()
                    separador = "\n" if a["texto"] else ""
                    a["texto"] += separador + emulacao.chamada_como_texto(bloco.get("name"), entrada)
            elif tipo == "tool_result":
                texto = texto_resultado(bloco.get("content"))
                nome = nomes.get(bloco.get("tool_use_id"), "")
                fechar_assistente()
                if modo == "nativo":
                    resultado = {"papel": "ferramenta", "id_chamada": bloco.get("tool_use_id"),
                                 "nome": nome, "texto": texto}
                else:
                    resultado = {"id": bloco.get("tool_use_id"), "nome": nome, "conteudo": texto,
                                 "erro": bool(bloco.get("is_error"))}
                # A janela não nomeia ao usuário as chamadas que ele nunca viu.
                if bloco.get("interno"):
                    resultado["interno"] = True
                # Quem tirou foi o usuário: a janela não conta como perda nem aponta como ler.
                if bloco.get("excluido"):
                    resultado["excluido"] = True
                if modo == "nativo":
                    saida.append(resultado)
                else:
                    pendentes.append(resultado)
            elif tipo == "correcao":
                fechar_assistente()
                saida.append({"papel": "usuario", "texto": bloco.get("texto", "")})
            # `thinking` e demais tipos não voltam ao modelo.
        emitir_orfas()
        fechar_resultados()
    return turnos
