"""Cliente de contrato: faz o papel do Duo guiado só pelo blueprint gerado.

Escrito do zero a partir de docs/blueprint.md. Cada operação busca no blueprint o caminho, o
corpo e os seletores, do jeito que o motor do Duo faz; por isso, se o servidor e o blueprint
divergirem, os testes que usam este cliente quebram. Não importa nada de `duo_provedor`.
"""

from __future__ import annotations

import copy
import http.client
import json
import re
from dataclasses import dataclass
from typing import Callable
from urllib.parse import quote, urlencode, urlsplit

_VARIAVEL = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


class ErroCliente(Exception):
    """Resposta fora de 200 numa rota JSON."""

    def __init__(self, status: int, corpo: object) -> None:
        super().__init__(f"HTTP {status}: {corpo}")
        self.status = status
        self.corpo = corpo


@dataclass
class Resposta:
    texto: str
    pensamento: str
    chamadas: list[tuple[str, str, dict]]
    uso: tuple[int, int] | None
    erro: str | None
    eventos: list[dict]


def _interpolar(valor: object, variaveis: dict) -> object:
    """Uma passada só: o que foi substituído não é reinterpretado (um prompt com "{leaf}" fica
    intacto). Marcador que ocupa o texto inteiro devolve o valor com o tipo original."""
    if isinstance(valor, str):
        exato = _VARIAVEL.fullmatch(valor)
        if exato and exato.group(1) in variaveis:
            return variaveis[exato.group(1)]
        return _VARIAVEL.sub(
            lambda m: str(variaveis[m.group(1)]) if m.group(1) in variaveis else m.group(0), valor)
    if isinstance(valor, list):
        return [_interpolar(v, variaveis) for v in valor]
    if isinstance(valor, dict):
        return {k: _interpolar(v, variaveis) for k, v in valor.items()}
    return valor


def _pegar(obj: object, caminho: str) -> object:
    """Lê "a.b.c" em objetos aninhados; caminho vazio devolve o próprio objeto."""
    if not caminho:
        return obj
    for parte in caminho.split("."):
        if not isinstance(obj, dict):
            return None
        obj = obj.get(parte)
    return obj


def _campo(item: dict, seletor: object) -> object:
    # O blueprint usa as duas formas: "uuid" ou {"caminho": "uuid"}.
    return _pegar(item, seletor["caminho"] if isinstance(seletor, dict) else seletor)


def _casa(evento: dict, seletor: dict) -> bool:
    if evento.get("type") != seletor.get("tipoEvento"):
        return False
    return all(_pegar(evento, k) == v for k, v in (seletor.get("igual") or {}).items())


def _mesclar(base: dict, extra: dict) -> None:
    for chave, valor in extra.items():
        if isinstance(valor, dict) and isinstance(base.get(chave), dict):
            _mesclar(base[chave], valor)
        else:
            base[chave] = valor


class _LeitorSSE:
    """Aplica os seletores de `stream`, `pensamento.stream` e `ferramentas` a cada evento."""

    def __init__(self, bp: dict) -> None:
        self._stream = bp["stream"]
        self._pensamento = bp["pensamento"]["stream"]
        self._ferramentas = bp["ferramentas"]
        self.eventos: list[dict] = []
        self.texto: list[str] = []
        self.pensamento: list[str] = []
        self.chamadas: list[tuple[str, str, dict]] = []
        self.pendentes: list[tuple[str, str, dict]] = []  # fechadas e ainda sem resultado
        self.uso: tuple[int, int] | None = None
        self.erro: str | None = None
        self._aberta: list | None = None  # [id, nome, json acumulado]

    def quadro(self, bruto: bytes) -> None:
        for linha in bruto.decode("utf-8").split("\n"):
            # ": ping" e qualquer outro comentário SSE são ignorados.
            if linha.startswith("data:"):
                evento = json.loads(linha[len("data:"):].strip())
                self.eventos.append(evento)
                self._evento(evento)

    def _evento(self, ev: dict) -> None:
        sel_texto = self._stream["texto"]
        if _casa(ev, sel_texto):
            self.texto.append(_pegar(ev, sel_texto["caminhoTexto"]) or "")
        for sel in self._pensamento:
            if _casa(ev, sel):
                self.pensamento.append(_pegar(ev, sel["caminhoTexto"]) or "")
        inicio, entrada, fim = (self._ferramentas[k] for k in ("toolCall", "toolCallInput", "toolCallFim"))
        if _casa(ev, inicio):
            self._aberta = [_pegar(ev, inicio["caminhoId"]), _pegar(ev, inicio["caminhoNome"]), ""]
        elif self._aberta is not None and _casa(ev, entrada):
            self._aberta[2] += _pegar(ev, entrada["caminhoTexto"]) or ""
        elif self._aberta is not None and _casa(ev, fim):
            tid, nome, bruto = self._aberta
            self._aberta = None
            chamada = (tid, nome, json.loads(bruto) if bruto.strip() else {})
            self.chamadas.append(chamada)
            self.pendentes.append(chamada)
        sel_uso = self._stream["usage"]
        if ev.get("type") == sel_uso["tipoEvento"]:
            entrada_t, saida_t = _pegar(ev, sel_uso["entrada"]), _pegar(ev, sel_uso["saida"])
            if entrada_t is not None and saida_t is not None:
                self.uso = (entrada_t, saida_t)
        if ev.get("type") == "error":
            self.erro = _pegar(ev, "error.message") or "erro sem mensagem"

    def resposta(self) -> Resposta:
        return Resposta("".join(self.texto), "".join(self.pensamento), list(self.chamadas),
                        self.uso, self.erro, list(self.eventos))


class ClienteDuo:
    TIMEOUT = 10

    def __init__(self, url_base: str, blueprint: dict) -> None:
        partes = urlsplit(url_base)
        self._host, self._porta = partes.hostname, partes.port
        self.bp = blueprint
        self.variaveis: dict = {}

    @property
    def raiz(self) -> str:
        return self.bp["conversas"]["ler"]["arvore"]["raiz"]

    # -- transporte --------------------------------------------------------

    def _abrir(self, metodo: str, caminho: str, corpo: object):
        conexao = http.client.HTTPConnection(self._host, self._porta, timeout=self.TIMEOUT)
        cabecalhos = dict(self.bp.get("headers") or {})
        dados = None
        if corpo is not None:
            dados = json.dumps(corpo, ensure_ascii=False).encode("utf-8")
            cabecalhos["Content-Type"] = "application/json"
        try:
            conexao.request(metodo, caminho, body=dados, headers=cabecalhos)
            return conexao, conexao.getresponse()
        except BaseException:
            conexao.close()
            raise

    def _chamar(self, metodo: str, caminho: str, corpo: object = None) -> object:
        conexao, resp = self._abrir(metodo, caminho, corpo)
        try:
            bruto = resp.read()
        finally:
            # Em HTTP/1.0 a conexão passa à resposta: fechar só a conexão deixaria o socket aberto.
            resp.close()
            conexao.close()
        valor = json.loads(bruto) if bruto else None
        if not 200 <= resp.status < 300:
            raise ErroCliente(resp.status, valor)
        return valor

    def _passos(self, passos: list[dict], extras: dict) -> None:
        for passo in passos:
            variaveis = {**self.variaveis, **extras}
            corpo = _interpolar(passo["corpo"], variaveis) if "corpo" in passo else None
            resp = self._chamar(passo.get("metodo", "GET"), _interpolar(passo["caminho"], variaveis), corpo)
            for nome, seletor in (passo.get("extrai") or {}).items():
                self.variaveis[nome] = _campo(resp, seletor)

    # -- contexto, conversas e pensamento ----------------------------------

    def contexto(self) -> dict:
        self._passos(self.bp["contexto"], {})
        return dict(self.variaveis)

    def criar_conversa(self, modelo: str = "padrao", projeto: str | None = None) -> str:
        op = self.bp["conversas"]["criar"]
        corpo = _interpolar(op["corpo"], {**self.variaveis, "modelo": modelo})
        if projeto:
            corpo[op["campoProjeto"]] = projeto
        return _campo(self._chamar(op.get("metodo", "POST"), op["caminho"], corpo), op["id"])

    def ligar_pensamento(self, cid: str, ativo: bool) -> None:
        self._passos(self.bp["pensamento"]["ligar" if ativo else "desligar"], {"convId": cid})

    def _resumos(self, op: dict, caminho: str) -> list[dict]:
        itens = _pegar(self._chamar(op.get("metodo", "GET"), caminho), op["itens"]) or []
        chaves = [c for c in ("id", "titulo", "atualizadoEm", "projetoId") if c in op]
        return [{c: _campo(item, op[c]) for c in chaves} for item in itens]

    def listar(self) -> list[dict]:
        op = self.bp["conversas"]["listar"]
        return self._resumos(op, op["caminho"])

    def buscar(self, termo: str) -> list[dict]:
        op = self.bp["conversas"]["buscar"]
        return self._resumos(op, _interpolar(op["caminho"], {"termo": quote(termo)}))

    # -- janela de contexto ----------------------------------------------------

    def _rota_janela(self, nome: str, metodo: str, cid: str) -> tuple[str, str]:
        rota = self.bp["janelaContexto"][nome]
        return rota.get("metodo", metodo), _interpolar(rota["caminho"], {"convId": cid})

    def janela(self, cid: str, modelo: str = "padrao", contexto: int | None = None) -> dict:
        """Como o motor do Duo: `modelo` sempre na consulta e `contexto` só se foi configurado."""
        metodo, caminho = self._rota_janela("ler", "GET", cid)
        consulta = {"modelo": modelo, **({"contexto": contexto} if contexto is not None else {})}
        return self._chamar(metodo, f"{caminho}?{urlencode(consulta)}")

    def janela_excluir(self, cid: str, excluidos: list[str]) -> None:
        metodo, caminho = self._rota_janela("excluir", "PUT", cid)
        self._chamar(metodo, caminho, {"excluidos": excluidos})

    def compactar(self, cid: str, modelo: str = "padrao") -> dict:
        metodo, caminho = self._rota_janela("compactar", "POST", cid)
        return self._chamar(metodo, caminho, {"modelo": modelo})

    def descompactar(self, cid: str) -> dict:
        metodo, caminho = self._rota_janela("descompactar", "DELETE", cid)
        return self._chamar(metodo, caminho)

    # -- envio ---------------------------------------------------------------

    def enviar(self, cid: str, prompt: str, leaf: str, ferramentas: list[dict] | None = None,
               executar: Callable[[str, dict], object] | None = None,
               extras: dict | None = None) -> Resposta:
        variaveis = {**self.variaveis, "convId": cid, "prompt": prompt, "leaf": leaf,
                     "modelo": "padrao", "timezone": "America/Bahia", "sistema": ""}
        op = self.bp["enviar"]
        corpo = _interpolar(op["corpo"], variaveis)
        if ferramentas is not None:
            corpo[self.bp["ferramentas"]["campoTools"]] = ferramentas
        if extras:
            _mesclar(corpo, copy.deepcopy(extras))
        conexao, resp = self._abrir("POST", _interpolar(op["caminho"], variaveis), corpo)
        try:
            if resp.status != 200:
                bruto = resp.read()
                detalhe = _pegar(json.loads(bruto), "error.message") if bruto else None
                return Resposta("", "", [], None, f"HTTP {resp.status}: {detalhe}", [])
            leitor = _LeitorSSE(self.bp)
            pendente = b""
            while True:
                # Como o Duo: processa o pedaço recebido inteiro e só então devolve os resultados
                # das chamadas fechadas, continuando a ler a MESMA resposta.
                pedaco = resp.fp.read1(65536)
                if not pedaco:
                    break
                *quadros, pendente = (pendente + pedaco).split(b"\n\n")
                for quadro in quadros:
                    leitor.quadro(quadro)
                while leitor.pendentes:
                    tid, nome, entrada = leitor.pendentes.pop(0)
                    if executar is None:
                        raise RuntimeError(f"o servidor pediu a ferramenta {nome}, mas o envio não tem `executar`")
                    self._devolver(cid, tid, executar(nome, entrada))
            if pendente.strip():
                leitor.quadro(pendente)
            return leitor.resposta()
        finally:
            resp.close()
            conexao.close()

    def _devolver(self, cid: str, tid: str, conteudo: object) -> None:
        op = self.bp["ferramentas"]["enviarResultado"]
        texto = conteudo if isinstance(conteudo, str) else json.dumps(conteudo, ensure_ascii=False)
        variaveis = {**self.variaveis, "convId": cid, "toolCallId": tid, "content": texto}
        self._chamar(op.get("metodo", "POST"), _interpolar(op["caminho"], variaveis),
                     _interpolar(op["corpo"], variaveis))

    # -- leitura -------------------------------------------------------------

    def ler(self, cid: str, folha: str | None = None) -> list[dict]:
        """Cadeia raiz→folha. Sem `folha`, usa o último nó gravado (o mais recente)."""
        op = self.bp["conversas"]["ler"]
        dados = self._chamar(op.get("metodo", "GET"), _interpolar(op["caminho"], {"convId": cid}))
        nos = _pegar(dados, op["itens"]) or []
        arvore = op["arvore"]
        por_id = {n.get(arvore["uuid"]): n for n in nos}
        atual = por_id.get(folha) if folha else (nos[-1] if nos else None)
        cadeia, vistos = [], set()
        while atual is not None and atual.get(arvore["uuid"]) not in vistos:
            vistos.add(atual.get(arvore["uuid"]))
            cadeia.append(atual)
            pai = atual.get(arvore["parent"])
            atual = None if pai == arvore["raiz"] else por_id.get(pai)
        cadeia.reverse()
        return [self._mensagem(no, op) for no in cadeia]

    @staticmethod
    def _mensagem(no: dict, op: dict) -> dict:
        papel = op["role"]["mapa"].get(no.get(op["role"]["campo"]))

        sel = op["texto"]
        texto = no.get(sel["campoDireto"]) or ""
        if not texto:
            b = sel["blocos"]
            texto = "".join(bl.get(b["campoTexto"]) or "" for bl in no.get(b["em"]) or []
                            if bl.get(b["filtroCampo"]) == b["filtroValor"])

        p = op["pensamento"]
        pensamento = "".join((item.get(p["itemCampo"]) or "") + p["sufixo"]
                             for bl in no.get(p["em"]) or [] if bl.get(p["filtroCampo"]) == p["filtroValor"]
                             for item in bl.get(p["arrayCampo"]) or [])

        f = op["ferramentas"]
        chamada, resultado = f["call"], f["result"]
        blocos = no.get(f["em"]) or []
        resultados = {bl.get(resultado["usoId"]): bl.get(resultado["conteudo"]) for bl in blocos
                      if bl.get(resultado["filtroCampo"]) == resultado["filtroValor"]}
        ferramentas = [(bl.get(chamada["id"]), bl.get(chamada["nome"]), bl.get(chamada["input"]),
                        resultados.get(bl.get(chamada["id"])))
                       for bl in blocos if bl.get(chamada["filtroCampo"]) == chamada["filtroValor"]]
        return {"role": papel, "texto": texto, "pensamento": pensamento, "ferramentas": ferramentas}
