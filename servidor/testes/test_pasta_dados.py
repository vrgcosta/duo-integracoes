"""Pasta de dados sincronizada ou de rede: detecção com ambiente e caminhos falsos (nada do disco real)."""

import unittest

from duo_provedor.pasta_dados import mensagem_pasta, pasta_que_sai_da_maquina

ONEDRIVE = {"OneDrive": r"C:\Users\ana\OneDrive"}


def local(**unidades):
    """`tipo_unidade` falso: a letra pedida devolve o tipo dado (3 = disco local, 4 = rede)."""
    return lambda raiz: unidades.get(raiz[0].upper(), 3)


def detectar(caminho: str, ambiente: dict | None = None, **unidades) -> str | None:
    return pasta_que_sai_da_maquina(caminho, ambiente or {}, windows=True, tipo_unidade=local(**unidades))


class TestPastaQueSaiDaMaquina(unittest.TestCase):
    def test_disco_local_comum_nao_avisa(self):
        self.assertIsNone(detectar(r"C:\duo-provider-server\dados", ONEDRIVE))

    def test_dentro_do_onedrive_pessoal_e_comercial(self):
        for nome in ("OneDrive", "OneDriveCommercial", "OneDriveConsumer"):
            with self.subTest(variavel=nome):
                ambiente = {nome: r"C:\Users\ana\OneDrive - Tribunal"}
                caminho = r"C:\Users\ana\OneDrive - Tribunal\Documentos\duo-provider-server\dados"
                self.assertEqual(detectar(caminho, ambiente), "OneDrive")

    def test_comparacao_ignora_caixa_barras_e_barra_final(self):
        # No Windows, "c:/users/ANA/onedrive/" e "C:\Users\ana\OneDrive" são a mesma pasta.
        ambiente = {"OneDrive": "c:/users/ANA/onedrive/"}
        self.assertEqual(detectar(r"C:\Users\ana\OneDrive\Desktop\duo\dados", ambiente), "OneDrive")
        self.assertEqual(detectar("c:/USERS/ana/OneDrive/duo/dados", ONEDRIVE), "OneDrive")

    def test_pasta_vizinha_com_o_mesmo_comeco_nao_e_do_onedrive(self):
        # "OneDrive-backup" começa com o mesmo texto, mas não está dentro do OneDrive.
        self.assertIsNone(detectar(r"C:\Users\ana\OneDrive-backup\duo\dados", ONEDRIVE))

    def test_variavel_vazia_nao_casa_com_tudo(self):
        self.assertIsNone(detectar(r"C:\duo\dados", {"OneDrive": ""}))

    def test_caminho_unc_e_rede(self):
        self.assertEqual(detectar(r"\\servidor\compartilhada\duo\dados"), "rede")
        self.assertEqual(detectar("//servidor/compartilhada/duo/dados"), "rede")
        self.assertEqual(detectar(r"\\?\UNC\servidor\compartilhada\duo\dados"), "rede")

    def test_caminho_longo_local_nao_e_rede(self):
        # "\\?\C:\..." é só a forma longa de um caminho local.
        self.assertIsNone(detectar(r"\\?\C:\duo-provider-server\dados"))
        self.assertEqual(detectar(r"\\?\C:\Users\ana\OneDrive\duo\dados", ONEDRIVE), "OneDrive")

    def test_unidade_mapeada_para_a_rede(self):
        self.assertEqual(detectar(r"Z:\duo\dados", Z=4), "rede")
        self.assertIsNone(detectar(r"D:\duo\dados", Z=4))

    def test_fora_do_windows_nao_detecta(self):
        # As variáveis do OneDrive e os caminhos UNC são do Windows.
        self.assertIsNone(pasta_que_sai_da_maquina(r"\\servidor\x", ONEDRIVE, windows=False))

    def test_tipo_de_unidade_que_falha_nao_impede_a_inicializacao(self):
        def quebra(raiz):
            raise OSError("sem acesso")

        self.assertIsNone(pasta_que_sai_da_maquina(r"D:\duo\dados", {}, windows=True, tipo_unidade=quebra))


class TestMensagem(unittest.TestCase):
    def test_onedrive_diz_nuvem_e_o_que_fazer(self):
        risco, acao = mensagem_pasta(r"C:\Users\ana\OneDrive\duo\dados", "OneDrive")
        self.assertIn("OneDrive", risco)
        self.assertIn("nuvem", risco)
        self.assertIn(r"C:\Users\ana\OneDrive\duo\dados", risco)
        self.assertTrue(risco[0].islower())  # vem depois de "ATENÇÃO:" no banner
        self.assertIn("Mova", acao)

    def test_rede_diz_servidor_de_arquivos(self):
        risco, acao = mensagem_pasta(r"\\srv\x\dados", "rede")
        self.assertIn("pasta de rede", risco)
        self.assertIn("servidor de arquivos", risco)
        self.assertIn("Mova", acao)


if __name__ == "__main__":
    unittest.main()
