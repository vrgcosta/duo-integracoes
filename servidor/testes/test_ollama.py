"""Backend Ollama contra um servidor falso (formato NDJSON da API nativa)."""

from __future__ import annotations

import json
import socket
import threading
import time
import unittest
from pathlib import Path

from duo_provedor.backends.base import (
    ChamadaFerramenta, Cortada, ErroAcimaDaJanela, ErroBackend, Pedido, Raciocinio, Texto, Uso,
)
from duo_provedor.backends.ollama import BackendOllama, com_etiqueta
from testes.falso import ServidorFalso
from testes.util import ServidorDeTeste

FIXTURES = Path(__file__).parent / "fixtures"


def corpo_acima(total: int, mensagem: str | None = None, contexto: int = 8192) -> bytes:
    """O 400 do `/api/chat` com `truncate: false`, como o Ollama 0.30.10 responde: um JSON dentro de outro."""
    texto = mensagem or f"request ({total} tokens) exceeds the available context size ({contexto} tokens), try increasing it"
    interno = {"error": {"code": 400, "message": texto, "type": "exceed_context_size_error",
                         "n_prompt_tokens": total, "n_ctx": contexto}}
    return json.dumps({"error": json.dumps(interno)}).encode("utf-8")


def fixture(nome: str) -> list[bytes]:
    return [linha.encode("utf-8") + b"\n"
            for linha in (FIXTURES / nome).read_text(encoding="utf-8").splitlines()]


def rota_chat(nome: str) -> dict:
    return {("POST", "/api/chat"): (200, "application/x-ndjson", fixture(nome))}


def pedido(**extra) -> Pedido:
    base = dict(modelo="qwen3:0.6b", mensagens=[{"papel": "usuario", "texto": "oi"}],
                ferramentas=[], pensar=False, contexto_tokens=8192, temperatura=None)
    base.update(extra)
    return Pedido(**base)


def mostrar(capacidades: list[str], contexto: int | None = 40960) -> tuple[int, str, list[bytes]]:
    corpo = {"capabilities": capacidades,
             "model_info": {"general.architecture": "qwen3"}}
    if contexto is not None:
        corpo["model_info"]["qwen3.context_length"] = contexto
    return 200, "application/json", [json.dumps(corpo).encode()]


class TestConversar(unittest.TestCase):
    def conversar(self, rotas, ped=None, **kw):
        with ServidorFalso(rotas) as srv:
            backend = BackendOllama(srv.url, **kw)
            eventos = list(backend.conversar(ped or pedido(), threading.Event()))
            return eventos, srv.pedidos

    def test_texto_e_uso(self):
        eventos, _ = self.conversar(rota_chat("ollama-texto.ndjson"))
        textos = [e.texto for e in eventos if isinstance(e, Texto)]
        self.assertEqual(textos, ["Olá", ", mundo!"])
        self.assertEqual(eventos[-1], Uso(120, 30))

    def test_pensamento(self):
        eventos, _ = self.conversar(rota_chat("ollama-pensamento.ndjson"))
        self.assertEqual([e.texto for e in eventos if isinstance(e, Raciocinio)],
                         ["Vou pensar", " um pouco."])
        self.assertEqual([e.texto for e in eventos if isinstance(e, Texto)], ["Pronto."])

    def test_tool_calls_viram_chamadas(self):
        eventos, _ = self.conversar(rota_chat("ollama-ferramenta.ndjson"))
        chamadas = [e for e in eventos if isinstance(e, ChamadaFerramenta)]
        self.assertEqual(chamadas, [ChamadaFerramenta("listar_documentos", {}),
                                    ChamadaFerramenta("ler_documento", {"id": "7"})])

    def test_argumentos_string_invalidos_viram_erro(self):
        linhas = [
            {"message": {"role": "assistant", "tool_calls": [
                {"function": {"name": "boa", "arguments": '{"a": 1}'}},
                {"function": {"name": "ruim", "arguments": "{quebrado"}}]}, "done": False},
            {"message": {"role": "assistant", "content": ""}, "done": True},
        ]
        rotas = {("POST", "/api/chat"): (200, "application/x-ndjson",
                 [json.dumps(x).encode() + b"\n" for x in linhas])}
        eventos, _ = self.conversar(rotas)
        boa, ruim = [e for e in eventos if isinstance(e, ChamadaFerramenta)]
        self.assertEqual(boa, ChamadaFerramenta("boa", {"a": 1}))
        self.assertIsNone(ruim.argumentos)
        self.assertIn("JSON", ruim.erro)
        self.assertFalse(any(isinstance(e, Uso) for e in eventos))

    def test_envio_real_nao_deixa_o_motor_cortar_calado(self):
        _, pedidos = self.conversar(rota_chat("ollama-texto.ndjson"))
        corpo = next(c for m, p, c, h in pedidos if p == "/api/chat")
        self.assertIs(corpo["truncate"], False)

    def test_estouro_no_envio_real_vem_como_erro_proprio_com_o_total(self):
        rotas = {("POST", "/api/chat"): (400, "application/json", [corpo_acima(6586)])}
        with self.assertRaises(ErroAcimaDaJanela) as erro:
            self.conversar(rotas)
        self.assertEqual(erro.exception.total, 6586)
        self.assertIsInstance(erro.exception, ErroBackend)

    def test_estouro_no_meio_do_stream_tambem(self):
        linha = json.dumps({"error": "request (7000 tokens) exceeds the available context size"}).encode() + b"\n"
        rotas = {("POST", "/api/chat"): (200, "application/x-ndjson", [linha])}
        with self.assertRaises(ErroAcimaDaJanela) as erro:
            self.conversar(rotas)
        self.assertEqual(erro.exception.total, 7000)

    def test_resposta_parada_no_limite_avisa(self):
        final = (b'{"message":{"role":"assistant","content":"meio"},"done":false}\n'
                 b'{"message":{"role":"assistant","content":""},"done":true,"done_reason":"length",'
                 b'"prompt_eval_count":10,"eval_count":5}\n')
        rotas = {("POST", "/api/chat"): (200, "application/x-ndjson", [final])}
        eventos, _ = self.conversar(rotas)
        self.assertEqual(eventos, [Texto("meio"), Cortada(), Uso(10, 5)])

    def test_resposta_que_acabou_nao_avisa(self):
        final = (b'{"message":{"role":"assistant","content":"fim"},"done":true,"done_reason":"stop",'
                 b'"prompt_eval_count":10,"eval_count":5}\n')
        rotas = {("POST", "/api/chat"): (200, "application/x-ndjson", [final])}
        eventos, _ = self.conversar(rotas)
        self.assertEqual(eventos, [Texto("fim"), Uso(10, 5)])

    def test_erro_no_meio_do_stream(self):
        rotas = {("POST", "/api/chat"): (200, "application/x-ndjson",
                 [b'{"error":"falhou o runner"}\n'])}
        with self.assertRaisesRegex(ErroBackend, "falhou o runner"):
            self.conversar(rotas)

    def test_corpo_do_pedido(self):
        defs = [{"name": "ler", "description": "Lê", "input_schema": {"type": "object"}}]
        mensagens = [
            {"papel": "sistema", "texto": "Seja breve."},
            {"papel": "usuario", "texto": "leia"},
            {"papel": "assistente", "texto": "", "chamadas": [
                {"id": "c1", "nome": "ler", "argumentos": {"id": 3}}]},
            {"papel": "ferramenta", "id_chamada": "c1", "nome": "ler", "texto": "conteúdo"},
        ]
        rotas = {**rota_chat("ollama-texto.ndjson"),
                 ("POST", "/api/show"): mostrar(["tools", "thinking"])}
        _, pedidos = self.conversar(rotas, pedido(ferramentas=defs, mensagens=mensagens,
                                                  pensar=True, temperatura=0.3))
        corpo = next(c for m, p, c, h in pedidos if p == "/api/chat")
        self.assertEqual(corpo["model"], "qwen3:0.6b")
        self.assertIs(corpo["stream"], True)
        self.assertEqual(corpo["options"], {"num_ctx": 8192, "temperature": 0.3})
        self.assertEqual(corpo["tools"][0]["function"]["name"], "ler")
        self.assertIs(corpo["think"], True)
        self.assertEqual(corpo["messages"], [
            {"role": "system", "content": "Seja breve."},
            {"role": "user", "content": "leia"},
            {"role": "assistant", "content": "",
             "tool_calls": [{"function": {"name": "ler", "arguments": {"id": 3}}}]},
            {"role": "tool", "content": "conteúdo", "tool_name": "ler"},
        ])

    def test_sem_ferramentas_sem_raciocinio_sem_temperatura(self):
        rotas = {**rota_chat("ollama-texto.ndjson"),
                 ("POST", "/api/show"): mostrar(["completion"])}
        _, pedidos = self.conversar(rotas, pedido(pensar=True))
        corpo = next(c for m, p, c, h in pedidos if p == "/api/chat")
        self.assertNotIn("tools", corpo)
        self.assertNotIn("think", corpo)
        self.assertEqual(corpo["options"], {"num_ctx": 8192})

    def test_imagens_no_usuario(self):
        msgs = [{"papel": "usuario", "texto": "veja", "imagens": ["QUJD"]}]
        _, pedidos = self.conversar(rota_chat("ollama-texto.ndjson"), pedido(mensagens=msgs))
        corpo = next(c for m, p, c, h in pedidos if p == "/api/chat")
        self.assertEqual(corpo["messages"],
                         [{"role": "user", "content": "veja", "images": ["QUJD"]}])

    def test_cancelar_antes_do_primeiro_byte_encerra_em_menos_de_1s(self):
        # Prefill em CPU pode levar minutos sem emitir nada: o cancelamento precisa
        # derrubar o socket em vez de esperar o primeiro byte.
        with ServidorFalso(rota_chat("ollama-texto.ndjson"), atraso_inicial=5.0,
                           atraso_caminho="/api/chat") as srv:
            backend = BackendOllama(srv.url, timeout=30.0)
            cancelado = threading.Event()
            threading.Timer(0.2, cancelado.set).start()
            inicio = time.monotonic()
            eventos = list(backend.conversar(pedido(), cancelado))
            self.assertLess(time.monotonic() - inicio, 1.0)
            self.assertEqual(eventos, [])

    def test_think_falso_explicito_quando_modelo_tem_raciocinio(self):
        rotas = {**rota_chat("ollama-texto.ndjson"),
                 ("POST", "/api/show"): mostrar(["tools", "thinking"])}
        _, pedidos = self.conversar(rotas, pedido(pensar=False))
        corpo = next(c for m, p, c, h in pedidos if p == "/api/chat")
        self.assertIs(corpo["think"], False)

    def test_sem_threads_extras_apos_conversar(self):
        antes = threading.active_count()
        self.conversar(rota_chat("ollama-texto.ndjson"))
        self.assertLessEqual(threading.active_count(), antes)


class TestContarTokens(unittest.TestCase):
    FINAL = (b'{"message":{"role":"assistant","content":"o"},"done":false}\n'
             b'{"message":{"role":"assistant","content":""},"done":true,"prompt_eval_count":4095,"eval_count":1}\n')

    def contar(self, rotas, p=None, **kwargs):
        with ServidorFalso(rotas) as falso:
            backend = BackendOllama(falso.url)
            return backend.contar_tokens(p or pedido(), **kwargs), falso.pedidos

    def _rotas(self, corpo: bytes = FINAL, capacidades=("tools", "thinking")) -> dict:
        return {("POST", "/api/chat"): (200, "application/x-ndjson", [corpo]),
                ("POST", "/api/show"): mostrar(list(capacidades))}

    def test_devolve_o_prompt_eval_count(self):
        total, _ = self.contar(self._rotas())
        self.assertEqual(total, 4095)

    def test_corpo_e_o_do_envio_real_com_um_so_token_de_saida(self):
        defs = [{"name": "ler", "description": "Lê", "input_schema": {"type": "object"}}]
        p = pedido(ferramentas=defs, pensar=True, temperatura=0.3,
                   mensagens=[{"papel": "sistema", "texto": "Seja breve."}, {"papel": "usuario", "texto": "leia"}])
        _, contagem = self.contar(self._rotas(), p)
        corpo_contagem = next(c for m, c_, c, h in contagem if c_ == "/api/chat")
        with ServidorFalso(self._rotas()) as falso:
            list(BackendOllama(falso.url).conversar(p, threading.Event()))
            corpo_real = next(c for m, c_, c, h in falso.pedidos if c_ == "/api/chat")
        self.assertEqual(corpo_contagem["options"].pop("num_predict"), 1)
        # Sem `truncate: false`, nos dois: o Ollama tiraria as mensagens antigas sem avisar e contaria só o que sobrou.
        self.assertIs(corpo_contagem["truncate"], False)
        self.assertEqual(corpo_contagem, corpo_real)
        self.assertEqual(corpo_real["options"]["num_ctx"], 8192)
        self.assertIs(corpo_real["think"], True)
        self.assertEqual(corpo_real["tools"][0]["function"]["name"], "ler")

    def test_sem_o_numero_no_fim_e_falha_e_nao_motor_que_nao_conta(self):
        # O turno avisa toda vez que a contagem falha; None é só para quem nunca teve como contar.
        with self.assertRaises(ErroBackend):
            self.contar(self._rotas(b'{"message":{"content":""},"done":true}\n'))

    def _contar_com_erro(self, corpo: bytes, status: int = 400):
        return self.contar({("POST", "/api/chat"): (status, "application/json", [corpo]),
                            ("POST", "/api/show"): mostrar([])})[0]

    def test_pedido_acima_da_janela_devolve_o_total_que_o_motor_informa(self):
        # Medido no Ollama 0.30.10, com três mensagens que somam 6586 tokens em num_ctx 4096: sem `truncate`, responde
        # 200 com prompt_eval_count 3304; com ele, 400 em 0,2 s, com o total no corpo, como `corpo_acima` monta.
        self.assertEqual(self._contar_com_erro(corpo_acima(6586)), 6586)

    def test_o_total_vem_do_campo_estruturado_mesmo_com_outro_texto(self):
        # O texto pode mudar de uma versão para outra; é o que deixaria a contagem falhar em silêncio.
        self.assertEqual(self._contar_com_erro(corpo_acima(6586, mensagem="prompt too long")), 6586)

    def test_sem_o_campo_o_texto_serve_de_reserva(self):
        corpo = json.dumps({"error": "request (6586 tokens) exceeds the available context size"}).encode()
        self.assertEqual(self._contar_com_erro(corpo), 6586)

    def test_o_tipo_certo_sem_numero_nenhum_prova_que_passou_sem_inventar_o_total(self):
        interno = {"error": {"code": 400, "message": "too long", "type": "exceed_context_size_error"}}
        corpo = json.dumps({"error": json.dumps(interno)}).encode()
        with self.assertRaises(ErroAcimaDaJanela) as erro:
            self._contar_com_erro(corpo)
        self.assertIsNone(erro.exception.total)

    def test_erro_estruturado_sem_estar_dentro_de_texto(self):
        corpo = json.dumps({"error": {"message": "x", "type": "exceed_context_size_error",
                                      "n_prompt_tokens": 9000}}).encode()
        self.assertEqual(self._contar_com_erro(corpo), 9000)

    def test_outro_400_nao_e_tomado_por_estouro(self):
        with self.assertRaisesRegex(ErroBackend, "opção inválida") as erro:
            self._contar_com_erro(json.dumps({"error": "opção inválida"}).encode())
        self.assertNotIsInstance(erro.exception, ErroAcimaDaJanela)

    def test_erro_do_motor_sobe_como_erro_de_backend(self):
        rotas = {("POST", "/api/chat"): (500, "application/json", [b'{"error":"sem memoria"}']),
                 ("POST", "/api/show"): mostrar([])}
        with self.assertRaisesRegex(ErroBackend, "sem memoria"):
            self.contar(rotas)

    def test_cancelar_encerra_sem_numero(self):
        cancelado = threading.Event()
        cancelado.set()
        total, _ = self.contar(self._rotas(), cancelado=cancelado)
        self.assertIsNone(total)


class TestVersao(unittest.TestCase):
    def test_versao_do_motor(self):
        rotas = {("GET", "/api/version"): (200, "application/json", [b'{"version":"0.30.10"}'])}
        with ServidorFalso(rotas) as falso:
            self.assertEqual(BackendOllama(falso.url).versao_motor(), "0.30.10")

    def test_sem_resposta_nao_ha_versao(self):
        with ServidorFalso({}) as falso:
            self.assertIsNone(BackendOllama(falso.url).versao_motor())


class TestModelos(unittest.TestCase):
    TAGS = {"models": [
        {"name": "qwen3:4b", "model": "qwen3:4b"},
        {"name": "gpt-oss:120b-cloud", "model": "gpt-oss:120b-cloud",
         "remote_model": "gpt-oss:120b", "remote_host": "https://ollama.com:443"},
    ]}

    def rotas(self):
        return {
            ("GET", "/api/tags"): (200, "application/json", [json.dumps(self.TAGS).encode()]),
            ("POST", "/api/show"): mostrar(["completion", "tools", "thinking", "vision"]),
        }

    def test_listar_omite_nuvem_e_rotula_capacidades(self):
        with ServidorFalso(self.rotas()) as srv:
            modelos = BackendOllama(srv.url).listar_modelos()
        self.assertEqual([m.id for m in modelos], ["qwen3:4b"])
        m = modelos[0]
        self.assertEqual((m.ferramentas, m.raciocinio, m.visao, m.nuvem, m.contexto_max),
                         (True, True, True, False, 40960))
        self.assertEqual(m.rotulo, "qwen3:4b · ferramentas · raciocínio · visão")

    def test_listar_mostra_nuvem_quando_permitido(self):
        with ServidorFalso(self.rotas()) as srv:
            modelos = BackendOllama(srv.url, permitir_nuvem=True).listar_modelos()
        self.assertEqual([(m.id, m.nuvem) for m in modelos],
                         [("qwen3:4b", False), ("gpt-oss:120b-cloud", True)])

    def test_rotulo_so_com_capacidades_presentes(self):
        rotas = {**self.rotas(), ("POST", "/api/show"): mostrar(["completion"], contexto=None)}
        with ServidorFalso(rotas) as srv:
            m = BackendOllama(srv.url).listar_modelos()[0]
        self.assertEqual(m.rotulo, "qwen3:4b")
        self.assertIsNone(m.contexto_max)

    def _com_embedding(self, capacidades_embedding=("embedding",)):
        tags = {"models": [{"name": "qwen3:4b"}, {"name": "qwen3-embedding:latest"}]}
        return {
            ("GET", "/api/tags"): (200, "application/json", [json.dumps(tags).encode()]),
            ("POST", "/api/show"): lambda corpo: mostrar(
                list(capacidades_embedding) if "embedding" in corpo["model"] else ["completion", "tools"]),
        }

    def test_listar_omite_modelo_que_nao_conversa(self):
        with ServidorFalso(self._com_embedding()) as srv:
            modelos = BackendOllama(srv.url).listar_modelos()
        self.assertEqual([m.id for m in modelos], ["qwen3:4b"])
        self.assertTrue(modelos[0].conversa)

    def test_info_modelo_ainda_responde_pelo_modelo_que_nao_conversa(self):
        # O --verificar e a avaliação perguntam direto, para dizer o motivo em vez de "não instalado".
        with ServidorFalso(self._com_embedding()) as srv:
            info = BackendOllama(srv.url).info_modelo("qwen3-embedding:latest")
        self.assertFalse(info.conversa)

    def test_capacidades_ausentes_nao_escondem_o_modelo(self):
        # Ollama antigo não manda `capabilities`: não dá para concluir que o modelo não conversa.
        with ServidorFalso(self._com_embedding(())) as srv:
            modelos = BackendOllama(srv.url).listar_modelos()
        self.assertEqual([m.id for m in modelos], ["qwen3:4b", "qwen3-embedding:latest"])

    def test_show_recusado_nao_esconde_o_modelo(self):
        rotas = {**self.rotas(), ("POST", "/api/show"): (404, "application/json",
                                                         [b'{"error":"model not found"}'])}
        with ServidorFalso(rotas) as srv:
            self.assertEqual([m.id for m in BackendOllama(srv.url).listar_modelos()], ["qwen3:4b"])

    def test_rota_de_modelos_nao_mostra_o_modelo_que_nao_conversa(self):
        with ServidorFalso(self._com_embedding()) as falso:
            with ServidorDeTeste(BackendOllama(falso.url)) as srv:
                _, corpo = srv.pedir("GET", "/api/modelos")
        self.assertEqual([m["id"] for m in corpo["modelos"]], ["padrao", "qwen3:4b"])

    def test_info_modelo_usa_cache(self):
        with ServidorFalso(self.rotas()) as srv:
            backend = BackendOllama(srv.url)
            backend.info_modelo("qwen3:4b")
            backend.info_modelo("qwen3:4b")
            self.assertEqual(sum(1 for m, p, c, h in srv.pedidos if p == "/api/show"), 1)

    def test_info_modelo_ausente_devolve_none(self):
        rotas = {("POST", "/api/show"): (404, "application/json",
                                         [b'{"error":"model \'x\' not found"}'])}
        with ServidorFalso(rotas) as srv:
            self.assertIsNone(BackendOllama(srv.url).info_modelo("x"))

    def test_contexto_em_uso(self):
        ps = {"models": [{"name": "outro:1b", "context_length": 2048},
                         {"name": "qwen3:4b", "model": "qwen3:4b", "context_length": 4096}]}
        rotas = {("GET", "/api/ps"): (200, "application/json", [json.dumps(ps).encode()])}
        with ServidorFalso(rotas) as srv:
            backend = BackendOllama(srv.url)
            self.assertEqual(backend.contexto_em_uso("qwen3:4b"), 4096)
            self.assertIsNone(backend.contexto_em_uso("nada:1b"))

    def test_memoria_em_uso_e_modelo_sem_etiqueta(self):
        # O /api/ps sempre traz a etiqueta; "gemma3" na configuração é o mesmo "gemma3:latest".
        ps = {"models": [
            {"name": "gemma3:latest", "model": "gemma3:latest", "size": 8_000_000_000,
             "size_vram": 6_800_000_000, "context_length": 4096},
            {"name": "torto:1b", "size": "muito", "size_vram": None},
            {"name": "zerado:1b", "size": 0, "size_vram": 0},
        ]}
        rotas = {("GET", "/api/ps"): (200, "application/json", [json.dumps(ps).encode()])}
        with ServidorFalso(rotas) as srv:
            backend = BackendOllama(srv.url)
            self.assertEqual(backend.memoria_em_uso("gemma3"), (8_000_000_000, 6_800_000_000))
            self.assertEqual(backend.memoria_em_uso("gemma3:latest"), (8_000_000_000, 6_800_000_000))
            self.assertEqual(backend.contexto_em_uso("gemma3"), 4096)
            self.assertIsNone(backend.memoria_em_uso("torto:1b"))  # campos fora do formato
            self.assertIsNone(backend.memoria_em_uso("zerado:1b"))
            self.assertIsNone(backend.memoria_em_uso("nada:1b"))  # não carregado
            self.assertIsNone(backend.memoria_em_uso("gemma3:12b"))  # outra etiqueta é outro modelo

    def test_com_etiqueta(self):
        self.assertEqual(com_etiqueta("gemma3"), "gemma3:latest")
        self.assertEqual(com_etiqueta("qwen3.5:9b"), "qwen3.5:9b")
        self.assertEqual(com_etiqueta("hf.co/fulano/modelo"), "hf.co/fulano/modelo:latest")
        # O ":" da porta de um registro não é etiqueta.
        self.assertEqual(com_etiqueta("localhost:5000/modelo"), "localhost:5000/modelo:latest")


class TestErros(unittest.TestCase):
    def test_conexao_recusada_explica(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            porta = s.getsockname()[1]
        url = f"http://127.0.0.1:{porta}"
        with self.assertRaises(ErroBackend) as ctx:
            list(BackendOllama(url, timeout=2).conversar(pedido(), threading.Event()))
        self.assertEqual(str(ctx.exception),
                         f"Não consegui falar com o Ollama em {url}. Ele está rodando? "
                         "Abra o app do Ollama ou rode `ollama serve`.")

    def test_modelo_nao_instalado_explica(self):
        rotas = {("POST", "/api/chat"): (404, "application/json",
                                         [b'{"error": "model \'x\' not found"}'])}
        with ServidorFalso(rotas) as srv:
            with self.assertRaises(ErroBackend) as ctx:
                list(BackendOllama(srv.url).conversar(pedido(modelo="x"), threading.Event()))
        self.assertEqual(str(ctx.exception), "O modelo x não está instalado. Rode: ollama pull x")

    def test_outro_status_inclui_mensagem(self):
        rotas = {("POST", "/api/chat"): (500, "application/json", [b'{"error": "sem memoria"}'])}
        with ServidorFalso(rotas) as srv:
            with self.assertRaises(ErroBackend) as ctx:
                list(BackendOllama(srv.url).conversar(pedido(), threading.Event()))
        self.assertEqual(str(ctx.exception), "O Ollama respondeu 500: sem memoria")


if __name__ == "__main__":
    unittest.main()
