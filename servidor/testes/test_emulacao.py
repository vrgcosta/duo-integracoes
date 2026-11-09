import unittest

from duo_provedor import emulacao

RESPOSTA = (
    "Vou consultar as etiquetas.\n"
    "```duo-ferramenta\n"
    '{"nome":"listar_etiquetas","argumentos":{"filtro":"teste"}}\n'
    "```\n"
    "```duo-ferramenta\n"
    '{"nome":"consultar_acervo","argumentos":{}}\n'
    "```"
)


def processar(pedacos):
    p = emulacao.ParserCercas()
    eventos = []
    for s in pedacos:
        eventos += p.alimentar(s)
    eventos += p.finalizar()
    texto = "".join(e[1] for e in eventos if e[0] == "texto")
    chamadas = [e[1] for e in eventos if e[0] == "chamada"]
    invalidas = [e for e in eventos if e[0] == "invalida"]
    return texto, chamadas, invalidas


class TesteParser(unittest.TestCase):
    def test_dois_blocos_geram_duas_chamadas(self):
        texto, chamadas, invalidas = processar([RESPOSTA])
        self.assertEqual(texto.strip(), "Vou consultar as etiquetas.")
        self.assertEqual(chamadas, [
            {"nome": "listar_etiquetas", "argumentos": {"filtro": "teste"}},
            {"nome": "consultar_acervo", "argumentos": {}},
        ])
        self.assertEqual(invalidas, [])

    def test_cerca_partida_entre_deltas_em_qualquer_corte(self):
        esperado = processar([RESPOSTA])
        for corte in range(1, len(RESPOSTA)):
            self.assertEqual(processar([RESPOSTA[:corte], RESPOSTA[corte:]]), esperado, corte)
        self.assertEqual(processar(list(RESPOSTA)), esperado)

    def test_texto_antes_e_depois_preservado(self):
        entrada = 'antes\n```duo-ferramenta\n{"nome":"x","argumentos":{}}\n```\ndepois'
        texto, chamadas, _ = processar(list(entrada))
        self.assertEqual(texto, "antes\n\ndepois")
        self.assertEqual(len(chamadas), 1)

    def test_outras_cercas_passam_como_texto(self):
        entrada = "Exemplo:\n```python\nprint('```')\n```\nfim"
        texto, chamadas, _ = processar(list(entrada))
        self.assertEqual(chamadas, [])
        self.assertTrue(texto.startswith("Exemplo:\n```python\n"))
        self.assertTrue(texto.endswith("fim"))

    def test_texto_sem_cercas_intacto(self):
        entrada = "Texto com `codigo` e ``dupla`` e acento: ação.\nOutra linha"
        self.assertEqual(processar(list(entrada))[0], entrada)
        self.assertEqual(processar([entrada])[0], entrada)

    def test_crases_no_meio_da_linha_nao_abrem_bloco(self):
        entrada = "use ```duo-ferramenta``` assim"
        texto, chamadas, _ = processar([entrada])
        self.assertEqual(chamadas, [])
        self.assertEqual(texto, entrada)

    def test_json_invalido_vira_invalida(self):
        _, chamadas, invalidas = processar(["```duo-ferramenta\n{nome: x}\n```"])
        self.assertEqual(chamadas, [])
        self.assertEqual(len(invalidas), 1)
        self.assertIn("JSON inválido", invalidas[0][2])

    def test_bloco_sem_fechamento_vira_chamada_no_final(self):
        _, chamadas, _ = processar(['```duo-ferramenta\n{"nome":"x","argumentos":{"a":1}}'])
        self.assertEqual(chamadas, [{"nome": "x", "argumentos": {"a": 1}}])

    def test_aliases_de_campos(self):
        _, chamadas, _ = processar(['```duo-ferramenta\n{"name":"x","arguments":"{\\"a\\": 2}"}\n```'])
        self.assertEqual(chamadas, [{"nome": "x", "argumentos": {"a": 2}}])

    def test_json_na_linha_de_abertura(self):
        _, chamadas, _ = processar(['```duo-ferramenta {"nome":"x","argumentos":{}}\n```'])
        self.assertEqual(chamadas, [{"nome": "x", "argumentos": {}}])


class TesteMensagens(unittest.TestCase):
    FERRAMENTAS = [{"name": "listar_etiquetas", "description": "Lista as etiquetas\n da unidade",
                    "input_schema": {"type": "object", "properties": {"filtro": {"type": "string"}},
                                     "required": []}}]

    def test_catalogo_texto_lista_cada_ferramenta_e_a_linguagem(self):
        texto = emulacao.catalogo_texto(self.FERRAMENTAS + [{"name": "outra", "description": "Outra"}])
        self.assertIn("• listar_etiquetas — Lista as etiquetas da unidade", texto)
        self.assertIn("• outra — Outra", texto)
        self.assertIn(emulacao.LINGUAGEM_CHAMADA, texto)
        self.assertIn(emulacao.MARCA_RESULTADOS, texto)

    def test_chamada_como_texto_volta_pelo_parser(self):
        p =emulacao.ParserCercas()
        eventos = p.alimentar(emulacao.chamada_como_texto("x", {"a": 1})) + p.finalizar()
        self.assertEqual(eventos, [("chamada", {"nome": "x", "argumentos": {"a": 1}})])

    def test_mensagem_resultados_marca_e_traz_cada_item(self):
        msg = emulacao.mensagem_resultados([
            {"nome": "a", "conteudo": "um", "erro": False},
            {"nome": "b", "conteudo": "falhou", "erro": True},
        ])
        self.assertTrue(msg.startswith(emulacao.MARCA_RESULTADOS))
        self.assertIn('"nome": "a"', msg)
        self.assertIn('"erro": true', msg)
        self.assertIn("falhou", msg)

    def test_mensagem_correcao_lista_erros(self):
        msg = emulacao.mensagem_correcao([("{x}", "JSON inválido (x)")])
        self.assertIn(emulacao.MARCA_RESULTADOS, msg)
        self.assertIn("- JSON inválido (x)", msg)

    def test_anexo_inline_delimita_com_nome(self):
        msg = emulacao.anexo_inline("peticao.md", "conteúdo")
        self.assertIn("peticao.md", msg)
        self.assertIn("conteúdo", msg)


if __name__ == "__main__":
    unittest.main()
