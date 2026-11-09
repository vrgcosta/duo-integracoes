import unittest

from duo_provedor.catalogo import META, Catalogo


def _d(nome, desc="Faz algo. Detalhe longo."):
    return {"name": nome, "description": desc, "input_schema": {"type": "object", "properties": {}}}


DEFS = [_d("a"), _d("b"), _d("c", "Sem ponto final")]


def _nomes(c):
    return [d["name"] for d in c.visiveis()]


class TestCatalogo(unittest.TestCase):
    def test_sem_compacto_tudo_visivel(self):
        c = Catalogo(DEFS, None, [], [])
        self.assertEqual(_nomes(c), ["a", "b", "c"])

    def test_compacto_mostra_favoritas_e_meta_com_resumos(self):
        c = Catalogo(DEFS, {"favoritas": ["a"], "resumos": {"b": "resumo do b"}}, [], [])
        self.assertEqual(_nomes(c), ["a", META])
        meta = c.visiveis()[-1]
        self.assertEqual(meta["input_schema"], {
            "type": "object",
            "properties": {"nomes": {"type": "array", "items": {"type": "string"},
                                     "description": "Nomes das ferramentas a carregar"}},
            "required": ["nomes"]})
        self.assertEqual(meta["description"],
                         "Carrega ferramentas adicionais do DUO para esta conversa. "
                         "Chame antes de usar qualquer uma destas:\n"
                         "• b — resumo do b\n• c — Sem ponto final")

    def test_resumo_cai_na_primeira_frase(self):
        c = Catalogo(DEFS, {"favoritas": ["a"], "resumos": {}}, [], [])
        self.assertIn("• b — Faz algo\n", c.visiveis()[-1]["description"])
        self.assertNotIn("Detalhe", c.visiveis()[-1]["description"])

    def test_todas_favoritas_sem_meta(self):
        c = Catalogo(DEFS, {"favoritas": ["a", "b", "c"], "resumos": {}}, [], [])
        self.assertEqual(_nomes(c), ["a", "b", "c"])

    def test_meta_carrega_e_passa_a_mostrar(self):
        c = Catalogo(DEFS, {"favoritas": ["a"], "resumos": {}}, [], [])
        self.assertTrue(c.eh_meta(META))
        r = c.responder_meta({"nomes": ["b"]})
        self.assertEqual(r, "Ferramentas carregadas: b. Já pode chamá-las.")
        self.assertEqual(c.carregadas, ["b"])
        self.assertEqual(_nomes(c), ["a", "b", META])
        self.assertTrue(c.conhece("b"))

    def test_meta_nome_desconhecido_avisa(self):
        c = Catalogo(DEFS, {"favoritas": ["a"], "resumos": {}}, [], [])
        r = c.responder_meta({"nomes": ["b", "zzz"]})
        self.assertEqual(r, "Ferramentas carregadas: b. Já pode chamá-las. Desconhecidas: zzz.")

    def test_meta_tolera_argumentos_malformados(self):
        # Modelos pequenos erram o formato; nada disso pode virar exceção e derrubar o turno.
        for argumentos in ({"nomes": 5}, {"nomes": {"b": 1}}, {"nomes": [1, None, {"x": 1}, ["b"]]},
                           {"nomes": None}, {}, ["b"], None, "b"):
            with self.subTest(argumentos=argumentos):
                c = Catalogo(DEFS, {"favoritas": ["a"], "resumos": {}}, [], [])
                self.assertEqual(c.responder_meta(argumentos), "Nenhuma ferramenta carregada.")
                self.assertEqual(c.carregadas, [])
        c = Catalogo(DEFS, {"favoritas": ["a"], "resumos": {}}, [], [])
        self.assertEqual(c.responder_meta({"nomes": [3, "b"]}), "Ferramentas carregadas: b. Já pode chamá-las.")
        self.assertEqual(c.responder_meta({"nomes": "c"}), "Ferramentas carregadas: c. Já pode chamá-las.")
        self.assertEqual(c.carregadas, ["b", "c"])

    def test_ativas_restringe(self):
        c = Catalogo(DEFS, None, ["b", "c"], [])
        self.assertEqual(_nomes(c), ["b", "c"])
        c2 = Catalogo(DEFS, {"favoritas": ["a", "b"], "resumos": {}}, ["b", "c"], [])
        # "a" fica fora por não ser ativa; "c" é adiada, então aparece só via meta
        self.assertEqual(_nomes(c2), ["b", META])
        self.assertEqual(c2.responder_meta({"nomes": ["a"]}),
                         "Nenhuma ferramenta carregada. Desconhecidas: a.")

    def test_pedir_a_que_ja_esta_visivel_nao_a_chama_de_desconhecida(self):
        # Era o sintoma: "Desconhecidas: listar_documentos", com ela na lista, e o modelo desistia de um caminho certo.
        c = Catalogo(DEFS, {"favoritas": ["a"], "resumos": {}}, [], [])
        resposta = c.responder_meta({"nomes": ["a"]})
        self.assertIn("Já disponíveis: a", resposta)
        self.assertIn("chamá-las", resposta)
        self.assertNotIn("Desconhecidas", resposta)
        self.assertNotIn("Nenhuma", resposta)
        self.assertEqual(c.carregadas, [])

    def test_pedido_misto_responde_cada_um_pelo_que_e(self):
        c = Catalogo(DEFS, {"favoritas": ["a"], "resumos": {}}, [], [])
        resposta = c.responder_meta({"nomes": ["a", "b", "zz"]})
        self.assertIn("Ferramentas carregadas: b.", resposta)
        self.assertIn("Já disponíveis: a", resposta)
        self.assertIn("Desconhecidas: zz.", resposta)

    def test_a_que_o_servidor_responde_tambem_esta_disponivel(self):
        c = Catalogo(DEFS, {"favoritas": ["a"], "resumos": {}}, [], [])
        for nome in (META, "ler_trecho_resultado"):
            with self.subTest(nome=nome):
                self.assertIn(f"Já disponíveis: {nome}", c.responder_meta({"nomes": [nome]}))

    def test_sem_catalogo_compacto_toda_ferramenta_ja_esta_disponivel(self):
        self.assertIn("Já disponíveis: a", Catalogo(DEFS, None, [], []).responder_meta({"nomes": ["a"]}))

    def test_conhece_rejeita_meta_e_inexistente(self):
        c = Catalogo(DEFS, {"favoritas": ["a"], "resumos": {}}, [], [])
        self.assertTrue(c.conhece("a"))
        self.assertFalse(c.conhece(META))
        self.assertFalse(c.conhece("b"))  # adiada e ainda não carregada
        self.assertFalse(c.conhece("inexistente"))


if __name__ == "__main__":
    unittest.main()
