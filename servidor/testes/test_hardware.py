import unittest
from unittest import mock

from duo_provedor.hardware import Hardware, PERFIS, detectar, escolher_perfil


class TestPerfil(unittest.TestCase):
    def test_sem_gpu_vai_para_cpu(self):
        self.assertEqual(escolher_perfil(Hardware(32, None, None), "Windows", "AMD64"), "cpu")

    def test_gpu_8gb_vai_para_gpu(self):
        self.assertEqual(escolher_perfil(Hardware(16, 8, "RTX"), "Windows", "AMD64"), "gpu")

    def test_gpu_6gb_fica_em_cpu(self):
        self.assertEqual(escolher_perfil(Hardware(16, 6, "GTX"), "Linux", "x86_64"), "cpu")

    def test_mac_apple_silicon_16gb_vai_para_gpu(self):
        self.assertEqual(escolher_perfil(Hardware(16, None, None), "Darwin", "arm64"), "gpu")

    def test_mac_apple_silicon_8gb_fica_em_cpu(self):
        self.assertEqual(escolher_perfil(Hardware(8, None, None), "Darwin", "arm64"), "cpu")

    def test_sistema_e_maquina_sao_lidos_na_chamada(self):
        with mock.patch("platform.system", return_value="Darwin"), mock.patch("platform.machine", return_value="arm64"):
            self.assertEqual(escolher_perfil(Hardware(16, None, None)), "gpu")

    def test_perfis_exatos(self):
        # Escolhidos pela avaliação de 2026-10-23 (avaliar_modelos.py; tabela no README).
        self.assertEqual(PERFIS["cpu"], {"modelo_padrao": "qwen3.5:4b", "contexto_tokens": 16384, "espera_primeiro_token": 600})
        self.assertEqual(PERFIS["gpu"], {"modelo_padrao": "qwen3.5:9b", "contexto_tokens": 32768, "espera_primeiro_token": 300})

    def test_detectar_nao_levanta(self):
        self.assertIsInstance(detectar(), Hardware)


if __name__ == "__main__":
    unittest.main()
