"""Servidor HTTP falso para testar clientes de motores locais (Ollama, OpenAI-compatível).

Serve cada pedaço da resposta com `flush`, para o cliente ver o streaming de verdade, e
grava o que recebeu para os testes conferirem o corpo do pedido.
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class ServidorFalso:
    """`rotas`: {(método, caminho): (status, content-type, [pedaços em bytes])}.

    A rota também pode ser uma função do corpo JSON, para um mesmo caminho responder conforme o
    pedido (o `/api/show` do Ollama, que muda com o modelo).

    `atraso_inicial`: segundos de espera antes do primeiro byte (simula o prefill lento
    em CPU). A espera é interrompida ao parar o servidor.
    """

    def __init__(self, rotas: dict[tuple[str, str], object],
                 atraso_inicial: float = 0.0, atraso_caminho: str | None = None) -> None:
        self.rotas = rotas
        self.atraso_inicial = atraso_inicial
        self.atraso_caminho = atraso_caminho
        self.pedidos: list[tuple[str, str, dict | None, dict]] = []
        self.url = ""
        self._parar = threading.Event()
        falso = self

        class Manipulador(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args) -> None:  # silencia o stderr dos testes
                pass

            def _tratar(self) -> None:
                tamanho = int(self.headers.get("Content-Length") or 0)
                bruto = self.rfile.read(tamanho) if tamanho else b""
                try:
                    corpo = json.loads(bruto) if bruto else None
                except ValueError:
                    corpo = None
                falso.pedidos.append((self.command, self.path, corpo, dict(self.headers)))
                rota = falso.rotas.get((self.command, self.path))
                if rota is None:
                    rota = (404, "application/json", [b'{"error":"rota desconhecida"}'])
                status, tipo, pedacos = rota(corpo) if callable(rota) else rota
                if (falso.atraso_inicial and falso.atraso_caminho in (None, self.path)
                        and falso._parar.wait(falso.atraso_inicial)):
                    return
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", tipo)
                    self.send_header("Connection", "close")
                    self.end_headers()
                    for pedaco in pedacos:
                        self.wfile.write(pedaco)
                        self.wfile.flush()
                except OSError:
                    pass
                self.close_connection = True

            do_GET = do_POST = _tratar

        self._servidor = ThreadingHTTPServer(("127.0.0.1", 0), Manipulador)
        self._servidor.daemon_threads = True
        self.url = f"http://127.0.0.1:{self._servidor.server_address[1]}"
        self._thread = threading.Thread(target=self._servidor.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)

    def __enter__(self) -> "ServidorFalso":
        self._thread.start()
        return self

    def __exit__(self, *args) -> None:
        self._parar.set()
        self._servidor.shutdown()
        self._servidor.server_close()
        self._thread.join(timeout=5)
