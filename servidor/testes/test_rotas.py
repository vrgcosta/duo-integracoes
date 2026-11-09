import os
import re
import unittest
import uuid
from urllib.parse import quote

from duo_provedor.armazenamento import RAIZ
from duo_provedor.backends.base import InfoModelo
from duo_provedor.servico import ROTAS
from testes.util import BackendGravador, ServidorDeTeste

UUID_INEXISTENTE = "11111111-2222-4333-8444-555555555555"


class TestRotas(unittest.TestCase):
    def setUp(self) -> None:
        self.srv = ServidorDeTeste().__enter__()
        self.addCleanup(self.srv.__exit__, None, None, None)

    def _criar(self, nome: str = "Teste", **extra) -> str:
        status, corpo = self.srv.pedir("POST", "/api/conversas", {"name": nome, "model": "padrao", **extra})
        self.assertEqual(status, 200)
        return corpo["uuid"]

    def test_ciclo_conversa(self) -> None:
        cid = self._criar("Ação de cobrança")

        status, conversa = self.srv.pedir("GET", f"/api/conversas/{cid}")
        self.assertEqual(status, 200)
        self.assertEqual(conversa["chat_messages"], [])

        _, lista = self.srv.pedir("GET", "/api/conversas")
        self.assertEqual([c["uuid"] for c in lista], [cid])

        status, corpo = self.srv.pedir("PUT", f"/api/conversas/{cid}", {"name": "Execução fiscal"})
        self.assertEqual((status, corpo), (200, {"ok": True}))
        _, conversa = self.srv.pedir("GET", f"/api/conversas/{cid}")
        self.assertEqual(conversa["name"], "Execução fiscal")

        # A busca ignora acento e caixa; o termo chega codificado na URL.
        _, achados = self.srv.pedir("GET", "/api/conversas/busca?termo=" + quote("EXECUCAO"))
        self.assertEqual([c["uuid"] for c in achados], [cid])
        _, achados = self.srv.pedir("GET", "/api/conversas/busca?termo=inexistente")
        self.assertEqual(achados, [])

        status, corpo = self.srv.pedir("DELETE", f"/api/conversas/{cid}")
        self.assertEqual((status, corpo), (200, {"ok": True}))
        self.assertEqual(self.srv.pedir("GET", f"/api/conversas/{cid}")[0], 404)
        self.assertEqual(self.srv.pedir("DELETE", f"/api/conversas/{cid}")[0], 404)
        self.assertEqual(self.srv.pedir("PUT", f"/api/conversas/{cid}", {"name": "x"})[0], 404)

    def test_pensamento_persiste(self) -> None:
        cid = self._criar()
        self.assertEqual(self.srv.pedir("PUT", f"/api/conversas/{cid}/pensamento", {"ativo": True})[1],
                         {"ok": True})
        self.assertIs(self.srv.servico.armazenamento.ler_conversa(cid)["pensamento"], True)
        self.srv.pedir("PUT", f"/api/conversas/{cid}/pensamento", {"ativo": False})
        self.assertIs(self.srv.servico.armazenamento.ler_conversa(cid)["pensamento"], False)
        self.assertEqual(self.srv.pedir("PUT", f"/api/conversas/{cid}/pensamento", {"ativo": "sim"})[0], 400)
        self.assertEqual(self.srv.pedir("PUT", f"/api/conversas/{UUID_INEXISTENTE}/pensamento",
                                        {"ativo": True})[0], 404)

    def test_projetos_e_conversas_do_projeto(self) -> None:
        pasta = os.path.join(self.srv.servico.armazenamento._projetos, "civel")
        os.makedirs(pasta)
        with open(os.path.join(pasta, "instrucoes.md"), "w", encoding="utf-8") as f:
            f.write("# Cível\nInstruções.")
        _, projetos = self.srv.pedir("GET", "/api/projetos")
        self.assertEqual(projetos, [{"uuid": "civel", "name": "Cível"}])

        dentro = self._criar("Dentro", project_uuid="civel")
        self._criar("Fora")
        _, lista = self.srv.pedir("GET", "/api/projetos/civel/conversas")
        self.assertEqual([c["uuid"] for c in lista], [dentro])

    def test_upload_multipart_nome_acentuado(self) -> None:
        limite = "----fronteira123"
        corpo = (f"--{limite}\r\n"
                 'Content-Disposition: form-data; name="file"; filename="petição.pdf"\r\n'
                 "Content-Type: application/pdf\r\n\r\n").encode("utf-8") + b"%PDF-1.4 conteudo" + \
                f"\r\n--{limite}--\r\n".encode("ascii")
        status, resp = self.srv.pedir("POST", "/api/upload", corpo,
                                      {"Content-Type": f"multipart/form-data; boundary={limite}"})
        self.assertEqual(status, 200)
        self.assertRegex(resp["file_uuid"], r"^[0-9a-f]{32}$")
        nome, mime, dados = self.srv.servico.armazenamento.obter_arquivo(resp["file_uuid"])
        self.assertEqual((nome, mime, dados), ("petição.pdf", "application/pdf", b"%PDF-1.4 conteudo"))

    def test_upload_sem_campo_400(self) -> None:
        limite = "xx"
        corpo = (f"--{limite}\r\nContent-Disposition: form-data; name=\"outro\"; filename=\"a.txt\"\r\n\r\n"
                 f"a\r\n--{limite}--\r\n").encode()
        status, _ = self.srv.pedir("POST", "/api/upload", corpo,
                                   {"Content-Type": f"multipart/form-data; boundary={limite}"})
        self.assertEqual(status, 400)

    def test_modelos_padrao_primeiro(self) -> None:
        _, corpo = self.srv.pedir("GET", "/api/modelos")
        modelos = corpo["modelos"]
        self.assertEqual(modelos[0]["id"], "padrao")
        self.assertEqual(modelos[0]["label"], f"Padrão do servidor ({self.srv.servico.cfg['modelo_padrao']})")
        self.assertIn("simulado", [m["id"] for m in modelos])

    def test_modelos_de_nuvem_so_quando_permitido(self) -> None:
        modelos = [InfoModelo("local", "Local", True, False, False, False, None),
                   InfoModelo("nuvem", "Nuvem", True, False, False, True, None)]
        for permitir, esperado in ((False, ["padrao", "local"]), (True, ["padrao", "local", "nuvem"])):
            with ServidorDeTeste(BackendGravador(modelos=modelos),
                                 {"permitir_modelos_nuvem": permitir}) as srv:
                _, corpo = srv.pedir("GET", "/api/modelos")
                self.assertEqual([m["id"] for m in corpo["modelos"]], esperado)

    def test_ler_esconde_blocos_internos(self) -> None:
        cid = self._criar()
        self.srv.servico.armazenamento.gravar_mensagem(cid, {
            "uuid": str(uuid.uuid4()), "parent_message_uuid": RAIZ, "sender": "assistant",
            "created_at": "2026-10-22T12:00:00Z", "text": "", "attachments": [], "files": [],
            "content": [
                {"type": "text", "text": "visível"},
                {"type": "tool_use", "id": "t1", "name": "carregar_ferramentas", "input": {}, "interno": True},
            ],
        })
        _, conversa = self.srv.pedir("GET", f"/api/conversas/{cid}")
        self.assertEqual([b["type"] for b in conversa["chat_messages"][0]["content"]], ["text"])
        self.assertNotIn("ferramentas_carregadas", conversa)

    def test_conversa_com_id_malformado_404(self) -> None:
        self.assertEqual(self.srv.pedir("GET", "/api/conversas/nao-e-uuid")[0], 404)

    def test_completion_e_tool_result_sem_turno(self) -> None:
        self.assertEqual(self.srv.pedir("POST", f"/api/conversas/{UUID_INEXISTENTE}/completion",
                                        {"prompt": "oi"})[0], 404)
        cid = self._criar()
        status, corpo = self.srv.pedir("POST", f"/api/conversas/{cid}/tool_result",
                                       {"tool_use_id": "toolu_local_x", "content": "x"})
        self.assertEqual(status, 409)
        self.assertEqual(corpo["error"]["type"], "duo-provedor")

    def test_completion_corpo_invalido_400(self) -> None:
        cid = self._criar()
        for corpo in ({"prompt": 5}, {"prompt": "oi", "tools": "listar"}, {"prompt": "oi", "tools": [{}]},
                      {"prompt": "oi", "attachments": ["x"]}, {"prompt": "oi", "catalogo": []},
                      {"prompt": "oi", "parent_message_uuid": 1}):
            with self.subTest(corpo=corpo):
                self.assertEqual(self.srv.pedir("POST", f"/api/conversas/{cid}/completion", corpo)[0], 400)
        # Nada ficou gravado nem preso: a conversa continua livre.
        self.assertEqual(self.srv.servico.armazenamento.ler_conversa(cid)["chat_messages"], [])
        self.assertEqual(self.srv.servico.turnos, {})

    def test_contrato_rotas_do_blueprint(self) -> None:
        """Toda chamada que o blueprint gerado faz precisa ter rota registrada, com o método certo."""
        bp = self.srv.blueprint
        uuid_exemplo = str(uuid.uuid4())

        chamadas = [(p.get("metodo", "GET"), p["caminho"]) for p in bp["contexto"]]
        chamadas.append(("GET", bp["modelosEndpoint"]["caminho"]))
        for chave in ("criar", "renomear", "ler", "listar", "projetos", "projetoConversas", "buscar", "excluir"):
            operacao = bp["conversas"][chave]
            chamadas.append((operacao.get("metodo", "POST" if chave == "criar" else "GET"), operacao["caminho"]))
        chamadas.append(("POST", bp["enviar"]["caminho"]))
        chamadas.append(("POST", bp["anexos"]["upload"]["caminho"]))
        chamadas.append(("POST", bp["ferramentas"]["enviarResultado"]["caminho"]))
        for chave in ("ligar", "desligar"):
            chamadas += [(p["metodo"], p["caminho"]) for p in bp["pensamento"][chave]]

        self.assertGreaterEqual(len(chamadas), 14)
        for metodo, caminho in chamadas:
            caminho = (caminho.replace("{convId}", uuid_exemplo).replace("{projetoId}", "civel")
                       .replace("{termo}", "x")).split("?")[0]
            self.assertTrue(any(m == metodo and re.fullmatch(padrao, caminho) for m, padrao, _ in ROTAS),
                            f"sem rota para {metodo} {caminho}")


if __name__ == "__main__":
    unittest.main()
