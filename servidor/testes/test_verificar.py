"""Diagnóstico --verificar: cada problema vira uma linha com o nível certo e a instrução de correção."""

from __future__ import annotations

import tempfile
import unittest

from duo_provedor.backends.simulado import BackendSimulado
from duo_provedor.backends.base import ErroBackend, InfoModelo
from duo_provedor.config import carregar_config
from duo_provedor.hardware import Hardware
from duo_provedor.verificar import Linha, verificar
from testes.util import BackendGravador


def configuracao(**extra) -> dict:
    with tempfile.TemporaryDirectory() as pasta:
        cfg = carregar_config(pasta, Hardware(16, None, None))
    cfg.update(extra)
    return cfg


def modelo(id_: str, ferramentas=True, raciocinio=True, contexto_max=40960, nuvem=False) -> InfoModelo:
    return InfoModelo(id_, id_, ferramentas, raciocinio, False, nuvem, contexto_max)


class BackendQueFalha(BackendGravador):
    def listar_modelos(self):
        raise ErroBackend("Não consegui falar com o Ollama em http://127.0.0.1:11434.")


def niveis(linhas: list[Linha]) -> list[str]:
    return [linha.nivel for linha in linhas]


def textos(linhas: list[Linha], nivel: str) -> str:
    return "\n".join(l.texto for l in linhas if l.nivel == nivel)


HW = Hardware(16, None, None)


class TestVerificar(unittest.TestCase):
    def test_tudo_certo_so_tem_ok_e_comeca_pelo_perfil(self):
        cfg = configuracao(modelo_padrao="m")
        linhas = verificar(cfg, BackendGravador(modelos=[modelo("m")]), HW)
        self.assertEqual(set(niveis(linhas)), {"ok"})
        self.assertIn("16", linhas[0].texto)
        self.assertIn(cfg["perfil"], linhas[0].texto)

    def test_modelo_ausente_e_erro(self):
        cfg = configuracao(modelo_padrao="qwen3:4b", backend="ollama")
        linhas = verificar(cfg, BackendGravador(modelos=[modelo("outro")]), HW)
        self.assertIn("erro", niveis(linhas))
        self.assertIn("Rode: ollama pull qwen3:4b", textos(linhas, "erro"))

    def test_dica_de_instalar_nomeia_o_modelo_do_perfil(self):
        # Ao trocar PERFIS, a dica de instalação acompanha, sem texto fixo no diagnóstico.
        for hw, esperado in ((Hardware(16, None, None), "qwen3.5:4b"), (Hardware(32, 12, "RTX"), "qwen3.5:9b")):
            with tempfile.TemporaryDirectory() as pasta:
                cfg = carregar_config(pasta, hw)
            linhas = verificar(cfg, BackendGravador(modelos=[modelo("outro")]), hw)
            self.assertIn(f"Rode: ollama pull {esperado}", textos(linhas, "erro"))

    def test_sem_tools_avisa_cercas(self):
        cfg = configuracao(modelo_padrao="m")
        linhas = verificar(cfg, BackendGravador(modelos=[modelo("m", ferramentas=False)]), HW)
        self.assertIn("usará emulação por cercas", textos(linhas, "aviso"))

    def test_url_remota_avisa(self):
        cfg = configuracao(modelo_padrao="m", url="http://192.168.0.9:11434")
        linhas = verificar(cfg, BackendGravador(modelos=[modelo("m")]), HW)
        self.assertIn("192.168.0.9", textos(linhas, "aviso"))

    def test_backend_fora_do_ar_e_erro(self):
        linhas = verificar(configuracao(), BackendQueFalha(), HW)
        self.assertIn("Não consegui falar com o Ollama", textos(linhas, "erro"))
        # Sem backend não há o que checar do modelo: não pode estourar nem inventar linhas de modelo.
        self.assertNotIn("ollama pull", textos(linhas, "erro"))

    def test_contexto_maior_que_o_do_modelo_avisa(self):
        cfg = configuracao(modelo_padrao="m", contexto_tokens=32768)
        linhas = verificar(cfg, BackendGravador(modelos=[modelo("m", contexto_max=8192)]), HW)
        self.assertIn("8192", textos(linhas, "aviso"))

    def test_contexto_em_uso_informa_num_ctx(self):
        class ComContexto(BackendGravador):
            def contexto_em_uso(self, modelo):
                return 16384

        cfg = configuracao(modelo_padrao="m", contexto_tokens=16384)
        linhas = verificar(cfg, ComContexto(modelos=[modelo("m")]), HW)
        self.assertTrue(any("num_ctx=16384" in l.texto and l.nivel == "ok" for l in linhas))

    # -- modelo carregado em parte no processador ------------------------------------------

    @staticmethod
    def carregado(total: int, vram: int):
        class Carregado(BackendGravador):
            def memoria_em_uso(self, modelo):
                return total, vram

        return Carregado

    def test_9b_em_parte_no_processador_avisa_e_sugere_o_modelo_do_perfil_cpu(self):
        # Uma placa de 8 GiB com a área de trabalho já usando parte dela: 6,4 dos 7,6 GB na VRAM.
        cfg = configuracao(modelo_padrao="qwen3.5:9b", perfil="gpu")
        backend = self.carregado(7_600_000_000, 6_400_000_000)(modelos=[modelo("qwen3.5:9b")])
        linhas = verificar(cfg, backend, HW)
        avisos = textos(linhas, "aviso")
        self.assertIn("16% no processador", avisos)
        self.assertIn("6,4 de 7,6 GB", avisos)
        self.assertIn("--modelo qwen3.5:4b", avisos)
        self.assertNotIn("erro", niveis(linhas))  # mais lento, mas funciona

    def test_modelo_todo_na_gpu_nao_avisa(self):
        cfg = configuracao(modelo_padrao="qwen3.5:9b", perfil="gpu")
        backend = self.carregado(7_600_000_000, 7_600_000_000)(modelos=[modelo("qwen3.5:9b")])
        self.assertEqual(set(niveis(verificar(cfg, backend, HW))), {"ok"})

    def test_sem_gpu_no_perfil_cpu_o_processador_e_o_esperado(self):
        cfg = configuracao(modelo_padrao="qwen3.5:4b", perfil="cpu")
        backend = self.carregado(4_800_000_000, 0)(modelos=[modelo("qwen3.5:4b")])
        self.assertNotIn("processador", textos(verificar(cfg, backend, HW), "aviso"))

    def test_perfil_gpu_com_o_modelo_todo_no_processador_avisa(self):
        cfg = configuracao(modelo_padrao="qwen3.5:9b", perfil="gpu")
        backend = self.carregado(7_600_000_000, 0)(modelos=[modelo("qwen3.5:9b")])
        self.assertIn("100% no processador", textos(verificar(cfg, backend, HW), "aviso"))

    def test_modelo_do_perfil_cpu_em_parte_no_processador_sugere_contexto_menor(self):
        # Já é o modelo menor: sugerir ele mesmo não ajudaria.
        cfg = configuracao(modelo_padrao="qwen3.5:4b", perfil="gpu")
        backend = self.carregado(4_800_000_000, 4_000_000_000)(modelos=[modelo("qwen3.5:4b")])
        avisos = textos(verificar(cfg, backend, HW), "aviso")
        self.assertIn("contexto_tokens", avisos)
        self.assertNotIn("--modelo qwen3.5:4b", avisos)

    def test_memoria_que_falha_nao_derruba_o_diagnostico(self):
        class Falha(BackendGravador):
            def memoria_em_uso(self, modelo):
                raise ErroBackend("o Ollama caiu no meio")

        cfg = configuracao(modelo_padrao="m")
        self.assertEqual(set(niveis(verificar(cfg, Falha(modelos=[modelo("m")]), HW))), {"ok"})

    def test_modelo_padrao_de_nuvem_e_erro_de_nuvem_nao_de_instalacao(self):
        nuvem = modelo("gpt-oss:120b-cloud", nuvem=True)

        class EscondeNuvem(BackendGravador):
            # Como o Ollama: a lista omite a nuvem, mas o modelo existe se perguntado direto.
            def listar_modelos(self):
                return [m for m in self.modelos if not m.nuvem]

        cfg = configuracao(modelo_padrao=nuvem.id, backend="ollama")
        for backend in (BackendGravador(modelos=[nuvem]), EscondeNuvem(modelos=[nuvem])):
            with self.subTest(backend=type(backend).__name__):
                erros = textos(verificar(cfg, backend, HW), "erro")
                self.assertIn(f"O modelo {nuvem.id} é de nuvem", erros)
                self.assertNotIn("não está instalado", erros)
                self.assertNotIn("ollama pull", erros)

    def test_modelo_padrao_que_nao_conversa_e_erro_proprio(self):
        embedding = InfoModelo("qwen3-embedding", "qwen3-embedding", False, False, False, False, None, conversa=False)

        class EscondeEmbedding(BackendGravador):
            # Como o Ollama: a lista omite o modelo, mas ele existe se perguntado direto.
            def listar_modelos(self):
                return [m for m in self.modelos if m.conversa]

        cfg = configuracao(modelo_padrao=embedding.id, backend="ollama")
        linhas = verificar(cfg, EscondeEmbedding(modelos=[embedding, modelo("m")]), HW)
        erros = textos(linhas, "erro")
        self.assertIn(f"O modelo padrão {embedding.id} está instalado, mas não conversa", erros)
        self.assertIn("--modelo", erros)
        self.assertNotIn("ollama pull", erros)
        self.assertIn("(1 modelo(s) disponível(is))", textos(linhas, "ok"))

    def test_modelo_padrao_de_nuvem_liberado_avisa(self):
        cfg = configuracao(modelo_padrao="m:cloud", permitir_modelos_nuvem=True)
        linhas = verificar(cfg, BackendGravador(modelos=[modelo("m:cloud", nuvem=True)]), HW)
        self.assertNotIn("erro", niveis(linhas))
        self.assertIn("O modelo padrão m:cloud é de nuvem", textos(linhas, "aviso"))

    def test_modelos_de_nuvem_permitidos_avisa(self):
        cfg = configuracao(modelo_padrao="m", permitir_modelos_nuvem=True)
        linhas = verificar(cfg, BackendGravador(modelos=[modelo("m")]), HW)
        self.assertIn("nuvem", textos(linhas, "aviso"))


class TestVersaoDoMotor(unittest.TestCase):
    def _linhas(self, versao):
        class ComVersao(BackendSimulado):
            def versao_motor(self):
                return versao

        cfg = {"modelo_padrao": "simulado", "perfil": "cpu", "contexto_tokens": 8192, "url": "http://127.0.0.1:1",
               "backend": "ollama"}
        return [l for l in verificar(cfg, ComVersao(), Hardware(16, None, None)) if "versão" in l.texto.lower()]

    def test_a_versao_medida_e_ok(self):
        (linha,) = self._linhas("0.30.10")
        self.assertEqual(linha.nivel, "ok")
        self.assertIn("0.30.10", linha.texto)

    def test_mais_nova_tambem(self):
        self.assertEqual(self._linhas("0.31.0")[0].nivel, "ok")

    def test_anterior_a_medida_avisa_sem_afirmar_onde_o_campo_surgiu(self):
        (linha,) = self._linhas("0.12.3")
        self.assertEqual(linha.nivel, "aviso")
        self.assertIn("0.12.3", linha.texto)
        self.assertIn("0.30.10", linha.texto)
        self.assertIn("confira", linha.texto)
        self.assertIn("truncate", linha.texto)

    def test_motor_que_nao_informa_nao_ganha_linha(self):
        self.assertEqual(self._linhas(None), [])

    def test_versao_estranha_nao_derruba(self):
        self.assertEqual(self._linhas("desenvolvimento")[0].nivel, "ok")


if __name__ == "__main__":
    unittest.main()
