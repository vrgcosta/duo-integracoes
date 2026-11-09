"""Linha de comando (`servidor.py`): banner, aviso de reimportar e o que é gravado em `dados/`.

O `main` roda de verdade, com a pasta de dados num temporário, um backend gravador no lugar do
motor e o `serve_forever` do servidor criado trocado por um Ctrl+C imediato: ele faz o bind,
imprime o banner e encerra. O `_configurar_log` fica desligado: ele mexe no logger raiz e sujaria a saída dos
outros testes.
"""

from __future__ import annotations

import io
import json
import logging
import os
import socket
import tempfile
import unittest
import uuid
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

import servidor
from duo_provedor.backends.base import InfoModelo
from duo_provedor.blueprint import NOME_ARQUIVO, gerar_blueprint
from duo_provedor.config import carregar_config, salvar_config
from duo_provedor.hardware import Hardware
from duo_provedor.http import criar_servidor
from testes.util import BackendGravador, ServidorDeTeste

NUVEM = InfoModelo("gpt-oss:120b-cloud", "gpt-oss:120b-cloud", True, False, False, True, None)
LOCAL = InfoModelo("qwen3:4b", "qwen3:4b", True, True, False, False, None)


def porta_livre() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class TestServidor(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.pasta = os.path.join(tmp.name, "dados")
        self.blueprint = os.path.join(self.pasta, NOME_ARQUIVO)
        cfg = carregar_config(self.pasta, Hardware(16, None, None))
        self.config(porta=porta_livre(), modelo_padrao=LOCAL.id)
        self.chave = cfg["chave"]
        self.backend = BackendGravador(modelos=[LOCAL, NUVEM])
        self.log = mock.patch.object(servidor, "_configurar_log").start()
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(servidor, "PASTA_DADOS", self.pasta).start()
        mock.patch.object(servidor, "criar_backend", lambda cfg, simulado: self.backend).start()
        mock.patch.object(servidor, "detectar", lambda: Hardware(16, None, None)).start()
        mock.patch.object(servidor, "criar_servidor", self._criar_sem_servir).start()

    @staticmethod
    def _criar_sem_servir(servico, porta):
        # Bind de verdade (é ele que descobre a porta ocupada), mas o laço vira um Ctrl+C imediato.
        # Só neste objeto: trocar na classe pararia também o ServidorDeTeste dos testes de porta.
        servidor_http = criar_servidor(servico, porta)
        servidor_http.serve_forever = mock.Mock(side_effect=KeyboardInterrupt)
        return servidor_http

    def config(self, **campos) -> None:
        cfg = carregar_config(self.pasta)
        cfg.update(campos)
        salvar_config(self.pasta, cfg)

    def bytes_de(self, caminho: str) -> bytes | None:
        if not os.path.exists(caminho):
            return None
        with open(caminho, "rb") as f:
            return f.read()

    def rodar(self, *argv: str) -> tuple[int, str]:
        saida = io.StringIO()
        with redirect_stdout(saida), redirect_stderr(saida):
            codigo = servidor.main(list(argv))
        return codigo, saida.getvalue()

    # -- banner -------------------------------------------------------------

    def test_banner_com_modelo_local(self) -> None:
        codigo, saida = self.rodar()
        self.assertEqual(codigo, 0)
        self.assertIn(" Destino: local", saida)
        self.assertNotIn("ATENÇÃO", saida)
        self.assertNotIn("REIMPORTE", saida)  # 1ª execução: não havia blueprint anterior

    def test_banner_avisa_modelo_padrao_de_nuvem_bloqueado(self) -> None:
        self.config(modelo_padrao=NUVEM.id)
        _, saida = self.rodar()
        self.assertIn(f"ATENÇÃO: O modelo {NUVEM.id} é de nuvem", saida)

    def test_banner_com_nuvem_liberada_nao_diz_que_o_backend_e_remoto(self) -> None:
        self.config(modelo_padrao=NUVEM.id, permitir_modelos_nuvem=True)
        _, saida = self.rodar()
        self.assertIn("permitir_modelos_nuvem está ligado", saida)
        self.assertNotIn("não é local", saida)
        self.assertNotIn("é de nuvem: com ele", saida)  # liberado: não há bloqueio a anunciar

    def test_banner_avisa_pasta_de_dados_no_onedrive(self) -> None:
        with mock.patch.object(servidor, "pasta_que_sai_da_maquina", return_value="OneDrive") as detectar:
            codigo, saida = self.rodar()
        self.assertEqual(codigo, 0)
        self.assertIn("ATENÇÃO", saida)
        self.assertIn("OneDrive", saida)
        self.assertIn("nuvem", saida)
        # A detecção recebe o caminho real (com junções e atalhos resolvidos), não o relativo.
        self.assertEqual(detectar.call_args.args[0], os.path.realpath(self.pasta))

    @unittest.skipUnless(os.name == "nt", "as variáveis do OneDrive são do Windows")
    def test_banner_detecta_o_onedrive_pela_variavel_de_ambiente(self) -> None:
        # realpath dos dois lados: o %TEMP% pode vir com nome curto (USUARI~1) e a detecção recebe o longo.
        with mock.patch.dict(os.environ, {"OneDrive": os.path.realpath(os.path.dirname(self.pasta))}):
            _, saida = self.rodar()
        self.assertIn("está dentro do OneDrive", saida)

    def test_verificar_tambem_avisa_a_pasta_de_rede(self) -> None:
        with mock.patch.object(servidor, "pasta_que_sai_da_maquina", return_value="rede"):
            codigo, saida = self.rodar("--verificar")
        self.assertEqual(codigo, 0)  # aviso, não erro
        self.assertTrue(any(l.startswith("[aviso]") and "rede" in l for l in saida.splitlines()), saida)

    # -- reimportar ---------------------------------------------------------

    def test_destino_que_muda_pede_reimportar_nos_dois_sentidos(self) -> None:
        self.rodar()
        self.config(permitir_modelos_nuvem=True)
        _, saida = self.rodar()
        self.assertIn("REIMPORTE o blueprint no DUO: o destino mudou de local para externo.", saida)

        _, saida = self.rodar()
        self.assertNotIn("REIMPORTE", saida)  # nada mudou desde a última geração

        self.config(permitir_modelos_nuvem=False)
        _, saida = self.rodar()
        self.assertIn("o destino mudou de externo para local", saida)

    def test_porta_que_muda_pede_reimportar(self) -> None:
        self.rodar()
        _, saida = self.rodar("--porta", str(porta_livre()))
        self.assertIn("O blueprint mudou (porta, chave ou molde): REIMPORTE-o no DUO.", saida)
        self.assertNotIn("o destino mudou", saida)

    # -- só grava depois do bind ------------------------------------------------

    def test_opcoes_gravadas_quando_o_servidor_sobe(self) -> None:
        codigo, _ = self.rodar("--modelo", "novo:1b")
        self.assertEqual(codigo, 0)
        self.assertEqual(carregar_config(self.pasta)["modelo_padrao"], "novo:1b")
        self.assertIsNotNone(self.bytes_de(self.blueprint))

    def test_ja_rodando_nao_aplica_nem_grava_as_opcoes(self) -> None:
        with ServidorDeTeste() as outro:
            # Outra instância deste servidor (mesma chave) ocupa a porta; o blueprint em dados/ é
            # o dela, com destino externo.
            self.config(porta=outro.servico.cfg["porta"], chave=outro.chave, permitir_modelos_nuvem=True)
            gerar_blueprint(carregar_config(self.pasta), self.blueprint)
            config_antes = self.bytes_de(os.path.join(self.pasta, "config.json"))
            blueprint_antes = self.bytes_de(self.blueprint)

            codigo, saida = self.rodar("--simulado", "--backend", "openai", "--modelo", "outro")

        self.assertEqual(codigo, 0)
        self.assertIn("O servidor já está rodando", saida)
        self.assertIn("As opções da linha de comando NÃO foram aplicadas", saida)
        self.assertEqual(self.bytes_de(os.path.join(self.pasta, "config.json")), config_antes)
        # O --simulado não pode reescrever o blueprint com destinoLocal: true por cima do da outra.
        self.assertEqual(self.bytes_de(self.blueprint), blueprint_antes)

    def test_ja_rodando_sem_opcoes_nao_fala_de_opcoes(self) -> None:
        with ServidorDeTeste() as outro:
            self.config(porta=outro.servico.cfg["porta"], chave=outro.chave)
            _, saida = self.rodar()
        self.assertIn("O servidor já está rodando", saida)
        self.assertNotIn("NÃO foram aplicadas", saida)

    def test_porta_de_outro_programa_nao_grava_nada(self) -> None:
        with socket.socket() as ocupante:
            ocupante.bind(("127.0.0.1", 0))
            ocupante.listen()
            self.config(porta=ocupante.getsockname()[1])
            config_antes = self.bytes_de(os.path.join(self.pasta, "config.json"))
            codigo, saida = self.rodar("--modelo", "outro")
        self.assertEqual(codigo, 1)
        self.assertIn("está ocupada por outro programa", saida)
        self.assertEqual(self.bytes_de(os.path.join(self.pasta, "config.json")), config_antes)
        self.assertIsNone(self.bytes_de(self.blueprint))

    def test_segunda_instancia_nao_migra_nem_abre_o_banco(self) -> None:
        # O histórico antigo em JSON fica em dados/conversas. Se a segunda instância abrisse o banco
        # antes do bind, migraria e renomearia a pasta por baixo do servidor que já roda.
        antiga = os.path.join(self.pasta, "conversas")
        os.makedirs(antiga)
        with open(os.path.join(antiga, f"{uuid.uuid4()}.json"), "w", encoding="utf-8") as f:
            json.dump({"name": "Antiga", "chat_messages": []}, f)
        with ServidorDeTeste() as outro:
            self.config(porta=outro.servico.cfg["porta"], chave=outro.chave)
            codigo, saida = self.rodar()
        self.assertEqual(codigo, 0)
        self.assertIn("O servidor já está rodando", saida)
        self.assertTrue(os.path.isdir(antiga))
        self.assertFalse(any(n.startswith("conversas.migrado") for n in os.listdir(self.pasta)))
        self.assertFalse(os.path.exists(os.path.join(self.pasta, "duo.sqlite3")))

    def test_banco_corrompido_explica_e_sai_com_1_sem_traceback(self) -> None:
        with open(os.path.join(self.pasta, "duo.sqlite3"), "wb") as f:
            f.write(b"isto nao e um banco SQLite " * 200)
        codigo, saida = self.rodar()
        self.assertEqual(codigo, 1)
        self.assertNotIn("Traceback", saida)
        self.assertIn(os.path.join(self.pasta, "duo.sqlite3"), saida)
        self.assertIn("renomeie", saida)
        # A porta foi solta: dá para subir de novo nela.
        with socket.socket() as s:
            s.bind(("127.0.0.1", carregar_config(self.pasta)["porta"]))

    def test_json_antigo_com_campo_de_tipo_errado_nomeia_o_arquivo(self) -> None:
        antiga = os.path.join(self.pasta, "conversas")
        os.makedirs(antiga)
        nome = f"{uuid.uuid4()}.json"
        with open(os.path.join(antiga, nome), "w", encoding="utf-8") as f:
            json.dump({"name": {"era": "um objeto"}, "chat_messages": []}, f)
        codigo, saida = self.rodar()
        self.assertEqual(codigo, 1)
        self.assertNotIn("Traceback", saida)
        self.assertIn(nome, saida)
        self.assertIn("Nada foi gravado nem renomeado", saida)
        self.assertTrue(os.path.isdir(antiga))  # a próxima abertura tenta de novo

    def test_verificar_nao_grava_as_opcoes(self) -> None:
        config_antes = self.bytes_de(os.path.join(self.pasta, "config.json"))
        codigo, saida = self.rodar("--verificar", "--modelo", NUVEM.id)
        self.assertEqual(codigo, 1)  # o modelo de nuvem bloqueado é erro
        self.assertIn(f"[erro] O modelo {NUVEM.id} é de nuvem", saida)
        self.assertEqual(self.bytes_de(os.path.join(self.pasta, "config.json")), config_antes)
        self.assertIsNone(self.bytes_de(self.blueprint))

    # -- log ------------------------------------------------------------------

    def test_main_configura_o_log(self) -> None:
        self.rodar()
        self.log.assert_called_once_with()


class TestConfigurarLog(unittest.TestCase):
    def test_info_com_data_e_hora(self) -> None:
        with mock.patch.object(servidor.logging, "basicConfig") as configurar:
            servidor._configurar_log()
        argumentos = configurar.call_args.kwargs
        self.assertEqual(argumentos["level"], logging.INFO)
        self.assertIn("%(asctime)s", argumentos["format"])


if __name__ == "__main__":
    unittest.main()
