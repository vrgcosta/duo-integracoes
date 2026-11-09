import json
import unittest

from duo_provedor.backends.base import ChamadaFerramenta, InfoModelo
from duo_provedor.ferramentas import (
    AcumuladorChamadas,
    limpar_resultado,
    modo_ferramentas,
    para_funcoes,
)


def _info(ferramentas: bool) -> InfoModelo:
    return InfoModelo("m", "M", ferramentas, False, False, False, None)


class TestFerramentas(unittest.TestCase):
    def test_para_funcoes(self):
        defs = [{"name": "ler", "description": "Lê", "input_schema": {"type": "object", "properties": {}}}]
        self.assertEqual(
            para_funcoes(defs),
            [{"type": "function", "function": {
                "name": "ler", "description": "Lê",
                "parameters": {"type": "object", "properties": {}}}}],
        )

    def test_acumula_argumentos_fatiados_por_indice(self):
        ac = AcumuladorChamadas()
        ac.alimentar([
            {"index": 1, "id": "b", "function": {"name": "segunda", "arguments": '{"y"'}},
            {"index": 0, "id": "a", "function": {"name": "primeira", "arguments": '{"x":'}},
        ])
        ac.alimentar([{"index": 0, "function": {"arguments": " 1"}},
                      {"index": 1, "function": {"arguments": ": 2"}}])
        ac.alimentar([{"index": 0, "function": {"arguments": "}"}},
                      {"index": 1, "function": {"arguments": "}"}}])
        self.assertEqual(ac.finalizar(), [
            ChamadaFerramenta("primeira", {"x": 1}),
            ChamadaFerramenta("segunda", {"y": 2}),
        ])

    def test_argumentos_vazios_viram_dict_vazio(self):
        ac = AcumuladorChamadas()
        ac.alimentar([{"index": 0, "function": {"name": "f", "arguments": ""}}])
        self.assertEqual(ac.finalizar(), [ChamadaFerramenta("f", {})])

    def test_json_invalido_vira_erro(self):
        ac = AcumuladorChamadas()
        ac.alimentar([{"index": 0, "function": {"name": "f", "arguments": "{quebrado"}}])
        (c,) = ac.finalizar()
        self.assertEqual(c.nome, "f")
        self.assertIsNone(c.argumentos)
        self.assertTrue(c.erro.startswith("argumentos não são JSON válido: "))

    def test_modo_precedencia(self):
        cfg = {"ferramentas": "cercas", "ferramentas_por_modelo": {"m": "nativo"}}
        self.assertEqual(modo_ferramentas("padrao", cfg, "m", None), "nativo")
        self.assertEqual(modo_ferramentas("padrao", cfg, "outro", None), "cercas")
        self.assertEqual(modo_ferramentas("cercas", cfg, "m", None), "cercas")
        self.assertEqual(modo_ferramentas("nativo", cfg, "outro", None), "nativo")

    def test_auto_sem_tools_vira_cercas(self):
        cfg = {"ferramentas": "auto", "ferramentas_por_modelo": {}}
        self.assertEqual(modo_ferramentas("padrao", cfg, "m", _info(False)), "cercas")
        self.assertEqual(modo_ferramentas("padrao", cfg, "m", _info(True)), "nativo")
        self.assertEqual(modo_ferramentas("padrao", cfg, "m", None), "nativo")

    def test_limpar_resultado_troca_base64(self):
        grande = "A" * 1500
        texto = json.dumps({"arquivos": [{"name": "a.pdf", "mediaType": "application/pdf", "data": grande}],
                            "ok": True})
        saida = json.loads(limpar_resultado(texto))
        self.assertEqual(saida["arquivos"], [{"arquivo_omitido": "a.pdf", "tipo": "application/pdf"}])
        self.assertTrue(saida["ok"])
        pequeno = json.dumps({"mimeType": "text/plain", "data": "oi"})
        self.assertEqual(json.loads(limpar_resultado(pequeno)), {"mimeType": "text/plain", "data": "oi"})

    def test_limpar_resultado_texto_comum_fica_igual(self):
        self.assertEqual(limpar_resultado("olá, não é JSON"), "olá, não é JSON")


if __name__ == "__main__":
    unittest.main()
