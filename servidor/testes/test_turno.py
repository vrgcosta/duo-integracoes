import json
import os
import threading
import time
import unittest
from unittest import mock

from duo_provedor.backends.base import ChamadaFerramenta, ErroBackend, InfoModelo, Texto, Uso
from duo_provedor.catalogo import META
from duo_provedor.emulacao import LINGUAGEM_CHAMADA, anexo_inline
from duo_provedor.janela import estimar_tokens
from duo_provedor.turno import Opcoes, mesclar_opcoes
from testes.cliente_duo import ClienteDuo
from testes.util import SISTEMA_DE_TESTE, BackendGravador, ServidorDeTeste, ferramentas_do_duo

LISTAR = {"name": "listar_documentos", "description": "Lista os documentos do processo.",
          "input_schema": {"type": "object", "properties": {}}}

CFG = {"contexto_tokens": 16384, "temperatura": 0.3, "max_rodadas": 15,
       "espera_primeiro_token": 600, "limite_resultado": 12000}


class Interromper(Exception):
    """O "usuário" fecha a aba no meio da execução da ferramenta."""


class BackendFalho(BackendGravador):
    def __init__(self, erro: Exception) -> None:
        super().__init__()
        self.erro = erro

    def conversar(self, pedido, cancelado):
        self.pedidos.append(pedido)
        raise self.erro
        yield  # pragma: no cover - torna o método um gerador, como os backends reais


class BackendMudo(BackendGravador):
    """Nunca responde: segura a thread até o turno desistir (ou 5 s, para não vazar)."""

    def conversar(self, pedido, cancelado):
        self.pedidos.append(pedido)
        cancelado.wait(5)
        yield from ()


def _eventos_sse(bruto: bytes) -> list[dict]:
    texto = bruto.decode("utf-8")
    return [json.loads(q[len("data: "):]) for q in texto.split("\n\n") if q.startswith("data: ")]


class TestMesclarOpcoes(unittest.TestCase):
    def test_zeros_e_padroes_caem_no_cfg(self) -> None:
        esperado = Opcoes(16384, 0.3, "padrao", 15, 600, 12000)
        self.assertEqual(mesclar_opcoes(CFG, None), esperado)
        self.assertEqual(mesclar_opcoes(CFG, {"contexto": 0, "temperatura": -1, "ferramentas": "padrao",
                                              "max_rodadas": 0}), esperado)

    def test_valores_do_duo_vencem_e_aceitam_texto_numerico(self) -> None:
        opcoes = mesclar_opcoes(CFG, {"contexto": "8192", "temperatura": "0.1", "ferramentas": "cercas",
                                      "max_rodadas": "3"})
        self.assertEqual((opcoes.contexto_tokens, opcoes.temperatura, opcoes.ferramentas, opcoes.max_rodadas),
                         (8192, 0.1, "cercas", 3))
        self.assertEqual(mesclar_opcoes(CFG, {"temperatura": 0}).temperatura, 0)

    def test_invalidos_caem_no_cfg(self) -> None:
        for ruim in ("abc", True, None, [], {}, -5, float("nan"), "inf"):
            with self.subTest(valor=ruim):
                opcoes = mesclar_opcoes(CFG, {"contexto": ruim, "temperatura": ruim, "ferramentas": ruim,
                                              "max_rodadas": ruim})
                self.assertEqual(opcoes, Opcoes(16384, 0.3, "padrao", 15, 600, 12000))
        self.assertEqual(mesclar_opcoes(CFG, "não é objeto"), mesclar_opcoes(CFG, None))

    def test_sem_limite_de_resultado_por_padrao(self) -> None:
        # 0 = sem teto: quem corta é a janela, e só o que não cabe.
        cfg = {**CFG, "limite_resultado": 0}
        self.assertEqual(mesclar_opcoes(cfg, None).limite_resultado, 0)
        self.assertEqual(mesclar_opcoes(cfg, {"contexto": 32768}).limite_resultado, 0)

    def test_limite_de_resultado_explicito_vence(self) -> None:
        self.assertEqual(mesclar_opcoes(CFG, {"contexto": 32768}).limite_resultado, 12000)

    def test_limite_de_resultado_invalido_vira_sem_limite(self) -> None:
        # O config.json é editado à mão: um valor estranho não pode derrubar o turno.
        for ruim in (-1, "abc", None, True, [], float("nan")):
            with self.subTest(valor=ruim):
                cfg = {**CFG, "limite_resultado": ruim}
                self.assertEqual(mesclar_opcoes(cfg, None).limite_resultado, 0)


class TestTurno(unittest.TestCase):
    def setUp(self) -> None:
        patcher = mock.patch("duo_provedor.turno.INTERVALO_PING", 0.05)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.srv = None
        self.addCleanup(self._descer)
        self._subir()

    def _subir(self, backend=None, cfg_extra=None) -> None:
        """Troca o servidor do teste (outro backend ou outra config)."""
        self._descer()
        self.srv = ServidorDeTeste(backend, cfg_extra).__enter__()
        self.cliente = ClienteDuo(self.srv.url, self.srv.blueprint)

    def _descer(self) -> None:
        if self.srv is not None:
            self.srv.__exit__(None, None, None)
            self.srv = None

    def _conversa(self, cid: str) -> dict:
        return self.srv.servico.armazenamento.ler_conversa(cid)

    def _no_assistente(self, cid: str) -> dict:
        return next(n for n in self._conversa(cid)["chat_messages"] if n["sender"] == "assistant")

    def _enviar_quando_livre(self, cid: str, prazo: float = 3):
        """Reenvia na conversa até ela sair do 409 (ou o prazo vencer); devolve (resposta, segundos)."""
        inicio = time.monotonic()
        while True:
            r = self.cliente.enviar(cid, "olá", self.cliente.raiz)
            if not (r.erro or "").startswith("HTTP 409") or time.monotonic() - inicio > prazo:
                return r, time.monotonic() - inicio
            time.sleep(0.02)

    # -- texto e pensamento ------------------------------------------------

    def test_texto_simples_e_releitura(self) -> None:
        self.assertEqual(self.cliente.contexto(), {"contaId": "local"})
        cid = self.cliente.criar_conversa()
        r = self.cliente.enviar(cid, "olá", self.cliente.raiz)
        self.assertIsNone(r.erro)
        self.assertEqual(r.texto, "Você disse: olá")
        self.assertIsNotNone(r.uso)
        self.assertGreater(r.uso[0], 0)
        self.assertEqual(r.eventos[-1], {"type": "message_stop"})

        lidas = self.cliente.ler(cid)
        self.assertEqual([(m["role"], m["texto"]) for m in lidas],
                         [("user", "olá"), ("assistant", "Você disse: olá")])
        humano, assistente = self._conversa(cid)["chat_messages"]
        self.assertEqual(humano["parent_message_uuid"], self.cliente.raiz)
        self.assertEqual(assistente["parent_message_uuid"], humano["uuid"])
        self.assertEqual(self._conversa(cid)["model"], self.srv.servico.cfg["modelo_padrao"])

        self.assertEqual([c["id"] for c in self.cliente.listar()], [cid])
        self.assertEqual([c["id"] for c in self.cliente.buscar("VOCE DISSE")], [cid])

    def test_pensamento_aparece_quando_ligado(self) -> None:
        cid = self.cliente.criar_conversa()
        self.cliente.ligar_pensamento(cid, True)
        r = self.cliente.enviar(cid, "olá", self.cliente.raiz)
        self.assertEqual(r.pensamento, "Pensando no pedido…")
        self.assertEqual(r.texto, "Você disse: olá")
        self.assertEqual(self.cliente.ler(cid)[-1]["pensamento"], "Pensando no pedido…\n")

        desligada = self.cliente.criar_conversa()
        r = self.cliente.enviar(desligada, "olá", self.cliente.raiz)
        self.assertEqual(r.pensamento, "")
        self.assertNotIn("thinking", json.dumps(r.eventos))

    # -- ferramentas -------------------------------------------------------

    def test_ferramenta_nativa_pausa_e_continua_no_mesmo_stream(self) -> None:
        cid = self.cliente.criar_conversa()
        pedidas = []

        def executar(nome, entrada):
            pedidas.append((nome, entrada))
            return "doc1, doc2"

        r = self.cliente.enviar(cid, "use a ferramenta", self.cliente.raiz, [LISTAR], executar)
        self.assertIsNone(r.erro)
        self.assertEqual(pedidas, [("listar_documentos", {})])
        self.assertEqual(len(r.chamadas), 1)
        tid, nome, entrada = r.chamadas[0]
        self.assertRegex(tid, r"^toolu_local_[0-9a-f]{24}$")
        self.assertEqual((nome, entrada), ("listar_documentos", {}))
        self.assertIn("Recebi o resultado de listar_documentos: doc1, doc2", r.texto)
        # Uma resposta só: um message_start, um message_stop, e o texto final depois da chamada.
        tipos = [e["type"] for e in r.eventos]
        self.assertEqual((tipos.count("message_start"), tipos.count("message_stop")), (1, 1))

        assistente = self.cliente.ler(cid)[-1]
        self.assertEqual(assistente["ferramentas"], [(tid, "listar_documentos", {}, "doc1, doc2")])
        self.assertIn("Recebi o resultado", assistente["texto"])

    def test_ferramenta_por_cercas_chega_ao_duo_como_tool_use(self) -> None:
        cid = self.cliente.criar_conversa()
        r = self.cliente.enviar(cid, "use a ferramenta", self.cliente.raiz, [LISTAR],
                                lambda nome, entrada: "doc1", extras={"opcoes": {"ferramentas": "cercas"}})
        self.assertIsNone(r.erro)
        self.assertEqual([c[1] for c in r.chamadas], ["listar_documentos"])
        self.assertNotIn("```" + LINGUAGEM_CHAMADA, r.texto)
        self.assertIn("Recebi o resultado de listar_documentos", r.texto)

    def test_meta_ferramenta_fica_no_servidor(self) -> None:
        cid = self.cliente.criar_conversa()
        r = self.cliente.enviar(cid, "use a ferramenta", self.cliente.raiz, [LISTAR],
                                lambda nome, entrada: "doc1, doc2",
                                extras={"catalogo": {"favoritas": [], "resumos": {}}})
        self.assertIsNone(r.erro)
        self.assertEqual([c[1] for c in r.chamadas], ["listar_documentos"])
        self.assertNotIn(META, json.dumps(r.eventos))
        self.assertIn("Recebi o resultado de listar_documentos", r.texto)

        ferramentas = [f for m in self.cliente.ler(cid) for f in m["ferramentas"]]
        self.assertEqual([f[1] for f in ferramentas], ["listar_documentos"])
        self.assertEqual(self._conversa(cid)["ferramentas_carregadas"], ["listar_documentos"])
        # A chamada da meta fica gravada, marcada interna, para o modelo lembrar que já carregou.
        internos = [b for b in self._no_assistente(cid)["content"] if b.get("interno")]
        self.assertEqual([(b["type"], b.get("name")) for b in internos],
                         [("tool_use", META), ("tool_result", None)])

    def test_json_quebrado_tem_uma_correcao(self) -> None:
        cid = self.cliente.criar_conversa()
        r = self.cliente.enviar(cid, "json-quebrado", self.cliente.raiz, [LISTAR],
                                lambda nome, entrada: "doc1", extras={"opcoes": {"ferramentas": "cercas"}})
        self.assertIsNone(r.erro)
        self.assertEqual([c[1] for c in r.chamadas], ["listar_documentos"])
        # O simulado ecoa a mensagem de resultados (com a cerca duo-resultado); a de chamada, nunca.
        self.assertNotIn("```" + LINGUAGEM_CHAMADA, r.texto)
        self.assertIn("Recebi o resultado de listar_documentos", r.texto)
        correcoes = [b for b in self._no_assistente(cid)["content"] if b["type"] == "correcao"]
        self.assertEqual(len(correcoes), 1)
        self.assertTrue(correcoes[0]["interno"])
        self.assertIn("JSON inválido", correcoes[0]["texto"])

    def test_ferramenta_inexistente_nunca_chega_ao_duo(self) -> None:
        cid = self.cliente.criar_conversa()
        r = self.cliente.enviar(cid, "ferramenta-inexistente", self.cliente.raiz, [LISTAR])
        self.assertIsNone(r.erro)
        self.assertEqual(r.chamadas, [])
        self.assertNotIn("nao_existe", json.dumps(r.eventos))
        self.assertEqual(r.texto, "Sem essa ferramenta, respondo direto.")
        self.assertEqual(r.eventos[-1], {"type": "message_stop"})
        correcao = next(b for b in self._no_assistente(cid)["content"] if b["type"] == "correcao")
        self.assertIn("a ferramenta nao_existe não existe", correcao["texto"])

    def test_resposta_vazia_vira_nota(self) -> None:
        cid = self.cliente.criar_conversa()
        r = self.cliente.enviar(cid, "responda vazio", self.cliente.raiz)
        self.assertIsNone(r.erro)
        self.assertIn("não produziu resposta", r.texto)
        self.assertIn({"type": "message_stop"}, r.eventos)

    def test_teto_de_rodadas(self) -> None:
        cid = self.cliente.criar_conversa()
        r = self.cliente.enviar(cid, "sempre-ferramenta", self.cliente.raiz, [LISTAR],
                                lambda nome, entrada: "ok", extras={"opcoes": {"max_rodadas": 1}})
        self.assertIsNone(r.erro)
        self.assertEqual(len(r.chamadas), 1)
        self.assertIn("limite de 1 rodadas", r.texto)

    # -- concorrência, cancelamento e erros --------------------------------

    def test_conversa_ocupada_409_e_outra_conversa_livre(self) -> None:
        ocupada, livre = self.cliente.criar_conversa(), self.cliente.criar_conversa()
        vistos = {}

        def executar(nome, entrada):
            # Enquanto o turno da conversa ocupada espera este resultado:
            vistos["mesma"] = self.srv.pedir("POST", f"/api/conversas/{ocupada}/completion",
                                             {"prompt": "de novo"})
            vistos["outra"] = self.cliente.enviar(livre, "olá", self.cliente.raiz)
            vistos["sem_turno"] = self.srv.pedir("POST", f"/api/conversas/{livre}/tool_result",
                                                 {"tool_use_id": "toolu_local_x", "content": "x"})
            vistos["id_estranho"] = self.srv.pedir("POST", f"/api/conversas/{ocupada}/tool_result",
                                                   {"tool_use_id": "toolu_local_x", "content": "x"})
            return "doc1"

        r = self.cliente.enviar(ocupada, "use a ferramenta", self.cliente.raiz, [LISTAR], executar)
        self.assertIsNone(r.erro)
        self.assertIn("Recebi o resultado de listar_documentos: doc1", r.texto)
        status, corpo = vistos["mesma"]
        self.assertEqual(status, 409)
        self.assertEqual(corpo["error"]["message"], "Já há um envio em andamento nesta conversa.")
        self.assertEqual(vistos["outra"].texto, "Você disse: olá")
        self.assertEqual(vistos["sem_turno"][0], 409)
        self.assertEqual(vistos["id_estranho"][0], 409)
        self.assertEqual(self.srv.servico.turnos, {})

    def test_cliente_fecha_durante_espera_de_ferramenta_libera_conversa(self) -> None:
        cid = self.cliente.criar_conversa()

        def executar(nome, entrada):
            raise Interromper()

        with self.assertRaises(Interromper):
            self.cliente.enviar(cid, "use a ferramenta", self.cliente.raiz, [LISTAR], executar)
        r, levou = self._enviar_quando_livre(cid)
        self.assertLess(levou, 2)
        self.assertIsNone(r.erro)
        self.assertEqual(r.texto, "Você disse: olá")

        primeiro = self._no_assistente(cid)
        self.assertEqual([b["name"] for b in primeiro["content"] if b["type"] == "tool_use"],
                         ["listar_documentos"])
        self.assertFalse(any(b["type"] == "tool_result" for b in primeiro["content"]))

    def test_parar_libera_a_conversa_em_segundos_com_ping_de_verdade(self) -> None:
        # Sem encurtar o ping: a 1ª escrita depois do fechamento costuma passar, então esperar o
        # ping falhar prenderia a conversa por até 2 × 15 s. Quem solta a conversa é o socket.
        cid = self.cliente.criar_conversa()

        def executar(nome, entrada):
            raise Interromper()

        with mock.patch("duo_provedor.turno.INTERVALO_PING", 15):
            with self.assertRaises(Interromper):
                self.cliente.enviar(cid, "use a ferramenta", self.cliente.raiz, [LISTAR], executar)
            r, levou = self._enviar_quando_livre(cid)
        self.assertLess(levou, 2)
        self.assertIsNone(r.erro)
        self.assertEqual(r.texto, "Você disse: olá")

    def test_resultado_atrasado_de_turno_encerrado_recebe_409(self) -> None:
        cid = self.cliente.criar_conversa()
        capturado = {}

        def executar(nome, entrada):
            capturado["turno"] = self.srv.servico.turnos[cid]
            raise Interromper()

        with self.assertRaises(Interromper):
            self.cliente.enviar(cid, "use a ferramenta", self.cliente.raiz, [LISTAR], executar)
        self._enviar_quando_livre(cid)
        tid = next(b["id"] for b in self._no_assistente(cid)["content"] if b["type"] == "tool_use")
        # Simula a corrida do /tool_result que pegou o turno antes de ele sair de `turnos`.
        self.srv.servico.turnos[cid] = capturado["turno"]
        try:
            status, _ = self.srv.pedir("POST", f"/api/conversas/{cid}/tool_result",
                                       {"tool_use_id": tid, "content": "tarde demais"})
        finally:
            self.srv.servico.turnos.pop(cid, None)
        self.assertEqual(status, 409)

    def test_resultado_que_nao_chega_estoura_espera(self) -> None:
        cid = self.cliente.criar_conversa()
        with mock.patch("duo_provedor.turno.ESPERA_RESULTADO", 0.3), \
                self.assertLogs("duo_provedor.turno", "WARNING"):
            # Um "Duo" que lê a chamada e nunca devolve o resultado.
            status, bruto = self.srv.pedir("POST", f"/api/conversas/{cid}/completion",
                                           {"prompt": "use a ferramenta", "tools": [LISTAR]})
        self.assertEqual(status, 200)
        self.assertIn(b": ping\n\n", bruto)
        eventos = _eventos_sse(bruto)
        self.assertIn("tool_use", [e.get("content_block", {}).get("type") for e in eventos])
        self.assertEqual(eventos[-1], {"type": "error", "error": {
            "type": "duo-provedor", "message": "O DUO não devolveu os resultados das ferramentas a tempo."}})
        self.assertEqual(self.srv.servico.turnos, {})

    def test_erro_do_backend_vira_evento_de_erro(self) -> None:
        mensagem = "Não consegui falar com o Ollama em http://127.0.0.1:1. Ele está rodando?"
        self._subir(BackendFalho(ErroBackend(mensagem)))
        cid = self.cliente.criar_conversa()
        inicio = time.monotonic()
        with self.assertLogs("duo_provedor.turno", "WARNING"):
            r = self.cliente.enviar(cid, "olá", self.cliente.raiz)
        self.assertLess(time.monotonic() - inicio, 2)
        self.assertEqual(r.erro, mensagem)
        self.assertNotIn({"type": "message_stop"}, r.eventos)
        # O pedido humano fica gravado; a resposta vazia, não.
        self.assertEqual([n["sender"] for n in self._conversa(cid)["chat_messages"]], ["human"])

    def test_erro_inesperado_vira_erro_interno(self) -> None:
        self._subir(BackendFalho(RuntimeError("pane no motor")))
        cid = self.cliente.criar_conversa()
        with self.assertLogs("duo_provedor.turno", "WARNING") as logs:
            r = self.cliente.enviar(cid, "olá", self.cliente.raiz)
        self.assertEqual(r.erro, "erro interno do servidor: pane no motor")
        self.assertTrue(any("Traceback" in linha for linha in logs.output))

    # -- falha de disco ------------------------------------------------------

    def _falhar_ao_gravar_o_assistente(self) -> None:
        """O disco recusa gravar o nó do assistente (o humano grava normalmente)."""
        armazenamento = self.srv.servico.armazenamento
        original = armazenamento.gravar_mensagem

        def gravar(cid, no):
            if no["sender"] == "assistant":
                raise PermissionError(13, "Acesso negado")
            return original(cid, no)

        patcher = mock.patch.object(armazenamento, "gravar_mensagem", side_effect=gravar)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_falha_ao_gravar_a_resposta_vira_erro_e_nao_message_stop(self) -> None:
        self._falhar_ao_gravar_o_assistente()
        cid = self.cliente.criar_conversa()
        with self.assertLogs("duo_provedor.turno", "WARNING"):
            r = self.cliente.enviar(cid, "olá", self.cliente.raiz)
        self.assertEqual(r.texto, "Você disse: olá")  # o texto saiu ao vivo...
        self.assertTrue(r.erro.startswith("Não foi possível gravar a conversa no disco (Acesso negado)"))
        self.assertNotIn({"type": "message_stop"}, r.eventos)  # ...mas não como resposta concluída

    def test_falha_ao_gravar_durante_ferramentas_vira_erro(self) -> None:
        self._falhar_ao_gravar_o_assistente()
        cid = self.cliente.criar_conversa()
        with self.assertLogs("duo_provedor.turno", "WARNING"):
            r = self.cliente.enviar(cid, "use a ferramenta", self.cliente.raiz, [LISTAR])
        self.assertTrue(r.erro.startswith("Não foi possível gravar a conversa no disco"))
        self.assertEqual(r.chamadas, [])  # a chamada não chega ao Duo sem estar gravada
        self.assertEqual(self.srv.servico.turnos, {})

    # -- sigilo: modelos de nuvem -------------------------------------------

    NUVEM = InfoModelo("gpt-oss:120b-cloud", "gpt-oss:120b-cloud", True, False, False, True, None)

    def test_modelo_de_nuvem_bloqueado_sem_chamar_o_backend(self) -> None:
        backend = BackendGravador([[Texto("não devia sair")]], modelos=[self.NUVEM])
        for como in ("padrão do servidor", "escolhido no DUO"):
            with self.subTest(como=como):
                if como == "padrão do servidor":
                    self._subir(backend, {"modelo_padrao": self.NUVEM.id})
                    extras = None
                else:
                    self._subir(backend)
                    extras = {"model": self.NUVEM.id}
                cid = self.cliente.criar_conversa()
                with self.assertLogs("duo_provedor.turno", "WARNING"):
                    r = self.cliente.enviar(cid, "resuma os autos", self.cliente.raiz, extras=extras)
                self.assertIn(f"O modelo {self.NUVEM.id} é de nuvem", r.erro)
                self.assertIn("permitir_modelos_nuvem", r.erro)
                self.assertIn("reimporte no DUO o blueprint", r.erro)
                self.assertEqual(r.eventos[-1]["type"], "error")
                self.assertEqual(backend.pedidos, [])
                # Nada foi gravado: o pedido nem começou.
                self.assertEqual(self._conversa(cid)["chat_messages"], [])

    def test_modelo_de_nuvem_liberado_pela_config(self) -> None:
        backend = BackendGravador([[Texto("ok")]], modelos=[self.NUVEM])
        self._subir(backend, {"modelo_padrao": self.NUVEM.id, "permitir_modelos_nuvem": True})
        cid = self.cliente.criar_conversa()
        r = self.cliente.enviar(cid, "oi", self.cliente.raiz)
        self.assertIsNone(r.erro)
        self.assertEqual(r.texto, "ok")
        self.assertEqual(len(backend.pedidos), 1)

    def test_modelo_mudo_estoura_espera_do_primeiro_token(self) -> None:
        self._subir(BackendMudo(), {"espera_primeiro_token": 0.2})
        cid = self.cliente.criar_conversa()
        inicio = time.monotonic()
        with self.assertLogs("duo_provedor.turno", "WARNING"):
            r = self.cliente.enviar(cid, "olá", self.cliente.raiz)
        self.assertLess(time.monotonic() - inicio, 2)
        self.assertEqual(r.erro, "O modelo não respondeu em 0.2 s.")
        self.assertNotIn("ping", json.dumps(r.eventos))  # comentários SSE não viram eventos

    # -- montagem do pedido ao backend -------------------------------------

    def test_anexo_texto_e_imagem_sem_visao(self) -> None:
        backend = BackendGravador([[Texto("ok")]])
        self._subir(backend)
        fid = self.srv.servico.armazenamento.salvar_arquivo("foto.png", "image/png", b"\x89PNG falso")
        anexo = {"file_name": "peticao.md", "file_type": "text/markdown", "file_size": 22,
                 "extracted_content": "CONTEÚDO DA PETIÇÃO"}
        cid = self.cliente.criar_conversa()
        r = self.cliente.enviar(cid, "resuma", self.cliente.raiz,
                                extras={"attachments": [anexo], "files": [fid]})
        self.assertIsNone(r.erro)
        usuario = backend.pedidos[0].mensagens[1]
        self.assertEqual(usuario["papel"], "usuario")
        self.assertIn(anexo_inline("peticao.md", "CONTEÚDO DA PETIÇÃO"), usuario["texto"])
        self.assertIn("[anexo foto.png (image/png) não pode ser lido por este modelo]", usuario["texto"])
        self.assertNotIn("imagens", usuario)
        humano = self._conversa(cid)["chat_messages"][0]
        self.assertEqual((humano["attachments"], humano["files"]), ([anexo], [fid]))

    def test_sistema_do_duo_e_do_projeto_entram_no_sistema(self) -> None:
        backend = BackendGravador([[Texto("ok")]])
        self._subir(backend)
        pasta = os.path.join(self.srv.servico.armazenamento._projetos, "civel")
        os.makedirs(pasta)
        with open(os.path.join(pasta, "instrucoes.md"), "w", encoding="utf-8") as f:
            f.write("# Cível\nINSTRUÇÕES DO CÍVEL")
        cid = self.cliente.criar_conversa(projeto="civel")
        self.cliente.enviar(cid, "oi", self.cliente.raiz, extras={"system": "REGRAS DO DUO"})
        sistema = backend.pedidos[0].mensagens[0]
        self.assertEqual(sistema["papel"], "sistema")
        texto = sistema["texto"]
        self.assertTrue(texto.startswith(SISTEMA_DE_TESTE))
        self.assertGreater(texto.index("INSTRUÇÕES DO CÍVEL"), texto.index(SISTEMA_DE_TESTE))
        self.assertGreater(texto.index("REGRAS DO DUO"), texto.index(SISTEMA_DE_TESTE))

    def test_catalogo_nativo_completo_conta_na_janela(self) -> None:
        # Três pedidos com anexos de ~3 mil tokens cabem numa janela de 16k; com as 21 definições
        # nativas (milhares de tokens no prompt do motor), não cabem mais. Sem resultados a limpar, o
        # primeiro corte é o volume do 1º pedido, que fica só com o texto; o resto da história fica.
        backend = BackendGravador([[Texto("ok")]] * 4)
        self._subir(backend)
        cid, folha = self.cliente.criar_conversa(), self.cliente.raiz
        opcoes = {"opcoes": {"contexto": 16384}}
        for i in range(3):
            anexo = {"file_name": f"doc{i}.txt", "file_type": "text/plain", "file_size": 9000,
                     "extracted_content": "abc"[i] * 13500}
            r = self.cliente.enviar(cid, f"pedido {i}", folha, extras={**opcoes, "attachments": [anexo]})
            self.assertNotIn("Servidor:", r.texto)
            folha = self._conversa(cid)["chat_messages"][-1]["uuid"]
        # Nada cortado no 3º: sistema + 2 turnos completos + o pedido atual.
        self.assertEqual(len(backend.pedidos[2].mensagens), 1 + 2 + 2 + 1)

        r = self.cliente.enviar(cid, "e agora?", folha, ferramentas_do_duo(), extras=opcoes)
        self.assertIsNone(r.erro)
        self.assertIn("Os anexos e a resposta do primeiro pedido saíram da janela "
                      "(o texto do pedido foi mantido).", r.texto)
        self.assertNotIn("turno(s)", r.texto)
        ultimo = backend.pedidos[3]
        self.assertEqual(len(ultimo.ferramentas), 21)
        textos = [m["texto"] for m in ultimo.mensagens]
        self.assertFalse(any("a" * 13500 in t for t in textos))  # o volume do 1º pedido saiu
        self.assertIn("pedido 0", textos)  # o texto dele fica (preâmbulo fixo)
        self.assertTrue(any("b" * 13500 in t for t in textos))  # e o turno do meio, mais novo, também
        self.assertIn("a resposta e os anexos do primeiro pedido foram omitidos", textos[0])

    def _turno_com_leituras(self, cid: str, folha: str, roteiro_resultado: str, opcoes: dict, prompt: str) -> str:
        r = self.cliente.enviar(cid, prompt, folha, [LISTAR], lambda nome, entrada: roteiro_resultado,
                                extras={"opcoes": opcoes})
        self.assertIsNone(r.erro)
        return self._conversa(cid)["chat_messages"][-1]["uuid"]

    def test_avisos_da_janela_sao_os_da_ultima_rodada(self) -> None:
        # A cada rodada sai mais um resultado: o usuário lê o saldo final, não "1", depois "2".
        def chamada(n: int) -> ChamadaFerramenta:
            return ChamadaFerramenta("listar_documentos", {"n": n})

        backend = BackendGravador([[chamada(1), chamada(2), chamada(3)], [Texto("li")],
                                   [chamada(4)], [chamada(5)], [Texto("fim")]])
        self._subir(backend)
        cid = self.cliente.criar_conversa()
        opcoes = {"contexto": 8192}
        folha = self._turno_com_leituras(cid, self.cliente.raiz, "x" * 5600, opcoes, "leia tudo")
        r = self.cliente.enviar(cid, "leia mais", folha, [LISTAR], lambda nome, entrada: "y" * 8400,
                                extras={"opcoes": opcoes})
        self.assertIsNone(r.erro)
        self.assertEqual(len(backend.pedidos), 5)
        self.assertEqual(r.texto.count("resultado(s) antigo(s)"), 1)
        self.assertIn("2 resultado(s) antigo(s) de ferramenta (listar_documentos; 2636 tokens) ficaram fora da "
                      "janela de contexto (8192 tokens).", r.texto)
        sistema = backend.pedidos[4].mensagens[0]["texto"]
        self.assertTrue(sistema.startswith(SISTEMA_DE_TESTE))
        self.assertIn("[Nota do servidor: para caber na janela, 2 resultado(s) de ferramenta foram omitidos.",
                      sistema)

    def test_log_do_turno_so_tem_metadados(self) -> None:
        backend = BackendGravador([[ChamadaFerramenta("listar_documentos", {})], [Texto("feito")]])
        self._subir(backend)
        cid = self.cliente.criar_conversa()
        anexo = {"file_name": "peticao.md", "file_type": "text/markdown", "file_size": 26,
                 "extracted_content": "CONTEÚDO SIGILOSO DO ANEXO"}
        with self.assertLogs("duo_provedor.turno", "INFO") as logs:
            r = self.cliente.enviar(
                cid, "PEDIDO SIGILOSO", self.cliente.raiz, [LISTAR], lambda nome, entrada: "RESULTADO SIGILOSO",
                extras={"system": "SISTEMA SIGILOSO", "attachments": [anexo],
                        "catalogo": {"favoritas": ["listar_documentos"], "resumos": {}},
                        "opcoes": {"contexto": 8192, "temperatura": 0.1, "max_rodadas": 4}})
        self.assertIsNone(r.erro)
        envio = next(linha for linha in logs.output if ": envio |" in linha)
        self.assertIn(f"turno {cid[:8]}: envio | modelo={self.srv.servico.cfg['modelo_padrao']} "
                      "ferramentas=nativo | contexto=8192 temperatura=0.1 max_rodadas=4 "
                      f"| system={estimar_tokens('SISTEMA SIGILOSO')} tokens | catalogo=sim (1 favoritas) | tools=1", envio)
        fim = next(linha for linha in logs.output if ": fim (" in linha)
        self.assertIn(f"turno {cid[:8]}: fim (concluído) | 2 rodada(s) | tokens: não informados "
                      "| 0 aviso(s)", fim)
        # Sigilo: nenhum texto do pedido, do sistema, do anexo ou do resultado vai para o log.
        for linha in logs.output:
            self.assertNotIn("SIGILOSO", linha)

    def _dois_turnos_com_uso(self, *usos: Uso | None) -> tuple[BackendGravador, list[str]]:
        """Um turno lê um documento; o seguinte lê outro em rodadas que reportam estes `usos`.

        Janela de 8192 (6144 de orçamento): os dois documentos, ~2 mil tokens cada, cabem pela
        estimativa e só deixam de caber se o uso real disser que ela está baixa.
        """
        chamada = ChamadaFerramenta("listar_documentos", {})
        rodadas = [[chamada, *([uso] if uso else [])] for uso in usos]
        backend = BackendGravador([[chamada], [Texto("li")], *rodadas, [Texto("fim")]])
        self._subir(backend)
        cid = self.cliente.criar_conversa()
        opcoes = {"contexto": 8192}
        folha = self._turno_com_leituras(cid, self.cliente.raiz, "x" * 10000, opcoes, "leia")
        with self.assertLogs("duo_provedor.turno", "INFO") as logs:
            r = self.cliente.enviar(cid, "leia mais", folha, [LISTAR], lambda nome, entrada: "y" * 4500,
                                    extras={"opcoes": opcoes})
        self.assertIsNone(r.erro)
        return backend, logs.output

    @staticmethod
    def _tem_marca(pedido) -> bool:
        return any("resultado omitido da janela" in m["texto"] for m in pedido.mensagens)

    def test_uso_real_alto_pesa_na_rodada_seguinte(self) -> None:
        backend, logs = self._dois_turnos_com_uso(Uso(7000, 1))
        self.assertFalse(self._tem_marca(backend.pedidos[2]))  # a 1ª rodada ainda não sabe
        self.assertTrue(self._tem_marca(backend.pedidos[3]))
        fim = [linha for linha in logs if ": fim (" in linha][-1]
        self.assertIn("| fator de tokens: 2.00", fim)

    def test_sem_uso_informado_vale_a_estimativa(self) -> None:
        backend, logs = self._dois_turnos_com_uso(None)
        self.assertFalse(self._tem_marca(backend.pedidos[3]))
        self.assertIn("| fator de tokens: 1.00", [linha for linha in logs if ": fim (" in linha][-1])

    def test_uso_real_baixo_nao_reduz_a_conta(self) -> None:
        # O prompt_eval_count do Ollama pode excluir o prefixo em cache: nunca abaixo de 1.
        backend, logs = self._dois_turnos_com_uso(Uso(10, 1))
        self.assertFalse(self._tem_marca(backend.pedidos[3]))
        self.assertIn("| fator de tokens: 1.00", [linha for linha in logs if ": fim (" in linha][-1])

    def test_cache_da_rodada_seguinte_nao_derruba_o_fator_medido(self) -> None:
        # A 2ª rodada, com o prefixo em cache, reporta só o trecho novo: isso não mostra que a
        # estimativa melhorou, então a medição completa da 1ª continua valendo.
        backend, logs = self._dois_turnos_com_uso(Uso(7000, 1), Uso(50, 1))
        self.assertTrue(self._tem_marca(backend.pedidos[4]))
        self.assertIn("| fator de tokens: 2.00", [linha for linha in logs if ": fim (" in linha][-1])

    def test_rodada_com_imagem_nao_mede_o_fator(self) -> None:
        # A janela não pesa as imagens, e o motor as conta na entrada: a razão sairia inflada sem
        # dizer nada sobre a estimativa do texto.
        visao = InfoModelo("gravador", "Gravador", True, False, True, False, None)
        backend = BackendGravador([[ChamadaFerramenta("listar_documentos", {}), Uso(7000, 1)], [Texto("vi")]],
                                  modelos=[visao])
        self._subir(backend)
        fid = self.srv.servico.armazenamento.salvar_arquivo("foto.png", "image/png", b"\x89PNG falso")
        cid = self.cliente.criar_conversa()
        with self.assertLogs("duo_provedor.turno", "INFO") as logs:
            r = self.cliente.enviar(cid, "veja", self.cliente.raiz, [LISTAR], lambda nome, entrada: "doc",
                                    extras={"files": [fid], "model": "gravador", "opcoes": {"contexto": 8192}})
        self.assertIsNone(r.erro)
        self.assertIn("imagens", backend.pedidos[0].mensagens[1])  # a rodada levou mesmo a imagem
        self.assertIn("| fator de tokens: 1.00", [linha for linha in logs.output if ": fim (" in linha][-1])

    # -- fator gravado na conversa -------------------------------------------

    def _turno_medido(self, uso: Uso | None, modelo: str = "padrao"):
        """Um turno que deixa um resultado de ~2600 tokens no histórico, e o seguinte, numa janela de 8192."""
        backend = BackendGravador([[ChamadaFerramenta("listar_documentos", {})],
                                   [Texto("li"), *([uso] if uso else [])], [Texto("fim")]])
        self._subir(backend)
        cid = self.cliente.criar_conversa()
        opcoes = {"contexto": 8192}
        primeiro = self.cliente.enviar(cid, "leia", self.cliente.raiz, [LISTAR], lambda nome, entrada: "x" * 11000,
                                       extras={"opcoes": opcoes})
        self.assertIsNone(primeiro.erro)
        folha = self._conversa(cid)["chat_messages"][-1]["uuid"]
        segundo = self.cliente.enviar(cid, "e agora?" + "y" * 10000, folha, [LISTAR],
                                      extras={"opcoes": opcoes, "model": modelo})
        self.assertIsNone(segundo.erro)
        return backend, cid

    def test_fator_do_turno_anterior_vale_na_1a_rodada_do_seguinte(self) -> None:
        # Pela estimativa cabe tudo; contado a 2 por 1, não. Era aqui que o modelo, sem o fator, recebia
        # um pedido acima da janela na 1ª rodada, e o motor cortava o começo em silêncio.
        backend, cid = self._turno_medido(Uso(14000, 1))
        self.assertEqual(self.srv.servico.armazenamento.estado_janela(cid)["fator_tokens"], 2.0)
        self.assertEqual(self.srv.servico.armazenamento.estado_janela(cid)["ultimo_uso"], 14000)
        self.assertTrue(self._tem_marca(backend.pedidos[2]))

    def test_sem_uso_medido_a_1a_rodada_do_seguinte_vale_a_estimativa(self) -> None:
        backend, cid = self._turno_medido(None)
        estado = self.srv.servico.armazenamento.estado_janela(cid)
        self.assertEqual((estado["fator_tokens"], estado["ultimo_uso"]), (1.0, None))
        self.assertFalse(self._tem_marca(backend.pedidos[2]))

    def test_o_que_saiu_nao_volta_no_turno_seguinte(self) -> None:
        # O fator não cai de um turno para o outro: é o que impede o que saiu de voltar e sair de novo.
        backend = BackendGravador([[ChamadaFerramenta("listar_documentos", {})], [Texto("li"), Uso(14000, 1)],
                                   [Texto("um"), Uso(10, 1)], [Texto("dois")]])
        self._subir(backend)
        cid = self.cliente.criar_conversa()
        opcoes = {"contexto": 8192}
        self.cliente.enviar(cid, "leia", self.cliente.raiz, [LISTAR], lambda nome, entrada: "x" * 11000,
                            extras={"opcoes": opcoes})
        for pergunta in ("primeira" + "y" * 10000, "segunda"):
            folha = self._conversa(cid)["chat_messages"][-1]["uuid"]
            self.assertIsNone(self.cliente.enviar(cid, pergunta, folha, [LISTAR], extras={"opcoes": opcoes}).erro)
        self.assertTrue(self._tem_marca(backend.pedidos[2]))
        self.assertTrue(self._tem_marca(backend.pedidos[3]))
        self.assertEqual(self.srv.servico.armazenamento.estado_janela(cid)["fator_tokens"], 2.0)

    def test_fator_medido_com_outro_modelo_nao_vale(self) -> None:
        # Cada modelo conta do seu jeito; a medição gravada é do modelo que respondeu por último.
        outro = InfoModelo("outro", "Outro", True, False, False, False, None)
        padrao = InfoModelo("padrao-teste", "Padrão", True, False, False, False, None)
        backend = BackendGravador([[ChamadaFerramenta("listar_documentos", {})], [Texto("li"), Uso(14000, 1)],
                                   [Texto("fim")]], modelos=[padrao, outro])
        self._subir(backend, {"modelo_padrao": "padrao-teste"})
        cid = self.cliente.criar_conversa()
        opcoes = {"contexto": 8192}
        self.cliente.enviar(cid, "leia", self.cliente.raiz, [LISTAR], lambda nome, entrada: "x" * 11000,
                            extras={"opcoes": opcoes})
        folha = self._conversa(cid)["chat_messages"][-1]["uuid"]
        self.cliente.enviar(cid, "e agora?" + "y" * 10000, folha, [LISTAR], extras={"opcoes": opcoes, "model": "outro"})
        self.assertFalse(self._tem_marca(backend.pedidos[2]))
        estado = self.srv.servico.armazenamento.estado_janela(cid)
        self.assertEqual((estado["fator_tokens"], estado["ultimo_uso"]), (1.0, None))

    def test_documento_que_cabe_chega_inteiro(self) -> None:
        # O caso real: 12.525 e 13.356 caracteres eram cortados com a janela sobrando.
        for tamanho in (12525, 13356):
            with self.subTest(tamanho=tamanho):
                documento = "x" * tamanho
                backend = BackendGravador([[ChamadaFerramenta("listar_documentos", {})], [Texto("li")]])
                self._subir(backend)
                self.assertEqual(self.srv.servico.cfg["limite_resultado"], 0)
                cid = self.cliente.criar_conversa()
                r = self.cliente.enviar(cid, "leia", self.cliente.raiz, [LISTAR], lambda nome, entrada: documento,
                                        extras={"opcoes": {"contexto": 16384}})
                self.assertIsNone(r.erro)
                self.assertIn(documento, [m["texto"] for m in backend.pedidos[1].mensagens])
                self.assertNotIn("Servidor:", r.texto)

    def test_opcoes_do_duo_chegam_ao_pedido(self) -> None:
        backend = BackendGravador([[Texto("ok")]])
        self._subir(backend)
        cid = self.cliente.criar_conversa()
        self.cliente.enviar(cid, "oi", self.cliente.raiz,
                            extras={"opcoes": {"contexto": 8192, "temperatura": 0.1}})
        pedido = backend.pedidos[0]
        self.assertEqual((pedido.contexto_tokens, pedido.temperatura), (8192, 0.1))
        self.assertEqual(pedido.modelo, self.srv.servico.cfg["modelo_padrao"])


if __name__ == "__main__":
    unittest.main()
