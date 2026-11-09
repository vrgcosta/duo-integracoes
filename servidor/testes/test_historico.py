import base64
import unittest

from duo_provedor import emulacao, historico


def no(uuid, pai, sender, content=None, text="", **extra):
    return {"uuid": uuid, "parent_message_uuid": pai, "sender": sender, "created_at": "2026-01-01",
            "text": text, "content": content or [], "attachments": [], "files": [], **extra}


class TesteRamo(unittest.TestCase):
    def test_ramo_segue_parents_da_folha_ate_a_raiz(self):
        conversa = {"chat_messages": [
            no("a", None, "human", text="oi"),
            no("b", "a", "assistant"),
            no("c", "b", "human", text="original"),
            no("d", "b", "human", text="editado"),
            no("e", "d", "assistant"),
        ]}
        self.assertEqual([n["uuid"] for n in historico.ramo(conversa, "e")], ["a", "b", "d", "e"])

    def test_ramo_folha_desconhecida_vazio(self):
        self.assertEqual(historico.ramo({"chat_messages": [no("a", None, "human")]}, "zz"), [])

    def test_ramo_protegido_contra_ciclo(self):
        conversa = {"chat_messages": [no("a", "b", "human"), no("b", "a", "assistant")]}
        self.assertEqual(len(historico.ramo(conversa, "b")), 2)


class TesteTurnos(unittest.TestCase):
    NO_FERRAMENTA = no("2", "1", "assistant", content=[
        {"type": "text", "text": "vou ver"},
        {"type": "tool_use", "id": "t1", "name": "listar", "input": {}},
        {"type": "tool_result", "tool_use_id": "t1", "content": "a,b", "is_error": False},
        {"type": "text", "text": "achei"},
    ])

    def test_nativo_intercala_chamadas_e_resultados(self):
        nos = [no("1", None, "human", text="liste"), self.NO_FERRAMENTA]
        turnos = historico.para_turnos(nos, "nativo")
        self.assertEqual(turnos, [[
            {"papel": "usuario", "texto": "liste"},
            {"papel": "assistente", "texto": "vou ver",
             "chamadas": [{"id": "t1", "nome": "listar", "argumentos": {}}]},
            {"papel": "ferramenta", "id_chamada": "t1", "nome": "listar", "texto": "a,b"},
            {"papel": "assistente", "texto": "achei"},
        ]])

    def test_cercas_agrupa_resultados_em_uma_mensagem_usuario(self):
        no_ass = no("2", "1", "assistant", content=[
            {"type": "tool_use", "id": "t1", "name": "a", "input": {"x": 1}},
            {"type": "tool_use", "id": "t2", "name": "b", "input": {"y": 2}},
            {"type": "tool_result", "tool_use_id": "t1", "content": "r1", "is_error": False},
            {"type": "tool_result", "tool_use_id": "t2", "content": [{"type": "text", "text": "r2"}],
             "is_error": True},
            {"type": "text", "text": "fim"},
        ])
        (turno,) = historico.para_turnos([no("1", None, "human", text="oi"), no_ass], "cercas")
        self.assertEqual([m["papel"] for m in turno], ["usuario", "assistente", "usuario", "assistente"])
        self.assertIn(emulacao.chamada_como_texto("a", {"x": 1}), turno[1]["texto"])
        self.assertIn(emulacao.chamada_como_texto("b", {"y": 2}), turno[1]["texto"])
        self.assertTrue(turno[2]["texto"].startswith(emulacao.MARCA_RESULTADOS))
        self.assertIn("r1", turno[2]["texto"])
        self.assertIn("r2", turno[2]["texto"])
        self.assertEqual(turno[3]["texto"], "fim")

    def test_cercas_marca_a_mensagem_de_resultados_e_guarda_cada_um(self):
        # A janela limpa e trunca um resultado por vez e refaz a mensagem: precisa deles separados,
        # e distingui-los do pedido humano, que também é `usuario` nas cercas.
        no_ass = no("2", "1", "assistant", content=[
            {"type": "tool_use", "id": "t1", "name": "a", "input": {}},
            {"type": "tool_use", "id": "t2", "name": "b", "input": {}},
            {"type": "tool_result", "tool_use_id": "t1", "content": "r1", "is_error": False},
            {"type": "tool_result", "tool_use_id": "t2", "content": "r2", "is_error": False},
        ])
        (turno,) = historico.para_turnos([no("1", None, "human", text="oi"), no_ass], "cercas")
        self.assertNotIn("resultado", turno[0])
        self.assertIs(turno[2]["resultado"], True)
        self.assertEqual(turno[2]["itens"], [{"id": "t1", "nome": "a", "conteudo": "r1", "erro": False},
                                             {"id": "t2", "nome": "b", "conteudo": "r2", "erro": False}])
        self.assertEqual(turno[2]["texto"], emulacao.mensagem_resultados(turno[2]["itens"]))

    def test_cercas_orfa_tambem_e_resultado(self):
        (turno,) = historico.para_turnos([no("1", None, "human", text="oi"), self._no_orfao()], "cercas")
        self.assertIs(turno[2]["resultado"], True)
        self.assertEqual([i["id"] for i in turno[2]["itens"]], ["t1", "t2"])

    def test_correcao_nao_e_marcada_como_resultado(self):
        no_ass = no("2", "1", "assistant", content=[{"type": "correcao", "texto": "corrija", "interno": True}])
        (turno,) = historico.para_turnos([no("1", None, "human", text="oi"), no_ass], "cercas")
        self.assertNotIn("resultado", turno[1])

    def test_thinking_nao_volta_ao_modelo(self):
        no_ass = no("2", "1", "assistant", content=[
            {"type": "thinking", "summaries": [{"summary": "segredo"}]},
            {"type": "text", "text": "resposta"},
        ])
        (turno,) = historico.para_turnos([no("1", None, "human", text="oi"), no_ass], "nativo")
        self.assertNotIn("segredo", str(turno))
        self.assertEqual(turno[1], {"papel": "assistente", "texto": "resposta"})

    def test_anexo_texto_vai_delimitado_e_texto_sem_anexos_guardado(self):
        h = no("1", None, "human", text="resuma")
        h["attachments"] = [{"file_name": "p.md", "extracted_content": "corpo"}]
        (turno,) = historico.para_turnos([h], "nativo")
        self.assertEqual(turno[0]["texto"], "resuma\n" + emulacao.anexo_inline("p.md", "corpo"))
        self.assertEqual(turno[0]["texto_sem_anexos"], "resuma")

    def _com_imagem(self):
        h = no("1", None, "human", text="veja")
        h["files"] = ["f1", {"file_uuid": "ignorado"}]
        dados = b"\x89PNG"
        return h, (lambda fid: ("foto.png", "image/png", dados) if fid == "f1" else None), dados

    def test_imagem_sem_visao_vira_nota(self):
        h, obter, _ = self._com_imagem()
        (turno,) = historico.para_turnos([h], "nativo", obter_arquivo=obter, visao=False)
        self.assertIn("[anexo foto.png (image/png) não pode ser lido por este modelo]", turno[0]["texto"])
        self.assertNotIn("imagens", turno[0])

    def test_imagem_com_visao_vai_em_imagens(self):
        h, obter, dados = self._com_imagem()
        (turno,) = historico.para_turnos([h], "nativo", obter_arquivo=obter, visao=True)
        self.assertEqual(turno[0]["imagens"], [base64.b64encode(dados).decode()])
        self.assertNotIn("não pode ser lido", turno[0]["texto"])

    def test_resultado_longo_chega_inteiro(self):
        # Quem decide o que cabe é a janela, que enxerga o espaço de verdade.
        for modo in ("nativo", "cercas"):
            with self.subTest(modo):
                no_ass = no("2", "1", "assistant", content=[
                    {"type": "tool_use", "id": "t1", "name": "a", "input": {}},
                    {"type": "tool_result", "tool_use_id": "t1", "content": "x" * 100000, "is_error": False},
                ])
                (turno,) = historico.para_turnos([no("1", None, "human", text="oi"), no_ass], modo)
                self.assertIn("x" * 100000, turno[2]["texto"])

    def test_correcao_vira_usuario(self):
        no_ass = no("2", "1", "assistant", content=[
            {"type": "text", "text": "tentei"},
            {"type": "correcao", "texto": "corrija o JSON", "interno": True},
        ])
        (turno,) = historico.para_turnos([no("1", None, "human", text="oi"), no_ass], "cercas")
        self.assertEqual(turno[-1], {"papel": "usuario", "texto": "corrija o JSON"})

    ORFA = "[a ferramenta não foi executada: o turno foi interrompido]"

    def _no_orfao(self):
        return no("2", "1", "assistant", content=[
            {"type": "text", "text": "vou"},
            {"type": "tool_use", "id": "t1", "name": "a", "input": {}},
            {"type": "tool_use", "id": "t2", "name": "b", "input": {}},
            {"type": "tool_result", "tool_use_id": "t2", "content": "ok", "is_error": False},
        ])

    def test_nativo_tool_use_orfao_ganha_resultado_sintetico(self):
        (turno,) = historico.para_turnos([no("1", None, "human", text="oi"), self._no_orfao()], "nativo")
        self.assertEqual(turno[2:], [
            {"papel": "ferramenta", "id_chamada": "t1", "nome": "a", "texto": self.ORFA},
            {"papel": "ferramenta", "id_chamada": "t2", "nome": "b", "texto": "ok"},
        ])

    def test_nativo_orfao_no_fim_do_no(self):
        n = no("2", "1", "assistant", content=[{"type": "tool_use", "id": "t1", "name": "a", "input": {}}])
        (turno,) = historico.para_turnos([no("1", None, "human", text="oi"), n], "nativo")
        self.assertEqual(turno[-1], {"papel": "ferramenta", "id_chamada": "t1", "nome": "a", "texto": self.ORFA})

    def test_cercas_tool_use_orfao_entra_nos_resultados_com_erro(self):
        (turno,) = historico.para_turnos([no("1", None, "human", text="oi"), self._no_orfao()], "cercas")
        msg = turno[2]
        self.assertEqual(msg["papel"], "usuario")
        self.assertTrue(msg["texto"].startswith(emulacao.MARCA_RESULTADOS))
        self.assertIn(self.ORFA, msg["texto"])
        self.assertLess(msg["texto"].index(self.ORFA), msg["texto"].index('"ok"'))
        self.assertIn('"erro": true', msg["texto"])

    def test_dois_turnos(self):
        nos = [no("1", None, "human", text="a"),
               no("2", "1", "assistant", content=[{"type": "text", "text": "b"}]),
               no("3", "2", "human", text="c")]
        self.assertEqual(len(historico.para_turnos(nos, "nativo")), 2)


class TesteVisivel(unittest.TestCase):
    def test_conversa_visivel_remove_internos(self):
        original = {"uuid": "c", "ferramentas_carregadas": ["x"], "chat_messages": [
            no("1", None, "assistant", content=[
                {"type": "text", "text": "ok"}, {"type": "correcao", "texto": "z", "interno": True}])]}
        vis = historico.conversa_visivel(original)
        self.assertNotIn("ferramentas_carregadas", vis)
        self.assertEqual(vis["chat_messages"][0]["content"], [{"type": "text", "text": "ok"}])
        self.assertIn("ferramentas_carregadas", original)
        self.assertEqual(len(original["chat_messages"][0]["content"]), 2)

    def test_texto_resultado(self):
        self.assertEqual(historico.texto_resultado("a"), "a")
        self.assertEqual(historico.texto_resultado([{"type": "text", "text": "a"}, {"type": "x"},
                                                    {"type": "text", "text": "b"}]), "ab")

    def test_texto_resultado_com_text_que_nao_e_texto(self):
        self.assertEqual(historico.texto_resultado([{"type": "text", "text": 42}, {"type": "text", "text": None},
                                                    {"type": "text"}, {"type": "text", "text": "!"}]), "42!")


if __name__ == "__main__":
    unittest.main()
