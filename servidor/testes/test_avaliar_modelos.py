"""Bateria de avaliação de modelos (`avaliar_modelos.py`), sem rede.

Os modelos aqui são backends de teste que respondem conforme o pedido: um "ideal", que faz
tudo certo, e variações que erram de propósito (ferramenta inexistente, JSON quebrado, id
errado, inglês, raciocínio que não desliga). O Ollama só aparece como servidor falso local.
"""

from __future__ import annotations

import io
import json
import os
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from typing import Callable, Iterator
from unittest import mock

import avaliar_modelos as av
from duo_provedor.backends.base import (
    Backend, ChamadaFerramenta, ErroBackend, Evento, InfoModelo, Pedido, Raciocinio, Texto, Uso,
)
from duo_provedor.backends.simulado import BackendSimulado
from duo_provedor.emulacao import (
    LINGUAGEM_CHAMADA, MARCA_RESULTADOS, catalogo_texto, chamada_como_texto,
)
from duo_provedor.hardware import Hardware
from testes.falso import ServidorFalso

GB = 10**9


def info(modelo: str = "m", ferramentas=True, raciocinio=True, visao=False, nuvem=False,
         contexto_max: int | None = 40960) -> InfoModelo:
    return InfoModelo(modelo, modelo, ferramentas, raciocinio, visao, nuvem, contexto_max)


class BackendRoteirizado(Backend):
    """Responde com `responder(pedido)` e grava os pedidos; `atraso` antes do 1º evento."""

    nome = "roteirizado"

    def __init__(self, responder: Callable[[Pedido], list[Evento]], modelo: InfoModelo | None = None,
                 atraso: float = 0.0, contexto: int | None = None) -> None:
        self.responder = responder
        self.modelo = modelo or info()
        self.atraso = atraso
        self.contexto = contexto
        self.pedidos: list[Pedido] = []

    def listar_modelos(self) -> list[InfoModelo]:
        return [self.modelo]

    def info_modelo(self, modelo: str) -> InfoModelo | None:
        return self.modelo if modelo == self.modelo.id else None

    def conversar(self, pedido: Pedido, cancelado: threading.Event) -> Iterator[Evento]:
        self.pedidos.append(pedido)
        if self.atraso:
            time.sleep(self.atraso)
        yield from self.responder(pedido)

    def contexto_em_uso(self, modelo: str) -> int | None:
        return self.contexto


def _sistema(pedido: Pedido) -> str:
    return "\n".join(m.get("texto") or "" for m in pedido.mensagens if m.get("papel") == "sistema")


def _cercas(pedido: Pedido) -> bool:
    return not pedido.ferramentas and LINGUAGEM_CHAMADA in _sistema(pedido)


RESUMO_PT = ("A autora cobra da ré seis parcelas não pagas de um contrato de manutenção. "
             "Ela pede a condenação ao pagamento de R$ 12.450,00, com juros e correção, "
             "além das custas e dos honorários.")
RESUMO_EN = ("The plaintiff seeks payment of six unpaid installments under a maintenance "
             "contract. She asks the court to order the defendant to pay with interest.")


def ideal(pedido: Pedido, chamar_ler: dict | None = None, resumo: str = RESUMO_PT,
          raciocina_sempre: bool = False) -> list[Evento]:
    """O modelo que faz tudo certo; os parâmetros permitem estragar uma parte de cada vez."""
    ultimo = pedido.mensagens[-1].get("texto") or ""
    historico = json.dumps(pedido.mensagens, ensure_ascii=False)

    def chamar(nome: str, argumentos: dict) -> list[Evento]:
        if _cercas(pedido):
            return [Texto("Vou consultar.\n"), Texto(chamada_como_texto(nome, argumentos))]
        return [ChamadaFerramenta(nome, argumentos)]

    pensa = pedido.pensar or raciocina_sempre
    if ultimo == av.PEDIDO_LISTAR:
        return chamar("listar_documentos", {})
    if ultimo == av.PEDIDO_LER and "101" in historico:
        return chamar("ler_documento", chamar_ler if chamar_ler is not None else {"id": "101"})
    if ultimo.startswith(av.PEDIDO_RESUMO):
        return [Texto(resumo[:40]), Texto(resumo[40:]), Uso(600, 50)]
    if ultimo == av.PEDIDO_RACIOCINIO:
        return ([Raciocinio("17 vezes 24 é 408.")] if pensa else []) + [Texto("408"), Uso(40, 3)]
    return [Texto("ok"), Uso(20, 1)]


def opcoes(**extra) -> av.Opcoes:
    base = dict(contexto=8192, prazo=5.0, temperatura=0.3, prazo_carregamento=5.0)
    base.update(extra)
    return av.Opcoes(**base)


class MedidorFalso:
    """Mesma interface do `MedidorOllama`, sem HTTP."""

    def __init__(self, memoria: dict | None = None, tamanhos: dict | None = None,
                 vram: list[int] | None = None) -> None:
        self._memoria = memoria or {}
        self._tamanhos = tamanhos or {}
        self._vram = list(vram or [])
        self.descarregados: list[str] = []

    def vram_usada(self) -> int | None:
        return self._vram.pop(0) if self._vram else None

    def versao(self) -> str | None:
        return "0.0.0-teste"

    def tamanhos(self) -> dict[str, int]:
        return dict(self._tamanhos)

    def memoria(self, modelo: str) -> dict | None:
        return self._memoria.get(modelo)

    def descarregar(self, modelo: str) -> None:
        self.descarregados.append(modelo)


# ----------------------------------------------------------------------------------------


class TestColeta(unittest.TestCase):
    def test_junta_eventos_e_mede_o_primeiro_token(self):
        backend = BackendRoteirizado(lambda p: [Raciocinio("hm"), Texto("oi"), Uso(3, 2)], atraso=0.05)
        coleta = av.coletar(backend, av.pedido_simples("m", "oi", opcoes()), 5.0)
        self.assertIsNone(coleta.falha())
        self.assertEqual((coleta.texto, coleta.raciocinio, coleta.uso), ("oi", "hm", Uso(3, 2)))
        self.assertGreaterEqual(coleta.primeiro_token, 0.05)
        self.assertGreaterEqual(coleta.primeiro_texto, coleta.primeiro_token)
        self.assertGreaterEqual(coleta.duracao, coleta.primeiro_texto)

    def test_prazo_esgotado_cancela_o_backend(self):
        visto = threading.Event()

        class Travado(BackendRoteirizado):
            def conversar(self, pedido, cancelado):
                cancelado.wait(5)
                if cancelado.is_set():
                    visto.set()
                return
                yield  # noqa: unreachable - faz da função um gerador

        inicio = time.monotonic()
        coleta = av.coletar(Travado(lambda p: []), av.pedido_simples("m", "oi", opcoes()), 0.2)
        self.assertLess(time.monotonic() - inicio, 2.0)
        self.assertTrue(coleta.esgotado)
        self.assertIn("prazo", coleta.falha())
        self.assertTrue(visto.wait(2), "o backend deveria ter visto o cancelamento")

    def test_parar_em_encerra_cedo_sem_ser_falha(self):
        class Infinito(BackendRoteirizado):
            def conversar(self, pedido, cancelado):
                yield Raciocinio("começando")
                while not cancelado.is_set():
                    time.sleep(0.01)
                    yield Raciocinio("…")

        inicio = time.monotonic()
        coleta = av.coletar(Infinito(lambda p: []), av.pedido_simples("m", "oi", opcoes()), 5.0,
                            parar_em=lambda e: isinstance(e, Raciocinio))
        self.assertLess(time.monotonic() - inicio, 2.0)
        self.assertIsNone(coleta.falha())
        self.assertEqual(coleta.raciocinio, "começando")

    def test_erro_do_backend_vira_falha_legivel(self):
        def explode(p):
            raise ErroBackend("O modelo x não está instalado.")

        coleta = av.coletar(BackendRoteirizado(explode), av.pedido_simples("m", "oi", opcoes()), 5.0)
        self.assertEqual(coleta.falha(), "O modelo x não está instalado.")

    def test_excecao_inesperada_tambem_vira_falha(self):
        def explode(p):
            raise KeyError("campo")

        coleta = av.coletar(BackendRoteirizado(explode), av.pedido_simples("m", "oi", opcoes()), 5.0)
        self.assertIn("campo", coleta.falha())


class TestPontuacao(unittest.TestCase):
    def test_chamada_certa_pontua_tudo(self):
        r = av.pontuar_chamadas([("ler_documento", {"id": "101"}, None)], "ler_documento", {"id": "101"})
        self.assertEqual((r.situacao, r.medidas["pontos"]), (av.OK, 3))

    def test_ferramenta_inexistente_falha(self):
        r = av.pontuar_chamadas([("listar_documentos", {}, None), ("abrir_autos", {}, None)],
                                "listar_documentos", {})
        self.assertEqual((r.situacao, r.medidas["pontos"]), (av.FALHA, 2))
        self.assertIn("abrir_autos", r.detalhe)

    def test_json_invalido_falha(self):
        r = av.pontuar_chamadas([("ler_documento", None, "argumentos não são JSON válido")],
                                "ler_documento", {"id": "101"})
        self.assertEqual(r.situacao, av.FALHA)
        self.assertIn("JSON", r.detalhe)

    def test_id_como_numero_nao_e_o_texto_pedido(self):
        r = av.pontuar_chamadas([("ler_documento", {"id": 101}, None)], "ler_documento", {"id": "101"})
        self.assertEqual((r.situacao, r.medidas["pontos"]), (av.FALHA, 2))

    def test_sem_chamada_falha_e_mostra_o_comeco_do_texto(self):
        r = av.pontuar_chamadas([], "listar_documentos", {}, texto="Não tenho acesso aos autos.")
        self.assertEqual((r.situacao, r.medidas["pontos"]), (av.FALHA, 0))
        self.assertIn("Não tenho acesso", r.detalhe)


class TestFerramentasFicticias(unittest.TestCase):
    def test_tres_ferramentas_no_formato_anthropic(self):
        nomes = [f["name"] for f in av.FERRAMENTAS]
        self.assertEqual(nomes, ["listar_documentos", "ler_documento", "buscar_movimentos"])
        ler = av.FERRAMENTAS[1]["input_schema"]
        self.assertEqual((ler["properties"]["id"]["type"], ler["required"]), ("string", ["id"]))
        self.assertEqual(av.FERRAMENTAS[0]["input_schema"]["properties"], {})

    def test_resultado_sintetico_tem_a_peticao_inicial_no_101(self):
        documentos = json.loads(av.RESULTADO_LISTA)["documentos"]
        self.assertEqual([d["id"] for d in documentos], ["101", "102", "103"])
        self.assertIn("inicial", documentos[0]["tipo"].lower())


class TestBateriaNativa(unittest.TestCase):
    def test_modelo_ideal_passa_nos_dois_passos(self):
        backend = BackendRoteirizado(ideal)
        r = av.bateria_nativa(backend, "m", opcoes())
        self.assertEqual(r.situacao, av.OK, r.detalhe)
        self.assertEqual([p.situacao for p in r.medidas["passos"]], [av.OK, av.OK])
        primeiro, segundo = backend.pedidos
        self.assertEqual(primeiro.ferramentas, av.FERRAMENTAS)
        self.assertEqual((primeiro.pensar, primeiro.contexto_tokens, primeiro.temperatura), (False, 8192, 0.3))
        papeis = [m["papel"] for m in segundo.mensagens]
        self.assertEqual(papeis, ["sistema", "usuario", "assistente", "ferramenta", "assistente", "usuario"])
        chamada = segundo.mensagens[2]["chamadas"][0]
        self.assertEqual((chamada["nome"], chamada["argumentos"]), ("listar_documentos", {}))
        self.assertEqual(segundo.mensagens[3]["id_chamada"], chamada["id"])
        self.assertEqual(segundo.mensagens[3]["texto"], av.RESULTADO_LISTA)
        self.assertNotIn("101", segundo.mensagens[4]["texto"], "o id deve vir do resultado, não do resumo")
        self.assertEqual(segundo.mensagens[-1]["texto"], av.PEDIDO_LER)

    def test_passo_2_com_id_errado_fica_1_de_2(self):
        backend = BackendRoteirizado(lambda p: ideal(p, chamar_ler={"id": "102"}))
        r = av.bateria_nativa(backend, "m", opcoes())
        self.assertEqual(r.situacao, av.FALHA)
        self.assertEqual([p.situacao for p in r.medidas["passos"]], [av.OK, av.FALHA])
        self.assertIn("102", r.detalhe)

    def test_passo_1_falho_nao_impede_o_passo_2(self):
        # O histórico do passo 2 é sintético: o resultado de cada passo é independente.
        backend = BackendRoteirizado(lambda p: [Texto("Os autos têm três documentos.")]
                                     if p.mensagens[-1]["texto"] == av.PEDIDO_LISTAR else ideal(p))
        r = av.bateria_nativa(backend, "m", opcoes())
        self.assertEqual([p.situacao for p in r.medidas["passos"]], [av.FALHA, av.OK])
        self.assertIn("três documentos", r.detalhe)


class TestBateriaCercas(unittest.TestCase):
    def test_modelo_ideal_passa_com_catalogo_no_sistema(self):
        backend = BackendRoteirizado(ideal)
        r = av.bateria_cercas(backend, "m", opcoes())
        self.assertEqual(r.situacao, av.OK, r.detalhe)
        primeiro, segundo = backend.pedidos
        self.assertEqual(primeiro.ferramentas, [])
        self.assertIn(catalogo_texto(av.FERRAMENTAS), _sistema(primeiro))
        self.assertEqual([m["papel"] for m in segundo.mensagens],
                         ["sistema", "usuario", "assistente", "usuario", "assistente", "usuario"])
        self.assertEqual(segundo.mensagens[2]["texto"], chamada_como_texto("listar_documentos", {}))
        self.assertTrue(segundo.mensagens[3]["texto"].startswith(MARCA_RESULTADOS))
        self.assertIn("101", segundo.mensagens[3]["texto"])

    def test_cerca_com_json_quebrado_falha(self):
        def quebrado(p):
            if p.mensagens[-1]["texto"] == av.PEDIDO_LISTAR:
                return [Texto(f"```{LINGUAGEM_CHAMADA}\n{{\"nome\": \"listar_documentos\", \"argumentos\": }}\n```")]
            return ideal(p)

        r = av.bateria_cercas(BackendRoteirizado(quebrado), "m", opcoes())
        self.assertEqual([p.situacao for p in r.medidas["passos"]], [av.FALHA, av.OK])
        self.assertIn("JSON", r.detalhe)

    def test_chamada_nativa_nas_cercas_nao_conta_mas_aparece_no_detalhe(self):
        def nativo_sempre(p):
            if p.mensagens[-1]["texto"] == av.PEDIDO_LISTAR:
                return [ChamadaFerramenta("listar_documentos", {})]
            return ideal(p)

        r = av.bateria_cercas(BackendRoteirizado(nativo_sempre), "m", opcoes())
        self.assertEqual(r.medidas["passos"][0].situacao, av.FALHA)
        self.assertIn("chamada nativa", r.medidas["passos"][0].detalhe)

    def test_so_raciocinio_sem_texto_aparece_no_detalhe(self):
        def so_pensa(p):
            if p.mensagens[-1]["texto"] == av.PEDIDO_LISTAR:
                return [Raciocinio("Preciso chamar listar_documentos.")]
            return ideal(p)

        r = av.bateria_cercas(BackendRoteirizado(so_pensa), "m", opcoes())
        self.assertIn("só raciocínio", r.medidas["passos"][0].detalhe)

    def test_cerca_de_outra_linguagem_nao_conta_como_chamada(self):
        def json_comum(p):
            if p.mensagens[-1]["texto"] == av.PEDIDO_LISTAR:
                return [Texto('```json\n{"nome": "listar_documentos", "argumentos": {}}\n```')]
            return ideal(p)

        r = av.bateria_cercas(BackendRoteirizado(json_comum), "m", opcoes())
        self.assertEqual(r.medidas["passos"][0].medidas["pontos"], 0)


class TestPortugues(unittest.TestCase):
    def test_fracao_distingue_portugues_de_ingles(self):
        self.assertGreaterEqual(av.fracao_portugues(RESUMO_PT), av.LIMIAR_PORTUGUES)
        self.assertLess(av.fracao_portugues(RESUMO_EN), av.LIMIAR_PORTUGUES)
        self.assertEqual(av.fracao_portugues(""), 0.0)
        self.assertEqual(av.fracao_portugues("申请人要求被告支付"), 0.0)

    def test_palavras_que_tambem_sao_inglesas_nao_contam(self):
        # Raciocínio em inglês vazado no texto (visto no qwen3:4b com o pensamento desligado).
        ingles = "As a rule, do a summary as asked; no need to give me a long answer, as I do know."
        self.assertLess(av.fracao_portugues(ingles), 0.05)
        misto = ingles * 3 + " " + RESUMO_PT
        self.assertLess(av.fracao_portugues(misto), av.LIMIAR_PORTUGUES)

    def test_contar_frases(self):
        self.assertEqual(av.contar_frases("Uma frase. Outra frase! E a terceira?"), 3)
        self.assertEqual(av.contar_frases("Pede R$ 12.450,00 e honorários."), 1)
        self.assertEqual(av.contar_frases("- item um\n- item dois"), 2)
        self.assertEqual(av.contar_frases("   "), 0)

    def test_tokens_por_segundo_desconta_o_primeiro_token(self):
        self.assertEqual(av.tokens_por_segundo(Uso(500, 50), 1.0, 6.0), 10.0)
        self.assertIsNone(av.tokens_por_segundo(None, 1.0, 6.0))
        self.assertIsNone(av.tokens_por_segundo(Uso(500, 50), None, 6.0))
        self.assertIsNone(av.tokens_por_segundo(Uso(500, 50), 6.0, 6.0))

    def test_trecho_sintetico_tem_cerca_de_1500_caracteres(self):
        self.assertTrue(1300 <= len(av.TRECHO_PETICAO) <= 1700, len(av.TRECHO_PETICAO))

    def test_resumo_em_portugues_passa_e_mede(self):
        backend = BackendRoteirizado(ideal, atraso=0.05)
        r = av.bateria_ptbr(backend, "m", opcoes())
        self.assertEqual(r.situacao, av.OK, r.detalhe)
        m = r.medidas
        self.assertGreaterEqual(m["primeiro_token"], 0.05)
        self.assertEqual((m["caracteres"], m["frases"]), (len(RESUMO_PT), 2))
        self.assertGreaterEqual(m["fracao_pt"], av.LIMIAR_PORTUGUES)
        self.assertIsNotNone(m["tokens_s"])
        pedido = backend.pedidos[0]
        self.assertIn(av.TRECHO_PETICAO, pedido.mensagens[-1]["texto"])
        self.assertEqual((pedido.ferramentas, pedido.pensar), ([], False))

    def test_resumo_em_ingles_falha(self):
        r = av.bateria_ptbr(BackendRoteirizado(lambda p: ideal(p, resumo=RESUMO_EN)), "m", opcoes())
        self.assertEqual(r.situacao, av.FALHA)
        self.assertIn("português", r.detalhe)

    def test_resposta_vazia_falha(self):
        r = av.bateria_ptbr(BackendRoteirizado(lambda p: [Uso(10, 0)]), "m", opcoes())
        self.assertEqual(r.situacao, av.FALHA)

    def test_modelo_que_nao_para_falha_mas_guarda_o_que_gerou(self):
        class SemFim(BackendRoteirizado):
            def conversar(self, pedido, cancelado):
                while not cancelado.is_set():
                    time.sleep(0.01)
                    yield Texto("A autora cobra da ré. ")

        r = av.bateria_ptbr(SemFim(ideal), "m", opcoes(prazo=0.3))
        self.assertEqual(r.situacao, av.FALHA)
        self.assertIn("prazo", r.detalhe)
        self.assertGreater(r.medidas["caracteres"], 0)
        self.assertIsNotNone(r.medidas["primeiro_token"])

    def test_trecho_do_relatorio_mostra_comeco_e_fim(self):
        texto = "Okay, let me think. " * 60 + "Resumo final em português."
        trecho = av.inicio_e_fim(texto, 300)
        self.assertTrue(trecho.startswith("Okay, let me think."))
        self.assertTrue(trecho.endswith("Resumo final em português."))
        self.assertLess(len(trecho), 340)
        self.assertEqual(av.inicio_e_fim("curto", 300), "curto")


class TestRaciocinio(unittest.TestCase):
    def test_simulado_obedece_ao_botao(self):
        r = av.bateria_raciocinio(BackendSimulado(), "simulado", info(), opcoes())
        self.assertEqual(r.situacao, av.OK, r.detalhe)
        self.assertEqual((r.medidas["sem_pensar"], r.medidas["com_pensar"]), (False, True))

    def test_raciocinio_que_nao_desliga_falha(self):
        backend = BackendRoteirizado(lambda p: ideal(p, raciocina_sempre=True))
        r = av.bateria_raciocinio(backend, "m", info(), opcoes())
        self.assertEqual(r.situacao, av.FALHA)
        self.assertIn("desligado", r.detalhe)

    def test_think_no_texto_nao_conta_como_raciocinio_separado(self):
        backend = BackendRoteirizado(lambda p: [Texto("<think>17 x 24</think>408")] if p.pensar
                                     else [Texto("408")])
        r = av.bateria_raciocinio(backend, "m", info(), opcoes())
        self.assertEqual(r.situacao, av.FALHA)
        self.assertIn("<think>", r.detalhe)

    def test_sem_capacidade_e_na_sem_chamar_o_modelo(self):
        backend = BackendRoteirizado(ideal)
        r = av.bateria_raciocinio(backend, "m", info(raciocinio=False), opcoes())
        self.assertEqual(r.situacao, av.NA)
        self.assertEqual(backend.pedidos, [])


class TestBateriaGpu(unittest.TestCase):
    def test_mede_a_fracao_na_vram(self):
        medidor = MedidorFalso({"m": {"size": 10 * GB, "size_vram": 5 * GB, "context_length": 8192}})
        r = av.bateria_gpu(BackendRoteirizado(ideal, contexto=8192), "m", opcoes(), medidor)
        self.assertEqual(r.situacao, av.OK, r.detalhe)
        self.assertEqual((r.medidas["contexto_em_uso"], r.medidas["tamanho"], r.medidas["fracao"]),
                         (8192, 10 * GB, 0.5))
        self.assertIn("50%", r.detalhe)

    def test_contexto_carregado_diferente_do_pedido_falha(self):
        r = av.bateria_gpu(BackendRoteirizado(ideal, contexto=4096), "m", opcoes(), MedidorFalso())
        self.assertEqual(r.situacao, av.FALHA)
        self.assertIn("4096", r.detalhe)

    def test_sem_medidor_e_sem_contexto_e_na(self):
        r = av.bateria_gpu(BackendRoteirizado(ideal), "m", opcoes(), None)
        self.assertEqual(r.situacao, av.NA)

    def test_vram_medida_corrige_o_ps_que_informa_menos_que_o_download(self):
        # Visto no Ollama 0.30.10 com o gemma4:12b: /api/ps dizia 1,1 GB, e a placa tinha 8,3 GB a mais.
        medidor = MedidorFalso({"m": {"size": 1_100_000_000, "size_vram": 1_100_000_000}},
                               vram=[10_600_000_000])
        r = av.bateria_gpu(BackendRoteirizado(ideal, contexto=8192), "m", opcoes(), medidor,
                           vram_antes=2_300_000_000, download=8_000_000_000)
        self.assertEqual(r.situacao, av.OK, r.detalhe)
        self.assertEqual((r.medidas["vram_medida"], r.medidas["necessaria"]), (8_300_000_000, 8_300_000_000))
        self.assertIn("inconsistente", r.detalhe)

    def test_necessaria_e_a_maior_das_medidas(self):
        # Parte na CPU: a placa mostra só a fatia da VRAM; o total do /api/ps é o que o modelo precisa.
        medidor = MedidorFalso({"m": {"size": 14 * GB, "size_vram": 10 * GB}}, vram=[12 * GB])
        r = av.bateria_gpu(BackendRoteirizado(ideal, contexto=8192), "m", opcoes(), medidor,
                           vram_antes=2 * GB, download=13 * GB)
        self.assertEqual(r.medidas["necessaria"], 14 * GB)
        self.assertNotIn("inconsistente", r.detalhe)


class TestAvaliarModelo(unittest.TestCase):
    def test_modelo_ideal_passa_em_tudo_e_e_descarregado(self):
        medidor = MedidorFalso({"m": {"size": 4 * GB, "size_vram": 4 * GB, "context_length": 8192}},
                               {"m": 3 * GB})
        backend = BackendRoteirizado(ideal, contexto=8192)
        a = av.avaliar_modelo(backend, "m", opcoes(), medidor)
        situacoes = {nome: r.situacao for nome, r in a.resultados.items()}
        self.assertEqual(situacoes, dict.fromkeys(av.BATERIAS, av.OK))
        self.assertEqual(a.tamanho_download, 3 * GB)
        self.assertIsNotNone(a.carregamento)
        self.assertEqual(medidor.descarregados, ["m"])

    def test_download_de_modelo_sem_etiqueta_entra_na_avaliacao(self):
        # O `ollama list` mostra "m:latest"; o usuário pediu "m".
        medidor = MedidorFalso({"m": {"size": 4 * GB, "size_vram": 4 * GB}}, {"m:latest": 3 * GB})
        a = av.avaliar_modelo(BackendRoteirizado(ideal, contexto=8192), "m", opcoes(), medidor)
        self.assertEqual(a.tamanho_download, 3 * GB)

    def test_vram_e_medida_antes_de_carregar_e_depois_das_baterias(self):
        medidor = MedidorFalso({"m": {"size": 4 * GB, "size_vram": 4 * GB}}, {"m": 3 * GB},
                               vram=[2 * GB, 6 * GB])
        a = av.avaliar_modelo(BackendRoteirizado(ideal, contexto=8192), "m", opcoes(), medidor)
        self.assertEqual(a.resultados["gpu"].medidas["vram_medida"], 4 * GB)

    def test_modelo_ausente_nao_chama_o_motor(self):
        backend = BackendRoteirizado(ideal)
        a = av.avaliar_modelo(backend, "outro", opcoes())
        self.assertEqual(a.resultados["capacidades"].situacao, av.FALHA)
        self.assertIn("não está instalado", a.resultados["capacidades"].detalhe)
        self.assertEqual({a.resultados[b].situacao for b in av.BATERIAS[1:]}, {av.NA})
        self.assertEqual(backend.pedidos, [])

    def test_modelo_de_nuvem_fica_de_fora(self):
        backend = BackendRoteirizado(ideal, modelo=info("x:cloud", nuvem=True))
        a = av.avaliar_modelo(backend, "x:cloud", opcoes())
        self.assertIn("nuvem", a.resultados["capacidades"].detalhe)
        self.assertEqual(backend.pedidos, [])

    def test_modelo_que_nao_conversa_fica_de_fora(self):
        modelo = InfoModelo("e", "e", False, False, False, False, None, conversa=False)
        backend = BackendRoteirizado(ideal, modelo=modelo)
        a = av.avaliar_modelo(backend, "e", opcoes())
        self.assertEqual(a.resultados["capacidades"].situacao, av.NA)
        self.assertIn("não conversa", a.resultados["capacidades"].detalhe)
        self.assertEqual({a.resultados[b].situacao for b in av.BATERIAS[1:]}, {av.NA})
        self.assertEqual(backend.pedidos, [])

    def test_falha_ao_carregar_pula_as_baterias(self):
        def explode(p):
            raise ErroBackend("sem memória para carregar o modelo")

        backend = BackendRoteirizado(explode)
        a = av.avaliar_modelo(backend, "m", opcoes())
        self.assertEqual(len(backend.pedidos), 1)
        for bateria in ("nativo", "cercas", "ptbr", "raciocinio"):
            self.assertEqual(a.resultados[bateria].situacao, av.FALHA)
            self.assertIn("sem memória", a.resultados[bateria].detalhe)

    def test_sem_ferramentas_nativas_pula_so_a_bateria_nativa(self):
        backend = BackendRoteirizado(ideal, modelo=info(ferramentas=False))
        a = av.avaliar_modelo(backend, "m", opcoes())
        self.assertEqual((a.resultados["nativo"].situacao, a.resultados["cercas"].situacao), (av.NA, av.OK))
        self.assertTrue(all(p.ferramentas == [] for p in backend.pedidos))


def avaliacao(modelo: str, download_gb: float, memoria_gb: float, falhas: tuple = (),
              necessaria_gb: float | None = None) -> av.Avaliacao:
    resultados = {b: av.Resultado(av.FALHA if b in falhas else av.OK) for b in av.BATERIAS}
    necessaria = int((necessaria_gb if necessaria_gb is not None else memoria_gb) * GB)
    resultados["gpu"] = av.Resultado(av.OK, medidas={"tamanho": int(memoria_gb * GB), "fracao": 1.0,
                                                     "necessaria": necessaria})
    return av.Avaliacao(modelo, info(modelo), resultados, 1.0, int(download_gb * GB))


class TestRecomendacao(unittest.TestCase):
    def test_melhor_por_faixa(self):
        avaliacoes = [
            avaliacao("pequeno", 2.5, 3.5),
            avaliacao("pequeno-maior", 3.3, 4.5),
            avaliacao("pequeno-sem-cercas", 4.0, 5.0, falhas=("cercas",)),
            avaliacao("medio", 6.5, 6.9),
            avaliacao("grande", 8.0, 10.0),
            avaliacao("enorme", 13.8, 15.0),
            avaliacao("sem-nativo", 1.0, 2.0, falhas=("nativo",)),
        ]
        escolhas = {rotulo: (a.modelo if a else None) for rotulo, a in av.recomendar(avaliacoes)}
        self.assertEqual(list(escolhas.values()), ["pequeno-maior", "medio", "grande"])

    def test_faixa_de_gpu_usa_a_memoria_necessaria_e_nao_o_ps(self):
        escolhas = av.recomendar([avaliacao("ps-subestima", 8.0, 1.1, necessaria_gb=8.3),
                                  avaliacao("medio", 6.5, 6.9)])
        gpu8, gpu12 = escolhas[1][1], escolhas[2][1]
        self.assertEqual((gpu8.modelo, gpu12.modelo), ("medio", "ps-subestima"))

    def test_sem_aprovado_fica_vazio(self):
        escolhas = av.recomendar([avaliacao("ruim", 2.0, 3.0, falhas=("ptbr",))])
        self.assertEqual([a for _, a in escolhas], [None, None, None])


class TestRelatorio(unittest.TestCase):
    def cabecalho(self) -> dict:
        return {"data": "2026-10-23", "backend": "ollama", "url": "http://127.0.0.1:11434",
                "versao": "0.30.10", "contexto": 16384, "temperatura": 0.3, "prazo": 180.0,
                "maquina": "RAM 63 GB, VRAM 12 GB (RTX)"}

    def test_uma_linha_por_modelo_e_o_resumo(self):
        a = av.avaliar_modelo(BackendRoteirizado(ideal, contexto=8192), "m", opcoes(),
                              MedidorFalso({"m": {"size": 4 * GB, "size_vram": 4 * GB}}, {"m": 3 * GB}))
        b = av.avaliar_modelo(BackendRoteirizado(ideal), "ausente", opcoes())
        texto = av.relatorio([a, b], self.cabecalho())
        linhas_tabela = [l for l in texto.splitlines() if l.startswith("|")]
        self.assertEqual(len(linhas_tabela), 4)  # cabeçalho, separador e dois modelos
        self.assertTrue(linhas_tabela[2].startswith("| m |"))
        self.assertTrue(linhas_tabela[3].startswith("| ausente |"))
        for trecho in ("2026-10-23", "16384", "0.30.10", "Recomendado para", "não é uma avaliação jurídica"):
            self.assertIn(trecho, texto)

    def test_texto_do_modelo_nao_quebra_a_tabela_nem_os_detalhes(self):
        r = {b: av.Resultado(av.OK) for b in av.BATERIAS}
        r["ptbr"] = av.Resultado(av.FALHA, "texto com | barra\ne quebra", {"texto": "a | b\n| c |"})
        texto = av.relatorio([av.Avaliacao("modelo|estranho", info(), r)], self.cabecalho())
        linha = next(l for l in texto.splitlines() if l.startswith("| modelo"))
        self.assertEqual(linha.count("|") - linha.count("\\|"), len(av.COLUNAS) + 1)
        self.assertIn("texto com | barra e quebra", texto)  # detalhe numa linha só
        self.assertEqual([l for l in texto.splitlines() if l.startswith("|")][2:], [linha])


class TestMedidorOllama(unittest.TestCase):
    """Contra o servidor falso local dos testes do backend (sem rede de verdade)."""

    def test_le_versao_tamanhos_memoria_e_descarrega(self):
        rotas = {
            ("GET", "/api/version"): (200, "application/json", [b'{"version":"0.30.10"}']),
            ("GET", "/api/tags"): (200, "application/json", [json.dumps({"models": [
                {"name": "qwen3.5:4b", "size": 3_300_000_000}, {"name": "sem-tamanho"}]}).encode()]),
            ("GET", "/api/ps"): (200, "application/json", [json.dumps({"models": [
                {"name": "qwen3.5:4b", "model": "qwen3.5:4b", "size": 5_000_000_000,
                 "size_vram": 5_000_000_000, "context_length": 16384}]}).encode()]),
            ("POST", "/api/generate"): (200, "application/json", [b'{"done":true}']),
        }
        with ServidorFalso(rotas) as srv:
            medidor = av.MedidorOllama(srv.url, espera_descarga=0)
            self.assertEqual(medidor.versao(), "0.30.10")
            self.assertEqual(medidor.tamanhos(), {"qwen3.5:4b": 3_300_000_000})
            self.assertEqual(medidor.memoria("qwen3.5:4b")["size_vram"], 5_000_000_000)
            self.assertIsNone(medidor.memoria("outro"))
            medidor.descarregar("qwen3.5:4b")
            corpos = [p[2] for p in srv.pedidos if p[:2] == ("POST", "/api/generate")]
        self.assertEqual(corpos, [{"model": "qwen3.5:4b", "keep_alive": 0}])

    def test_modelo_sem_etiqueta_casa_com_latest(self):
        # `--modelos gemma3` e o Ollama listando "gemma3:latest": sem normalizar, a memória e o
        # download sumiam, e o `descarregar` não esperava o modelo sair da placa.
        rotas = {
            ("GET", "/api/tags"): (200, "application/json", [json.dumps({"models": [
                {"name": "gemma3:latest", "size": 3_300_000_000}]}).encode()]),
            ("GET", "/api/ps"): (200, "application/json", [json.dumps({"models": [
                {"name": "gemma3:latest", "model": "gemma3:latest", "size": 5_000_000_000,
                 "size_vram": 5_000_000_000}]}).encode()]),
        }
        with ServidorFalso(rotas) as srv:
            medidor = av.MedidorOllama(srv.url)
            self.assertEqual(medidor.memoria("gemma3")["size"], 5_000_000_000)
            self.assertIsNone(medidor.memoria("gemma3:12b"))
            self.assertEqual(av._tamanho_download(medidor, "gemma3"), 3_300_000_000)
            self.assertEqual(av._tamanho_download(medidor, "gemma3:latest"), 3_300_000_000)

    def test_falha_de_leitura_vira_none(self):
        with ServidorFalso({}) as srv:  # tudo 404
            medidor = av.MedidorOllama(srv.url)
            self.assertIsNone(medidor.versao())
            self.assertEqual(medidor.tamanhos(), {})
            self.assertIsNone(medidor.memoria("m"))
            medidor.descarregar("m")  # não levanta: descarregar é cortesia

    def test_descarregar_espera_o_modelo_sair_com_limite(self):
        ps = {("GET", "/api/ps"): (200, "application/json", [b'{"models":[{"name":"m"}]}']),
              ("POST", "/api/generate"): (200, "application/json", [b'{"done":true}'])}
        with ServidorFalso(ps) as srv:
            inicio = time.monotonic()
            av.MedidorOllama(srv.url, espera_descarga=0.3).descarregar("m")
            self.assertGreaterEqual(time.monotonic() - inicio, 0.3)
            self.assertLess(time.monotonic() - inicio, 3.0)
            self.assertGreater(sum(1 for p in srv.pedidos if p[1] == "/api/ps"), 1)

    def test_vram_usada_soma_as_placas_pelo_nvidia_smi(self):
        saida = mock.Mock(stdout="9034\n1000\n")
        with mock.patch("subprocess.run", return_value=saida) as rodar:
            self.assertEqual(av.MedidorOllama("http://127.0.0.1:11434").vram_usada(), 10034 * 1024**2)
        self.assertIn("--query-gpu=memory.used", rodar.call_args.args[0])
        with mock.patch("subprocess.run", side_effect=FileNotFoundError("nvidia-smi")):
            self.assertIsNone(av.MedidorOllama("http://127.0.0.1:11434").vram_usada())

    def test_ollama_remoto_nao_mede_a_placa_local(self):
        with mock.patch("subprocess.run") as rodar:
            self.assertIsNone(av.MedidorOllama("http://192.168.0.10:11434").vram_usada())
        rodar.assert_not_called()


class TestLinhaDeComando(unittest.TestCase):
    def rodar(self, *argv: str) -> tuple[int, str, str]:
        saida, erros = io.StringIO(), io.StringIO()
        with redirect_stdout(saida), redirect_stderr(erros), \
                mock.patch.object(av, "detectar", return_value=Hardware(16, None, None)):
            codigo = av.main(list(argv))
        return codigo, saida.getvalue(), erros.getvalue()

    def test_modelos_separados_por_virgula(self):
        self.assertEqual(av.lista_modelos(" a, b,,c "), ["a", "b", "c"])

    def test_simulado_grava_o_relatorio(self):
        with tempfile.TemporaryDirectory() as pasta:
            destino = os.path.join(pasta, "sub", "avaliacao.md")
            codigo, saida, erros = self.rodar("--simulado", "--modelos", "simulado", "--prazo", "5",
                                              "--saida", destino)
            self.assertEqual(codigo, 0, erros)
            with open(destino, encoding="utf-8") as f:
                gravado = f.read()
        self.assertIn("| simulado |", gravado)
        self.assertIn("| simulado |", saida)
        self.assertIn("simulado", erros)  # o progresso vai para o stderr

    def test_motor_fora_do_ar_sai_com_1(self):
        class ForaDoAr(BackendRoteirizado):
            def listar_modelos(self):
                raise ErroBackend("Não consegui falar com o Ollama.")

        with mock.patch.object(av, "criar_backend", return_value=ForaDoAr(ideal)):
            codigo, _, erros = self.rodar("--modelos", "m")
        self.assertEqual(codigo, 1)
        self.assertIn("Não consegui falar com o Ollama", erros)

    def test_sem_modelos_sai_com_2(self):
        with self.assertRaises(SystemExit) as saida:
            self.rodar("--modelos", " , ")
        self.assertEqual(saida.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
