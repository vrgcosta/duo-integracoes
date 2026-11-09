"""O que vai ao modelo numa rodada: uma conta só, para o turno e para o medidor da janela.

O medidor do Duo tem de dizer o que o próximo envio vai usar, e duas contas separadas acabariam
divergindo. Por isso o `turno` e o `medidor` passam pelas mesmas duas funções: `preparar`, que junta o
que não muda de uma rodada para outra, e `montar`, que aplica o que o usuário tirou e ajusta à janela.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Callable

from . import emulacao, historico, janela
from .backends.base import InfoModelo
from .catalogo import Catalogo
from .ferramentas import modo_ferramentas, para_funcoes

MODOS_FERRAMENTAS = ("padrao", "auto", "nativo", "cercas")
# O que o medidor precisa rever do último envio: o Duo manda isso a cada mensagem e não na leitura da janela.
_CAMPOS_DO_ENVIO = ("system", "tools", "catalogo", "opcoes")
# Abaixo disso da estimativa, com ela acima da janela, o número do motor não é de todo o pedido (ver `contagem_suspeita`).
LIMITE_CONTAGEM_SUSPEITA = 0.7


@dataclass
class Opcoes:
    contexto_tokens: int
    temperatura: float | None
    ferramentas: str
    max_rodadas: int
    espera_primeiro_token: int
    limite_resultado: int


def _numero(valor: object) -> float | None:
    # O Duo grava o que o usuário digitou; número em texto vale, o resto é ignorado.
    if isinstance(valor, bool):
        return None
    if isinstance(valor, str):
        try:
            valor = float(valor.strip())
        except ValueError:
            return None
    if isinstance(valor, (int, float)) and math.isfinite(valor):
        return float(valor)
    return None


def _limite_resultado(configurado: object) -> int:
    """O teto em tokens por resultado escolhido na config; 0 (ou valor inválido) é sem teto."""
    limite = _numero(configurado)
    return int(limite) if limite is not None and limite >= 1 else 0


def mesclar_opcoes(cfg: dict, pedido: dict | None) -> Opcoes:
    """Opções do Duo por cima da config. 0 / -1 / "padrao" e valores inválidos ficam com o cfg."""
    p = pedido if isinstance(pedido, dict) else {}
    contexto, temperatura, rodadas = (_numero(p.get(c)) for c in ("contexto", "temperatura", "max_rodadas"))
    ferramentas = p.get("ferramentas")
    return Opcoes(
        contexto_tokens=int(contexto) if contexto is not None and contexto >= 1 else int(cfg["contexto_tokens"]),
        temperatura=temperatura if temperatura is not None and temperatura >= 0 else cfg.get("temperatura"),
        # "padrao" não é resolvido aqui: modo_ferramentas ainda consulta ferramentas_por_modelo.
        ferramentas=ferramentas if ferramentas in MODOS_FERRAMENTAS else "padrao",
        max_rodadas=int(rodadas) if rodadas is not None and rodadas >= 1 else int(cfg["max_rodadas"]),
        espera_primeiro_token=cfg["espera_primeiro_token"],
        limite_resultado=_limite_resultado(cfg.get("limite_resultado")),
    )


def resolver_modelo(cfg: dict, modelo: object) -> str:
    return cfg["modelo_padrao"] if modelo in (None, "", "padrao") else modelo


def medicao_da_conversa(conversa: dict, estado: dict | None, modelo: str) -> tuple[float, int | None]:
    """(fator, último uso) gravados, se foram medidos com este modelo: cada um conta do seu jeito."""
    if estado is None or conversa.get("model") != modelo:
        return 1.0, None
    return estado["fator_tokens"], estado["ultimo_uso"]


def para_guardar(corpo: dict) -> dict:
    return {campo: corpo.get(campo) for campo in _CAMPOS_DO_ENVIO}


def montar_janela(catalogo: Catalogo, cercas: bool, partes: list[str], sistema_duo: str, turnos: list[list[dict]],
                  opcoes: Opcoes, fator_tokens: float,
                  com_ferramentas: bool = True) -> tuple[janela.ResultadoJanela, list[dict]]:
    """As mensagens ajustadas à janela e as ferramentas visíveis.

    A leitura só entra no catálogo quando há marca na janela, para não pesar em toda conversa; mas o peso
    dela é decidido por `janela.ajustar`, que o reserva assim que algo precisa sair. Decidir aqui, com um
    ajuste sem ela e outro com ela conforme o primeiro deixasse marca, faria o peso ir e vir de um envio
    para outro, e o que saiu por causa dele voltaria à janela.

    Sem ferramentas, que é a rodada final do turno, as definições não vão ao modelo em nenhum dos modos e
    não contam: reservar o peso delas truncaria resultados por causa de algo que não será enviado.
    """

    def versao(com_leitura: bool) -> tuple[str, int]:
        visiveis = catalogo.visiveis(com_leitura) if com_ferramentas else []
        textos = list(partes)
        if cercas and visiveis:
            textos.append(emulacao.catalogo_texto(visiveis))
        # No modo nativo as definições vão no campo `tools`, fora do texto, mas o motor as
        # escreve no prompt, e o catálogo completo do Duo (21 ferramentas) pesa milhares de
        # tokens. Nas cercas o catálogo já está no texto do sistema e já é contado.
        peso_ferramentas = 0 if cercas or not visiveis else janela.estimar_tokens(
            json.dumps(para_funcoes(visiveis), ensure_ascii=False))
        return "\n\n".join(p for p in textos if p), peso_ferramentas

    sistema, peso_ferramentas = versao(False)
    # Sem as instruções do Duo, o 1º pedido traz o preâmbulo da tarefa: fixá-lo na janela.
    jan = janela.ajustar(sistema, sistema_duo, turnos, opcoes.contexto_tokens, fixar_primeiro=not sistema_duo,
                         peso_ferramentas=peso_ferramentas, fator_tokens=fator_tokens,
                         limite_resultado=opcoes.limite_resultado,
                         com_leitura=janela.ComLeitura(*versao(True)) if com_ferramentas else None,
                         sem_ferramentas=not com_ferramentas)
    return jan, catalogo.visiveis(jan.leitura) if com_ferramentas else []


@dataclass
class Preparo:
    """O que entra na conta da janela e não muda de uma rodada para outra."""

    catalogo: Catalogo
    cercas: bool
    partes: list[str]
    sistema_duo: str
    opcoes: Opcoes
    obter_arquivo: Callable[[str], tuple[str, str, bytes] | None]
    visao: bool

    def catalogo_em_texto(self, com_leitura: bool) -> str:
        visiveis = self.catalogo.visiveis(com_leitura)
        return emulacao.catalogo_texto(visiveis) if self.cercas and visiveis else ""


def preparar(cfg: dict, sistema_padrao: str, armazenamento, conversa: dict, corpo: dict, opcoes: Opcoes, modelo: str,
             info: InfoModelo | None) -> Preparo:
    """`corpo` é o envio do Duo ou, no medidor, o último envio gravado (`para_guardar`)."""
    catalogo = Catalogo(corpo.get("tools") or [], corpo.get("catalogo") or None, cfg.get("ferramentas_ativas") or [],
                        conversa.get("ferramentas_carregadas") or [])
    projeto = conversa.get("project_uuid")
    instrucoes = armazenamento.instrucoes_projeto(projeto) if projeto else ""
    return Preparo(catalogo, modo_ferramentas(opcoes.ferramentas, cfg, modelo, info) == "cercas",
                   [sistema_padrao, instrucoes], corpo.get("system") or "", opcoes, armazenamento.obter_arquivo,
                   info.visao if info else False)


def contagem_suspeita(exato: int, jan: janela.ResultadoJanela, contexto: int) -> bool:
    """O número do motor está tão abaixo da estimativa que ele deve ter cortado antes de contar?

    Com `truncate: false`, o Ollama recusa o que passa da janela. Um que ignore o campo tira as mensagens antigas e conta
    o que sobrou: 840 para 2346 reais em `num_ctx` 2048, abaixo do orçamento e sem saturar. Nada denuncia isso, salvo a
    estimativa, sem fator, passar da janela e o número vir bem abaixo dela; medida, ela erra uns 17% para mais.
    """
    return exato < contexto - 1 and jan.estimativa > contexto and exato < jan.estimativa * LIMITE_CONTAGEM_SUSPEITA


def aviso_com_contagem(jan: janela.ResultadoJanela, exato: int, contexto: int) -> str | None:
    """Com o número do motor, nada foi cortado: o risco de passar do espaço útil é a resposta faltar."""
    if exato <= jan.orcamento:
        return None
    return (f"O pedido tem {exato} tokens numa janela de {contexto}: sobram só {contexto - exato} para a resposta, "
            "que pode sair cortada. Aumente a janela nas opções do plugin ou compacte a conversa.")


def aviso_sem_contagem(jan: janela.ResultadoJanela, contexto: int, motivo: str, primeiro_envio: bool,
                       pode_cortar_calado: bool) -> str | None:
    """O que dizer de um pedido que vai sem o número do motor. Sem ele, ninguém tem como saber se foi cortado.

    `motivo`: "falhou" (a contagem falhou, demorou ou veio torta), "esgotou" (a última montagem vai sem conferir, depois
    de todas as tentativas) ou "nao_conta" (o motor nunca conta). Este último fala uma vez por conversa, e não a cada
    resposta: o aviso fica no texto, volta ao modelo a cada turno e, repetido em todas, ninguém mais o lê. Passou do
    espaço útil pela estimativa, avisa sempre.
    """
    conselho = "Aumente a janela nas opções do plugin ou compacte a conversa."
    medida = "A medida é estimada e o pedido foi enviado assim mesmo: " + (
        "o motor pode cortar o começo sem avisar." if pode_cortar_calado else
        "se não couber, o motor recusa, em vez de cortar.")
    tamanho = (f"Pela estimativa, o pedido tem ~{jan.total} tokens, mais que o espaço útil da janela ({jan.orcamento} de "
               f"{contexto})." if jan.excede else
               f"Pela estimativa, o pedido tem ~{jan.total} tokens, de {jan.orcamento} que cabem.")
    if motivo == "falhou":
        return f"A contagem do motor falhou nesta resposta. {tamanho} {medida} {conselho if jan.excede else ''}".rstrip()
    if motivo == "esgotou":
        return (f"O motor contou acima do espaço útil em todas as tentativas, e o pedido foi cortado de novo e enviado "
                f"sem conferir. {tamanho} {medida} {conselho}")
    if jan.excede:
        return f"{tamanho} {medida} {conselho}"
    if primeiro_envio and pode_cortar_calado:
        return ("Este motor não informa a contagem de tokens: os números desta conversa são estimados, e ele pode cortar "
                "o começo de um pedido grande sem avisar.")
    return None


def aviso_de_suspeita(jan: janela.ResultadoJanela, exato: int) -> str:
    return (f"O motor contou {exato} tokens, bem menos que os ~{jan.estimativa} estimados: ele pode ter cortado o pedido "
            "sem avisar. Isso acontece num Ollama que não conhece `truncate: false`: atualize-o, ou aumente a janela nas "
            "opções do plugin.")


def _candidatos_do_erro(jan: janela.ResultadoJanela, humano: dict) -> list[tuple[str, int]]:
    """O que pode ser o motivo de não caber, com o peso estimado. As definições não vão: o pedido já está sem elas."""
    itens = [("as instruções do sistema", jan.peso_sistema),
             ("o texto da mensagem", janela.estimar_tokens(humano.get("text") or ""))]
    for anexo in humano.get("attachments") or []:
        nome = anexo.get("file_name", "anexo")
        itens.append((f'o anexo "{nome}"',
                      janela.estimar_tokens(emulacao.anexo_inline(nome, anexo.get("extracted_content") or ""))))
    # Depois de todos os cortes, dos turnos anteriores só resta o que é fixo: o 1º pedido, sem `system`, que numa
    # conversa compactada é o resumo.
    for turno in jan.turnos[:-1]:
        for mensagem in turno:
            nome = "o resumo da conversa" if mensagem.get("resumo") else "o 1º pedido da conversa"
            itens.append((nome, janela.peso_mensagem(mensagem)))
    for mensagem in jan.turnos[-1] if jan.turnos else []:
        for item in janela.resultados_de(mensagem):
            if not item.get("interno"):
                itens.append((f"o resultado de {janela.nome_de(item)}", janela.estimar_tokens(item["conteudo"])))
    return itens


def mensagem_nao_cabe(jan: janela.ResultadoJanela, humano: dict, exato: int | None, contexto: int) -> str:
    """O erro ao usuário quando o motor prova que não cabe: qual é o maior item, quanto ocupa e quanto cabe.

    O texto vai ao usuário e não ao log, porque nome de anexo pode citar as partes.

    Sem total (`exato` None), o motor recusou sem dizer quanto: não há o que afirmar além do maior item pela estimativa.
    Quem satura em `num_ctx - 1` (Ollama antigo) só dá um piso, e aí o tamanho vem da estimativa. O que a estimativa
    não vê são as imagens, que o motor conta.
    """
    itens = _candidatos_do_erro(jan, humano)
    nome, peso = max(itens, key=lambda par: par[1])
    conselho = "Reduza esse item, aumente a janela nas opções do plugin ou abra uma conversa nova."
    if exato is None:
        return (f"O pedido não foi enviado ao modelo: o motor o recusou por passar da janela de {contexto} tokens, mesmo "
                f"sem as ferramentas, sem dizer o tamanho. O maior item: {nome}, com ~{peso} tokens pela estimativa. "
                f"{conselho}")
    total = max(exato, jan.estimativa) if exato == contexto - 1 else exato
    com_imagens = any(m.get("imagens") for m in jan.mensagens)
    escala = 1.0 if com_imagens or not jan.estimativa else total / jan.estimativa
    ocupa = math.ceil(peso * escala)
    if com_imagens and total - jan.estimativa > ocupa:
        nome, ocupa = "as imagens anexadas", total - jan.estimativa
    cabem = max(0, jan.orcamento - (total - ocupa))
    ocupar = "ocupam" if nome.startswith("as ") else "ocupa"
    return (f"O pedido não foi enviado ao modelo: não cabe na janela de contexto, mesmo sem as ferramentas e depois de "
            f"tirar tudo o que dava. {nome[0].upper()}{nome[1:]} {ocupar} ~{ocupa} tokens e só cabem ~{cabem} (o pedido "
            f"inteiro tem {'~' if total != exato else ''}{total} tokens, numa janela de {contexto} tokens, com "
            f"{contexto - jan.orcamento} guardados para a resposta). {conselho}")


def montar(preparo: Preparo, nos: list[dict], excluidos: set[str], resumo: dict | None, fator_tokens: float,
           com_ferramentas: bool = True) -> tuple[janela.ResultadoJanela, list[dict]]:
    """A conta da rodada, a partir dos nós do ramo; o medidor passa o próximo pedido, vazio, como último nó."""
    turnos = historico.para_turnos(historico.sem_excluidos(historico.com_resumo(nos, resumo), excluidos),
                                   "cercas" if preparo.cercas else "nativo", preparo.obter_arquivo, preparo.visao)
    return montar_janela(preparo.catalogo, preparo.cercas, preparo.partes, preparo.sistema_duo, turnos,
                         preparo.opcoes, fator_tokens, com_ferramentas)
