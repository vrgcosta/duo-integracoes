import unittest

from testes.util import ServidorDeTeste


class TestHttp(unittest.TestCase):
    def setUp(self) -> None:
        self.srv = ServidorDeTeste().__enter__()
        self.addCleanup(self.srv.__exit__, None, None, None)

    def test_sem_chave_403(self) -> None:
        status, corpo = self.srv.pedir("GET", "/api/contexto", chave=False)
        self.assertEqual(status, 403)
        self.assertEqual(corpo["error"]["type"], "duo-provedor")

    def test_chave_errada_403(self) -> None:
        status, _ = self.srv.pedir("GET", "/api/contexto", chave="chave-errada")
        self.assertEqual(status, 403)

    def test_origin_web_403_mesmo_com_chave(self) -> None:
        # Uma página qualquer na web não pode usar o servidor, nem que descubra a chave.
        for origem in ("https://pje.exemplo.jus.br", "http://localhost:3000"):
            status, _ = self.srv.pedir("GET", "/api/contexto", cabecalhos={"Origin": origem})
            self.assertEqual(status, 403, origem)

    def test_origin_de_extensao_passa(self) -> None:
        status, corpo = self.srv.pedir("GET", "/api/contexto",
                                       cabecalhos={"Origin": "chrome-extension://abcdefghijklmnop"})
        self.assertEqual(status, 200)
        self.assertEqual(corpo["conta"], "local")

    def test_options_403(self) -> None:
        # Sem CORS: o preflight do navegador precisa falhar.
        status, _ = self.srv.pedir("OPTIONS", "/api/contexto")
        self.assertEqual(status, 403)

    def test_rota_inexistente_404_json(self) -> None:
        status, corpo = self.srv.pedir("GET", "/api/nao-existe")
        self.assertEqual(status, 404)
        self.assertEqual(corpo["error"]["type"], "duo-provedor")

    def test_corpo_json_invalido_400_json(self) -> None:
        status, corpo = self.srv.pedir("POST", "/api/conversas", corpo=b"{isto nao e json",
                                       cabecalhos={"Content-Type": "application/json"})
        self.assertEqual(status, 400)
        self.assertIn("message", corpo["error"])

    def test_header_x_extension_info_tolerado(self) -> None:
        status, _ = self.srv.pedir("GET", "/api/contexto", cabecalhos={"x-extension-info": "{}"})
        self.assertEqual(status, 200)


if __name__ == "__main__":
    unittest.main()
