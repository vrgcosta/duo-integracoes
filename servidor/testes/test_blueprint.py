import json
import os
import tempfile
import unittest

from duo_provedor import blueprint
from duo_provedor.backends.base import InfoModelo
from duo_provedor.config import CABECALHO_CHAVE, PORTA_PADRAO


def _cfg(**extra) -> dict:
    cfg = {"chave": "segredo-de-teste", "porta": PORTA_PADRAO, "url": "http://127.0.0.1:11434",
           "permitir_modelos_nuvem": False}
    cfg.update(extra)
    return cfg


def _por_caminho(obj: object, caminho: str) -> object:
    for parte in caminho.split("."):
        obj = obj[parte]  # type: ignore[index]
    return obj


class TestBlueprint(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.destino = os.path.join(self._tmp.name, blueprint.NOME_ARQUIVO)
        self.bp = blueprint.gerar_blueprint(_cfg(), self.destino)
        with open(self.destino, encoding="utf-8") as f:
            self.texto = f.read()

    def test_eh_loopback(self) -> None:
        for url in ("http://localhost:1", "http://127.0.0.1:8766", "http://127.5.5.5", "http://[::1]:11434"):
            self.assertTrue(blueprint.eh_loopback(url), url)
        for url in ("http://192.168.0.10", "https://exemplo.com", "http://localhost.exemplo.com", "", "lixo"):
            self.assertFalse(blueprint.eh_loopback(url), url)

    def test_destino_local(self) -> None:
        self.assertTrue(blueprint.destino_local(_cfg()))
        self.assertFalse(blueprint.destino_local(_cfg(permitir_modelos_nuvem=True)))
        self.assertFalse(blueprint.destino_local(_cfg(url="https://api.exemplo.com")))
        self.assertTrue(blueprint.destino_local(_cfg(url="https://api.exemplo.com"), simulado=True))

    def test_nuvem_bloqueada(self) -> None:
        nuvem = InfoModelo("m-cloud", "m-cloud", True, False, False, True, None)
        local = InfoModelo("m", "m", True, False, False, False, None)
        self.assertTrue(blueprint.nuvem_bloqueada(_cfg(), nuvem))
        self.assertFalse(blueprint.nuvem_bloqueada(_cfg(permitir_modelos_nuvem=True), nuvem))
        self.assertFalse(blueprint.nuvem_bloqueada(_cfg(), local))
        self.assertFalse(blueprint.nuvem_bloqueada(_cfg(), None))

    def test_ler_blueprint(self) -> None:
        self.assertEqual(blueprint.ler_blueprint(self.destino), self.bp)
        self.assertIsNone(blueprint.ler_blueprint(os.path.join(self._tmp.name, "nao-existe.json")))
        quebrado = os.path.join(self._tmp.name, "quebrado.json")
        with open(quebrado, "w", encoding="utf-8") as f:
            f.write("{meio")
        self.assertIsNone(blueprint.ler_blueprint(quebrado))

    def test_preencher(self) -> None:
        resultado = blueprint.preencher(
            {"a": "{PORTA}", "b": "http://x:{PORTA}/{convId}", "c": ["{FLAG}", "{desconhecido}"]},
            {"PORTA": 8766, "FLAG": True},
        )
        self.assertEqual(resultado, {"a": 8766, "b": "http://x:8766/{convId}", "c": [True, "{desconhecido}"]})

    def test_campos_obrigatorios_do_schema(self) -> None:
        bp = self.bp
        self.assertEqual(bp["id"], "duo-provedor-local")
        self.assertEqual(bp["rotulo"], "Plugin (LLM local)")
        self.assertEqual(bp["baseURL"], "http://127.0.0.1:8766")
        self.assertIsInstance(bp["contexto"], list)
        self.assertTrue(bp["conversas"]["ler"]["caminho"])
        self.assertTrue(bp["enviar"]["caminho"])
        self.assertEqual(bp["stream"]["framing"], "sse")

    def test_placeholders_do_servidor_resolvidos(self) -> None:
        for marcador in ("{PORTA}", "{CHAVE}", "{DESTINO_LOCAL}"):
            self.assertNotIn(marcador, self.texto)
        self.assertIsInstance(self.bp["destinoLocal"], bool)
        self.assertEqual(self.bp["headers"][CABECALHO_CHAVE], "segredo-de-teste")

    def test_variaveis_do_duo_preservadas(self) -> None:
        for variavel in ("{convId}", "{prompt}", "{sistema}", "{leaf}", "{toolCallId}"):
            self.assertIn(variavel, self.texto)

    def test_sem_emulacao_e_com_catalogo(self) -> None:
        self.assertNotIn("emulacao", self.bp["ferramentas"])
        self.assertEqual(self.bp["ferramentas"]["campoCatalogo"], "catalogo")

    def test_configuraveis_apontam_para_caminhos_existentes(self) -> None:
        self.assertEqual(len(self.bp["configuraveis"]), 4)
        for item in self.bp["configuraveis"]:
            _por_caminho(self.bp, item["caminho"])

    def test_destino_local_reflete_a_config(self) -> None:
        nuvem = blueprint.gerar_blueprint(_cfg(permitir_modelos_nuvem=True), self.destino)
        self.assertIs(nuvem["destinoLocal"], False)
        simulado = blueprint.gerar_blueprint(_cfg(url="https://x.com"), self.destino, simulado=True)
        self.assertIs(simulado["destinoLocal"], True)

    def test_arquivo_em_utf8_sem_escape(self) -> None:
        self.assertIn("Modelo padrão do servidor", self.texto)
        self.assertEqual(json.loads(self.texto), self.bp)


if __name__ == "__main__":
    unittest.main()
