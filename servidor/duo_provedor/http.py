"""Camada HTTP: autorização, leitura de corpo, respostas JSON/SSE e o servidor em loopback.

Adaptado do servidor da Ponte 0.2.0 (ver créditos no README), sem as rotas da ponte.
"""

from __future__ import annotations

import json
import re
import secrets
import select
import socket
import sys
import traceback
from email.parser import BytesParser
from email.policy import default as POLITICA_EMAIL
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from . import VERSAO
from .config import CABECALHO_CHAVE

# Acima de 30 MB por arquivo (limite do Duo) cabem alguns anexos; o teto só evita
# que um cliente ruim faça o servidor ler gigabytes para a memória.
LIMITE_CORPO = 128 * 1024 * 1024


class ErroHTTP(Exception):
    def __init__(self, mensagem: str, status: int = 400) -> None:
        super().__init__(mensagem)
        self.status = status


def corpo_erro(msg: str) -> dict:
    return {"error": {"type": "duo-provedor", "message": msg}}


def autorizado(origem: str, chave_recebida: str, chave: str) -> bool:
    # Páginas web sempre mandam Origin http(s); só a extensão (chrome-extension://) ou
    # clientes sem Origin passam. Assim um site aberto no navegador não alcança o servidor
    # mesmo que descubra a chave.
    if origem.startswith(("http://", "https://")):
        return False
    return secrets.compare_digest(chave_recebida.encode("utf-8"), chave.encode("utf-8"))


class Manipulador(BaseHTTPRequestHandler):
    # HTTP/1.0: o SSE termina fechando a conexão, sem chunked.
    protocol_version = "HTTP/1.0"
    server_version = "DuoProvedorLocal/" + VERSAO
    servico = None  # definido em criar_servidor()

    def log_message(self, formato, *args) -> None:
        pass

    def do_GET(self) -> None:
        self._rotear("GET")

    def do_POST(self) -> None:
        self._rotear("POST")

    def do_PUT(self) -> None:
        self._rotear("PUT")

    def do_DELETE(self) -> None:
        self._rotear("DELETE")

    def do_OPTIONS(self) -> None:
        # Sem CORS: páginas web não devem falar com o servidor.
        self.responder_json(403, corpo_erro("origem não permitida"))

    # -- roteamento --------------------------------------------------------

    def _rotear(self, metodo: str) -> None:
        partes = urlsplit(self.path)
        consulta = parse_qs(partes.query)
        origem = self.headers.get("Origin", "")
        chave = self.headers.get(CABECALHO_CHAVE, "")
        if not autorizado(origem, chave, self.servico.cfg["chave"]):
            self.responder_json(403, corpo_erro("chave inválida ou origem não permitida"))
            return
        try:
            for m, padrao, nome in self.servico.ROTAS:
                achou = re.fullmatch(padrao, partes.path)
                if m == metodo and achou:
                    resposta = getattr(self.servico, nome)(self, consulta, *achou.groups())
                    if resposta is not None:
                        self.responder_json(200, resposta)
                    return
            self.responder_json(404, corpo_erro(f"rota inexistente: {metodo} {partes.path}"))
        except ErroHTTP as e:
            self._tentar(lambda: self.responder_json(e.status, corpo_erro(str(e))))
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        except Exception as e:  # noqa: BLE001 - devolve o erro ao Duo em vez de derrubar a conexão
            print("erro interno:\n" + traceback.format_exc(), file=sys.stderr, flush=True)
            self._tentar(lambda: self.responder_json(500, corpo_erro(f"erro interno do servidor: {e}")))

    @staticmethod
    def _tentar(func) -> None:
        try:
            func()
        except OSError:
            pass

    # -- entrada -----------------------------------------------------------

    def _ler_corpo(self) -> bytes:
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise ErroHTTP("Content-Length inválido", 400) from None
        if n < 0:
            raise ErroHTTP("Content-Length inválido", 400)
        if n > LIMITE_CORPO:
            raise ErroHTTP("corpo grande demais", 413)
        return self.rfile.read(n) if n else b""

    def ler_json(self) -> dict:
        if not hasattr(self, "_json_cache"):
            bruto = self._ler_corpo()
            try:
                dados = json.loads(bruto) if bruto else {}
            except ValueError:
                raise ErroHTTP("corpo JSON inválido", 400) from None
            if not isinstance(dados, dict):
                raise ErroHTTP("o corpo deve ser um objeto JSON", 400)
            self._json_cache = dados
        return self._json_cache

    def ler_multipart(self, campo: str) -> tuple[str, str, bytes]:
        """Devolve (nome do arquivo, tipo MIME, bytes) da parte `campo`."""
        tipo = self.headers.get("Content-Type", "")
        cabecalho = f"Content-Type: {tipo}\r\nMIME-Version: 1.0\r\n\r\n".encode("latin-1", "replace")
        mensagem = BytesParser(policy=POLITICA_EMAIL).parsebytes(cabecalho + self._ler_corpo())
        if not mensagem.is_multipart():
            raise ErroHTTP("upload deve ser multipart/form-data", 400)
        for parte in mensagem.iter_parts():
            if parte.get_param("name", header="content-disposition") == campo:
                nome = parte.get_filename() or "arquivo"
                # O navegador manda o nome em UTF-8 cru; o parser de e-mail o lê como latin-1
                # com surrogateescape, e é preciso desfazer isso para "petição.pdf" não virar lixo.
                try:
                    nome = nome.encode("utf-8", "surrogateescape").decode("utf-8")
                except UnicodeError:
                    pass
                return nome, parte.get_content_type(), parte.get_payload(decode=True) or b""
        raise ErroHTTP(f"upload sem o campo '{campo}'", 400)

    # -- saída -------------------------------------------------------------

    def responder_json(self, status: int, obj: object) -> None:
        corpo = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(corpo)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(corpo)

    def iniciar_sse(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        self.wfile.flush()

    def cliente_conectado(self) -> bool:
        """False quando o Duo já fechou a conexão, sem precisar escrever nada nela.

        Em HTTP/1.0 há uma requisição por conexão e o corpo já foi lido, então o socket só fica
        legível quando chega o fechamento (recv espiando devolve b"") ou um reset. Mesma ideia do
        `cliente_vivo` da Ponte 0.2.0.
        """
        try:
            legivel, _, _ = select.select([self.connection], [], [], 0)
            if not legivel:
                return True
            return bool(self.connection.recv(1, socket.MSG_PEEK))
        except (OSError, ValueError):  # ValueError: socket já fechado (fileno -1)
            return False


class ServidorLocal(ThreadingHTTPServer):
    # Sem SO_REUSEADDR: no Windows ele deixa uma 2ª instância abrir a mesma porta e dividir as
    # requisições com a 1ª; SO_EXCLUSIVEADDRUSE garante uso exclusivo.
    allow_reuse_address = False
    daemon_threads = True

    def server_bind(self) -> None:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()

    def definir_servico(self, servico) -> None:
        """Liga o serviço a um servidor criado sem ele. Tem de vir antes do `serve_forever`.

        Existe para o bind vir primeiro: abrir o serviço abre (e pode migrar) o banco, e isso só
        pode acontecer depois de garantida a porta, ou seja, de saber que nenhuma outra instância
        está usando a mesma pasta de dados. As conexões que chegarem nesse meio-tempo esperam na
        fila do socket.
        """
        self.RequestHandlerClass.servico = servico


def criar_servidor(servico, porta: int) -> ServidorLocal:
    """Sempre em 127.0.0.1: o conteúdo dos autos não pode ficar exposto à rede.

    `servico` pode ser None e ficar para `definir_servico`. A classe do manipulador é criada a cada
    servidor, então o serviço guardado nela não vaza para outro servidor do mesmo processo (os testes
    sobem vários).
    """
    classe = type("ManipuladorLocal", (Manipulador,), {"servico": servico})
    return ServidorLocal(("127.0.0.1", porta), classe)
