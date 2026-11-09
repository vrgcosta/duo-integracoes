import json
import os
import shutil
import tempfile
import threading
import time
import unittest
from unittest import mock

from duo_provedor.config import ErroConfig, carregar_config, gravar_json, salvar_config
from duo_provedor.hardware import Hardware


class TestConfig(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_gera_chave_e_persiste(self):
        a = carregar_config(self.tmp, Hardware(16, None, None))
        b = carregar_config(self.tmp)
        self.assertEqual(a["chave"], b["chave"])
        self.assertGreaterEqual(len(a["chave"]), 32)

    def test_padroes(self):
        c = carregar_config(self.tmp, Hardware(16, None, None))
        self.assertEqual((c["porta"], c["backend"], c["url"], c["perfil"], c["modelo_padrao"], c["contexto_tokens"]),
                         (8766, "ollama", "http://127.0.0.1:11434", "cpu", "qwen3.5:4b", 16384))
        self.assertEqual((c["temperatura"], c["ferramentas"], c["max_rodadas"], c["limite_resultado"], c["permitir_modelos_nuvem"]),
                         (0.3, "auto", 15, 0, False))
        self.assertEqual((c["ferramentas_por_modelo"], c["ferramentas_ativas"], c["chave_backend"]), ({}, [], ""))

    def test_limite_de_resultado_gravado_nao_muda_sozinho(self):
        # Valor explícito vence o automático, inclusive o 12000 que versões antigas gravaram.
        salvar_config(self.tmp, {"perfil": "cpu", "limite_resultado": 12000})
        self.assertEqual(carregar_config(self.tmp)["limite_resultado"], 12000)

    def test_perfil_gpu_define_modelo_e_contexto(self):
        c = carregar_config(self.tmp, Hardware(32, 12, "RTX"))
        self.assertEqual((c["perfil"], c["modelo_padrao"], c["contexto_tokens"], c["espera_primeiro_token"]),
                         ("gpu", "qwen3.5:9b", 32768, 300))

    def test_valores_existentes_preservados(self):
        gravar_json(os.path.join(self.tmp, "config.json"), {"porta": 9000, "modelo_padrao": "llama3.1:8b"})
        c = carregar_config(self.tmp, Hardware(16, None, None))
        self.assertEqual((c["porta"], c["modelo_padrao"]), (9000, "llama3.1:8b"))

    def test_json_invalido_explica_o_arquivo(self):
        with open(os.path.join(self.tmp, "config.json"), "w") as f:
            f.write("{quebrado")
        with self.assertRaises(ErroConfig) as e:
            carregar_config(self.tmp)
        self.assertIn("config.json", str(e.exception))

    def test_perfil_invalido_levanta_erro_config(self):
        gravar_json(os.path.join(self.tmp, "config.json"), {"perfil": "turbo"})
        with self.assertRaises(ErroConfig) as e:
            carregar_config(self.tmp, Hardware(16, None, None))
        msg = str(e.exception)
        for trecho in ("config.json", "turbo", "cpu", "gpu"):
            self.assertIn(trecho, msg)

    def test_gravar_json_atomico_sem_tmp(self):
        gravar_json(os.path.join(self.tmp, "x.json"), {"a": "ç"})
        self.assertEqual(os.listdir(self.tmp), ["x.json"])

    def test_url_segue_o_backend_escolhido(self):
        gravar_json(os.path.join(self.tmp, "config.json"), {"backend": "openai"})
        c = carregar_config(self.tmp, Hardware(16, None, None))
        self.assertEqual(c["url"], "http://127.0.0.1:1234/v1")

    # --- arquivo aberto por outro programa (no Windows, os.replace falha com WinError 5) ---

    def _segurar_aberto(self, caminho: str, segundos: float) -> threading.Thread:
        """Faz o papel de outro programa (antivírus, editor) com o arquivo aberto para leitura."""
        aberto = threading.Event()

        def segurar():
            with open(caminho, "rb"):
                aberto.set()
                time.sleep(segundos)

        leitor = threading.Thread(target=segurar)
        leitor.start()
        self.addCleanup(leitor.join)
        self.assertTrue(aberto.wait(2))
        return leitor

    def test_gravar_json_com_o_arquivo_aberto_por_outro_programa(self):
        caminho = os.path.join(self.tmp, "config.json")
        gravar_json(caminho, {"v": 1})
        self._segurar_aberto(caminho, 0.1)
        with mock.patch("duo_provedor.config.time.sleep", wraps=time.sleep) as espera:
            gravar_json(caminho, {"v": 2})
        with open(caminho, encoding="utf-8") as f:
            self.assertEqual(json.load(f), {"v": 2})
        if os.name == "nt":
            # Prova de que o cenário é o real: a 1ª troca falhou e a nova tentativa resolveu.
            self.assertGreater(espera.call_count, 0)

    @unittest.skipUnless(os.name == "nt", "só o Windows impede trocar um arquivo aberto")
    def test_gravar_json_desiste_depois_das_tentativas(self):
        caminho = os.path.join(self.tmp, "config.json")
        gravar_json(caminho, {"v": 1})
        leitor = self._segurar_aberto(caminho, 1.0)
        with mock.patch("duo_provedor.config.ESPERA_ENTRE_TENTATIVAS", 0.01):
            with self.assertRaises(PermissionError):
                gravar_json(caminho, {"v": 2})
        leitor.join()
        with open(caminho, encoding="utf-8") as f:
            self.assertEqual(json.load(f), {"v": 1})  # o arquivo bom ficou


if __name__ == "__main__":
    unittest.main()
