"""Bateria de capacidade de modelos locais para o servidor do Duo.

Uso:
    py avaliar_modelos.py --backend ollama --modelos qwen3.5:4b,qwen3.5:9b
    py avaliar_modelos.py --modelos qwen3.5:4b --contexto 32768 --saida dados/avaliacao-20261023.md

Para cada modelo, roda baterias curtas com o backend do próprio servidor (`criar_backend`), sem
o Duo no meio: o que o modelo declara (ferramentas, raciocínio, visão, contexto), se ele chama
ferramentas nativas e por cercas no formato que o servidor usa, se resume em português, se o
botão de pensamento liga e desliga o raciocínio e quanto do modelo ficou na GPU.

É um teste de CAPACIDADE, não uma avaliação jurídica: confere se o modelo consegue operar o Duo
(chamar a ferramenta certa, com JSON válido, responder em português), não se a resposta está
certa. Todos os textos enviados são sintéticos, escritos aqui; nenhum dado real vai ao modelo.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import queue
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import date
from typing import Callable

from duo_provedor.backends import criar_backend
from duo_provedor.backends.base import (
    Backend, ChamadaFerramenta, ErroBackend, Evento, InfoModelo, Pedido, Raciocinio, Texto, Uso,
)
from duo_provedor.backends.ollama import com_etiqueta
from duo_provedor.blueprint import eh_loopback
from duo_provedor.config import URL_PADRAO
from duo_provedor.emulacao import ParserCercas, catalogo_texto, chamada_como_texto, mensagem_resultados
from duo_provedor.hardware import detectar
from servidor import ARQUIVO_SISTEMA, SISTEMA_MINIMO

OK, FALHA, NA = "ok", "falha", "n.a."

# Quanto esperar o leitor do backend terminar depois do fim (ou do cancelamento) de um pedido.
# Sem isso, o pedido seguinte poderia começar com o anterior ainda ocupando o motor.
ESPERA_LEITOR = 10.0

# --- Ferramentas e conversa sintéticas ------------------------------------------------------

# Três ferramentas fictícias no formato em que o Duo as manda (Anthropic). Uma sem argumentos,
# uma com argumento obrigatório e uma que não deve ser chamada: a terceira mede se o modelo
# escolhe entre opções, em vez de chamar a única que existe.
FERRAMENTAS = [
    {"name": "listar_documentos",
     "description": "Lista os documentos juntados aos autos do processo aberto, com id, tipo e "
                    "data de juntada.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "ler_documento",
     "description": "Lê o texto integral de um documento dos autos pelo id devolvido por "
                    "listar_documentos.",
     "input_schema": {"type": "object",
                      "properties": {"id": {"type": "string", "description": "Id do documento."}},
                      "required": ["id"]}},
    {"name": "buscar_movimentos",
     "description": "Busca, nos movimentos do processo, os que contêm o termo informado.",
     "input_schema": {"type": "object",
                      "properties": {"termo": {"type": "string", "description": "Termo a procurar."}}}},
]
NOMES_FERRAMENTAS = frozenset(f["name"] for f in FERRAMENTAS)

PEDIDO_LISTAR = "Liste os documentos do processo."
PEDIDO_LER = "Leia a petição inicial."
ESPERADO_LISTAR = ("listar_documentos", {})
ESPERADO_LER = ("ler_documento", {"id": "101"})
ID_CHAMADA = "chamada-1"

# O resultado sintético de listar_documentos. A petição inicial é o 101, e o resumo do assistente
# (abaixo) não repete os ids: no passo 2 o modelo precisa tirá-lo do resultado da ferramenta.
RESULTADO_LISTA = json.dumps({"documentos": [
    {"id": "101", "tipo": "Petição inicial", "juntada": "2026-03-02", "polo": "ativo"},
    {"id": "102", "tipo": "Contestação", "juntada": "2026-04-15", "polo": "passivo"},
    {"id": "103", "tipo": "Despacho", "juntada": "2026-04-20", "polo": None},
]}, ensure_ascii=False)
RESPOSTA_LISTA = "Os autos têm três documentos: a petição inicial, a contestação e um despacho."

PEDIDO_RESUMO = "Resuma em até 3 frases o trecho de petição abaixo."
# Petição fictícia (nomes, lugares e números inventados), do tamanho de um trecho que o Duo
# mandaria: grande o bastante para medir o prefill, pequena para não depender do contexto.
TRECHO_PETICAO = """\
EXCELENTÍSSIMO SENHOR DOUTOR JUIZ DE DIREITO DA VARA CÍVEL DA COMARCA DE EXEMPLÓPOLIS

JOANA FICTÍCIA DE SOUZA, brasileira, comerciante, inscrita no CPF sob o nº 000.000.000-00, \
residente na Rua das Amostras, 100, em Exemplópolis, vem, por sua advogada, propor AÇÃO DE \
COBRANÇA contra OFICINA MODELO LTDA., pessoa jurídica de direito privado inscrita no CNPJ sob o \
nº 00.000.000/0001-00, com sede na Avenida dos Testes, 200, pelos fatos e fundamentos a seguir.

1. DOS FATOS. Em 10 de março de 2026, as partes celebraram contrato de prestação de serviços de \
manutenção de câmaras frias, com pagamento mensal de R$ 2.075,00. A autora executou os serviços \
de abril a setembro, conforme as ordens de serviço assinadas pelo gerente da ré (documentos 2 a \
7). A ré, porém, deixou de pagar as seis parcelas, que somam R$ 12.450,00, e não respondeu à \
notificação extrajudicial enviada em 5 de outubro (documento 8).

2. DO DIREITO. O contrato faz lei entre as partes, e o inadimplemento sujeita o devedor ao \
pagamento do valor devido, com juros de mora, atualização monetária e honorários de advogado \
(arts. 389 e 395 do Código Civil).

3. DOS PEDIDOS. Diante do exposto, requer: a) a citação da ré para, querendo, contestar a ação; \
b) a condenação da ré ao pagamento de R$ 12.450,00, atualizados desde cada vencimento e \
acrescidos de juros de mora desde a citação; c) a condenação da ré nas custas e em honorários.

Dá-se à causa o valor de R$ 12.450,00.

Exemplópolis, 20 de outubro de 2026."""

PEDIDO_RACIOCINIO = "Quanto é 17 × 24? Responda só com o número."
# O 1º pedido carrega o modelo (pode levar minutos); feito à parte, o tempo de carga não entra
# no prazo nem no "1º token" das baterias.
PEDIDO_AQUECIMENTO = "Responda apenas: ok."

# --- Heurística de português -------------------------------------------------------------------

# Palavras funcionais (artigos, preposições, pronomes, conjunções, verbos auxiliares) do
# português. Elas são ~30–40% de um texto corrido em português e quase não aparecem em inglês
# ou em chinês, as trocas de idioma mais comuns em modelo pequeno. Não mede qualidade.
# Ficam de fora as que também são palavras comuns em inglês (a, as, do, no, me, um, era, logo):
# com elas, um raciocínio em inglês seguido do resumo em português chegou a 18% e quase passou.
PALAVRAS_FUNCIONAIS = frozenset("""
o os uma uns umas de da dos das em na nos nas num numa por pelo pela pelos pelas
para pra com sem sob sobre entre até após ante contra desde perante ao aos à às e ou mas nem
que se porque como quando onde quanto qual quais quem cujo cuja já não sim também só ainda mais
menos muito pouco bem mal assim então porém contudo todavia pois ele ela eles elas eu tu
você vocês nós te lhe vos lhes seu sua seus suas meu minha nosso nossa dele dela deles
delas este esta estes estas esse essa esses essas isto isso aquilo aquele aquela é são foi
foram será ser está estão estava estar tem têm tinha ter há houve haver deve devem pode podem
""".split())
LIMIAR_PORTUGUES = 0.2

_PALAVRA = re.compile(r"[a-zà-öø-ÿ]+")  # letras latinas, com acento; dígitos e outros alfabetos ficam de fora
_FIM_DE_FRASE = re.compile(r"(?<=[.!?…])\s+|\n+")
_TEM_LETRA = re.compile(r"[^\W\d_]")


def fracao_portugues(texto: str) -> float:
    """Fração das palavras do texto que são palavras funcionais do português."""
    palavras = _PALAVRA.findall(texto.lower())
    if not palavras:
        return 0.0
    return sum(p in PALAVRAS_FUNCIONAIS for p in palavras) / len(palavras)


def contar_frases(texto: str) -> int:
    """Contagem aproximada: pontuação final seguida de espaço, ou quebra de linha (listas)."""
    return sum(1 for parte in _FIM_DE_FRASE.split(texto.strip()) if _TEM_LETRA.search(parte))


def tokens_por_segundo(uso: Uso | None, primeiro: float | None, duracao: float) -> float | None:
    """Velocidade de geração: tokens de saída sobre o tempo depois do 1º token.

    O tempo até o 1º token é o prefill (ler o prompt) e fica de fora, senão a velocidade de
    gerar texto pareceria pior em prompts longos.
    """
    if uso is None or primeiro is None or not uso.saida or duracao <= primeiro:
        return None
    return uso.saida / (duracao - primeiro)


# --- Pedidos ----------------------------------------------------------------------------------


def ler_sistema() -> str:
    """O mesmo prompt de sistema do servidor: a bateria mede o modelo como o Duo o usará."""
    try:
        with open(ARQUIVO_SISTEMA, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return SISTEMA_MINIMO


@dataclass
class Opcoes:
    contexto: int
    prazo: float = 180.0  # por bateria (as de dois passos dividem o mesmo prazo)
    temperatura: float | None = 0.3  # o padrão do config.json do servidor
    prazo_carregamento: float = 600.0  # carregar do disco um modelo grande pode levar minutos
    sistema: str = field(default_factory=ler_sistema)


def _usuario(texto: str) -> dict:
    return {"papel": "usuario", "texto": texto}


def pedido_simples(modelo: str, texto: str, opcoes: Opcoes, pensar: bool = False) -> Pedido:
    mensagens = [{"papel": "sistema", "texto": opcoes.sistema}, _usuario(texto)]
    return Pedido(modelo, mensagens, [], pensar, opcoes.contexto, opcoes.temperatura)


# --- Coleta com prazo ---------------------------------------------------------------------------


@dataclass
class Coleta:
    """Tudo o que veio de um pedido ao backend, com os tempos medidos do lado de cá."""

    eventos: list[Evento] = field(default_factory=list)
    primeiro_token: float | None = None  # 1º texto, raciocínio ou chamada (fim do prefill)
    primeiro_texto: float | None = None  # 1º texto visível: o que o usuário do Duo espera
    duracao: float = 0.0
    erro: str | None = None
    esgotado: bool = False

    @property
    def texto(self) -> str:
        return "".join(e.texto for e in self.eventos if isinstance(e, Texto))

    @property
    def raciocinio(self) -> str:
        return "".join(e.texto for e in self.eventos if isinstance(e, Raciocinio))

    @property
    def chamadas(self) -> list[ChamadaFerramenta]:
        return [e for e in self.eventos if isinstance(e, ChamadaFerramenta)]

    @property
    def uso(self) -> Uso | None:
        return next((e for e in reversed(self.eventos) if isinstance(e, Uso)), None)

    def falha(self) -> str | None:
        if self.erro:
            return self.erro
        return "prazo esgotado" if self.esgotado else None


def _ler(backend: Backend, pedido: Pedido, cancelado: threading.Event, fila: queue.Queue) -> None:
    eventos = None
    try:
        eventos = backend.conversar(pedido, cancelado)
        for evento in eventos:
            if cancelado.is_set():
                return
            fila.put(("evento", evento))
        fila.put(("fim", None))
    except ErroBackend as e:
        fila.put(("erro", str(e)))
    except Exception as e:  # noqa: BLE001 - um modelo que quebra o backend vira falha, e a bateria segue
        fila.put(("erro", f"erro inesperado no backend: {e!r}"))
    finally:
        # Fecha o gerador já: o backend real solta a conexão com o motor sem esperar o GC.
        fechar = getattr(eventos, "close", None)
        if fechar:
            fechar()


def coletar(backend: Backend, pedido: Pedido, prazo: float,
            parar_em: Callable[[Evento], bool] | None = None) -> Coleta:
    """Faz um pedido e junta os eventos até o fim, um erro, o prazo ou `parar_em(evento)`.

    O backend é lido numa thread, como no turno do servidor: a espera fica aqui, com prazo, e não
    dentro de uma leitura de socket. Assim um modelo que trava (ou que entra em laço gerando sem
    parar) não segura a avaliação inteira. Ao sair, o `cancelado` é sinalizado e o backend fecha
    a conexão, o que faz o motor parar de gerar.
    """
    cancelado = threading.Event()
    fila: queue.Queue = queue.Queue()
    leitor = threading.Thread(target=_ler, args=(backend, pedido, cancelado, fila), daemon=True,
                              name="avaliacao-backend")
    coleta = Coleta()
    inicio = time.monotonic()
    limite = inicio + prazo
    leitor.start()
    try:
        while True:
            restante = limite - time.monotonic()
            if restante <= 0:
                coleta.esgotado = True
                break
            try:
                tipo, valor = fila.get(timeout=restante)
            except queue.Empty:
                continue
            if tipo == "fim":
                break
            if tipo == "erro":
                coleta.erro = valor
                break
            agora = time.monotonic() - inicio
            if not isinstance(valor, Uso):
                if coleta.primeiro_token is None:
                    coleta.primeiro_token = agora
                if coleta.primeiro_texto is None and isinstance(valor, Texto) and valor.texto.strip():
                    coleta.primeiro_texto = agora
            coleta.eventos.append(valor)
            if parar_em is not None and parar_em(valor):
                break
    finally:
        coleta.duracao = time.monotonic() - inicio
        cancelado.set()
        leitor.join(ESPERA_LEITOR)
    return coleta


# --- Resultados e pontuação --------------------------------------------------------------------


@dataclass
class Resultado:
    situacao: str  # OK | FALHA | NA
    detalhe: str = ""
    medidas: dict = field(default_factory=dict)
    duracao: float | None = None


def _trecho(texto: str, limite: int = 160) -> str:
    plano = " ".join(texto.split())
    return plano if len(plano) <= limite else plano[:limite] + "…"


def inicio_e_fim(texto: str, limite: int) -> str:
    """Começo e fim de um texto longo, numa linha: um raciocínio vazado aparece no começo, e o
    resumo de verdade, quando vem, costuma estar no fim."""
    plano = " ".join(texto.split())
    if len(plano) <= limite:
        return plano
    fim = limite // 3
    return f"{plano[:limite - fim]} […] {plano[-fim:]}"


def _json(valor) -> str:
    return json.dumps(valor, ensure_ascii=False)


def pontuar_chamadas(chamadas: list[tuple[str | None, dict | None, str | None]], nome: str,
                     argumentos: dict, texto: str = "") -> Resultado:
    """Pontua as chamadas de um passo: chamada certa, JSON válido, nenhuma ferramenta inexistente.

    Cada chamada é (nome, argumentos, erro); `erro` preenchido é JSON que não deu para ler, e
    `nome` None é um bloco de cerca ilegível, em que nem o nome se aproveita. "Chamada certa" é o
    nome E os argumentos esperados, exatamente: o id como número (101) conta como errado, porque
    o esquema da ferramenta pede texto, e um argumento a mais é invenção do modelo.
    """
    if not chamadas:
        resposta = f"; respondeu: «{_trecho(texto)}»" if texto.strip() else ""
        return Resultado(FALHA, f"nenhuma chamada{resposta}", {"pontos": 0})
    certa = any(n == nome and a == argumentos for n, a, e in chamadas if e is None)
    json_valido = all(e is None for _, _, e in chamadas)
    inexistentes = [n for n, _, _ in chamadas if n is not None and n not in NOMES_FERRAMENTAS]
    descricoes = [f"JSON inválido ({e})" if e else f"{n} {_json(a)}" for n, a, e in chamadas]
    problemas = []
    if not certa:
        problemas.append(f"esperado {nome} {_json(argumentos)}")
    if inexistentes:
        problemas.append(f"ferramenta inexistente: {', '.join(inexistentes)}")
    pontos = certa + json_valido + (not inexistentes)
    detalhe = "; ".join(descricoes + problemas)
    return Resultado(OK if pontos == 3 else FALHA, detalhe, {"pontos": pontos})


class Prazo:
    """Prazo de uma bateria inteira: os passos dividem o mesmo tempo."""

    def __init__(self, segundos: float) -> None:
        self.segundos = segundos
        self._limite = time.monotonic() + segundos

    def restante(self) -> float:
        return max(0.0, self._limite - time.monotonic())

    def mensagem(self) -> str:
        return f"prazo de {self.segundos:g} s esgotado"


def _falha(coleta: Coleta, prazo: Prazo) -> str | None:
    if coleta.erro:
        return coleta.erro
    return prazo.mensagem() if coleta.esgotado else None


# --- Baterias -----------------------------------------------------------------------------------

Extrator = Callable[[Coleta], tuple[list[tuple[str | None, dict | None, str | None]], str]]


def _passo(backend: Backend, pedido: Pedido, prazo: Prazo, extrair: Extrator,
           esperado: tuple[str, dict]) -> Resultado:
    if prazo.restante() <= 0:
        return Resultado(FALHA, prazo.mensagem())
    coleta = coletar(backend, pedido, prazo.restante())
    falha = _falha(coleta, prazo)
    if falha:
        return Resultado(FALHA, falha)
    chamadas, texto = extrair(coleta)
    resultado = pontuar_chamadas(chamadas, *esperado, texto=texto)
    if not chamadas:
        # "Nenhuma chamada" sozinho não diz o que fazer; o que veio no lugar, diz.
        observacoes = []
        # Só acontece nas cercas. O turno do servidor trataria a chamada nativa como chamada (e a
        # aceitaria se o nome existisse), mas esta bateria mede a emulação, então ela não conta.
        if coleta.chamadas:
            observacoes.append("chamada nativa fora das cercas: " + ", ".join(
                f"{c.nome} {_json(c.argumentos)}" for c in coleta.chamadas))
        if not coleta.texto.strip() and coleta.raciocinio.strip():
            observacoes.append(f"só raciocínio ({len(coleta.raciocinio)} caracteres), sem texto")
        if observacoes:
            resultado.detalhe = "; ".join([resultado.detalhe] + observacoes)
    return resultado


def _juntar_passos(passos: list[Resultado]) -> Resultado:
    certos = sum(p.situacao == OK for p in passos)
    detalhe = "; ".join(f"passo {i}: {p.situacao}" + (f" ({p.detalhe})" if p.detalhe else "")
                        for i, p in enumerate(passos, 1))
    return Resultado(OK if certos == len(passos) else FALHA, detalhe, {"passos": passos})


def _chamadas_nativas(coleta: Coleta):
    return [(c.nome, c.argumentos, c.erro) for c in coleta.chamadas], coleta.texto


def bateria_nativa(backend: Backend, modelo: str, opcoes: Opcoes) -> Resultado:
    """Tool-calling nativo, em dois passos, com as ferramentas no campo `tools` do motor.

    O histórico do passo 2 é sintético (a chamada certa do passo 1 e o seu resultado), e não o
    que o modelo respondeu no passo 1: assim cada passo mede uma coisa só, e um erro no
    primeiro não contamina o segundo.
    """
    prazo = Prazo(opcoes.prazo)
    passo1 = [{"papel": "sistema", "texto": opcoes.sistema}, _usuario(PEDIDO_LISTAR)]
    passo2 = passo1 + [
        {"papel": "assistente", "texto": "",
         "chamadas": [{"id": ID_CHAMADA, "nome": "listar_documentos", "argumentos": {}}]},
        {"papel": "ferramenta", "id_chamada": ID_CHAMADA, "nome": "listar_documentos",
         "texto": RESULTADO_LISTA},
        {"papel": "assistente", "texto": RESPOSTA_LISTA},
        _usuario(PEDIDO_LER),
    ]
    passos = [_passo(backend, Pedido(modelo, mensagens, list(FERRAMENTAS), False, opcoes.contexto,
                                     opcoes.temperatura), prazo, _chamadas_nativas, esperado)
              for mensagens, esperado in ((passo1, ESPERADO_LISTAR), (passo2, ESPERADO_LER))]
    return _juntar_passos(passos)


def _chamadas_das_cercas(coleta: Coleta):
    parser = ParserCercas()
    saidas = []
    for evento in coleta.eventos:
        if isinstance(evento, Texto):
            saidas += parser.alimentar(evento.texto)
    saidas += parser.finalizar()
    chamadas, texto = [], ""
    for saida in saidas:
        if saida[0] == "texto":
            texto += saida[1]
        elif saida[0] == "chamada":
            chamadas.append((saida[1]["nome"], saida[1]["argumentos"], None))
        else:
            chamadas.append((None, None, saida[2]))
    return chamadas, texto


def bateria_cercas(backend: Backend, modelo: str, opcoes: Opcoes) -> Resultado:
    """A mesma bateria, emulada por cercas: catálogo no sistema e chamadas em blocos de texto.

    As mensagens têm a forma que `historico.para_turnos` monta no modo cercas: a chamada volta
    como bloco duo-ferramenta no texto do assistente, e o resultado vem numa mensagem de
    usuário marcada. Não há rodada de correção: mede o acerto de primeira.
    """
    prazo = Prazo(opcoes.prazo)
    sistema = opcoes.sistema + "\n\n" + catalogo_texto(FERRAMENTAS)
    passo1 = [{"papel": "sistema", "texto": sistema}, _usuario(PEDIDO_LISTAR)]
    resultados = mensagem_resultados([{"nome": "listar_documentos", "conteudo": RESULTADO_LISTA,
                                       "erro": False}])
    passo2 = passo1 + [
        {"papel": "assistente", "texto": chamada_como_texto("listar_documentos", {})},
        _usuario(resultados),
        {"papel": "assistente", "texto": RESPOSTA_LISTA},
        _usuario(PEDIDO_LER),
    ]
    passos = [_passo(backend, Pedido(modelo, mensagens, [], False, opcoes.contexto, opcoes.temperatura),
                     prazo, _chamadas_das_cercas, esperado)
              for mensagens, esperado in ((passo1, ESPERADO_LISTAR), (passo2, ESPERADO_LER))]
    return _juntar_passos(passos)


def _pct(fracao: float) -> str:
    return f"{fracao:.0%}"


def bateria_ptbr(backend: Backend, modelo: str, opcoes: Opcoes) -> Resultado:
    """Resumo em português: mede tempos, tamanho e idioma; o mérito do resumo não é avaliado."""
    prazo = Prazo(opcoes.prazo)
    pedido = pedido_simples(modelo, f"{PEDIDO_RESUMO}\n\n{TRECHO_PETICAO}", opcoes)
    coleta = coletar(backend, pedido, prazo.restante())
    if coleta.erro:
        return Resultado(FALHA, coleta.erro)
    texto = coleta.texto.strip()
    medidas = {
        "primeiro_token": coleta.primeiro_token,
        "primeiro_texto": coleta.primeiro_texto,
        "tokens_s": tokens_por_segundo(coleta.uso, coleta.primeiro_token, coleta.duracao),
        "caracteres": len(texto),
        "frases": contar_frases(texto),
        "fracao_pt": fracao_portugues(texto),
        "texto": texto,
    }
    resumo = (f"{medidas['frases']} frase(s), {medidas['caracteres']} caracteres, "
              f"{_pct(medidas['fracao_pt'])} de palavras funcionais do português")
    if coleta.esgotado:
        # Um modelo que não para de gerar é um resultado em si: o que veio até o prazo fica medido.
        return Resultado(FALHA, f"{prazo.mensagem()} sem terminar: {resumo}", medidas)
    if not texto:
        return Resultado(FALHA, "resposta vazia", medidas)
    if medidas["fracao_pt"] < LIMIAR_PORTUGUES:
        return Resultado(FALHA, f"não parece português: {resumo} (mínimo {_pct(LIMIAR_PORTUGUES)})",
                         medidas)
    return Resultado(OK, resumo, medidas)


def bateria_raciocinio(backend: Backend, modelo: str, info: InfoModelo, opcoes: Opcoes) -> Resultado:
    """O botão de pensamento do Duo funciona? Desligado não pode vir raciocínio; ligado, tem de vir.

    Com o pensamento ligado basta o 1º trecho de raciocínio: o pedido é encerrado ali, para um
    modelo que pensa muito não gastar o prazo à toa.
    """
    if not info.raciocinio:
        return Resultado(NA, "o modelo não declara raciocínio")
    prazo = Prazo(opcoes.prazo)
    desligado = coletar(backend, pedido_simples(modelo, PEDIDO_RACIOCINIO, opcoes, pensar=False),
                        prazo.restante())
    falha = _falha(desligado, prazo)
    if falha:
        return Resultado(FALHA, f"pensamento desligado: {falha}")
    ligado = coletar(backend, pedido_simples(modelo, PEDIDO_RACIOCINIO, opcoes, pensar=True),
                     prazo.restante(), parar_em=lambda e: isinstance(e, Raciocinio))
    falha = _falha(ligado, prazo)
    if falha:
        return Resultado(FALHA, f"pensamento ligado: {falha}")
    sem, com = bool(desligado.raciocinio.strip()), bool(ligado.raciocinio.strip())
    medidas = {"sem_pensar": sem, "com_pensar": com}
    problemas = []
    if sem:
        problemas.append(f"com o pensamento desligado ainda veio raciocínio "
                         f"({len(desligado.raciocinio)} caracteres)")
    if "<think>" in desligado.texto:
        problemas.append("com o pensamento desligado veio <think> no texto")
    if not com:
        problemas.append("com o pensamento ligado não veio raciocínio separado"
                         + (" (veio <think> no texto)" if "<think>" in ligado.texto else ""))
    if problemas:
        return Resultado(FALHA, "; ".join(problemas), medidas)
    return Resultado(OK, "desligado: sem raciocínio; ligado: com raciocínio", medidas)


def _gb(valor: int | float | None) -> str:
    return "—" if valor is None else f"{valor / 10**9:.1f} GB".replace(".", ",")


def bateria_gpu(backend: Backend, modelo: str, opcoes: Opcoes, medidor,
                vram_antes: int | None = None, download: int | None = None) -> Resultado:
    """O que ficou carregado: o contexto real e, no Ollama, quanto do modelo está na VRAM.

    O contexto diferente do pedido é falha porque o servidor conta a janela pelo valor pedido:
    se o motor carregou menos, ele trunca o prompt por conta própria.

    A memória vem de duas fontes, porque uma sozinha já enganou: o `/api/ps` do Ollama 0.30.10
    informou 1,1 GB para o gemma4:12b, que ocupava 8,8 GB na placa. Por isso também se mede a
    VRAM pelo nvidia-smi (a diferença entre antes de carregar e agora), e a memória
    "necessária", que decide a faixa de hardware, é a maior das medidas, incluindo o download:
    o modelo carregado não cabe em menos que o próprio arquivo.
    """
    try:
        contexto = backend.contexto_em_uso(modelo)
    except ErroBackend:
        contexto = None
    memoria = medidor.memoria(modelo) if medidor is not None else None
    if contexto is None and memoria is None:
        if medidor is not None:
            return Resultado(FALHA, "o modelo não aparece entre os carregados (/api/ps)")
        return Resultado(NA, "o backend não informa o que está carregado")
    medidas: dict = {"contexto_em_uso": contexto}
    partes = [f"contexto em uso {contexto}" if contexto is not None else "contexto em uso não informado"]
    tamanho, vram = (memoria or {}).get("size"), (memoria or {}).get("size_vram")
    if isinstance(tamanho, int) and tamanho > 0 and isinstance(vram, int):
        medidas.update(tamanho=tamanho, vram=vram, fracao=vram / tamanho)
        partes.append(f"/api/ps: {_gb(tamanho)} carregados, {_pct(vram / tamanho)} na VRAM")
        if download and tamanho < download:
            partes.append("/api/ps inconsistente: informa menos que o próprio download")
    agora = medidor.vram_usada() if medidor is not None else None
    if agora is not None and vram_antes is not None:
        medidas["vram_medida"] = agora - vram_antes
        partes.append(f"nvidia-smi: +{_gb(agora - vram_antes)} na placa")
    candidatas = [v for v in (medidas.get("tamanho"), medidas.get("vram_medida"), download) if v]
    if candidatas:
        medidas["necessaria"] = max(candidatas)
        partes.append(f"memória necessária {_gb(medidas['necessaria'])}")
    if contexto is not None and contexto != opcoes.contexto:
        return Resultado(FALHA, f"contexto carregado ({contexto}) diferente do pedido "
                                f"({opcoes.contexto}); " + "; ".join(partes[1:]), medidas)
    return Resultado(OK, "; ".join(partes), medidas)


# --- Avaliação de um modelo ---------------------------------------------------------------------

BATERIAS = ("capacidades", "nativo", "cercas", "ptbr", "raciocinio", "gpu")


@dataclass
class Avaliacao:
    modelo: str
    info: InfoModelo | None = None
    resultados: dict[str, Resultado] = field(default_factory=dict)
    carregamento: float | None = None  # segundos do 1º pedido (carga do modelo + resposta curta)
    tamanho_download: int | None = None  # bytes, como o `ollama list` mostra

    def situacao(self, bateria: str) -> str:
        r = self.resultados.get(bateria)
        return r.situacao if r else NA


def _capacidades(info: InfoModelo) -> str:
    declara = [nome for nome, tem in (("ferramentas", info.ferramentas), ("raciocínio", info.raciocinio),
                                      ("visão", info.visao)) if tem]
    contexto = f"contexto máximo {info.contexto_max}" if info.contexto_max else "contexto máximo não informado"
    return f"{', '.join(declara) or 'nenhuma capacidade extra'}; {contexto}"


def avaliar_modelo(backend: Backend, modelo: str, opcoes: Opcoes, medidor=None,
                   progresso: Callable[[str], None] = lambda texto: None) -> Avaliacao:
    """Roda as baterias de um modelo, na ordem, e descarrega o modelo no fim (Ollama).

    Descarregar libera a VRAM para o próximo: sem isso, o modelo anterior ainda ocupa a placa
    e a medida de memória do seguinte sai pior do que seria numa máquina só com ele.
    """
    avaliacao = Avaliacao(modelo)

    def anotar(bateria: str, executar: Callable[[], Resultado]) -> None:
        inicio = time.monotonic()
        resultado = executar()
        resultado.duracao = time.monotonic() - inicio
        avaliacao.resultados[bateria] = resultado
        progresso(f"[{modelo}] {bateria}: {resultado.situacao} ({resultado.duracao:.1f} s)"
                  + (f" - {resultado.detalhe}" if resultado.detalhe else ""))

    try:
        info = backend.info_modelo(modelo)
        motivo = None
    except ErroBackend as e:
        info, motivo = None, str(e)
    if motivo is None and info is None:
        motivo = "o modelo não está instalado"
    if motivo:
        anotar("capacidades", lambda: Resultado(FALHA, motivo))
    elif info.nuvem:
        # Os textos são sintéticos, mas a bateria é de modelos locais: nada sai da máquina.
        motivo = "modelo de nuvem: fora do escopo (a bateria só avalia modelos locais)"
        anotar("capacidades", lambda: Resultado(NA, motivo))
    elif not info.conversa:
        motivo = "o modelo não conversa (é de embedding, por exemplo): as baterias não se aplicam"
        anotar("capacidades", lambda: Resultado(NA, motivo))
    if motivo:
        for bateria in BATERIAS[1:]:
            avaliacao.resultados[bateria] = Resultado(NA, motivo)
        return avaliacao

    avaliacao.info = info
    anotar("capacidades", lambda: Resultado(OK, _capacidades(info), {
        "ferramentas": info.ferramentas, "raciocinio": info.raciocinio, "visao": info.visao,
        "contexto_max": info.contexto_max}))
    if medidor is not None:
        avaliacao.tamanho_download = _tamanho_download(medidor, modelo)

    # Medida antes de carregar: a diferença para a medida da bateria de GPU é o que o modelo ocupa.
    vram_antes = medidor.vram_usada() if medidor is not None else None
    progresso(f"[{modelo}] carregando com contexto {opcoes.contexto}…")
    aquecimento = coletar(backend, pedido_simples(modelo, PEDIDO_AQUECIMENTO, opcoes),
                          opcoes.prazo_carregamento)
    avaliacao.carregamento = aquecimento.duracao
    carga = _falha(aquecimento, Prazo(opcoes.prazo_carregamento))
    try:
        if carga:
            for bateria in ("nativo", "cercas", "ptbr", "raciocinio"):
                anotar(bateria, lambda: Resultado(FALHA, f"o modelo não carregou: {carga}"))
        else:
            anotar("nativo", lambda: bateria_nativa(backend, modelo, opcoes) if info.ferramentas
                   else Resultado(NA, "o modelo não declara ferramentas nativas"))
            anotar("cercas", lambda: bateria_cercas(backend, modelo, opcoes))
            anotar("ptbr", lambda: bateria_ptbr(backend, modelo, opcoes))
            anotar("raciocinio", lambda: bateria_raciocinio(backend, modelo, info, opcoes))
        anotar("gpu", lambda: bateria_gpu(backend, modelo, opcoes, medidor, vram_antes,
                                          avaliacao.tamanho_download))
    finally:
        if medidor is not None:
            medidor.descarregar(modelo)
    return avaliacao


# --- Medidas que só o Ollama dá --------------------------------------------------------------------


class MedidorOllama:
    """Versão, tamanho do download e memória na GPU, pela API nativa do Ollama e pelo nvidia-smi.

    Fica fora do contrato dos backends porque só a avaliação precisa disso. Toda falha vira None
    (ou dicionário vazio): a medida é complemento, e a bateria segue sem o número.
    """

    def __init__(self, url: str, timeout: float = 10.0, espera_descarga: float = 15.0) -> None:
        self.url = url.rstrip("/")
        self.timeout = timeout
        self.espera_descarga = espera_descarga

    def _pedir(self, caminho: str, corpo: dict | None = None) -> dict | None:
        dados = json.dumps(corpo).encode("utf-8") if corpo is not None else None
        cabecalhos = {"Content-Type": "application/json"} if dados is not None else {}
        pedido = urllib.request.Request(self.url + caminho, data=dados, headers=cabecalhos,
                                        method="POST" if dados is not None else "GET")
        try:
            with urllib.request.urlopen(pedido, timeout=self.timeout) as resposta:
                obj = json.load(resposta)
        except urllib.error.HTTPError as erro:
            erro.close()  # o erro segura a resposta aberta; sem fechar, o socket fica para o GC
            return None
        except (OSError, ValueError):
            return None
        return obj if isinstance(obj, dict) else None

    def versao(self) -> str | None:
        valor = (self._pedir("/api/version") or {}).get("version")
        return valor if isinstance(valor, str) else None

    def tamanhos(self) -> dict[str, int]:
        """Download de cada modelo instalado, pelo nome que o Ollama lista (sempre com etiqueta)."""
        modelos = (self._pedir("/api/tags") or {}).get("models") or []
        return {m["name"]: m["size"] for m in modelos
                if isinstance(m, dict) and isinstance(m.get("name"), str) and isinstance(m.get("size"), int)}

    def memoria(self, modelo: str) -> dict | None:
        # `gemma3` pedido na linha de comando é o `gemma3:latest` que o /api/ps mostra. Sem
        # normalizar, a medida sumia e o `descarregar` nem esperava o modelo sair da placa.
        alvo = com_etiqueta(modelo)
        for item in (self._pedir("/api/ps") or {}).get("models") or []:
            if isinstance(item, dict) and alvo in {com_etiqueta(n) for n in (item.get("name"), item.get("model"))
                                                   if isinstance(n, str)}:
                return item
        return None

    def descarregar(self, modelo: str) -> None:
        # `keep_alive: 0` sem prompt é o jeito documentado de tirar o modelo da memória.
        self._pedir("/api/generate", {"model": modelo, "keep_alive": 0})
        # A resposta chega antes de a memória ser solta. Esperar o modelo sair do /api/ps (nesta
        # máquina, a VRAM volta ao normal junto) faz a medida do próximo começar do zero.
        limite = time.monotonic() + self.espera_descarga
        while self.memoria(modelo) is not None and time.monotonic() < limite:
            time.sleep(0.25)

    def vram_usada(self) -> int | None:
        """VRAM em uso agora, somando as placas NVIDIA, em bytes (nvidia-smi).

        Só com o Ollama nesta máquina: a placa local não diz nada de um Ollama remoto.
        """
        if not eh_loopback(self.url):
            return None
        try:
            saida = subprocess.run(["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
                                   capture_output=True, text=True, timeout=5, check=True)
            return sum(int(linha) for linha in saida.stdout.split()) * 1024**2  # nounits devolve MiB
        except (OSError, subprocess.SubprocessError, ValueError):
            return None


def _tamanho_download(medidor, modelo: str) -> int | None:
    """O download de `modelo`, com ou sem etiqueta (`gemma3` acha o `gemma3:latest` da lista)."""
    alvo = com_etiqueta(modelo)
    return next((tamanho for nome, tamanho in medidor.tamanhos().items() if com_etiqueta(nome) == alvo), None)


# --- Recomendação ------------------------------------------------------------------------------

OBRIGATORIAS = ("nativo", "ptbr")
OPCIONAIS = ("cercas", "raciocinio")
GIB = 1024**3
# Sem GPU, acima de ~5 GB de download o modelo fica lento demais no processador e aperta 16 GB de
# RAM junto com o navegador.
LIMITE_CPU = 5 * 10**9
# VRAM que a área de trabalho, o navegador e o driver já ocupam: o que sobra é o que o modelo pode
# usar sem ir para a CPU. Na avaliação de 2026-10-23, entre um modelo e outro, o nvidia-smi marcava
# de 1305 a 1752 MiB em uso (de 1,3 a 1,8 GB); 1,5 GB fica no meio dessa faixa.
MARGEM_VRAM = int(1.5 * 10**9)
# (rótulo, medida comparada, limite em bytes). Uma placa "de 8 GB" tem 8 GiB. A faixa de 8 GB não
# leva o nome do perfil gpu de propósito: o perfil vale de 8 GB para cima, e o padrão dele é uma
# decisão à parte (ver PERFIS em duo_provedor/hardware.py), não o resultado desta regra.
FAIXAS = (
    ("Sem GPU (perfil cpu)", "download", LIMITE_CPU),
    ("GPU com 8 GB de VRAM", "memoria", 8 * GIB - MARGEM_VRAM),
    ("GPU com 12 GB de VRAM", "memoria", 12 * GIB - MARGEM_VRAM),
)
NOMES_BATERIAS = {"capacidades": "capacidades", "nativo": "ferramentas nativas", "cercas": "cercas",
                  "ptbr": "pt-BR", "raciocinio": "raciocínio", "gpu": "GPU"}


def _memoria(avaliacao: Avaliacao) -> int | None:
    """Memória que o modelo precisa com este contexto (ver `bateria_gpu`)."""
    gpu = avaliacao.resultados.get("gpu")
    if gpu is None:
        return None
    return gpu.medidas.get("necessaria") or gpu.medidas.get("tamanho")


def recomendar(avaliacoes: list[Avaliacao]) -> list[tuple[str, Avaliacao | None]]:
    """O melhor modelo de cada faixa de hardware, ou None se nenhum serve.

    Entram os que passaram nas ferramentas nativas (os dois passos) e no pt-BR e que cabem no
    limite da faixa. Vence quem passou em mais baterias opcionais e, no empate, o maior: sem
    avaliar o mérito, o tamanho é o único indício de qualidade que a bateria tem.
    """
    escolhas = []
    for rotulo, medida, limite in FAIXAS:
        candidatos = []
        for a in avaliacoes:
            valor = a.tamanho_download if medida == "download" else _memoria(a)
            if valor is not None and valor <= limite and all(a.situacao(b) == OK for b in OBRIGATORIAS):
                candidatos.append(a)
        candidatos.sort(key=lambda a: (sum(a.situacao(b) == OK for b in OPCIONAIS), a.tamanho_download or 0),
                        reverse=True)
        escolhas.append((rotulo, candidatos[0] if candidatos else None))
    return escolhas


# --- Relatório ---------------------------------------------------------------------------------

COLUNAS = ("Modelo", "Download", "Declara", "Nativo", "Cercas", "pt-BR", "1º token", "Tokens/s",
           "Raciocínio", "GPU")
LEGENDA = (
    "**Legenda.** *Download*: tamanho no `ollama list`. *Declara*: o que o modelo informa "
    "(ferramentas, raciocínio, visão; contexto máximo em milhares de tokens). *Nativo* e *Cercas*: "
    "passos certos de 2 (listar os documentos; ler a petição inicial, id 101). *pt-BR*: frases do "
    "resumo e fração de palavras funcionais do português. *1º token*: espera até o 1º token do "
    "resumo; entre parênteses, até o 1º texto visível, quando o modelo raciocina antes. "
    "*Tokens/s*: velocidade de geração do resumo. *GPU*: quanto do modelo ficou na VRAM, segundo "
    "o `/api/ps`, e a memória necessária com este contexto (a maior entre o `/api/ps`, o aumento "
    "de VRAM medido pelo nvidia-smi e o download). *n.a.*: não se aplica (o modelo não declara a "
    "capacidade ou o backend não informa)."
)
REGRA = (
    "Regra do resumo: entram os modelos que passaram nas ferramentas nativas (os dois passos) e "
    "no pt-BR. Sem GPU, o limite é 5 GB de download; nas faixas de GPU, a memória necessária com "
    "este contexto tem de caber na VRAM menos 1,5 GB (área de trabalho, navegador e driver). Vence "
    "quem passou em mais baterias (cercas, raciocínio) e, no empate, o maior: sem avaliar o mérito, "
    "o tamanho é o único indício de qualidade que a bateria tem."
)


def _plano(texto) -> str:
    return " ".join(str(texto).split())


def _celula(texto) -> str:
    return _plano(texto).replace("|", "\\|") or "—"


def _segundos(valor: float | None) -> str:
    return "—" if valor is None else f"{valor:.1f} s".replace(".", ",")


def _declara(a: Avaliacao) -> str:
    if a.info is None:
        return "—"
    partes = [nome for nome, tem in (("ferr.", a.info.ferramentas), ("rac.", a.info.raciocinio),
                                      ("visão", a.info.visao)) if tem]
    contexto = f"{a.info.contexto_max // 1024}K" if a.info.contexto_max else ""
    return "; ".join(p for p in (", ".join(partes), contexto) if p) or "—"


def _passos(r: Resultado) -> str:
    passos = r.medidas.get("passos")
    if not passos:
        return r.situacao
    return f"{r.situacao} {sum(p.situacao == OK for p in passos)}/{len(passos)}"


def _linha(a: Avaliacao) -> list[str]:
    vazio = Resultado(NA)
    pt = a.resultados.get("ptbr", vazio)
    m = pt.medidas
    ptbr = pt.situacao + (f" ({m['frases']} fr., pt {_pct(m['fracao_pt'])})" if "fracao_pt" in m else "")
    token = _segundos(m.get("primeiro_token"))
    if (m.get("primeiro_token") is not None and m.get("primeiro_texto") is not None
            and m["primeiro_texto"] - m["primeiro_token"] >= 1.0):
        token += f" (texto {_segundos(m['primeiro_texto'])})"
    velocidade = f"{m['tokens_s']:.0f}" if m.get("tokens_s") else "—"
    g = a.resultados.get("gpu", vazio).medidas
    gpu = (f"{_pct(g['fracao'])} de {_gb(_memoria(a))}" if "fracao" in g and _memoria(a)
           else a.situacao("gpu"))
    return [a.modelo, _gb(a.tamanho_download), _declara(a), _passos(a.resultados.get("nativo", vazio)),
            _passos(a.resultados.get("cercas", vazio)), ptbr, token, velocidade, a.situacao("raciocinio"), gpu]


def _motivo(a: Avaliacao) -> str:
    passou = [NOMES_BATERIAS[b] for b in ("nativo", "cercas", "ptbr", "raciocinio") if a.situacao(b) == OK]
    return (f"`{a.modelo}` ({_gb(a.tamanho_download)} de download, {_gb(_memoria(a))} de memória com "
            f"este contexto); passou em: {', '.join(passou)}.")


def _detalhes(a: Avaliacao) -> list[str]:
    linhas = [f"### `{a.modelo}`", ""]
    for bateria in BATERIAS:
        r = a.resultados.get(bateria)
        if r is None:
            continue
        tempo = f" ({_segundos(r.duracao)})" if r.duracao is not None else ""
        linhas.append(f"- {NOMES_BATERIAS[bateria]}: {r.situacao}{tempo}"
                      + (f" — {_plano(r.detalhe)}" if r.detalhe else ""))
        if bateria == "capacidades" and a.carregamento is not None:
            linhas.append(f"- carregamento (1º pedido): {_segundos(a.carregamento)}")
    m = (a.resultados.get("ptbr") or Resultado(NA)).medidas
    if m.get("texto"):
        linhas.append(f"- resumo pt-BR (1º token {_segundos(m.get('primeiro_token'))}, 1º texto "
                      f"{_segundos(m.get('primeiro_texto'))}):")
        linhas.append(f"  > {inicio_e_fim(m['texto'], 900)}")
    return linhas + [""]


def relatorio(avaliacoes: list[Avaliacao], cabecalho: dict) -> str:
    c = cabecalho
    versao = f" (versão {c['versao']})" if c.get("versao") else ""
    linhas = [
        f"# Avaliação de modelos locais ({c['data']})",
        "",
        "Teste de capacidade: confere se cada modelo consegue operar o Duo (chamar ferramentas, "
        "responder em português, ligar e desligar o raciocínio, caber na GPU). Isto **não é uma "
        "avaliação jurídica**: o mérito das respostas não é avaliado, e os textos enviados são "
        "sintéticos.",
        "",
        f"- Backend: {c['backend']} em {c['url']}{versao}",
        f"- Contexto pedido: {c['contexto']} tokens; temperatura {c['temperatura']}; "
        f"prazo por bateria {c['prazo']:g} s",
        f"- Máquina: {c['maquina']}",
        "",
        "| " + " | ".join(COLUNAS) + " |",
        "|" + "|".join("---" for _ in COLUNAS) + "|",
    ]
    linhas += ["| " + " | ".join(_celula(v) for v in _linha(a)) + " |" for a in avaliacoes]
    linhas += ["", LEGENDA, "", "## Recomendado para", ""]
    for rotulo, escolha in recomendar(avaliacoes):
        linhas.append(f"- **{rotulo}:** " + (_motivo(escolha) if escolha else
                                              "nenhum modelo testado passou nas baterias obrigatórias "
                                              "dentro do limite."))
    linhas += ["", REGRA, "", "## Detalhes", ""]
    for a in avaliacoes:
        linhas += _detalhes(a)
    return "\n".join(linhas)


# --- Linha de comando ---------------------------------------------------------------------------


def lista_modelos(texto: str) -> list[str]:
    return [m.strip() for m in texto.split(",") if m.strip()]


def _nome_cpu() -> str:
    """Nome comercial do processador, em melhor esforço (o `platform` só dá a família)."""
    try:
        if platform.system() == "Windows":
            import winreg

            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as chave:
                return str(winreg.QueryValueEx(chave, "ProcessorNameString")[0]).strip()
        if platform.system() == "Linux":
            with open("/proc/cpuinfo", encoding="utf-8") as f:
                for linha in f:
                    if linha.startswith("model name"):
                        return linha.split(":", 1)[1].strip()
    except OSError:
        pass
    return platform.processor() or "processador não identificado"


def _maquina() -> str:
    hw = detectar()

    def gb(valor: float) -> str:
        return f"{valor:g} GB".replace(".", ",")

    ram = f"{gb(hw.ram_gb)} de RAM" if hw.ram_gb is not None else "RAM não detectada"
    gpu = f"{hw.gpu} ({gb(hw.vram_gb)} de VRAM)" if hw.gpu and hw.vram_gb is not None else "GPU NVIDIA não detectada"
    return f"{platform.system()} {platform.release()}; {_nome_cpu()}; {ram}; {gpu}"


def _argumentos(argv: list[str] | None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="Bateria de capacidade de modelos locais para o servidor do Duo "
                    "(não é uma avaliação jurídica).",
        epilog="Exemplo: py avaliar_modelos.py --modelos qwen3.5:4b,qwen3.5:9b --contexto 16384 "
               "--saida dados/avaliacao-16k.md")
    ap.add_argument("--backend", choices=("ollama", "openai"), default="ollama", help="motor de LLM")
    ap.add_argument("--url", help="URL do motor (padrão: a usual do backend)")
    ap.add_argument("--modelos", required=True, help="modelos separados por vírgula")
    ap.add_argument("--contexto", type=int, default=16384, help="janela pedida, em tokens (padrão 16384)")
    ap.add_argument("--saida", help="grava o relatório Markdown neste arquivo (ex.: dados/avaliacao-AAAAMMDD.md)")
    ap.add_argument("--prazo", type=float, default=180.0, help="segundos por bateria (padrão 180)")
    ap.add_argument("--temperatura", type=float, default=0.3, help="padrão 0.3, o mesmo do servidor")
    ap.add_argument("--simulado", action="store_true", help="backend simulado (testa o script sem LLM)")
    args = ap.parse_args(argv)
    args.lista = lista_modelos(args.modelos)
    if not args.lista:
        ap.error("informe ao menos um modelo em --modelos")
    return args


def _progresso(texto: str) -> None:
    print(texto, file=sys.stderr, flush=True)


def main(argv: list[str] | None = None) -> int:
    args = _argumentos(argv)
    url = args.url or URL_PADRAO[args.backend]
    # Nuvem sempre bloqueada: a bateria é de modelos locais (ver avaliar_modelo).
    cfg = {"backend": args.backend, "url": url, "permitir_modelos_nuvem": False}
    try:
        backend = criar_backend(cfg, args.simulado)
        backend.listar_modelos()  # motor fora do ar: melhor dizer já do que falhar modelo a modelo
    except ErroBackend as erro:
        print(erro, file=sys.stderr)
        return 1
    medidor = MedidorOllama(url) if args.backend == "ollama" and not args.simulado else None
    opcoes = Opcoes(args.contexto, args.prazo, args.temperatura)
    cabecalho = {"data": date.today().isoformat(), "backend": "simulado" if args.simulado else args.backend,
                 "url": url, "versao": medidor.versao() if medidor else None, "contexto": args.contexto,
                 "temperatura": args.temperatura, "prazo": args.prazo, "maquina": _maquina()}

    avaliacoes, interrompido = [], False
    try:
        for modelo in args.lista:
            avaliacoes.append(avaliar_modelo(backend, modelo, opcoes, medidor, _progresso))
    except KeyboardInterrupt:
        interrompido = True
        _progresso("Interrompido: o relatório traz só os modelos concluídos.")

    texto = relatorio(avaliacoes, cabecalho)
    print(texto)
    if args.saida:
        pasta = os.path.dirname(os.path.abspath(args.saida))
        os.makedirs(pasta, exist_ok=True)
        with open(args.saida, "w", encoding="utf-8") as f:
            f.write(texto + "\n")
        _progresso(f"Relatório gravado em {args.saida}")
    return 130 if interrompido else 0


if __name__ == "__main__":
    # O console do Windows costuma ser cp1252; o relatório tem acentos e travessões.
    for fluxo in (sys.stdout, sys.stderr):
        if hasattr(fluxo, "reconfigure"):
            fluxo.reconfigure(encoding="utf-8")
    raise SystemExit(main())
