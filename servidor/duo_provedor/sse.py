"""Saída SSE no formato do claude.ai, que o blueprint ensina o Duo a ler.

Adaptado dos métodos de SSE do `Turno` da Ponte 0.2.0 (ver créditos no README). Cada método
monta todos os eventos de uma ação e os envia numa só escrita: o Duo processa o pedaço recebido
inteiro antes de pausar, então eventos que precisam chegar juntos (as chamadas de ferramenta)
não podem ser fatiados entre escritas.
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Callable

from .backends.base import Uso


class ClienteDesconectou(Exception):
    """O Duo fechou a conexão (botão parar, aba fechada, extensão recarregada)."""


def _quadro(evento: dict) -> bytes:
    return ("data: " + json.dumps(evento, ensure_ascii=False) + "\n\n").encode("utf-8")


class EmissorSSE:
    def __init__(self, escrever: Callable[[bytes], None],
                 conectado: Callable[[], bool] | None = None) -> None:
        self._escrever = escrever
        # Sonda opcional da conexão: descobre que o Duo saiu sem depender de uma escrita falhar.
        self._conectado = conectado
        self._indice = 0
        self._bloco: str | None = None  # "text" ou "thinking" enquanto houver bloco aberto
        # Quem usa o emissor consulta isto para mandar ping quando a conexão fica muda.
        self.ultima_escrita = time.monotonic()

    def _bruto(self, dados: bytes) -> None:
        try:
            self._escrever(dados)
        except OSError as e:  # BrokenPipe, ConnectionReset e ConnectionAborted são OSError
            raise ClienteDesconectou(str(e) or type(e).__name__) from e
        self.ultima_escrita = time.monotonic()

    def _enviar(self, eventos: list[dict]) -> None:
        if eventos:
            self._bruto(b"".join(_quadro(e) for e in eventos))

    def _fechamento(self) -> list[dict]:
        """O `content_block_stop` do bloco aberto, para ir na mesma escrita do que vem depois."""
        if self._bloco is None:
            return []
        evento = {"type": "content_block_stop", "index": self._indice}
        self._bloco = None
        self._indice += 1
        return [evento]

    def _delta(self, tipo: str, delta: dict) -> None:
        eventos = []
        if self._bloco != tipo:
            eventos += self._fechamento()
            vazio = {"type": "text", "text": ""} if tipo == "text" else {"type": "thinking", "thinking": ""}
            eventos.append({"type": "content_block_start", "index": self._indice, "content_block": vazio})
            self._bloco = tipo
        eventos.append({"type": "content_block_delta", "index": self._indice, "delta": delta})
        self._enviar(eventos)

    # -- API -----------------------------------------------------------------

    def iniciar(self) -> None:
        self._enviar([{"type": "message_start",
                       "message": {"id": "msg_local_" + uuid.uuid4().hex[:16], "role": "assistant"}}])

    def texto(self, s: str) -> None:
        if s:
            self._delta("text", {"type": "text_delta", "text": s})

    def raciocinio(self, s: str) -> None:
        if s:
            self._delta("thinking", {"type": "thinking_delta", "thinking": s})

    def chamadas(self, lista: list[tuple[str, str, dict]]) -> None:
        """Todas as chamadas da rodada numa escrita: o motor do Duo pausa ao ver a 1ª completa."""
        eventos = self._fechamento()
        for tid, nome, argumentos in lista:
            eventos += [
                {"type": "content_block_start", "index": self._indice,
                 "content_block": {"type": "tool_use", "id": tid, "name": nome, "input": {}}},
                {"type": "content_block_delta", "index": self._indice,
                 "delta": {"type": "input_json_delta",
                           "partial_json": json.dumps(argumentos, ensure_ascii=False)}},
                {"type": "content_block_stop", "index": self._indice},
            ]
            self._indice += 1
        self._enviar(eventos)

    def verificar_conexao(self) -> None:
        """ClienteDesconectou se a sonda diz que o Duo fechou a conexão. Não escreve nada."""
        if self._conectado is not None and not self._conectado():
            raise ClienteDesconectou("o Duo fechou a conexão")

    def ping(self) -> None:
        # Comentário SSE: mantém a conexão viva e revela cedo que o Duo foi embora.
        self._bruto(b": ping\n\n")

    def finalizar(self, uso: Uso | None) -> None:
        delta = {"type": "message_delta", "delta": {"stop_reason": "end_turn"}}
        if uso is not None:
            delta["usage"] = {"input_tokens": uso.entrada, "output_tokens": uso.saida}
        self._enviar(self._fechamento() + [delta, {"type": "message_stop"}])

    def erro(self, mensagem: str) -> None:
        """Último evento do fluxo. Se o Duo já fechou a conexão, não há a quem avisar."""
        try:
            self._enviar(self._fechamento() + [
                {"type": "error", "error": {"type": "duo-provedor", "message": mensagem}}])
        except ClienteDesconectou:
            pass
