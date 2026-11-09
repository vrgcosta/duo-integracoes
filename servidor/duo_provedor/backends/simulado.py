"""Backend simulado: respostas previsíveis, sem LLM, para testar o Duo e o servidor.

O comportamento é escolhido por palavras-chave no pedido humano (a primeira mensagem de
usuário do turno atual), não na última mensagem: depois de uma rodada de ferramentas a
última mensagem é um resultado, e a palavra-chave precisa continuar valendo.
"""

from __future__ import annotations

import json
import re
import threading
from typing import Iterator

from ..catalogo import META
from ..emulacao import LINGUAGEM_CHAMADA, MARCA_RESULTADOS, chamada_como_texto
from ..ferramentas import eh_lembrete_interno
from ..historico import ABERTURA_RESUMO
from ..janela import estimar_tokens
from .base import Backend, ChamadaFerramenta, Evento, InfoModelo, Pedido, Raciocinio, Texto, Uso

_PEDACO = 8
_LIMITE_RESULTADO = 200
_LISTADO_NA_META = re.compile(r"• (\S+) —")
# Linha de cada ferramenta no catálogo em texto (ver emulacao.catalogo_texto).
_LINHA_CATALOGO = re.compile(
    r"^• (\S+) — (.*)\n  argumentos \(JSON Schema\): (.*)$", re.MULTILINE
)
_NOME_NO_RESULTADO = re.compile(r'"nome":\s*"([^"]+)"')
_MARCA_CORRECAO = "não puderam ser interpretados"
RESUMO_SIMULADO = "Resumo simulado da conversa até aqui."


def _eh_resultado(mensagem: dict) -> bool:
    if mensagem.get("papel") == "ferramenta":
        return True
    return mensagem.get("papel") == "usuario" and MARCA_RESULTADOS in (mensagem.get("texto") or "")


def _eh_correcao(mensagem: dict) -> bool:
    return (mensagem.get("papel") == "usuario" and MARCA_RESULTADOS in (mensagem.get("texto") or "")
            and _MARCA_CORRECAO in (mensagem.get("texto") or ""))


def _eh_resultado_da_meta(mensagem: dict) -> bool:
    if mensagem.get("papel") == "ferramenta":
        return mensagem.get("nome") == META
    return META in (mensagem.get("texto") or "")


def _ferramentas_do_catalogo(sistema: str) -> list[dict]:
    """Reconstrói as ferramentas a partir do catálogo em texto, no modo cercas."""
    achadas = []
    for nome, descricao, esquema in _LINHA_CATALOGO.findall(sistema):
        try:
            requeridos = json.loads(esquema).get("required") or []
        except (ValueError, AttributeError):
            requeridos = []
        achadas.append({"name": nome, "description": descricao,
                        "input_schema": {"type": "object", "required": requeridos}})
    return achadas


def _primeiro_listado_na_meta(meta: dict) -> str | None:
    descricao = meta.get("description") or ""
    # No modo cercas a descrição vem na linha "• meta — descrição": o 1º "• nome —" é a própria meta.
    achados = [n for n in _LISTADO_NA_META.findall(descricao) if n != META]
    return achados[0] if achados else None


class BackendSimulado(Backend):
    nome = "simulado"
    pode_cortar_calado = False

    _INFO = InfoModelo("simulado", "Simulado (sem LLM)", True, True, False, False, None)

    def listar_modelos(self) -> list[InfoModelo]:
        return [self._INFO]

    def info_modelo(self, modelo: str) -> InfoModelo | None:
        return self._INFO

    def conversar(self, pedido: Pedido, cancelado: threading.Event) -> Iterator[Evento]:
        saida = 0
        for evento in self._eventos(pedido):
            if cancelado.is_set():
                return
            if isinstance(evento, (Texto, Raciocinio)):
                saida += estimar_tokens(evento.texto)
            elif isinstance(evento, ChamadaFerramenta):
                saida += estimar_tokens(json.dumps(evento.argumentos or {}, ensure_ascii=False)) + 1
            yield evento
        entrada = sum(estimar_tokens(m.get("texto") or "") for m in pedido.mensagens)
        yield Uso(entrada, saida)

    # ------------------------------------------------------------------

    def _eventos(self, pedido: Pedido) -> Iterator[Evento]:
        mensagens = pedido.mensagens
        sistema = "\n".join(m.get("texto") or "" for m in mensagens if m.get("papel") == "sistema")
        cercas = not pedido.ferramentas and LINGUAGEM_CHAMADA in sistema
        ferramentas = _ferramentas_do_catalogo(sistema) if cercas else list(pedido.ferramentas)

        indice_pedido = next(
            (i for i in range(len(mensagens) - 1, -1, -1)
             if mensagens[i].get("papel") == "usuario" and MARCA_RESULTADOS not in (mensagens[i].get("texto") or "")
             and not eh_lembrete_interno(mensagens[i].get("texto") or "")),
            None,
        )
        texto = (mensagens[indice_pedido].get("texto") or "") if indice_pedido is not None else ""
        palavras = texto.lower()
        primeira_rodada = indice_pedido is None or indice_pedido == len(mensagens) - 1
        ultima = mensagens[-1] if mensagens else {}

        if pedido.pensar:
            yield Raciocinio("Pensando no pedido…")

        # A compactação: o eco devolveria o texto inteiro, e ela falharia por não ter encurtado nada.
        if texto.startswith(ABERTURA_RESUMO):
            yield Texto(RESUMO_SIMULADO)
            return

        if "sempre-ferramenta" in palavras and ferramentas:
            yield from self._chamar(self._primeira_real(ferramentas), cercas)
            return

        if _eh_resultado(ultima) and not _eh_correcao(ultima) and not _eh_resultado_da_meta(ultima):
            yield Texto(f"Recebi o resultado de {self._nome_do_resultado(ultima)}: "
                        f"{self._conteudo_do_resultado(ultima)[:_LIMITE_RESULTADO]}")
            return

        if "vazio" in palavras:
            return

        if "ferramenta-inexistente" in palavras:
            if primeira_rodada:
                yield from self._emitir("nao_existe", {}, cercas)
            else:
                yield Texto("Sem essa ferramenta, respondo direto.")
            return

        if "json-quebrado" in palavras:
            if primeira_rodada:
                if cercas:
                    yield Texto(f"```{LINGUAGEM_CHAMADA}\n{{\"nome\": \"ler\", \"argumentos\": }}\n```")
                    return
            elif ferramentas:
                yield from self._chamar(self._primeira_real(ferramentas), cercas)
                return

        if "ferramenta" in palavras:
            simples = next((f for f in ferramentas if f["name"] != META and not self._requeridos(f)), None)
            if simples is not None:
                yield from self._emitir(simples["name"], {}, cercas)
                return
            meta = next((f for f in ferramentas if f["name"] == META), None)
            alvo = _primeiro_listado_na_meta(meta) if meta else None
            if alvo:
                yield from self._emitir(META, {"nomes": [alvo]}, cercas)
                return

        eco = "Você disse: " + texto
        for i in range(0, len(eco), _PEDACO):
            yield Texto(eco[i:i + _PEDACO])

    # ------------------------------------------------------------------

    @staticmethod
    def _requeridos(ferramenta: dict) -> list:
        return (ferramenta.get("input_schema") or {}).get("required") or []

    @staticmethod
    def _primeira_real(ferramentas: list[dict]) -> dict:
        """1ª ferramenta que não é a meta; a meta só serve quando é a única visível."""
        return next((f for f in ferramentas if f["name"] != META), ferramentas[0])

    def _chamar(self, ferramenta: dict, cercas: bool) -> Iterator[Evento]:
        if ferramenta["name"] == META:
            alvo = _primeiro_listado_na_meta(ferramenta)
            yield from self._emitir(META, {"nomes": [alvo] if alvo else []}, cercas)
        else:
            yield from self._emitir(ferramenta["name"], {}, cercas)

    @staticmethod
    def _emitir(nome: str, argumentos: dict, cercas: bool) -> Iterator[Evento]:
        if cercas:
            yield Texto(chamada_como_texto(nome, argumentos))
        else:
            yield ChamadaFerramenta(nome, argumentos)

    @staticmethod
    def _nome_do_resultado(mensagem: dict) -> str:
        if mensagem.get("papel") == "ferramenta":
            return mensagem.get("nome") or "ferramenta"
        achado = _NOME_NO_RESULTADO.search(mensagem.get("texto") or "")
        return achado.group(1) if achado else "ferramenta"

    @staticmethod
    def _conteudo_do_resultado(mensagem: dict) -> str:
        return mensagem.get("texto") or ""
