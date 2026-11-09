"""Backend OpenAI-compatível contra um servidor falso (SSE de /chat/completions)."""

from __future__ import annotations

import json
import socket
import threading
import time
import unittest
from pathlib import Path

from duo_provedor.backends.base import (
    ChamadaFerramenta, Cortada, ErroBackend, InfoModelo, Pedido, Raciocinio, Texto, Uso,
)
from duo_provedor.backends.openai import BackendOpenAI, SeparadorThink
from testes.falso import ServidorFalso

FIXTURES = Path(__file__).parent / "fixtures"
CAMINHO = "/v1/chat/completions"


def fixture(nome: str) -> list[bytes]:
    return [(FIXTURES / nome).read_bytes()]


def rota_chat(nome: str) -> dict:
    return {("POST", CAMINHO): (200, "text/event-stream", fixture(nome))}


def pedido(**extra) -> Pedido:
    base = dict(modelo="qwen3:0.6b", mensagens=[{"papel": "usuario", "texto": "oi"}],
                ferramentas=[], pensar=False, contexto_tokens=8192, temperatura=None)
    base.update(extra)
    return Pedido(**base)


def porta_livre() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class TestSeparadorThink(unittest.TestCase):
    def juntar(self, pedacos):
        sep, saida = SeparadorThink(), []
        for p in pedacos:
            saida += sep.alimentar(p)
        return saida + sep.finalizar()

    def test_think_partido_entre_deltas(self):
        eventos = self.juntar(["<th", "ink>a</thi", "nk>b"])
        self.assertEqual("".join(e.texto for e in eventos if isinstance(e, Raciocinio)), "a")
        self.assertEqual("".join(e.texto for e in eventos if isinstance(e, Texto)), "b")

    def test_texto_sem_tag_sai_na_hora(self):
        sep = SeparadorThink()
        self.assertEqual(sep.alimentar("Olá, mundo"), [Texto("Olá, mundo")])

    def test_sufixo_que_parece_tag_fica_retido(self):
        sep = SeparadorThink()
        self.assertEqual(sep.alimentar("a <"), [Texto("a ")])
        self.assertEqual(sep.alimentar("b"), [Texto("<b")])

    def test_sufixo_retido_sai_no_finalizar(self):
        sep = SeparadorThink()
        sep.alimentar("fim <th")
        self.assertEqual(sep.finalizar(), [Texto("<th")])

    def test_dentro_do_think_tambem_segura_o_fecho_parcial(self):
        sep = SeparadorThink()
        self.assertEqual(sep.alimentar("<think>x</thi"), [Raciocinio("x")])
        self.assertEqual(sep.alimentar("nk>y"), [Texto("y")])


class TestConversar(unittest.TestCase):
    def conversar(self, rotas, ped=None, **kw):
        with ServidorFalso(rotas) as srv:
            backend = BackendOpenAI(srv.url + "/v1", **kw)
            eventos = list(backend.conversar(ped or pedido(), threading.Event()))
            return eventos, list(srv.pedidos)

    def _com_motivo(self, motivo: str) -> list:
        linhas = [b'data: {"choices":[{"delta":{"content":"meio"},"finish_reason":null}]}\n\n',
                  ('data: {"choices":[{"delta":{},"finish_reason":"' + motivo + '"}]}\n\n').encode(),
                  b'data: {"choices":[],"usage":{"prompt_tokens":10,"completion_tokens":5}}\n\n',
                  b'data: [DONE]\n\n']
        eventos, _ = self.conversar({("POST", CAMINHO): (200, "text/event-stream", linhas)})
        return eventos

    def test_resposta_parada_no_limite_avisa(self):
        self.assertEqual(self._com_motivo("length"), [Texto("meio"), Cortada(), Uso(10, 5)])

    def test_resposta_que_acabou_nao_avisa(self):
        self.assertEqual(self._com_motivo("stop"), [Texto("meio"), Uso(10, 5)])

    def test_texto_e_uso(self):
        eventos, _ = self.conversar(rota_chat("openai-texto.sse"))
        self.assertEqual("".join(e.texto for e in eventos if isinstance(e, Texto)), "Olá, mundo")
        self.assertEqual(eventos[-1], Uso(24, 6))

    def test_reasoning_content_e_reasoning(self):
        # O Ollama usa `reasoning`; DeepSeek/vLLM usam `reasoning_content`. Os dois valem.
        eventos, _ = self.conversar(rota_chat("openai-reasoning.sse"))
        self.assertEqual([e.texto for e in eventos if isinstance(e, Raciocinio)], ["Pensando", " bem"])
        self.assertEqual([e.texto for e in eventos if isinstance(e, Texto)], ["Resposta"])
        linhas = [
            {"choices": [{"delta": {"reasoning_content": "r1"}}]},
            {"choices": [{"delta": {"content": "t1"}}]},
        ]
        bruto = b"".join(b"data: " + json.dumps(x).encode() + b"\n\n" for x in linhas)
        rotas = {("POST", CAMINHO): (200, "text/event-stream", [bruto + b"data: [DONE]\n\n"])}
        eventos, _ = self.conversar(rotas)
        self.assertEqual(eventos, [Raciocinio("r1"), Texto("t1")])

    def test_think_no_conteudo(self):
        eventos, _ = self.conversar(rota_chat("openai-think.sse"))
        self.assertEqual([e for e in eventos if not isinstance(e, Uso)],
                         [Raciocinio("a"), Texto("b")])

    def test_ferramentas_fatiadas_e_paralelas(self):
        eventos, _ = self.conversar(rota_chat("openai-ferramentas.sse"))
        self.assertEqual([e for e in eventos if isinstance(e, ChamadaFerramenta)],
                         [ChamadaFerramenta("listar_documentos", {}),
                          ChamadaFerramenta("ler_documento", {"id": "7"})])
        self.assertEqual(eventos[-1], Uso(168, 28))

    def test_linhas_sem_choices_e_comentarios_sao_ignorados(self):
        bruto = (b': keep-alive\n\ndata: {"choices": []}\n\n'
                 b'data: {"choices": [{"delta": {"content": "x"}}]}\n\ndata: [DONE]\n\n'
                 b'data: {"choices": [{"delta": {"content": "depois"}}]}\n\n')
        eventos, _ = self.conversar({("POST", CAMINHO): (200, "text/event-stream", [bruto])})
        self.assertEqual(eventos, [Texto("x")])

    def test_corpo_do_pedido(self):
        defs = [{"name": "ler", "description": "Lê", "input_schema": {"type": "object"}}]
        mensagens = [
            {"papel": "sistema", "texto": "Seja breve."},
            {"papel": "usuario", "texto": "leia", "imagens": ["QUJD"]},
            {"papel": "assistente", "texto": "", "chamadas": [
                {"id": "c1", "nome": "ler", "argumentos": {"id": 3}}]},
            {"papel": "ferramenta", "id_chamada": "c1", "nome": "ler", "texto": "conteúdo"},
            {"papel": "assistente", "texto": "feito"},
        ]
        _, pedidos = self.conversar(rota_chat("openai-texto.sse"),
                                    pedido(ferramentas=defs, mensagens=mensagens, temperatura=0.3))
        corpo = next(c for m, p, c, h in pedidos if p == CAMINHO)
        self.assertEqual(corpo["model"], "qwen3:0.6b")
        self.assertIs(corpo["stream"], True)
        self.assertEqual(corpo["stream_options"], {"include_usage": True})
        self.assertEqual(corpo["temperature"], 0.3)
        self.assertEqual(corpo["tools"][0]["function"]["name"], "ler")
        self.assertEqual(corpo["messages"], [
            {"role": "system", "content": "Seja breve."},
            {"role": "user", "content": [
                {"type": "text", "text": "leia"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,QUJD"}}]},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c1", "type": "function",
                 "function": {"name": "ler", "arguments": '{"id": 3}'}}]},
            {"role": "tool", "tool_call_id": "c1", "content": "conteúdo"},
            {"role": "assistant", "content": "feito"},
        ])

    def test_sem_temperatura_nem_tools_omite_os_campos(self):
        _, pedidos = self.conversar(rota_chat("openai-texto.sse"))
        corpo = next(c for m, p, c, h in pedidos if p == CAMINHO)
        self.assertNotIn("temperature", corpo)
        self.assertNotIn("tools", corpo)

    def test_authorization_quando_ha_chave(self):
        _, pedidos = self.conversar(rota_chat("openai-texto.sse"), chave="sk-segredo")
        cab = next(h for m, p, c, h in pedidos if p == CAMINHO)
        self.assertEqual(cab.get("Authorization"), "Bearer sk-segredo")

    def test_sem_chave_nao_envia_authorization(self):
        _, pedidos = self.conversar(rota_chat("openai-texto.sse"))
        cab = next(h for m, p, c, h in pedidos if p == CAMINHO)
        self.assertNotIn("Authorization", cab)

    def test_cancelado_antes_do_primeiro_byte_encerra_em_menos_de_1s(self):
        # O prefill em CPU pode levar minutos sem emitir nada: o cancelamento precisa valer aí.
        with ServidorFalso(rota_chat("openai-texto.sse"), atraso_inicial=5.0,
                           atraso_caminho=CAMINHO) as srv:
            backend = BackendOpenAI(srv.url + "/v1", timeout=30.0)
            cancelado = threading.Event()
            threading.Timer(0.2, cancelado.set).start()
            inicio = time.monotonic()
            eventos = list(backend.conversar(pedido(), cancelado))
            self.assertLess(time.monotonic() - inicio, 1.0)
            self.assertEqual(eventos, [])

    def test_cancelado_no_meio_do_stream_para_na_proxima_linha(self):
        cancelado = threading.Event()
        with ServidorFalso(rota_chat("openai-texto.sse")) as srv:
            backend = BackendOpenAI(srv.url + "/v1")
            eventos = []
            for e in backend.conversar(pedido(), cancelado):
                eventos.append(e)
                cancelado.set()
        self.assertEqual(eventos, [Texto("Olá")])

    def test_sem_threads_extras_apos_conversar(self):
        antes = threading.active_count()
        self.conversar(rota_chat("openai-texto.sse"))
        self.assertLessEqual(threading.active_count(), antes)


class TestModelos(unittest.TestCase):
    def test_listar_modelos(self):
        corpo = {"object": "list", "data": [{"id": "gpt-x", "object": "model"}, {"id": "outro"}, {}]}
        rotas = {("GET", "/v1/models"): (200, "application/json", [json.dumps(corpo).encode()])}
        with ServidorFalso(rotas) as srv:
            backend = BackendOpenAI(srv.url + "/v1/", chave="k")
            modelos = backend.listar_modelos()
            cab = srv.pedidos[0][3]
        self.assertEqual(modelos, [InfoModelo("gpt-x", "gpt-x", True, False, False, False, None),
                                   InfoModelo("outro", "outro", True, False, False, False, None)])
        self.assertEqual(cab.get("Authorization"), "Bearer k")

    def test_info_modelo_nao_consulta_o_servidor(self):
        with ServidorFalso({}) as srv:
            info = BackendOpenAI(srv.url + "/v1").info_modelo("m")
            self.assertEqual(srv.pedidos, [])
        self.assertEqual(info, InfoModelo("m", "m", True, False, False, False, None))

    # O Ollama /v1 lista os modelos de nuvem junto dos locais; o sufixo é a única pista.
    NUVEM_NA_LISTA = {"object": "list", "data": [
        {"id": "qwen3:4b"}, {"id": "gpt-oss:120b-cloud"}, {"id": "glm-4.6:cloud"}, {"id": "cloudy:1b"}]}

    def _listar(self, **kw) -> list[InfoModelo]:
        rotas = {("GET", "/v1/models"): (200, "application/json",
                                         [json.dumps(self.NUVEM_NA_LISTA).encode()])}
        with ServidorFalso(rotas) as srv:
            return BackendOpenAI(srv.url + "/v1", **kw).listar_modelos()

    def test_sufixo_cloud_some_da_lista_por_padrao(self):
        self.assertEqual([(m.id, m.nuvem) for m in self._listar()],
                         [("qwen3:4b", False), ("cloudy:1b", False)])

    def test_sufixo_cloud_aparece_marcado_quando_permitido(self):
        self.assertEqual([(m.id, m.nuvem) for m in self._listar(permitir_nuvem=True)],
                         [("qwen3:4b", False), ("gpt-oss:120b-cloud", True), ("glm-4.6:cloud", True),
                          ("cloudy:1b", False)])

    def test_info_modelo_marca_nuvem_pelo_sufixo(self):
        backend = BackendOpenAI("http://127.0.0.1:1/v1")
        self.assertTrue(backend.info_modelo("gpt-oss:120b-cloud").nuvem)
        self.assertTrue(backend.info_modelo("glm-4.6:cloud").nuvem)
        self.assertFalse(backend.info_modelo("qwen3:4b").nuvem)


class TestFabrica(unittest.TestCase):
    def test_criar_backend_openai_passa_a_chave(self):
        from duo_provedor.backends import criar_backend
        backend = criar_backend({"backend": "openai", "url": "http://x/v1", "chave_backend": "k"}, False)
        self.assertIsInstance(backend, BackendOpenAI)
        self.assertEqual((backend.url, backend.chave), ("http://x/v1", "k"))
        self.assertFalse(backend.permitir_nuvem)

    def test_criar_backend_openai_passa_a_permissao_de_nuvem(self):
        from duo_provedor.backends import criar_backend
        backend = criar_backend({"backend": "openai", "url": "http://x/v1",
                                 "permitir_modelos_nuvem": True}, False)
        self.assertTrue(backend.permitir_nuvem)


class TestErros(unittest.TestCase):
    def test_conexao_recusada_explica(self):
        url = f"http://127.0.0.1:{porta_livre()}/v1"
        with self.assertRaises(ErroBackend) as ctx:
            list(BackendOpenAI(url, timeout=2).conversar(pedido(), threading.Event()))
        self.assertEqual(str(ctx.exception),
                         f"Não consegui falar com o servidor em {url}. Ele está rodando?")

    def test_listar_com_conexao_recusada_explica(self):
        url = f"http://127.0.0.1:{porta_livre()}/v1"
        with self.assertRaisesRegex(ErroBackend, "Não consegui falar com o servidor"):
            BackendOpenAI(url, timeout=2).listar_modelos()

    def test_401_explica(self):
        for status in (401, 403):
            rotas = {("POST", CAMINHO): (status, "application/json", [b'{"error": {"message": "x"}}'])}
            with ServidorFalso(rotas) as srv:
                with self.assertRaises(ErroBackend) as ctx:
                    list(BackendOpenAI(srv.url + "/v1").conversar(pedido(), threading.Event()))
            self.assertEqual(str(ctx.exception),
                             "O servidor recusou a chave do backend (chave_backend no config.json).")

    def test_evento_fora_do_formato_explica_em_vez_de_quebrar(self):
        for dados in (b"[1, 2]", b'"texto"', b'{"usage": 5}', b'{"choices": [{"delta": "x"}]}',
                      b'{"choices": ["x"], "usage": []}'):
            with self.subTest(dados=dados):
                rotas = {("POST", CAMINHO): (200, "text/event-stream", [b"data: " + dados + b"\n\n"])}
                with ServidorFalso(rotas) as srv:
                    with self.assertRaisesRegex(ErroBackend, "^O servidor enviou um evento inválido"):
                        list(BackendOpenAI(srv.url + "/v1").conversar(pedido(), threading.Event()))

    def test_outro_status_inclui_mensagem(self):
        rotas = {("POST", CAMINHO): (500, "application/json",
                                     [b'{"error": {"message": "sem memoria", "type": "x"}}'])}
        with ServidorFalso(rotas) as srv:
            with self.assertRaises(ErroBackend) as ctx:
                list(BackendOpenAI(srv.url + "/v1").conversar(pedido(), threading.Event()))
        self.assertEqual(str(ctx.exception), "O servidor respondeu 500: sem memoria")


if __name__ == "__main__":
    unittest.main()
