"""Contagem exata antes de o modelo processar, e o que não cabe nunca sai incompleto em silêncio.

O Ollama trunca calado ao estourar `num_ctx`: o `prompt_eval_count` satura em `num_ctx - 1` e o
modelo perde o começo, que é onde estão as regras e a tarefa. Por isso o tamanho é conferido antes
de cada rodada, e a falha da contagem nunca trava o envio.
"""

import collections
import json
import logging
import re
import threading
import time
import unittest
from unittest import mock

from duo_provedor import janela, turno
from duo_provedor.backends.base import (Backend, ChamadaFerramenta, Cortada, ErroAcimaDaJanela, ErroBackend, InfoModelo,
                                        Pedido, Texto, Uso)
from duo_provedor.backends.ollama import BackendOllama
from duo_provedor.backends.openai import BackendOpenAI
from duo_provedor.backends.simulado import BackendSimulado
from duo_provedor.emulacao import mensagem_resultados
from duo_provedor.ferramentas import LEMBRETE_SEM_ESPACO, eh_lembrete_interno, para_funcoes
from testes.cliente_duo import ClienteDuo
from testes.util import BackendGravador, ServidorDeTeste, chamada_unica, ferramentas_do_duo

LISTAR = {"name": "listar_documentos", "description": "Lista os documentos do processo.",
          "input_schema": {"type": "object", "properties": {}}}
MODELO = "gravador"
# Numa janela de 8192 tokens, cabem 6144 no pedido.
OPCOES = {"opcoes": {"contexto": 8192}}
ORCAMENTO = 6144


def _peso_estimado(pedido: Pedido) -> int:
    return sum(janela.estimar_tokens(m["texto"]) for m in pedido.mensagens)


class BackendContador(BackendGravador):
    """Grava cada contagem e responde o que `contar` disser."""

    def __init__(self, roteiro=None, contar=None, modelos=None) -> None:
        super().__init__(roteiro, modelos=modelos)
        self.contar = contar or (lambda pedido: 100)
        self.contagens: list[Pedido] = []
        self.ordem: list[str] = []
        self.parar_contagem: threading.Event | None = None

    def contar_tokens(self, pedido, cancelado=None):
        self.contagens.append(pedido)
        self.ordem.append("contar")
        self.parar_contagem = cancelado
        return self.contar(pedido)

    def conversar(self, pedido, cancelado):
        self.ordem.append("conversar")
        return super().conversar(pedido, cancelado)


class Base(unittest.TestCase):
    def _subir(self, backend, cfg_extra=None) -> None:
        self.backend = backend
        self.srv = ServidorDeTeste(backend, {"modelo_padrao": MODELO, **(cfg_extra or {})}).__enter__()
        self.addCleanup(self.srv.__exit__, None, None, None)
        self.cliente = ClienteDuo(self.srv.url, self.srv.blueprint)
        self.cid = self.cliente.criar_conversa()
        self.arm = self.srv.servico.armazenamento

    def _folha(self) -> str:
        nos = self.arm.ler_conversa(self.cid)["chat_messages"]
        return nos[-1]["uuid"] if nos else self.cliente.raiz

    def _enviar(self, prompt, resultado="x", extras=OPCOES, ferramentas=(LISTAR,)):
        return self.cliente.enviar(self.cid, prompt, self._folha(), list(ferramentas),
                                   lambda nome, entrada: resultado, extras=extras)

    def _enviado(self, indice=-1) -> str:
        return json.dumps(self.backend.pedidos[indice].mensagens, ensure_ascii=False)


class TestLembrete(unittest.TestCase):
    def test_o_lembrete_da_rodada_final_e_interno(self):
        # Sem isto, o simulado o tomaria pelo pedido, e a compactação o levaria ao resumo como se fosse do usuário.
        self.assertTrue(eh_lembrete_interno(LEMBRETE_SEM_ESPACO))


class TestInterface(unittest.TestCase):
    def test_sem_como_contar_devolve_none(self):
        pedido = Pedido(MODELO, [{"papel": "usuario", "texto": "oi"}], [], False, 8192, None)
        self.assertIsNone(BackendSimulado().contar_tokens(pedido))
        self.assertIsNone(BackendOpenAI("http://127.0.0.1:1/v1").contar_tokens(pedido))
        self.assertIsNone(BackendGravador().contar_tokens(pedido))
        self.assertIn("contar_tokens", vars(Backend))


class TestAntesDeCadaRodada(Base):
    def test_conta_antes_de_cada_rodada_o_mesmo_pedido_que_vai_ao_modelo(self):
        self._subir(BackendContador([[chamada_unica()], [Texto("fim")]]))
        r = self._enviar("leia")
        self.assertIsNone(r.erro)
        self.assertEqual(self.backend.ordem, ["contar", "conversar", "contar", "conversar"])
        for contado, enviado in zip(self.backend.contagens, self.backend.pedidos):
            self.assertEqual(contado, enviado)

    def test_o_total_contado_mede_o_fator_e_fica_gravado(self):
        self._subir(BackendContador([[Texto("fim")]], contar=lambda p: _peso_estimado(p) * 3 // 2))
        self.assertIsNone(self._enviar("leia" + " o documento" * 500, ferramentas=()).erro)
        estado = self.arm.estado_janela(self.cid)
        self.assertGreaterEqual(estado["fator_tokens"], 1.4)
        self.assertEqual(estado["ultimo_uso"], _peso_estimado(self.backend.contagens[-1]) * 3 // 2)
        janela_lida = self.cliente.janela(self.cid)
        self.assertEqual(janela_lida["fonte"], "real")

    def test_contagem_baixa_nao_reduz_o_fator(self):
        self._subir(BackendContador([[Texto("fim")]], contar=lambda p: 10))
        self.assertIsNone(self._enviar("leia").erro)
        self.assertEqual(self.arm.estado_janela(self.cid)["fator_tokens"], 1.0)

    def test_ler_a_janela_nao_conta(self):
        self._subir(BackendContador([[Texto("fim")]]))
        self._enviar("leia")
        contagens = len(self.backend.contagens)
        self.cliente.janela(self.cid)
        self.assertEqual(len(self.backend.contagens), contagens)

    def test_acima_do_orcamento_corta_de_novo_antes_de_enviar(self):
        # A estimativa acha que cabe; o motor conta mais. O modelo só recebe depois do novo ajuste.
        def contar(pedido):
            return int(_peso_estimado(pedido) * 1.6)

        self._subir(BackendContador([[chamada_unica()], [Texto("li")], [Texto("fim")]],
                                    contar=lambda p: 10))
        self.assertIsNone(self._enviar("leia", "x" * 18000).erro)
        self.backend.contar = contar
        self.backend.contagens.clear()
        self.assertIsNone(self._enviar("de novo").erro)
        self.assertGreaterEqual(len(self.backend.contagens), 2)
        self.assertIn(self.backend.pedidos[-1], self.backend.contagens)
        self.assertIn(janela.MARCA_OMITIDO, self._enviado())
        self.assertNotIn("x" * 1000, self._enviado())
        self.assertLessEqual(contar(self.backend.pedidos[-1]), ORCAMENTO)

    def test_motor_que_trunca_calado_nao_engana(self):
        # Acima de `num_ctx`, o `prompt_eval_count` fica em num_ctx - 1: é maior que o orçamento mesmo assim.
        def contar(pedido):
            real = int(_peso_estimado(pedido) * 1.6)
            return min(real, 8191)

        self._subir(BackendContador([[chamada_unica()], [Texto("li")], [Texto("fim")]],
                                    contar=lambda p: 10))
        self._enviar("leia", "x" * 24000, extras={"opcoes": {"contexto": 16384}})
        self.backend.contar = contar
        self.assertIsNone(self._enviar("de novo").erro)
        self.assertLessEqual(contar(self.backend.pedidos[-1]), ORCAMENTO)
        self.assertNotIn("x" * 1000, self._enviado())

    def test_limite_de_tentativas_sai_pela_ultima_correcao(self):
        # Um motor que acha o mesmo, por mais que se corte: depois da última contagem, vale o fator que ela deu.
        leitura = [[chamada_unica()], [Texto("li")]]
        self._subir(BackendContador([*leitura, *leitura, *leitura, [Texto("fim")]], contar=lambda p: 10))
        for pergunta in ("um", "dois", "três"):
            self.assertIsNone(self._enviar(pergunta, "x" * 8000).erro)
        self.backend.contar = lambda p: 6500
        self.backend.contagens.clear()
        self.assertIsNone(self._enviar("quatro").erro)
        self.assertEqual(len(self.backend.contagens), turno.TENTATIVAS_CONTAGEM)
        self.assertEqual(len(self.backend.pedidos), 7)
        self.assertNotEqual(self.backend.pedidos[-1].mensagens, self.backend.contagens[-1].mensagens)
        self.assertNotIn("x" * 1000, self._enviado())

    def test_sem_mais_o_que_cortar_nao_insiste(self):
        # Não há o que cortar: a 2ª contagem já mostra que o ajuste não mudou nada.
        self._subir(BackendContador([[Texto("Respondi.")]], contar=lambda p: 7000))
        self.assertIsNone(self._enviar("y" * 21000).erro)
        self.assertLessEqual(len(self.backend.contagens), 2)
        self.assertEqual(len(self.backend.pedidos), 1)

    def test_uma_contagem_de_rodada_com_imagem_corta_sem_gravar_o_fator(self):
        # O motor conta as imagens e a janela não as pesa: serve para o ajuste, e não como medida da estimativa.
        visao = InfoModelo(MODELO, "Gravador", True, False, True, False, None)
        self._subir(BackendContador([[chamada_unica()], [Texto("li")], [Texto("fim")]],
                                    contar=lambda p: 10, modelos=[visao]))
        self._enviar("leia", "x" * 18000)
        fid = self.arm.salvar_arquivo("foto.png", "image/png", b"\x89PNG falso")
        self.backend.contar = lambda p: _peso_estimado(p) + 3000
        self.assertIsNone(self._enviar("veja", extras={**OPCOES, "files": [fid]}).erro)
        self.assertIn(janela.MARCA_OMITIDO, self._enviado())
        self.assertEqual(self.arm.estado_janela(self.cid)["fator_tokens"], 1.0)


class TestContagemNaoTravaOEnvio(Base):
    def test_falha_na_contagem_cai_para_a_estimativa(self):
        def falhar(pedido):
            raise ErroBackend("não deu")

        self._subir(BackendContador([[chamada_unica()], [Texto("fim")]], contar=falhar))
        with self.assertLogs("duo_provedor.turno", "INFO") as logs:
            r = self._enviar("leia")
        self.assertIsNone(r.erro)
        self.assertEqual(len(self.backend.pedidos), 2)
        self.assertTrue([linha for linha in logs.output if "contagem" in linha and "estimativa" in linha])
        # Quem falhou uma vez não é consultado de novo no mesmo turno.
        self.assertEqual(len(self.backend.contagens), 1)

    def test_erro_inesperado_na_contagem_tambem(self):
        def falhar(pedido):
            raise ValueError("bug")

        self._subir(BackendContador([[Texto("fim")]], contar=falhar))
        self.assertIsNone(self._enviar("leia").erro)
        self.assertEqual(len(self.backend.pedidos), 1)

    def test_numero_torto_vale_como_sem_contagem(self):
        for torto in (None, 0, -5, "muitos", True):
            with self.subTest(valor=torto):
                self._subir(BackendContador([[Texto("fim")]], contar=lambda p: torto))
                self.assertIsNone(self._enviar("leia").erro)
                self.assertEqual(len(self.backend.pedidos), 1)
                self.assertIsNone(self.arm.estado_janela(self.cid)["ultimo_uso"])

    def test_demora_na_contagem_cai_para_a_estimativa(self):
        soltar = threading.Event()
        self.addCleanup(soltar.set)
        cancelada = []

        class Demorado(BackendContador):
            def contar_tokens(self, pedido, cancelado=None):
                self.contagens.append(pedido)
                cancelada.append(cancelado.wait(5))
                return 100

        self._subir(Demorado([[Texto("fim")]]), {"espera_primeiro_token": 0.3})
        inicio = time.monotonic()
        r = self._enviar("leia")
        self.assertIsNone(r.erro)
        self.assertLess(time.monotonic() - inicio, 3)
        self.assertEqual(len(self.backend.pedidos), 1)
        prazo = time.monotonic() + 3
        while not cancelada and time.monotonic() < prazo:
            time.sleep(0.01)
        self.assertEqual(cancelada, [True])  # e a contagem que sobrou é cancelada

    def test_duo_fechar_durante_a_contagem_cancela_tudo(self):
        contando, soltar = threading.Event(), threading.Event()
        self.addCleanup(soltar.set)
        cancelada = []

        class Demorado(BackendContador):
            def contar_tokens(self, pedido, cancelado=None):
                contando.set()
                cancelada.append(cancelado.wait(5))
                return 100

        self._subir(Demorado([[Texto("fim")]]))
        conexao, resposta = self.cliente._abrir(
            "POST", f"/api/conversas/{self.cid}/completion",
            {"prompt": "oi", "parent_message_uuid": self.cliente.raiz, "model": MODELO})
        self.assertTrue(contando.wait(5))
        resposta.close()
        conexao.close()
        prazo = time.monotonic() + 5
        while (self.srv.servico.turnos or not cancelada) and time.monotonic() < prazo:
            time.sleep(0.02)
        self.assertEqual(self.srv.servico.turnos, {})
        self.assertEqual(cancelada, [True])
        self.assertEqual(self.backend.pedidos, [])

    def test_ping_durante_a_contagem(self):
        # O SSE não pode ficar mudo: a contagem pode levar o tempo do prefill.
        class Lento(BackendContador):
            def contar_tokens(self, pedido, cancelado=None):
                cancelado.wait(0.4)
                return 100

        with mock.patch.object(turno, "INTERVALO_PING", 0.05):
            self._subir(Lento([[Texto("fim")]]))
            status, bruto = self.srv.pedir("POST", f"/api/conversas/{self.cid}/completion", {"prompt": "leia"})
        self.assertEqual(status, 200)
        self.assertIn(b": ping\n\n", bruto)
        self.assertIn(b"fim", bruto)


class TestFatorAcompanhaAMedicaoExata(Base):
    """Com o número do motor, o fator é o da última medição, para os dois lados: um documento denso que já saiu não
    pode continuar encarecendo o resto da conversa. Sem ele, só cresce (ver test_turno)."""

    def _fator(self) -> float:
        return self.arm.estado_janela(self.cid)["fator_tokens"]

    def _com_proporcao(self, *proporcoes: float) -> None:
        """Um turno por proporção, sem ferramentas: o motor conta `proporção × estimativa`."""
        self._subir(BackendContador([[Texto("ok")] for _ in proporcoes]))
        for proporcao in proporcoes:
            self.backend.contar = lambda p, proporcao=proporcao: int(_peso_estimado(p) * proporcao)
            self.assertIsNone(self._enviar("leia" + " o documento" * 100, ferramentas=()).erro)

    def test_desce_junto_com_a_medicao(self):
        self._com_proporcao(1.8, 1.0)
        self.assertEqual(self._fator(), 1.0)

    def test_sobe_junto_com_a_medicao(self):
        self._com_proporcao(1.2, 1.6)
        self.assertAlmostEqual(self._fator(), 1.6, places=1)

    def test_uma_descida_de_ate_5_por_cento_nao_mexe_no_fator(self):
        self._com_proporcao(1.5, 1.45)
        self.assertAlmostEqual(self._fator(), 1.5, places=1)

    def test_uma_descida_de_mais_de_5_por_cento_mexe(self):
        self._com_proporcao(1.5, 1.3)
        self.assertAlmostEqual(self._fator(), 1.3, places=1)

    def test_a_medicao_nao_vai_abaixo_de_1(self):
        # A estimativa erra um pouco para mais de propósito; é o lado seguro se a contagem falhar depois.
        self._com_proporcao(1.5, 0.5)
        self.assertEqual(self._fator(), 1.0)

    def test_quem_satura_em_num_ctx_menos_1_nao_faz_o_fator_descer(self):
        # Esse número é só um piso do tamanho real: a proporção que sai dele não diz nada.
        self._com_proporcao(1.5)
        self.backend.contar = lambda p: 8191
        with self.assertLogs("duo_provedor.turno", "WARNING"):
            self.assertIsNotNone(self._enviar("x" * 60000).erro)
        self.assertAlmostEqual(self._fator(), 1.5, places=1)

    def test_o_uso_informado_na_rodada_contada_nao_mexe_no_fator(self):
        # O número da contagem é o total; o `Uso` da mesma rodada não acrescenta nada, e pode vir de outra conta.
        self._subir(BackendContador([[Texto("ok"), Uso(99999, 1)]], contar=lambda p: _peso_estimado(p)))
        self.assertIsNone(self._enviar("leia", ferramentas=()).erro)
        estado = self.arm.estado_janela(self.cid)
        self.assertEqual(estado["fator_tokens"], 1.0)
        self.assertEqual(estado["ultimo_uso"], _peso_estimado(self.backend.contagens[-1]))

    def test_rodada_sem_contagem_depois_de_uma_contada_volta_a_so_crescer(self):
        contagens = iter([100])

        def contar(pedido):
            return next(contagens, None)

        self._subir(BackendContador([[chamada_unica()], [Texto("ok"), Uso(99999, 1)]],
                                    contar=contar))
        self.assertIsNone(self._enviar("leia").erro)
        self.assertEqual(self._fator(), 2.0)

    def test_fator_velho_nao_corta_o_que_cabe_ja_na_rodada_em_que_desce(self):
        # O caso do fator 2 que ficou de um documento denso: a medição mostra que era demais, e o ajuste é refeito,
        # em vez de mandar só o sistema e a mensagem atual até o turno seguinte.
        self._subir(BackendContador([[chamada_unica()], [Texto("li")], [Texto("ok")]],
                                    contar=lambda p: _peso_estimado(p)))
        self.assertIsNone(self._enviar("leia", "x" * 15000).erro)
        self.arm.atualizar_conversa(self.cid, fator_tokens=2.0)
        self.backend.contagens.clear()
        self.assertIsNone(self._enviar("e agora?").erro)
        self.assertEqual(len(self.backend.contagens), 2)
        self.assertIn("x" * 15000, self._enviado())
        self.assertEqual(self._fator(), 1.0)

    # -- o caso da revisão: documento de OCR denso, janela de 16384 e 21 ferramentas -----------------------

    DENSO = "OCRDENSO "

    def _real(self, pedido) -> int:
        """O motor: o documento pesa 3 vezes a estimativa; o resto, igual a ela."""
        textos = sum(janela.estimar_tokens(m["texto"]) * (3 if self.DENSO * 50 in m["texto"] else 1)
                     for m in pedido.mensagens)
        ferramentas = janela.estimar_tokens(json.dumps(para_funcoes(pedido.ferramentas), ensure_ascii=False))
        return textos + (ferramentas if pedido.ferramentas else 0)

    def _conversa_com_documento_denso(self):
        leitura = [[chamada_unica()], [Texto("Li o documento.")]]
        respostas = [[Texto(f"resposta {n}")] for n in ("dois", "três", "quatro", "cinco")]
        self._subir(BackendContador([*leitura, *respostas], contar=self._real))
        extras = {"opcoes": {"contexto": 16384}}
        defs = ferramentas_do_duo()
        self.assertIsNone(self._enviar("leia", self.DENSO * 3000, extras=extras, ferramentas=defs).erro)
        for pergunta in ("pergunta dois", "pergunta três", "pergunta quatro"):
            self.assertIsNone(self._enviar(pergunta, extras=extras, ferramentas=defs).erro)
        return extras, defs

    def test_documento_denso_nao_encarece_o_resto_da_conversa(self):
        self._conversa_com_documento_denso()
        self.assertLess(self._fator(), 1.2)
        # Ele segue no histórico, e não é por ter saído da conversa que o resto pesa menos.
        self.assertIn(self.DENSO * 50, json.dumps(self.arm.ler_conversa(self.cid)["chat_messages"][1]["content"]))
        # Antes, de T2 em diante cada pedido levava só o sistema e a mensagem atual.
        enviado = self._enviado()
        for texto in ("pergunta dois", "resposta dois", "pergunta três", "resposta três", "pergunta quatro"):
            self.assertIn(texto, enviado)
        self.assertNotIn(self.DENSO * 50, enviado)
        self.assertLessEqual(self._real(self.backend.pedidos[-1]), 12288)

    def test_com_o_documento_no_historico_o_medidor_conta_com_ele_e_diz_o_ultimo_real(self):
        # A ressalva do medidor. Ele não chama o modelo, então conta o que a próxima montagem tentaria, com o documento
        # dentro; é a contagem do envio que o tira. O número real está à parte, em `ultimo_uso`, e o que está alto não é
        # o fator: as seções fixas são o que vai ao motor.
        extras, defs = self._conversa_com_documento_denso()
        ultimo = self.backend.pedidos[-1]
        medido = self.cliente.janela(self.cid, contexto=16384)
        self.assertEqual(medido["fonte"], "real")
        self.assertEqual(medido["ultimo_uso"], self._real(ultimo))
        documento = next(i for i in medido["itens"] if i["tipo"] == "resultado")
        self.assertTrue(documento["incluido"])
        self.assertLess(self._fator(), 1.2)
        ferramentas = janela.estimar_tokens(json.dumps(para_funcoes(defs), ensure_ascii=False))
        self.assertLess(medido["secoes"]["ferramentas"], ferramentas * 1.2)

        self.assertIsNone(self._enviar("", extras=extras, ferramentas=defs).erro)
        real = self._real(self.backend.pedidos[-1])
        self.assertNotIn(self.DENSO * 50, self._enviado())
        self.assertGreater(medido["usado"], real)

    def test_sem_o_documento_o_medidor_fica_perto_do_que_vai_ao_motor(self):
        # Antes, ele chegava a quase o dobro do real, com `fonte: real`.
        extras, defs = self._conversa_com_documento_denso()
        documento = next(i["id"] for i in self.cliente.janela(self.cid)["itens"] if i["tipo"] == "resultado")
        self.cliente.janela_excluir(self.cid, [documento])
        medido = self.cliente.janela(self.cid, contexto=16384)
        self.assertEqual(medido["fonte"], "real")
        self.assertIsNone(self._enviar("", extras=extras, ferramentas=defs).erro)
        real = self._real(self.backend.pedidos[-1])
        self.assertLess(abs(medido["usado"] - real) / real, 0.1)

    NORMAL = "dado do processo em texto corrido com partes e datas. "

    def test_dentro_do_turno_vale_a_ultima_medicao(self):
        # O caso da revisão, no mesmo turno: um documento denso e depois dois normais. A proporção do denso não pode
        # ficar valendo até o fim: os normais chegavam com 687 e 598 de 3008 tokens, com 6316 livres, e o aviso
        # "para caber na janela" era falso.
        self._subir(BackendContador([[chamada_unica()] for _ in range(3)] + [[Texto("Respondi.")]], contar=self._real))
        resultados = iter([self.DENSO * 2200, self.NORMAL * 220, self.NORMAL * 220])
        r = self.cliente.enviar(self.cid, "leia os três", self.cliente.raiz, ferramentas_do_duo(),
                                lambda nome, entrada: next(resultados), extras={"opcoes": {"contexto": 16384}})
        self.assertIsNone(r.erro)
        ultima = self.backend.pedidos[-1]
        self.assertEqual("".join(m["texto"] for m in ultima.mensagens).count(self.NORMAL * 220), 2)
        self.assertLessEqual(self._real(ultima), 12288)
        self.assertEqual(r.texto.count("Para caber na janela"), 1, "só o documento denso foi truncado")
        self.assertLess(self._fator(), 1.2)

    def _documento_denso_e_um_normal_no_historico(self):
        leitura = [[chamada_unica()], [Texto("li")]]
        self._subir(BackendContador([*leitura, *leitura, [Texto("fim")]], contar=lambda p: 100))
        extras = {"opcoes": {"contexto": 16384}}
        defs = ferramentas_do_duo()
        self.assertIsNone(self._enviar("um", self.DENSO * 950, extras=extras, ferramentas=defs).erro)
        self.assertIsNone(self._enviar("dois", self.NORMAL * 300, extras=extras, ferramentas=defs).erro)
        self.backend.contar = self._real
        self.backend.contagens.clear()
        return extras, defs

    def test_o_reajuste_com_o_fator_novo_que_nao_cabe_volta_ao_que_cabia(self):
        # O fator alto tirou o documento denso; a medição desce, e com ela ele voltaria. O motor conta acima, e vai o
        # pedido que já tinha cabido, sem mais tentativas: é uma contagem extra por rodada.
        extras, defs = self._documento_denso_e_um_normal_no_historico()
        self.arm.atualizar_conversa(self.cid, fator_tokens=1.3)
        self.assertIsNone(self._enviar("três", extras=extras, ferramentas=defs).erro)
        self.assertEqual(len(self.backend.contagens), 2)
        (pedido,) = [p for p in self.backend.pedidos[-1:]]
        self.assertNotIn(self.DENSO * 50, self._enviado())
        self.assertEqual(pedido.mensagens, self.backend.contagens[0].mensagens)
        self.assertLessEqual(self._real(pedido), 12288)
        self.assertLess(self._fator(), 1.2, "o fator é o do pedido que foi, e não o do que passou")

    def test_depois_de_subir_a_contagem_extra_tenta_levar_o_que_cabe(self):
        # A proporção do denso é demais para o resto: com ela sai também o resultado normal, e sobra metade da janela.
        extras, defs = self._documento_denso_e_um_normal_no_historico()
        self.assertIsNone(self._enviar("três", extras=extras, ferramentas=defs).erro)
        self.assertEqual(len(self.backend.contagens), 3)
        enviado = self._enviado()
        self.assertNotIn(self.DENSO * 50, enviado)
        self.assertIn(self.NORMAL * 300, enviado)
        self.assertLessEqual(self._real(self.backend.pedidos[-1]), 12288)

    def test_reajuste_que_daria_no_mesmo_pedido_nao_conta_de_novo(self):
        # O prefill custa; contar o mesmo pedido duas vezes é à toa.
        self._subir(BackendContador([[Texto("ok")]], contar=lambda p: _peso_estimado(p)))
        self.assertIsNone(self._enviar("x" * 60000, extras={"opcoes": {"contexto": 262144}}).erro)
        self.backend.contagens.clear()
        self.backend.roteiro.append([Texto("de novo")])
        self.arm.atualizar_conversa(self.cid, fator_tokens=2.0)
        self.assertIsNone(self._enviar("y", extras={"opcoes": {"contexto": 262144}}).erro)
        self.assertEqual(len(self.backend.contagens), 1, "nada foi cortado: não há o que levar a mais")


class TestSemContagemNuncaRecusa(Base):
    """Sem contagem exata não há prova de que o motor cortaria: a estimativa costuma errar para mais, e no backend
    `openai` o `contexto_tokens` nem chega ao servidor. Vai ao modelo, e o usuário fica sabendo."""

    def test_anexo_acima_da_janela_vai_com_aviso(self):
        self._subir(BackendGravador([[Texto("Respondi.")]]))
        anexos = [{"file_name": "peticao.txt", "extracted_content": "p" * 60000}]
        r = self._enviar("resuma", extras={**OPCOES, "attachments": anexos})
        self.assertIsNone(r.erro)
        self.assertEqual(len(self.backend.pedidos), 1)
        self.assertEqual([f["name"] for f in self.backend.pedidos[0].ferramentas], ["listar_documentos"])
        self.assertIn("p" * 60000, self._enviado())
        self.assertIn("Respondi.", r.texto)
        self.assertIn("estimada", r.texto)
        self.assertIn("recusa", r.texto)
        self.assertNotIn("pode cortar", r.texto)
        self.assertNotIn("Servidor: A entrada", r.texto)

    def test_falha_na_contagem_tambem(self):
        def falhar(pedido):
            raise ErroBackend("não deu")

        self._subir(BackendContador([[Texto("Respondi.")]], contar=falhar))
        r = self._enviar("x" * 60000)
        self.assertIsNone(r.erro)
        self.assertEqual(len(self.backend.pedidos), 1)
        self.assertIn("estimada", r.texto)

    def test_definicoes_maiores_que_a_janela_tambem(self):
        self._subir(BackendGravador([[Texto("Respondi.")]]))
        grandes = [{**LISTAR, "name": f"f{i}", "description": "descrição longa " * 400} for i in range(4)]
        r = self._enviar("oi", ferramentas=grandes)
        self.assertIsNone(r.erro)
        self.assertEqual(len(self.backend.pedidos[0].ferramentas), 4)
        self.assertIn("estimada", r.texto)

    def test_no_meio_do_turno_segue_com_as_ferramentas(self):
        self._subir(BackendGravador([[Texto("Vou ler. "), *[chamada_unica() for _ in range(20)]],
                                     [Texto("Li tudo.")]]))
        r = self._enviar("leia tudo", "z" * 9000)
        self.assertIsNone(r.erro)
        self.assertEqual(len(self.backend.pedidos), 2)
        self.assertTrue(self.backend.pedidos[1].ferramentas)
        self.assertIn("Li tudo.", r.texto)
        self.assertIn("estimada", r.texto)

    def test_o_que_cabe_nao_avisa(self):
        self._subir(BackendGravador([[Texto("Respondi.")]]))
        self.assertNotIn("Servidor:", self._enviar("oi").texto)

    def test_antigos_saem_antes(self):
        self._subir(BackendGravador([[chamada_unica()], [Texto("li")], [Texto("ok")]]))
        self.assertIsNone(self._enviar("leia", "x" * 18000).erro)
        r = self._enviar("y" * 24000)
        self.assertIsNone(r.erro)
        self.assertNotIn("x" * 1000, self._enviado())
        self.assertNotIn("estimada", r.texto)


class TestComContagemExata(Base):
    """Só o número do motor prova que ele cortaria: é a única base para deixar de enviar."""

    def _recusar(self, *args, **kwargs) -> str:
        """Envia, com a recusa esperada: o aviso dela vai ao log, e o modelo não pode ter recebido nada."""
        with self.assertLogs("duo_provedor.turno", "WARNING"):
            resposta = self._enviar(*args, **kwargs)
        self.assertIsNotNone(resposta.erro)
        self.assertEqual(self.backend.pedidos, [], "não pode ir ao modelo")
        return resposta.erro

    def test_abaixo_do_espaco_util_nao_avisa(self):
        self._subir(BackendContador([[Texto("ok")]], contar=lambda p: 5000))
        r = self._enviar("x" * 27000)
        self.assertIsNone(r.erro)
        self.assertEqual(len(self.backend.pedidos), 1)
        self.assertNotIn("Servidor:", r.texto)

    def test_entre_o_espaco_util_e_a_janela_envia_e_avisa_que_sobra_pouco(self):
        self._subir(BackendContador([[Texto("Respondi.")]], contar=lambda p: 7000))
        r = self._enviar("y" * 21000)
        self.assertIsNone(r.erro)
        self.assertEqual(len(self.backend.pedidos), 1)
        self.assertIn("y" * 21000, self._enviado())
        self.assertIn("7000", r.texto)
        self.assertIn("sobram só 1192", r.texto)
        self.assertNotIn("estimada", r.texto)

    def test_perto_do_limite_com_a_contagem_na_mao_nao_diz_que_pode_ter_perdido_o_comeco(self):
        # A contagem prova que nada foi cortado; o aviso genérico é só para quem não tem como saber.
        self._subir(BackendContador([[Texto("Respondi."), Uso(7900, 1)]], contar=lambda p: 7900))
        r = self._enviar("y" * 21000)
        self.assertNotIn(turno.AVISO_LIMITE_JANELA, r.texto)
        self.assertIn("sobram só 292", r.texto)

    def test_acima_da_janela_recusa_sem_ir_ao_modelo(self):
        self._subir(BackendContador([[Texto("não deveria")]], contar=lambda p: 9000))
        erro = self._recusar("x" * 8000)
        self.assertIn("O texto da mensagem ocupa", erro)
        self.assertIn("9000", erro)
        self.assertNotIn("xxxx", erro)
        self.assertNotIn("definições", erro)
        self.assertLessEqual(len(self.backend.contagens), turno.TENTATIVAS_CONTAGEM * 2)

    def test_motor_antigo_que_satura_em_num_ctx_menos_1_tambem_prova(self):
        # O Ollama sem `truncate: false` conta no máximo isto; não passa do contexto, mas só chega aqui quem passou.
        self._subir(BackendContador([[Texto("não deveria")]], contar=lambda p: 8191))
        erro = self._recusar("x" * 60000)
        # O número do motor é só um piso: o tamanho do item vem da estimativa, e não encolhe por causa dele.
        self.assertRegex(erro, rf"ocupa ~{janela.estimar_tokens('x' * 60000) // 100}\d\d tokens")

    def test_o_erro_aponta_o_maior_anexo_e_quanto_cabe(self):
        self._subir(BackendContador([[Texto("não deveria")]], contar=lambda p: 15000))
        anexos = [{"file_name": "certidao.txt", "extracted_content": "c" * 500},
                  {"file_name": "peticao.txt", "extracted_content": "p" * 60000}]
        erro = self._recusar("resuma", extras={**OPCOES, "attachments": anexos})
        self.assertIn('O anexo "peticao.txt" ocupa', erro)
        self.assertNotIn("certidao", erro)
        self.assertIn("8192", erro)
        self.assertIn("15000 tokens", erro)
        ocupa, cabem = (int(n) for n in re.search(r"ocupa ~(\d+) tokens e só cabem ~(\d+)", erro).groups())
        self.assertEqual(cabem, ORCAMENTO - (15000 - ocupa))

    def test_o_primeiro_pedido_fixado_entra_nos_candidatos(self):
        # Sem `system`, ele fica na janela e é a causa de verdade: culpar a mensagem curta não ajudaria em nada.
        self._subir(BackendContador([[Texto("ok")]], contar=lambda p: 10))
        self.assertIsNone(self._enviar("a" * 26000).erro)
        self.backend.contar = lambda p: 9000
        self.backend.pedidos.clear()
        erro = self._recusar("b" * 1600)
        self.assertIn("O 1º pedido da conversa ocupa", erro)

    def test_sem_as_definicoes_cabe_responde_sem_ferramentas_ja_na_1a_rodada(self):
        self._subir(BackendContador([[Texto("Respondi sem consultar.")]],
                                    contar=lambda p: 9000 if p.ferramentas else 3000))
        r = self._enviar("resuma o que anexei")
        self.assertIsNone(r.erro)
        (pedido,) = self.backend.pedidos
        self.assertEqual(pedido.ferramentas, [])
        self.assertIn("Respondi sem consultar.", r.texto)
        self.assertIn("sem poder chamar ferramentas", r.texto)
        self.assertIn(LEMBRETE_SEM_ESPACO, self._enviado())
        self.assertNotIn(LEMBRETE_SEM_ESPACO, r.texto)

    def test_meio_do_turno_sem_espaco_vai_para_a_rodada_final_e_nada_se_perde(self):
        contagens = iter([100])

        def contar(pedido):
            primeira = next(contagens, None)
            return primeira if primeira is not None else (9000 if pedido.ferramentas else 5000)

        self._subir(BackendContador([[Texto("Vou ler. "), *[chamada_unica() for _ in range(3)]],
                                     [Texto("Com o que li.")]], contar=contar))
        r = self._enviar("leia tudo", "z" * 3000)
        self.assertIsNone(r.erro)
        self.assertEqual(len(self.backend.pedidos), 2)
        self.assertEqual(self.backend.pedidos[1].ferramentas, [])
        self.assertIn(LEMBRETE_SEM_ESPACO, self._enviado())
        self.assertIn("Com o que li.", r.texto)
        self.assertIn("sem poder chamar ferramentas", r.texto)
        self._conferir_gravados(3)

    def _conferir_gravados(self, quantos: int) -> None:
        """As chamadas e os resultados da rodada ficam na conversa: é com eles que o Duo a relê."""
        conteudo = self.arm.ler_conversa(self.cid)["chat_messages"][-1]["content"]
        self.assertEqual(len([b for b in conteudo if b["type"] == "tool_use"]), quantos)
        resultados = [b for b in conteudo if b["type"] == "tool_result"]
        self.assertEqual(len(resultados), quantos)
        self.assertTrue(all("zzz" in json.dumps(b["content"]) for b in resultados))

    def test_meio_do_turno_nem_sem_ferramentas_cabe_recusa_apontando_o_resultado(self):
        contagens = iter([100])
        self._subir(BackendContador([[Texto("Vou ler. "), *[chamada_unica() for _ in range(3)]],
                                     [Texto("não deveria")]],
                                    contar=lambda p: next(contagens, 9000)))
        with self.assertLogs("duo_provedor.turno", "WARNING"):
            r = self._enviar("leia tudo", "z" * 9000)
        self.assertEqual(len(self.backend.pedidos), 1)
        self.assertIn("O resultado de listar_documentos ocupa", r.erro)
        self.assertNotIn("definições", r.erro)
        self.assertIn("Vou ler.", r.texto)
        self._conferir_gravados(3)
        self.assertEqual(self.srv.servico.turnos, {})

    def test_o_caso_da_revisao_duas_chamadas_com_folga_em_num_ctx(self):
        # Janela de 8192, 21 ferramentas e contagem de 6675: há 1517 livres, e o turno não pode morrer.
        self._subir(BackendContador([[chamada_unica() for _ in range(2)], [Texto("Li os dois.")]],
                                    contar=lambda p: 6675))
        r = self._enviar("leia", "r" * 2000, ferramentas=ferramentas_do_duo())
        self.assertIsNone(r.erro)
        self.assertEqual(len(self.backend.pedidos), 2)
        self.assertTrue(self.backend.pedidos[1].ferramentas)
        self.assertIn("Li os dois.", r.texto)
        self.assertIn("sobram só 1517", r.texto)

    def test_pedido_recusado_na_1a_rodada_fica_sem_resposta_e_a_conversa_segue(self):
        # Sem nó do assistente, nem interno: é assim que o Duo sabe que o envio seguinte sai do pai dele.
        self._subir(BackendContador([[Texto("segunda deu certo")]], contar=lambda p: 9000))
        raiz = self.cliente.raiz
        with self.assertLogs("duo_provedor.turno", "WARNING"):
            self.assertIsNotNone(self._enviar("x" * 60000).erro)
        self.assertEqual(self.srv.servico.turnos, {})
        self.assertEqual([n["sender"] for n in self.arm.ler_conversa(self.cid)["chat_messages"]], ["human"])
        self.backend.contar = lambda p: 100
        r = self.cliente.enviar(self.cid, "menor", raiz, [LISTAR], extras=OPCOES)
        self.assertIsNone(r.erro)
        self.assertNotIn("xxxx", self._enviado())
        self.assertNotIn(LEMBRETE_SEM_ESPACO, self._enviado())

    def test_erro_sai_no_log_sem_conteudo(self):
        self._subir(BackendContador([[Texto("não deveria")]], contar=lambda p: 30000))
        anexo = {"file_name": "sigilo.txt", "extracted_content": "SEGREDOCONTEUDO " * 5000}
        with self.assertLogs("duo_provedor.turno", logging.INFO) as logs:
            self.assertIsNotNone(self._enviar("PERGUNTASIGILOSA" * 2000, extras={**OPCOES, "attachments": [anexo]}).erro)
        juntos = "\n".join(logs.output)
        self.assertNotIn("SEGREDOCONTEUDO", juntos)
        self.assertNotIn("PERGUNTASIGILOSA", juntos)
        self.assertNotIn("sigilo.txt", juntos)  # nome de arquivo pode citar as partes
        self.assertIn("não cabe na janela", juntos)


class TestFaltaDeContagemAvisa(Base):
    """Nada sai sem o usuário saber que o número é estimado. Com uma exceção que é só de frequência: o motor que nunca
    conta avisa uma vez por conversa, porque o aviso fica no texto da resposta, volta ao modelo a cada turno e, repetido
    em todas, ninguém mais lê."""

    def _falhar(self, pedido):
        raise ErroBackend("O Ollama respondeu 400: prompt too long")

    def test_falha_avisa_mesmo_com_a_estimativa_cabendo(self):
        self._subir(BackendContador([[Texto("Respondi.")]], contar=self._falhar))
        r = self._enviar("oi")
        self.assertIsNone(r.erro)
        self.assertIn("A contagem do motor falhou", r.texto)
        self.assertIn("estimada", r.texto)
        self.assertIn("recusa", r.texto)
        self.assertNotIn("pode cortar", r.texto)

    def test_a_cada_turno_em_que_falhar(self):
        self._subir(BackendContador([[Texto("um")], [Texto("dois")]], contar=self._falhar))
        self.assertIn("falhou", self._enviar("oi").texto)
        self.assertIn("falhou", self._enviar("de novo").texto)

    def test_uma_vez_so_na_resposta_mesmo_com_varias_rodadas(self):
        self._subir(BackendContador([[chamada_unica()], [Texto("fim")]], contar=self._falhar))
        self.assertEqual(self._enviar("leia").texto.count("A contagem do motor falhou"), 1)

    def test_demora_tambem_avisa(self):
        class Demorado(BackendContador):
            def contar_tokens(self, pedido, cancelado=None):
                cancelado.wait(5)
                return 100

        self._subir(Demorado([[Texto("Respondi.")]]), {"espera_primeiro_token": 0.2})
        self.assertIn("A contagem do motor falhou", self._enviar("oi").texto)

    def test_numero_torto_tambem_avisa(self):
        for torto in (0, -5, "muitos", True):
            with self.subTest(valor=torto):
                self._subir(BackendContador([[Texto("Respondi.")]], contar=lambda p: torto))
                self.assertIn("A contagem do motor falhou", self._enviar("oi").texto)

    def test_contagem_que_deu_certo_nao_avisa(self):
        self._subir(BackendContador([[Texto("Respondi.")]], contar=lambda p: 100))
        self.assertNotIn("Servidor:", self._enviar("oi").texto)

    def test_a_ultima_montagem_que_vai_sem_conferir_avisa(self):
        # O motor contou acima em todas as tentativas, e a última montagem vai sem número: quem a recusaria é ele.
        leitura = [[chamada_unica()], [Texto("li")]]
        self._subir(BackendContador([*leitura, *leitura, *leitura, [Texto("fim")]], contar=lambda p: 10))
        for pergunta in ("um", "dois", "três"):
            self.assertIsNone(self._enviar(pergunta, "x" * 8000).erro)
        self.backend.contar = lambda p: 6500
        r = self._enviar("quatro")
        self.assertIsNone(r.erro)
        self.assertIn("sem conferir", r.texto)
        self.assertIn("estimada", r.texto)

    def test_motor_que_nunca_conta_avisa_uma_vez_por_conversa(self):
        class SemContagem(BackendGravador):
            pode_cortar_calado = True

        self._subir(SemContagem([[Texto("um")], [Texto("dois")]]))
        primeira = self._enviar("oi")
        self.assertIn("não informa a contagem", primeira.texto)
        self.assertIn("estimad", primeira.texto)
        self.assertNotIn("Servidor:", self._enviar("de novo").texto)

    def test_motor_que_nunca_conta_avisa_sempre_que_a_estimativa_passar(self):
        class SemContagem(BackendGravador):
            pode_cortar_calado = True

        self._subir(SemContagem([[Texto("um")], [Texto("dois")]]))
        self._enviar("oi")
        self.assertIn("estimada", self._enviar("x" * 60000).texto)

    def test_o_simulado_nao_tem_motor_que_corte(self):
        self._subir(BackendSimulado())
        self.assertEqual(self._enviar("olá", ferramentas=()).texto, "Você disse: olá")


class TestOllamaQueIgnoraTruncate(Base):
    """Sem o campo, em várias mensagens ele tira as antigas e conta o que sobrou: 840 para 2346 reais, em `num_ctx` 2048.
    O número não passa da janela e nada o denuncia, a não ser estar muito abaixo do que a estimativa dá."""

    OPCOES = {"opcoes": {"contexto": 2048}}

    def test_contagem_muito_abaixo_da_estimativa_nao_vale_como_exata(self):
        self._subir(BackendContador([[Texto("Respondi.")]], contar=lambda p: 840))
        self.arm.atualizar_conversa(self.cid, fator_tokens=1.5, ultimo_uso=100, model=MODELO)
        with self.assertLogs("duo_provedor.turno", "WARNING"):
            r = self._enviar("x" * 12000, extras=self.OPCOES)
        self.assertIsNone(r.erro)
        self.assertEqual(len(self.backend.pedidos), 1)
        self.assertIn("pode ter cortado", r.texto)
        self.assertIn("840", r.texto)
        estado = self.arm.estado_janela(self.cid)
        self.assertEqual((estado["fator_tokens"], estado["ultimo_uso"]), (1.5, 100))

    def test_com_a_estimativa_dentro_da_janela_nao_desconfia(self):
        # Texto que rende muito por token, com a estimativa na janela: nada indica corte.
        self._subir(BackendContador([[Texto("Respondi.")]], contar=lambda p: 300))
        self.assertNotIn("Servidor:", self._enviar("x" * 3000, extras=self.OPCOES).texto)

    def test_com_a_contagem_perto_da_estimativa_nao_desconfia(self):
        self._subir(BackendContador([[Texto("Respondi.")]], contar=lambda p: janela.estimar_tokens("x" * 8000) - 200))
        r = self._enviar("x" * 8000, extras={"opcoes": {"contexto": 2000}})
        self.assertNotIn("pode ter cortado", r.texto or "")

    def test_a_saturacao_continua_sendo_prova(self):
        self._subir(BackendContador([[Texto("não deveria")]], contar=lambda p: 2047))
        with self.assertLogs("duo_provedor.turno", "WARNING"):
            r = self._enviar("x" * 60000, extras=self.OPCOES)
        self.assertIn("não cabe na janela", r.erro)
        self.assertEqual(self.backend.pedidos, [])


class BackendQueRecusa(BackendGravador):
    """No envio real, recusa como o Ollama com `truncate: false`; nenhuma contagem (ou a que falhou) o avisou antes."""

    def __init__(self, roteiro, recusar) -> None:
        super().__init__(roteiro)
        self.recusar = recusar
        self.recusas: list[Pedido] = []

    def conversar(self, pedido, cancelado):
        total = self.recusar(pedido)
        if total is not None:
            self.recusas.append(pedido)
            raise ErroAcimaDaJanela(total)
        return super().conversar(pedido, cancelado)


class TestEstouroNoEnvioReal(Base):
    """O envio real vai com `truncate: false`: quando a contagem faltou, é ele que prova que não cabe."""

    def test_vai_para_a_rodada_final_sem_ferramentas(self):
        self._subir(BackendQueRecusa([[Texto("Respondi sem consultar.")]], lambda p: 9000 if p.ferramentas else None))
        r = self._enviar("resuma")
        self.assertIsNone(r.erro)
        self.assertEqual(len(self.backend.recusas), 1)
        (pedido,) = self.backend.pedidos
        self.assertEqual(pedido.ferramentas, [])
        self.assertIn(LEMBRETE_SEM_ESPACO, self._enviado())
        self.assertIn("Respondi sem consultar.", r.texto)
        self.assertIn("sem poder chamar ferramentas", r.texto)

    def test_nem_sem_ferramentas_recusa_com_o_item_certo_e_o_numero_do_motor(self):
        self._subir(BackendQueRecusa([[Texto("não deveria")]], lambda p: 15000))
        anexos = [{"file_name": "peticao.txt", "extracted_content": "p" * 60000}]
        with self.assertLogs("duo_provedor.turno", "WARNING") as logs:
            r = self._enviar("resuma", extras={**OPCOES, "attachments": anexos})
        self.assertEqual(self.backend.pedidos, [])
        self.assertIn('O anexo "peticao.txt" ocupa', r.erro)
        self.assertIn("15000 tokens", r.erro)
        self.assertNotIn("peticao", "\n".join(logs.output))
        self.assertEqual([n["sender"] for n in self.arm.ler_conversa(self.cid)["chat_messages"]], ["human"])

    def test_no_meio_do_turno_o_que_ja_foi_feito_fica(self):
        recusar = iter([None])
        self._subir(BackendQueRecusa([[Texto("Vou ler. "), *[chamada_unica() for _ in range(2)]],
                                      [Texto("Com o que li.")]],
                                     lambda p: next(recusar, 9000 if p.ferramentas else None)))
        r = self._enviar("leia tudo", "z" * 3000)
        self.assertIsNone(r.erro)
        self.assertEqual(len(self.backend.recusas), 1)
        self.assertEqual(self.backend.pedidos[-1].ferramentas, [])
        self.assertIn("z" * 3000, self._enviado())
        self.assertIn("Com o que li.", r.texto)

    def test_o_numero_do_motor_mede_o_fator(self):
        self._subir(BackendQueRecusa([[Texto("ok")]], lambda p: 9000 if p.ferramentas else None))
        self.assertIsNone(self._enviar("x" * 4000).erro)
        self.assertEqual(self.arm.estado_janela(self.cid)["fator_tokens"], 2.0)
        self.assertEqual(self.arm.estado_janela(self.cid)["ultimo_uso"], 9000)

    def test_outro_erro_do_motor_continua_sendo_o_erro_dele(self):
        class Falha(BackendGravador):
            def conversar(self, pedido, cancelado):
                raise ErroBackend("O Ollama respondeu 400: opção inválida")

        self._subir(Falha())
        with self.assertLogs("duo_provedor.turno", "WARNING"):
            r = self._enviar("oi")
        self.assertIn("opção inválida", r.erro)


class TestSaturarNaoRepeteAContagem(Base):
    """Acima de FATOR_MAXIMO, o fator gravado não alcança a proporção: cada rodada recomeçava dele, passava do orçamento e
    contava três vezes, com quatro prefills. Agora é só a rodada em que saturou, e o turno segue com o fator que funcionou."""

    DENSO = "ID 123456789 | 01/02/2026 | 0000000-00.2026.8.99.0000\n" * 300
    EXTRAS = {"opcoes": {"contexto": 16384}}
    ORCAMENTO = 12288

    class PorRodada(BackendContador):
        def __init__(self, *args, **kwargs) -> None:
            super().__init__(*args, **kwargs)
            self.por_rodada: collections.Counter = collections.Counter()

        def contar_tokens(self, pedido, cancelado=None):
            self.por_rodada[len(self.pedidos)] += 1
            return super().contar_tokens(pedido, cancelado)

    @staticmethod
    def _medir(pedido, razao: float) -> int:
        total = janela.estimar_tokens(json.dumps(pedido.ferramentas))
        for mensagem in pedido.mensagens:
            texto = mensagem.get("texto") or ""
            total += janela.estimar_tokens(texto) * (razao if "0000000-00" in texto else 1.0)
        return int(total)

    def _subir_turno(self, razao, rodadas=8) -> None:
        def contar(pedido):
            return self._medir(pedido, razao() if callable(razao) else razao)

        roteiro = [[chamada_unica()] for _ in range(rodadas)] + [[Texto("fim")]]
        self._subir(self.PorRodada(roteiro, contar=contar))

    def _contagens(self) -> list[int]:
        return [self.backend.por_rodada[i] for i in range(len(self.backend.pedidos))]

    def test_depois_de_saturar_e_uma_contagem_por_rodada(self):
        self._subir_turno(2.5)
        self.assertIsNone(self._enviar("pergunta", self.DENSO, extras=self.EXTRAS).erro)
        contagens = self._contagens()
        saturou = next(i for i, n in enumerate(contagens) if n > 1)
        self.assertLessEqual(contagens[saturou], turno.TENTATIVAS_CONTAGEM)
        depois = contagens[saturou + 1:]
        self.assertGreaterEqual(len(depois), 3)
        self.assertEqual(depois, [1] * len(depois))

    def test_cada_pedido_enviado_coube_sem_aviso_de_conferir(self):
        self._subir_turno(2.5)
        r = self._enviar("pergunta", self.DENSO, extras=self.EXTRAS)
        self.assertNotIn("sem conferir", r.texto)
        for pedido in self.backend.pedidos:
            self.assertLessEqual(self._medir(pedido, 2.5), self.ORCAMENTO)

    def test_o_fator_gravado_continua_no_teto(self):
        self._subir_turno(2.5)
        self._enviar("pergunta", self.DENSO, extras=self.EXTRAS)
        self.assertLessEqual(self.arm.estado_janela(self.cid)["fator_tokens"], janela.FATOR_MAXIMO)

    def test_o_documento_denso_que_saiu_nao_deixa_o_resto_sob_a_proporcao_dele(self):
        # O defeito que a contagem extra evita: resultados normais cortados com metade da janela livre.
        normal = "O autor requereu a produção de prova testemunhal, deferida na audiência seguinte. " * 70
        resultados = iter([self.DENSO] + [normal] * 7)
        self._subir_turno(2.5)
        r = self.cliente.enviar(self.cid, "pergunta", self._folha(), [LISTAR], lambda nome, entrada: next(resultados),
                                extras=self.EXTRAS)
        self.assertIsNone(r.erro)
        self.assertEqual(self._contagens()[2:], [1] * (len(self.backend.pedidos) - 2))
        self.assertGreater(self._medir(self.backend.pedidos[-1], 2.5), self.ORCAMENTO / 2)

    def _contar_na_rodada(self, rodada: int, contagens: list[int]):
        """A partir de `rodada`, o que `contagens` disser; antes dela, e depois que acabar, a medida de sempre."""
        restantes = iter(contagens)

        def contar(pedido):
            if len(self.backend.pedidos) == rodada:
                total = next(restantes, None)
                if total is not None:
                    return total
            return self._medir(pedido, 2.5)
        return contar

    def test_passando_do_orcamento_abaixo_da_janela_vai_como_esta_com_aviso(self):
        # Já se pagou o prefill, o pedido cabe na janela, e cortar de novo pagaria outro, para ir sem conferir.
        self._subir_turno(2.5, rodadas=5)
        self.backend.contar = self._contar_na_rodada(5, [14000])
        r = self._enviar("pergunta", self.DENSO, extras=self.EXTRAS)
        self.assertIsNone(r.erro)
        self.assertEqual(self.backend.por_rodada[5], 1)
        self.assertEqual(self.backend.pedidos[5].mensagens, self.backend.contagens[-1].mensagens)
        self.assertIn("O pedido tem 14000 tokens numa janela de 16384: sobram", r.texto)
        self.assertNotIn("sem conferir", r.texto)

    def test_o_fator_sobe_com_a_medicao_que_passou(self):
        self._subir_turno(2.5, rodadas=6)
        self.backend.contar = self._contar_na_rodada(5, [14000])
        self.assertIsNone(self._enviar("pergunta", self.DENSO, extras=self.EXTRAS).erro)
        self.assertEqual(self.backend.por_rodada[6], 1)
        # Só de segurar o fator, cai menos de 2% por rodada, com o peso das marcas de corte.
        self.assertLess(_peso_estimado(self.backend.pedidos[6]), 0.95 * _peso_estimado(self.backend.pedidos[5]))
        self.assertLessEqual(self._medir(self.backend.pedidos[6], 2.5), self.ORCAMENTO)

    def test_acima_da_janela_conta_de_novo_porque_nao_paga_prefill_e_e_a_prova(self):
        self._subir_turno(2.5, rodadas=5)
        self.backend.contar = self._contar_na_rodada(5, [20000])
        r = self._enviar("pergunta", self.DENSO, extras=self.EXTRAS)
        self.assertIsNone(r.erro)
        self.assertEqual(self.backend.por_rodada[5], 2)
        self.assertTrue(self.backend.pedidos[5].ferramentas, "dava para cortar: não é caso de perder as ferramentas")
        self.assertLessEqual(self._medir(self.backend.pedidos[5], 2.5), self.ORCAMENTO)
        self.assertNotIn("sem conferir", r.texto)

    def test_o_turno_seguinte_recomeca_do_fator_gravado(self):
        self._subir_turno(2.5, rodadas=4)
        self.assertIsNone(self._enviar("pergunta", self.DENSO, extras=self.EXTRAS).erro)
        ja_foram = len(self.backend.pedidos)
        self.backend.roteiro = [*self.backend.roteiro, *[[chamada_unica()] for _ in range(3)],
                                [Texto("ok")]]
        self.assertIsNone(self._enviar("outra", self.DENSO, extras=self.EXTRAS).erro)
        self.assertGreater(max(self._contagens()[ja_foram:]), 1)

    def test_imagem_nao_vale_como_saturacao(self):
        # A proporção de uma imagem não é a do texto: segurá-la cortaria à toa o que viesse depois.
        visao = InfoModelo(MODELO, "Gravador", True, False, True, False, None)
        roteiro = [[chamada_unica()], [Texto("fim")]]
        self._subir(self.PorRodada(roteiro, contar=lambda p: _peso_estimado(p) + 12300, modelos=[visao]))
        fid = self.arm.salvar_arquivo("foto.png", "image/png", b"\x89PNG falso")
        self.assertIsNone(self._enviar("veja", extras={**self.EXTRAS, "files": [fid]}).erro)
        self.assertEqual(len(self.backend.pedidos), 2)
        self.assertGreater(self.backend.por_rodada[1], 1)


class TestAvisoDependeDeQuemCortaCalado(Base):
    """"Pode cortar o começo" só é verdade de quem corta: o Ollama vai com `truncate: false`, e recusa."""

    class QueCorta(BackendGravador):
        pode_cortar_calado = True

    def test_quem_corta_calado_e_avisado_de_que_pode_cortar(self):
        self._subir(self.QueCorta([[Texto("Respondi.")]]))
        anexos = [{"file_name": "peticao.txt", "extracted_content": "p" * 60000}]
        r = self._enviar("resuma", extras={**OPCOES, "attachments": anexos})
        self.assertIn("estimada", r.texto)
        self.assertIn("pode cortar", r.texto)
        self.assertNotIn("recusa", r.texto)

    def test_o_ollama_nao_corta_calado(self):
        self.assertFalse(BackendOllama.pode_cortar_calado)

    def test_o_openai_corta(self):
        self.assertTrue(BackendOpenAI.pode_cortar_calado)

    def _perto_do_limite(self, classe, entrada: int) -> str:
        self._subir(classe([[Texto("Respondi."), Uso(entrada, 5)]]))
        return self._enviar("oi").texto

    def test_uso_perto_do_limite_em_quem_corta(self):
        self.assertIn(turno.AVISO_LIMITE_JANELA, self._perto_do_limite(self.QueCorta, 8000))

    def test_uso_perto_do_limite_em_quem_recusa_nao_fala_em_perder_o_comeco(self):
        texto = self._perto_do_limite(BackendGravador, 8000)
        self.assertNotIn(turno.AVISO_LIMITE_JANELA, texto)
        self.assertIn(turno.AVISO_POUCO_PARA_RESPONDER, texto)

    def test_uso_saturado_e_sinal_de_corte_mesmo_em_quem_deveria_recusar(self):
        # 8191 de 8192: é assim que um Ollama antigo, que ignora `truncate: false`, responde depois de cortar.
        self.assertIn(turno.AVISO_LIMITE_JANELA, self._perto_do_limite(BackendGravador, 8191))


class TestRecusaSemTotal(Base):
    """O motor recusou sem dizer o tamanho: é prova de que não cabe, mas não há número para mostrar."""

    def _recusar_sempre(self):
        self._subir(BackendQueRecusa([[Texto("não deveria")]], lambda p: None))
        self.backend.conversar = self._recusa_sem_total

    def _recusa_sem_total(self, pedido, cancelado):
        raise ErroAcimaDaJanela(None)

    def test_o_erro_nao_inventa_numero(self):
        self._recusar_sempre()
        with self.assertLogs("duo_provedor.turno", "WARNING"):
            r = self._enviar("x" * 4000)
        self.assertIn("sem dizer o tamanho", r.erro)
        self.assertIn("O maior item: o texto da mensagem", r.erro)
        self.assertIn(f"~{janela.estimar_tokens('x' * 4000)} tokens", r.erro)
        self.assertNotIn("8193", r.erro)
        self.assertNotIn("só cabem", r.erro)
        self.assertIn("8192", r.erro)

    def test_nao_mexe_na_medicao(self):
        self._recusar_sempre()
        self.arm.atualizar_conversa(self.cid, fator_tokens=1.3, ultimo_uso=500, model=MODELO)
        with self.assertLogs("duo_provedor.turno", "WARNING"):
            self._enviar("oi")
        estado = self.arm.estado_janela(self.cid)
        self.assertEqual((estado["fator_tokens"], estado["ultimo_uso"]), (1.3, 500))


class TestContagemQueLevantaARecusa(Base):
    """Um backend pode responder à contagem levantando `ErroAcimaDaJanela`: é a prova, e não uma falha da contagem."""

    def _subir_com(self, levantar, roteiro=None) -> None:
        class Recusa(BackendContador):
            def contar_tokens(self, pedido, cancelado=None):
                self.contagens.append(pedido)
                levantar(pedido)
                return 100

        self._subir(Recusa(roteiro or [[Texto("não deveria")]]))

    def test_com_total_vale_como_a_contagem(self):
        def levantar(pedido):
            raise ErroAcimaDaJanela(20000)

        self._subir_com(levantar)
        with self.assertLogs("duo_provedor.turno", "WARNING"):
            r = self._enviar("x" * 4000)
        self.assertEqual(self.backend.pedidos, [], "não pode ir ao modelo")
        self.assertIn("20000 tokens", r.erro)
        self.assertNotIn("falhou", r.erro)
        self.assertEqual(self.arm.estado_janela(self.cid)["ultimo_uso"], 20000)

    def test_sem_total_nao_inventa(self):
        def levantar(pedido):
            raise ErroAcimaDaJanela(None)

        self._subir_com(levantar)
        with self.assertLogs("duo_provedor.turno", "WARNING"):
            r = self._enviar("x" * 4000)
        self.assertEqual(self.backend.pedidos, [])
        self.assertIn("sem dizer o tamanho", r.erro)

    def test_sem_as_ferramentas_cabendo_responde_sem_elas(self):
        def levantar(pedido):
            if pedido.ferramentas:
                raise ErroAcimaDaJanela(9000)

        self._subir_com(levantar, [[Texto("Respondi.")]])
        r = self._enviar("oi")
        self.assertIsNone(r.erro)
        self.assertEqual(self.backend.pedidos[0].ferramentas, [])
        self.assertIn("sem poder chamar ferramentas", r.texto)
        self.assertNotIn("A contagem do motor falhou", r.texto)

    def test_outro_erro_na_contagem_continua_sendo_falha(self):
        def levantar(pedido):
            raise ErroBackend("não deu")

        self._subir_com(levantar, [[Texto("Respondi.")]])
        self.assertIn("A contagem do motor falhou", self._enviar("oi").texto)


class TestRespostaCortada(Base):
    def test_o_usuario_fica_sabendo(self):
        # O pedido entre o espaço útil e a janela vai, e o risco é a resposta faltar: quem conta isso é o motor.
        self._subir(BackendGravador([[Texto("O processo tem"), Cortada()]]))
        r = self._enviar("oi")
        self.assertIsNone(r.erro)
        self.assertIn("O processo tem", r.texto)
        self.assertIn(turno.AVISO_RESPOSTA_CORTADA, r.texto)


    def test_rodada_do_meio_cortada_nao_avisa_numa_resposta_completa(self):
        # A resposta que o usuário lê é a da última rodada: ela terminou, e "antes de terminar" seria falso.
        self._subir(BackendGravador([[Texto("Vou "), Cortada(), chamada_unica()],
                                     [Texto("Terminei.")]]))
        r = self._enviar("leia")
        self.assertIsNone(r.erro)
        self.assertNotIn(turno.AVISO_RESPOSTA_CORTADA, r.texto)

    def test_a_ultima_cortada_avisa_mesmo_depois_de_rodadas_inteiras(self):
        self._subir(BackendGravador([[chamada_unica()], [Texto("O processo"), Cortada()]]))
        r = self._enviar("leia")
        self.assertEqual(r.texto.count(turno.AVISO_RESPOSTA_CORTADA), 1)


class TestRodadaFinalSemFerramentas(Base):
    """A rodada sem ferramentas não pode reservar o peso de definições que não vão ao modelo."""

    def _bater_no_teto(self, modo: str, chamadas: list):
        self._subir(BackendGravador([chamadas, [Texto("Com o que li.")]]))
        extras = {"opcoes": {"contexto": 8192, "max_rodadas": 1, "ferramentas": modo}}
        return self._enviar("leia", "r" * 5000, extras=extras, ferramentas=ferramentas_do_duo())

    def _conferir_sem_corte(self, resposta) -> None:
        self.assertIsNone(resposta.erro)
        pedido = self.backend.pedidos[-1]
        self.assertEqual(pedido.ferramentas, [])
        # Pelo texto: nas cercas, a mensagem também guarda os resultados em `itens`, que não vão ao motor.
        self.assertEqual("".join(m["texto"] for m in pedido.mensagens).count("r" * 5000), 3)
        self.assertNotIn("tokens omitidos", self._enviado())
        self.assertNotIn("Para caber na janela", resposta.texto)

    def test_nativo_nao_conta_as_definicoes(self):
        self._conferir_sem_corte(self._bater_no_teto("nativo", [chamada_unica() for _ in range(3)]))

    def test_nas_cercas_o_catalogo_nem_vai(self):
        bloco = '```duo-ferramenta\n{{"nome": "listar_documentos", "argumentos": {{"pagina": "{}"}}}}\n```'
        self._conferir_sem_corte(self._bater_no_teto("cercas", [Texto("\n".join(bloco.format(n) for n in range(3)))]))
        self.assertNotIn("Ferramentas disponíveis", self.backend.pedidos[-1].mensagens[0]["texto"])

    def _marcas_da_rodada_final(self, mensagens) -> str:
        return "".join(m["texto"] for m in mensagens)

    def test_marcas_nao_mandam_ler_com_a_ferramenta_que_nao_foi_oferecida(self):
        # O modelo que obedece tem a chamada descartada e fica sem resposta. Uma limpeza e um truncamento, nos dois modos.
        chamada = [{"id": "c1", "nome": "ler", "argumentos": {}}]
        nativo = [[{"papel": "usuario", "texto": "um"}, {"papel": "assistente", "texto": "", "chamadas": chamada},
                   {"papel": "ferramenta", "id_chamada": "c1", "nome": "ler", "texto": "a" * 9000}],
                  [{"papel": "usuario", "texto": "dois"},
                   {"papel": "assistente", "texto": "", "chamadas": [{"id": "c2", "nome": "ler", "argumentos": {}}]},
                   {"papel": "ferramenta", "id_chamada": "c2", "nome": "ler", "texto": "b" * 30000}]]
        item = {"id": "c1", "nome": "ler", "conteudo": "a" * 9000}
        grande = {"id": "c2", "nome": "ler", "conteudo": "b" * 30000}
        cercas = [[{"papel": "usuario", "texto": "um"},
                   {"papel": "usuario", "texto": mensagem_resultados([item]), "resultado": True, "itens": [item]}],
                  [{"papel": "usuario", "texto": "dois"},
                   {"papel": "usuario", "texto": mensagem_resultados([grande]), "resultado": True, "itens": [grande]}]]
        for nome, turnos in (("nativo", nativo), ("cercas", cercas)):
            with self.subTest(modo=nome):
                r = janela.ajustar("", "", turnos, 4096, False, sem_ferramentas=True)
                texto = self._marcas_da_rodada_final(r.mensagens)
                self.assertIn("omitid", texto)
                self.assertNotIn(janela.LER_TRECHO, texto)
                self.assertNotIn("leia com", texto)

    def test_com_ferramentas_as_marcas_continuam_dizendo_como_ler(self):
        turnos = [[{"papel": "usuario", "texto": "um"},
                   {"papel": "assistente", "texto": "", "chamadas": [{"id": "c1", "nome": "ler", "argumentos": {}}]},
                   {"papel": "ferramenta", "id_chamada": "c1", "nome": "ler", "texto": "a" * 30000}],
                  [{"papel": "usuario", "texto": "dois"}]]
        self.assertIn(janela.LER_TRECHO, self._marcas_da_rodada_final(janela.ajustar("", "", turnos, 4096, False).mensagens))

    def test_na_rodada_final_de_verdade_o_modelo_nao_e_mandado_ler(self):
        contagens = iter([100])

        def contar(pedido):
            primeira = next(contagens, None)
            return primeira if primeira is not None else (9000 if pedido.ferramentas else _peso_estimado(pedido))

        self._subir(BackendContador([[Texto("Vou ler. "), *[chamada_unica() for _ in range(3)]],
                                     [Texto("Com o que li.")]], contar=contar))
        r = self._enviar("leia tudo", "dado do processo. " * 900)
        self.assertIsNone(r.erro)
        (_, final) = self.backend.pedidos
        self.assertEqual(final.ferramentas, [])
        self.assertIn("omitid", self._marcas_da_rodada_final(final.mensagens))
        self.assertNotIn(f"{janela.LER_TRECHO}(", self._marcas_da_rodada_final(final.mensagens))

    def test_nota_nao_manda_ler_com_a_ferramenta_que_nao_foi_oferecida(self):
        r = janela.ajustar("", "", [[{"papel": "usuario", "texto": "um"}, {"papel": "assistente", "texto": "a" * 30000}],
                                    [{"papel": "usuario", "texto": "dois"}]], 4096, False, sem_ferramentas=True)
        self.assertIn("Nota do servidor", r.mensagens[0]["texto"])
        self.assertNotIn(janela.LER_TRECHO, r.mensagens[0]["texto"])
        self.assertNotIn("consulte de novo", r.mensagens[0]["texto"])


class TestAjuste(unittest.TestCase):
    def test_excede_e_total_trazem_a_conta_com_o_fator(self):
        r = janela.ajustar("", "", [[{"papel": "usuario", "texto": "x" * 42500}]], 8192, True, fator_tokens=2.0)
        self.assertTrue(r.excede)
        self.assertEqual(r.total, 20000)
        self.assertEqual(r.orcamento, 6144)
        self.assertFalse(janela.ajustar("", "", [[{"papel": "usuario", "texto": "oi"}]], 8192, True).excede)


if __name__ == "__main__":
    unittest.main()
