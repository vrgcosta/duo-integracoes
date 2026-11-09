"""O repositório não guarda número de processo real, nem em fixture: é o que impede uma cópia de medição de ficar nele."""

import os
import re
import subprocess
import unittest

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NUMERO_CNJ = re.compile(r"\d{7}-\d{2}\.\d{4}\.\d\.\d{2}\.\d{4}")
# Justiça 8, tribunal 99: não existe, e é o único permitido aqui.
FICTICIO = re.compile(r"\.8\.99\.\d{4}$")


class TestSemDadosReais(unittest.TestCase):
    def test_numero_de_processo_so_de_tribunal_que_nao_existe(self):
        arquivos = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard"], cwd=RAIZ,
                                  capture_output=True, text=True, encoding="utf-8", check=True).stdout.splitlines()
        reais = []
        for nome in arquivos:
            caminho = os.path.join(RAIZ, nome)
            if not os.path.isfile(caminho):
                continue
            with open(caminho, encoding="utf-8", errors="ignore") as f:
                reais += [f"{nome}: {achado}" for achado in NUMERO_CNJ.findall(f.read()) if not FICTICIO.search(achado)]
        self.assertEqual(reais, [])


if __name__ == "__main__":
    unittest.main()
