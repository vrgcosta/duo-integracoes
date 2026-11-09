"""Compactação: o resumo no lugar dos turnos antigos, sem apagar o original, e como desfazer.

A compactação é assíncrona (`POST` só inicia), então o que importa nos testes é o que se vê pelo
`GET /janela` e no pedido seguinte ao backend. Nada aqui usa o Ollama de verdade.
"""

import json
import logging
import re
import threading
import time
import unittest
from unittest import mock

from duo_provedor import compactacao, historico
from duo_provedor.backends.base import (ChamadaFerramenta, Cortada, ErroAcimaDaJanela, ErroBackend, InfoModelo,
                                        Raciocinio, Texto, Uso)
from duo_provedor.backends.simulado import RESUMO_SIMULADO, BackendSimulado
from duo_provedor.compactacao import agrupar, transcrever
from duo_provedor.ferramentas import LEMBRETE_RODADA_VAZIA, para_funcoes
from duo_provedor.janela import estimar_tokens
from testes.cliente_duo import ClienteDuo, ErroCliente
from testes.util import BackendGravador, ServidorDeTeste

LISTAR = {"name": "listar_documentos", "description": "Lista os documentos do processo.",
          "input_schema": {"type": "object", "properties": {}}}
MODELO = "gravador"
RESUMO = "Processo 0001234-56.2024.8.99.0001: o usuário pediu a análise; a petição é o documento 77."
UUID_INEXISTENTE = "11111111-2222-4333-8444-555555555555"


def eh_resumo(pedido) -> bool:
    return pedido.mensagens[0]["texto"] == compactacao.INSTRUCAO


class BackendResumidor(BackendGravador):
    """Responde os turnos pelo roteiro e cada passada de resumo por `resumir(pedido)`."""

    def __init__(self, roteiro=None, resumir=None, modelos=None) -> None:
        super().__init__(modelos=modelos)
        self.roteiro_turnos = list(roteiro or [])
        self.resumir = resumir or (lambda pedido: [Texto(RESUMO)])
        self.resumos = []
        self.turnos = []

    def conversar(self, pedido, cancelado):
        self.pedidos.append(pedido)
        if eh_resumo(pedido):
            self.resumos.append(pedido)
            yield from self.resumir(pedido)
            return
        self.turnos.append(pedido)
        indice = len(self.turnos) - 1
        yield from self.roteiro_turnos[indice] if indice < len(self.roteiro_turnos) else [Texto("ok")]


class Base(unittest.TestCase):
    def _subir(self, backend=None, cfg_extra=None, roteiro=None) -> None:
        self.backend = backend or BackendResumidor(roteiro)
        self.srv = ServidorDeTeste(self.backend, {"modelo_padrao": MODELO, **(cfg_extra or {})}).__enter__()
        self.addCleanup(self.srv.__exit__, None, None, None)
        self.cliente = ClienteDuo(self.srv.url, self.srv.blueprint)
        self.cid = self.cliente.criar_conversa()
        self.arm = self.srv.servico.armazenamento

    def _folha(self) -> str:
        nos = self.arm.ler_conversa(self.cid)["chat_messages"]
        return nos[-1]["uuid"] if nos else self.cliente.raiz

    def _enviar(self, prompt, resultado="x", **extras):
        return self.cliente.enviar(self.cid, prompt, self._folha(), [LISTAR], lambda nome, entrada: resultado,
                                   extras=extras)

    def _tres_turnos(self) -> None:
        self._subir(roteiro=[[ChamadaFerramenta("listar_documentos", {})], [Texto("resposta um")],
                             [Texto("resposta dois")], [Texto("resposta três")]])
        for pergunta, resultado in (("pergunta um", "resultado um " * 300), ("pergunta dois", "x"),
                                    ("pergunta três", "x")):
            self.assertIsNone(self._enviar(pergunta, resultado).erro)

    def _esperar(self, prazo: float = 5) -> dict:
        """Como o Duo: lê a janela até a compactação deixar de rodar."""
        limite = time.monotonic() + prazo
        while True:
            janela = self.cliente.janela(self.cid)
            if (janela["compactacao"] or {}).get("estado") != "rodando":
                return janela
            self.assertLess(time.monotonic(), limite, "a compactação não terminou")
            time.sleep(0.01)

    def _esperar_fim_no_servidor(self) -> None:
        """Até a thread terminar, sem ler a janela: quem lê fica sabendo do desfecho, e aqui ninguém leu ainda."""
        limite = time.monotonic() + 5
        while any(j.rodando for j in self.srv.servico.compactacoes.values()):
            self.assertLess(time.monotonic(), limite, "a compactação não terminou")
            time.sleep(0.01)

    def _compactar_e_esperar(self) -> dict:
        self.assertEqual(self.cliente.compactar(self.cid), {"estado": "rodando"})
        return self._esperar()

    def _falhar_e_esperar(self) -> dict:
        """Compacta, com a falha esperada: o motivo também vai ao log."""
        with self.assertLogs("duo_provedor.compactacao", "WARNING"):
            return self._compactar_e_esperar()

    def _status_compactar(self, corpo=None) -> tuple[int, object]:
        return self.srv.pedir("POST", f"/api/conversas/{self.cid}/compactar", corpo or {})


class TestFluxo(Base):
    def test_inicia_com_202_e_o_estado_vem_na_janela(self):
        comecou, soltar = threading.Event(), threading.Event()
        self.addCleanup(soltar.set)

        def resumir(pedido):
            comecou.set()
            soltar.wait(5)
            return [Texto(RESUMO)]

        self._subir(BackendResumidor(resumir=resumir))
        self._enviar("um" * 600)
        self._enviar("dois")
        status, corpo = self._status_compactar({"modelo": MODELO})
        self.assertEqual((status, corpo), (202, {"estado": "rodando"}))
        self.assertTrue(comecou.wait(5))

        janela = self.cliente.janela(self.cid)
        self.assertEqual(janela["compactacao"]["estado"], "rodando")
        self.assertRegex(janela["compactacao"]["inicio"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
        self.assertIsNone(janela["resumo"])

        soltar.set()
        janela = self._esperar()
        self.assertIsNone(janela["compactacao"])
        resumo = janela["resumo"]
        self.assertEqual(resumo["texto"], RESUMO)
        self.assertEqual(resumo["modelo"], MODELO)
        self.assertLess(resumo["tokens_depois"], resumo["tokens_antes"])
        nos = self.arm.ler_conversa(self.cid)["chat_messages"]
        self.assertEqual(resumo["ate"], nos[1]["uuid"])  # o último nó antes do último turno

    def test_passada_de_resumo_sem_ferramentas_sem_raciocinio_e_so_com_os_turnos_anteriores(self):
        self._tres_turnos()
        self.arm.atualizar_conversa(self.cid, pensamento=True)
        self._compactar_e_esperar()
        (pedido,) = self.backend.resumos
        self.assertEqual(pedido.ferramentas, [])
        self.assertIs(pedido.pensar, False)
        self.assertEqual(pedido.modelo, MODELO)
        entrada = pedido.mensagens[1]["texto"]
        for texto in ("pergunta um", "resposta um", "resultado um", "pergunta dois", "resposta dois"):
            self.assertIn(texto, entrada)
        for texto in ("pergunta três", "resposta três"):
            self.assertNotIn(texto, entrada)

    def test_instrucao_pede_o_que_a_spec_manda_preservar(self):
        for trecho in ("número do processo", "IDs de documentos", "datas", "nomes", "pedidos", "[dúvida]",
                       "Não acrescente nada"):
            self.assertIn(trecho, compactacao.INSTRUCAO)

    def test_leitura_que_pega_o_fim_da_compactacao_nunca_fica_sem_andamento_e_sem_resumo(self):
        # A compactação termina no meio da leitura da janela, depois de ela ler o estado. Se o andamento fosse lido
        # depois, sairia uma resposta sem os dois, e o Duo pararia de acompanhar sem ver o resultado.
        comecou, soltar = threading.Event(), threading.Event()
        self.addCleanup(soltar.set)

        def resumir(pedido):
            comecou.set()
            soltar.wait(5)
            return [Texto(RESUMO)]

        self._subir(BackendResumidor(resumir=resumir))
        self._enviar("um" * 600)
        self._enviar("dois")
        self.cliente.compactar(self.cid)
        self.assertTrue(comecou.wait(5))
        original = self.arm.estado_janela

        def estado_e_termina(cid):
            estado = original(cid)
            soltar.set()
            self._esperar_fim_no_servidor()
            return estado

        with mock.patch.object(self.arm, "estado_janela", estado_e_termina):
            janela = self.cliente.janela(self.cid)
        self.assertEqual(janela["compactacao"]["estado"], "rodando")
        self.assertEqual(self.cliente.janela(self.cid)["resumo"]["texto"], RESUMO)

    def test_raciocinio_nao_entra_no_resumo(self):
        self._subir(BackendResumidor(resumir=lambda p: [Raciocinio("pensando alto"), Texto(RESUMO), Uso(10, 5)]))
        self._enviar("um" * 600)
        self._enviar("dois")
        self.assertEqual(self._compactar_e_esperar()["resumo"]["texto"], RESUMO)

    def test_proximo_envio_usa_o_resumo_no_lugar_dos_turnos(self):
        self._tres_turnos()
        self._compactar_e_esperar()
        self.assertIsNone(self._enviar("pergunta quatro").erro)
        mensagens = self.backend.turnos[-1].mensagens
        enviado = json.dumps(mensagens, ensure_ascii=False)
        self.assertEqual(mensagens[1]["papel"], "usuario")
        self.assertEqual(mensagens[1]["texto"], f"{historico.MARCA_RESUMO}\n\n{RESUMO}")
        self.assertIn("gerado pelo modelo; confira nos autos antes de citar", mensagens[1]["texto"])
        for texto in ("pergunta um", "resposta um", "resultado um", "pergunta dois", "resposta dois"):
            self.assertNotIn(texto, enviado)
        for texto in ("pergunta três", "resposta três", "pergunta quatro"):
            self.assertIn(texto, enviado)

    def test_resumo_vale_nas_cercas(self):
        self._subir(roteiro=[[Texto("resposta um")], [Texto("resposta dois")], [Texto("resposta três")]])
        extras = {"opcoes": {"ferramentas": "cercas"}}
        self._enviar("pergunta um" * 100, **extras)
        self._enviar("pergunta dois", **extras)
        self._compactar_e_esperar()
        self._enviar("pergunta três", **extras)
        enviado = json.dumps(self.backend.turnos[-1].mensagens, ensure_ascii=False)
        self.assertIn(RESUMO, enviado)
        self.assertNotIn("pergunta um", enviado)

    def test_arvore_e_releitura_do_duo_ficam_intactas(self):
        self._tres_turnos()
        antes = self.srv.pedir("GET", f"/api/conversas/{self.cid}")[1]
        self._compactar_e_esperar()
        depois = self.srv.pedir("GET", f"/api/conversas/{self.cid}")[1]
        self.assertEqual(depois["chat_messages"], antes["chat_messages"])
        self.assertNotIn(RESUMO, json.dumps(depois, ensure_ascii=False))

    def test_depois_de_compactar_o_modelo_so_conhece_o_trecho_pelo_resumo(self):
        # Sem marca do trecho na janela, a leitura não é oferecida, e o id do resultado não aparece no pedido: ler o
        # original é para quem desfaz o resumo, e não para o modelo.
        self._tres_turnos()
        r1 = next(b["tool_use_id"] for n in self.arm.ler_conversa(self.cid)["chat_messages"]
                  for b in n["content"] if b.get("type") == "tool_result")
        self._compactar_e_esperar()
        self.assertIsNone(self._enviar("pergunta quatro").erro)
        pedido = self.backend.turnos[-1]
        self.assertNotIn("ler_trecho_resultado", [f["name"] for f in pedido.ferramentas])
        self.assertNotIn(r1, json.dumps(pedido.mensagens, ensure_ascii=False))

    def test_leitura_de_um_id_que_o_modelo_ja_tinha_visto_ainda_responde(self):
        # Não é um caminho oferecido, mas também não é erro: ela só lê o que já é da conversa.
        self._tres_turnos()
        r1 = next(b["tool_use_id"] for n in self.arm.ler_conversa(self.cid)["chat_messages"]
                  for b in n["content"] if b.get("type") == "tool_result")
        self._compactar_e_esperar()
        self.backend.roteiro_turnos.extend([[ChamadaFerramenta("ler_trecho_resultado", {"id": r1, "inicio": 0})],
                                            [Texto("reli")]])
        self.assertIsNone(self._enviar("releia").erro)
        self.assertIn("resultado um", json.dumps(self.backend.turnos[-1].mensagens, ensure_ascii=False))


class TestJanelaComResumo(Base):
    def test_janela_conta_o_resumo_e_nao_lista_o_que_ele_substituiu(self):
        self._tres_turnos()
        antes = self.cliente.janela(self.cid)
        depois = self._compactar_e_esperar()
        self.assertLess(depois["usado"], antes["usado"])
        self.assertEqual(sum(depois["secoes"].values()), depois["usado"])
        turnos = [i for i in depois["itens"] if i["tipo"] == "turno"]
        self.assertEqual([t["rotulo"] for t in turnos], ["pergunta três"])
        self.assertFalse([i for i in depois["itens"] if i["tipo"] == "resultado"])
        self.assertEqual(self.cliente.janela(self.cid, contexto=8192)["resumo"]["texto"], RESUMO)

    def test_soma_igual_ao_proximo_envio_tambem_com_resumo(self):
        self._tres_turnos()
        janela = self._compactar_e_esperar()
        self._enviar("")
        pedido = self.backend.turnos[-1]
        peso = (sum(estimar_tokens(m["texto"]) for m in pedido.mensagens)
                + estimar_tokens(json.dumps(para_funcoes(pedido.ferramentas), ensure_ascii=False)))
        self.assertEqual(janela["usado"], peso)

    def _compactada_com_system(self, resumo: str = RESUMO) -> None:
        """Com as instruções do Duo, nada fica fixado: o resumo está sujeito ao corte como os turnos."""
        self._subir(BackendResumidor(resumir=lambda p: [Texto(resumo)]))
        self._enviar("pergunta um " * 2000, system="Regras.", opcoes={"contexto": 16384})
        self._enviar("pergunta dois", system="Regras.", opcoes={"contexto": 16384})
        self._compactar_e_esperar()

    def test_resumo_sai_depois_dos_outros_turnos_anteriores(self):
        # Era o primeiro a sair justamente na janela apertada em que se compacta: quem compactou quer manter aquilo.
        self._compactada_com_system()
        grande = {"system": "Regras.", "opcoes": {"contexto": 8192}}
        self.assertIsNone(self._enviar("texto três " * 1300, **grande).erro)
        self.assertIsNone(self._enviar("texto quatro " * 1300, **grande).erro)
        enviado = json.dumps(self.backend.turnos[-1].mensagens, ensure_ascii=False)
        self.assertIn(RESUMO, enviado)
        self.assertNotIn("pergunta dois", enviado)
        self.assertNotIn("texto três", enviado)
        janela = self.cliente.janela(self.cid, contexto=8192)
        self.assertIs(janela["resumo"]["incluido"], True)
        self.assertNotIn("motivo", janela["resumo"])

    def test_janela_diz_quando_o_resumo_saiu(self):
        # O anti-objetivo "resumo silencioso" da spec: o painel mostrava o resumo como valendo, sem ele ir ao modelo.
        grande = "Resumo muito grande para esta janela. " * 500
        self._compactada_com_system(grande)
        janela = self.cliente.janela(self.cid, contexto=4096)
        self.assertIs(janela["resumo"]["incluido"], False)
        self.assertEqual(janela["resumo"]["motivo"], "corte")
        self.assertEqual(janela["resumo"]["texto"], grande.strip())
        resposta = self._enviar("", system="Regras.", opcoes={"contexto": 4096})
        self.assertNotIn("Resumo muito grande", json.dumps(self.backend.turnos[-1].mensagens, ensure_ascii=False))
        self.assertIn("O resumo da conversa ficou fora da janela", resposta.texto)

    def test_com_a_janela_folgada_o_resumo_vale(self):
        self._compactada_com_system()
        self.assertIs(self.cliente.janela(self.cid)["resumo"]["incluido"], True)

    def test_sem_system_o_resumo_fica_fixado_e_o_erro_aponta_para_ele(self):
        # Sem as instruções do Duo, o 1º turno é fixado, e o resumo é o 1º turno: ele não sai, e pode ser a causa.
        class Contador(BackendResumidor):
            contar = staticmethod(lambda pedido: 10)

            def contar_tokens(self, pedido, cancelado=None):
                return self.contar(pedido)

        grande = "Resumo muito grande para esta janela. " * 500
        self._subir(Contador(resumir=lambda p: [Texto(grande)]))
        self._enviar("pergunta um " * 2000, opcoes={"contexto": 16384})
        self._enviar("pergunta dois", opcoes={"contexto": 16384})
        self._compactar_e_esperar()
        self.backend.contar = staticmethod(lambda pedido: 9000)
        with self.assertLogs("duo_provedor.turno", "WARNING"):
            resposta = self._enviar("curta", opcoes={"contexto": 8192})
        self.assertIn("O resumo da conversa ocupa", resposta.erro)

    def test_desfazer_volta_aos_turnos_originais(self):
        self._tres_turnos()
        self._compactar_e_esperar()
        self.assertEqual(self.cliente.descompactar(self.cid), {"ok": True})
        janela = self.cliente.janela(self.cid)
        self.assertIsNone(janela["resumo"])
        self.assertEqual(len([i for i in janela["itens"] if i["tipo"] == "turno"]), 3)
        self._enviar("pergunta quatro")
        enviado = json.dumps(self.backend.turnos[-1].mensagens, ensure_ascii=False)
        self.assertIn("pergunta um", enviado)
        self.assertNotIn(RESUMO, enviado)

    def test_desfazer_sem_resumo_e_inofensivo(self):
        self._subir()
        self.assertEqual(self.cliente.descompactar(self.cid), {"ok": True})
        self.assertEqual(self.srv.pedir("DELETE", f"/api/conversas/{UUID_INEXISTENTE}/compactar")[0], 404)

    def test_em_outro_ramo_o_resumo_nao_vale(self):
        self._tres_turnos()
        nos = self.arm.ler_conversa(self.cid)["chat_messages"]
        self._compactar_e_esperar()
        # O usuário volta ao 1º pedido e segue por outro caminho: o trecho resumido não faz parte deste ramo.
        ramo_novo = nos[1]["uuid"]
        resposta = self.cliente.enviar(self.cid, "outra pergunta", ramo_novo, [LISTAR])
        self.assertIsNone(resposta.erro)
        enviado = json.dumps(self.backend.turnos[-1].mensagens, ensure_ascii=False)
        self.assertNotIn(RESUMO, enviado)
        self.assertIn("pergunta um", enviado)
        self.assertIsNone(self.cliente.janela(self.cid)["resumo"])

    def test_compactar_de_novo_resume_o_resumo_com_os_turnos_novos(self):
        self._tres_turnos()
        self._compactar_e_esperar()
        self._enviar("pergunta quatro")
        self.backend.resumir = lambda p: [Texto("Segundo resumo.")]
        janela = self._compactar_e_esperar()
        self.assertEqual(janela["resumo"]["texto"], "Segundo resumo.")
        entrada = self.backend.resumos[-1].mensagens[1]["texto"]
        self.assertIn(RESUMO, entrada)
        self.assertIn(historico.MARCA_RESUMO, entrada)
        self.assertIn("pergunta três", entrada)
        self.assertNotIn("pergunta um", entrada)
        self.assertNotIn("pergunta quatro", entrada)

    def test_sem_turno_novo_nao_ha_o_que_compactar_de_novo(self):
        self._tres_turnos()
        self._compactar_e_esperar()
        status, corpo = self._status_compactar()
        self.assertEqual(status, 400)
        self.assertIn("Não há turnos", corpo["error"]["message"])
        self.assertEqual(self.cliente.janela(self.cid)["resumo"]["texto"], RESUMO)


class TestExclusoesComResumo(Base):
    """O painel manda a lista montada com o que vê em `itens`: o que ele não vê não pode cair por estar fora dela."""

    def _tres_com_resultado(self) -> tuple[str, str, str]:
        leitura = [ChamadaFerramenta("listar_documentos", {})]
        self._subir(roteiro=[leitura, [Texto("r1")], leitura, [Texto("r2")], leitura, [Texto("r3")]])
        for n in ("um", "dois", "três"):
            self.assertIsNone(self._enviar(f"pergunta {n}", f"resultado {n} " * 100).erro)
        return tuple(b["tool_use_id"] for no in self.arm.ler_conversa(self.cid)["chat_messages"]
                     for b in no["content"] if b.get("type") == "tool_result")

    def test_o_que_o_resumo_escondeu_nao_volta_ao_desfazer(self):
        # O caso reproduzido na revisão: tirar, compactar (o item some de `itens`), tirar outro, desfazer.
        r1, _, r3 = self._tres_com_resultado()
        self.cliente.janela_excluir(self.cid, [r1])
        janela = self._compactar_e_esperar()
        self.assertNotIn(r1, [i["id"] for i in janela["itens"]])
        visiveis_tirados = [i["id"] for i in janela["itens"] if i.get("motivo") == "usuario"]
        self.cliente.janela_excluir(self.cid, visiveis_tirados + [r3])
        self.cliente.descompactar(self.cid)

        fora = next(i for i in self.cliente.janela(self.cid)["itens"] if i["id"] == r1)
        self.assertEqual((fora["incluido"], fora["motivo"]), (False, "usuario"))
        self._enviar("pergunta quatro")
        self.assertNotIn("resultado um", json.dumps(self.backend.turnos[-1].mensagens, ensure_ascii=False))

    def test_lista_vazia_devolve_o_que_esta_visivel_e_so_isso(self):
        r1, _, r3 = self._tres_com_resultado()
        self.cliente.janela_excluir(self.cid, [r1, r3])
        self._compactar_e_esperar()
        self.cliente.janela_excluir(self.cid, [])
        self.assertEqual(self.arm.estado_janela(self.cid)["excluidos"], [r1])

    def test_item_de_outro_ramo_tambem(self):
        r1, _, _ = self._tres_com_resultado()
        nos = self.arm.ler_conversa(self.cid)["chat_messages"]
        self.cliente.janela_excluir(self.cid, [r1])
        # Volta ao começo e segue por outro ramo: o resultado tirado não aparece em `itens` deste.
        self.cliente.enviar(self.cid, "outro caminho", nos[0]["parent_message_uuid"], [LISTAR])
        self.assertNotIn(r1, [i["id"] for i in self.cliente.janela(self.cid)["itens"]])
        self.cliente.janela_excluir(self.cid, [])
        self.assertEqual(self.arm.estado_janela(self.cid)["excluidos"], [r1])

    def test_o_que_esta_visivel_ainda_se_religa(self):
        r1, _, _ = self._tres_com_resultado()
        self.cliente.janela_excluir(self.cid, [r1])
        self.cliente.janela_excluir(self.cid, [])
        self.assertEqual(self.arm.estado_janela(self.cid)["excluidos"], [])

    def test_item_tirado_enquanto_a_compactacao_comeca_nao_vai_ao_resumo(self):
        # A corrida: o PUT chega depois de o plano ler as exclusões e antes de a compactação aparecer como em
        # andamento. Ou ele é recusado, e o usuário sabe, ou o item fica fora: aceitar e resumir não pode.
        comecou, soltar = threading.Event(), threading.Event()
        self.addCleanup(soltar.set)

        def resumir(pedido):
            comecou.set()
            soltar.wait(5)
            return [Texto(RESUMO)]

        r1, _, _ = self._tres_com_resultado()
        self.backend.resumir = resumir
        original = compactacao.planejar
        resposta = {}
        pedidos = []

        def planejar_e_tirar(*args):
            plano = original(*args)
            pedido = threading.Thread(target=lambda: resposta.update(status=self.srv.pedir(
                "PUT", f"/api/conversas/{self.cid}/janela", {"excluidos": [r1]})[0]))
            pedido.start()
            pedido.join(0.3)
            pedidos.append(pedido)
            return plano

        with mock.patch.object(compactacao, "planejar", planejar_e_tirar):
            self.assertEqual(self._status_compactar()[0], 202)
        pedidos[0].join(5)
        self.assertEqual(resposta["status"], 409)
        self.assertEqual(self.arm.estado_janela(self.cid)["excluidos"], [])
        self.assertTrue(comecou.wait(5))
        soltar.set()
        self._esperar()


class TestAte(Base):
    def _nos(self):
        return self.arm.ler_conversa(self.cid)["chat_messages"]

    def test_ate_um_turno_mantem_os_seguintes_inteiros(self):
        self._tres_turnos()
        nos = self._nos()
        humano_um = nos[0]["uuid"]
        self.assertEqual(self._status_compactar({"ate": humano_um})[0], 202)
        janela = self._esperar()
        # O id é o do pedido, mas o turno vai inteiro: parar nele deixaria a resposta sem o começo.
        self.assertEqual(janela["resumo"]["ate"], nos[1]["uuid"])
        entrada = self.backend.resumos[-1].mensagens[1]["texto"]
        self.assertIn("resposta um", entrada)
        self.assertNotIn("pergunta dois", entrada)
        self.assertEqual([i["rotulo"] for i in janela["itens"] if i["tipo"] == "turno"],
                         ["pergunta dois", "pergunta três"])

    def test_ate_invalido(self):
        self._tres_turnos()
        nos = self._nos()
        for corpo in ({"ate": "nao-existe"}, {"ate": nos[-1]["uuid"]}, {"ate": nos[-2]["uuid"]}, {"ate": 5},
                      {"modelo": 5}):
            with self.subTest(corpo=corpo):
                self.assertEqual(self._status_compactar(corpo)[0], 400)
        self.assertEqual(self.backend.resumos, [])

    def test_um_turno_so_nao_tem_o_que_compactar(self):
        self._subir()
        self._enviar("único")
        status, corpo = self._status_compactar()
        self.assertEqual(status, 400)
        self.assertIn("Não há turnos", corpo["error"]["message"])
        self.assertIsNone(self.cliente.janela(self.cid)["compactacao"])

    def test_conversa_vazia_e_inexistente(self):
        self._subir()
        self.assertEqual(self._status_compactar()[0], 400)
        self.assertEqual(self.srv.pedir("POST", f"/api/conversas/{UUID_INEXISTENTE}/compactar", {})[0], 404)


class TestConflitos(Base):
    def test_409_com_turno_aberto(self):
        pronto, soltar = threading.Event(), threading.Event()
        self.addCleanup(soltar.set)

        class Parado(BackendResumidor):
            def conversar(self, pedido, cancelado):
                self.pedidos.append(pedido)
                if len(self.pedidos) > 1:
                    pronto.set()
                    soltar.wait(5)
                yield Texto("ok")

        self._subir(Parado())
        self._enviar("um")
        resultado = {}
        thread = threading.Thread(target=lambda: resultado.update(r=self._enviar("dois")))
        thread.start()
        self.addCleanup(thread.join, 5)
        self.assertTrue(pronto.wait(5))
        self.assertEqual(self._status_compactar()[0], 409)
        self.assertEqual(self.backend.resumos, [])
        soltar.set()
        thread.join(5)
        self.assertIsNone(resultado["r"].erro)

    def _rodando(self):
        comecou, soltar = threading.Event(), threading.Event()
        self.addCleanup(soltar.set)

        def resumir(pedido):
            comecou.set()
            soltar.wait(5)
            return [Texto(RESUMO)]

        self._subir(BackendResumidor(resumir=resumir))
        self._enviar("um" * 600)
        self._enviar("dois")
        self.cliente.compactar(self.cid)
        self.assertTrue(comecou.wait(5))
        return soltar

    def test_409_com_compactacao_em_andamento_e_novo_turno_tambem(self):
        soltar = self._rodando()
        self.assertEqual(self._status_compactar()[0], 409)
        turnos = len(self.backend.turnos)
        resposta = self._enviar("três")
        self.assertTrue(resposta.erro.startswith("HTTP 409"), resposta.erro)
        self.assertEqual(len(self.backend.turnos), turnos)
        self.assertEqual(self.srv.pedir("PUT", f"/api/conversas/{self.cid}/janela", {"excluidos": []})[0], 409)
        soltar.set()
        self._esperar()
        self.assertIsNone(self._enviar("três").erro)

    def test_cancelar_nao_muda_nada_e_libera_a_conversa(self):
        soltar = self._rodando()
        antes = self.arm.ler_conversa(self.cid)
        self.assertEqual(self.cliente.descompactar(self.cid), {"ok": True, "cancelada": True})
        janela = self.cliente.janela(self.cid)
        self.assertIsNone(janela["compactacao"])
        self.assertIsNone(janela["resumo"])
        self.assertIsNone(self._enviar("três").erro)
        self.assertNotIn(RESUMO, json.dumps(self.backend.turnos[-1].mensagens, ensure_ascii=False))
        soltar.set()  # a resposta que chega depois de cancelar é descartada
        time.sleep(0.3)
        self.assertIsNone(self.arm.estado_janela(self.cid)["resumo"])
        self.assertEqual(self.arm.ler_conversa(self.cid)["chat_messages"][:4], antes["chat_messages"])

    def test_cancelar_avisa_o_backend(self):
        comecou, cancelou = threading.Event(), threading.Event()

        class Mudo(BackendResumidor):
            def conversar(self, pedido, cancelado):
                if eh_resumo(pedido):
                    comecou.set()
                    if cancelado.wait(5):
                        cancelou.set()
                    return
                yield Texto("ok")

        self._subir(Mudo())
        self._enviar("um" * 600)
        self._enviar("dois")
        self.cliente.compactar(self.cid)
        self.assertTrue(comecou.wait(5))
        compactacao_aberta = self.srv.servico.compactacoes[self.cid]
        # O motor devolve uma resposta vazia ao ser cancelado: isso não é a falha "resumo vazio".
        with self.assertNoLogs("duo_provedor.compactacao", "WARNING"):
            self.cliente.descompactar(self.cid)
            self.assertTrue(cancelou.wait(5))
            compactacao_aberta.aguardar(5)

    def test_cancelar_guarda_o_resumo_anterior(self):
        self._subir()
        self._enviar("um" * 600)
        self._enviar("dois")
        self._compactar_e_esperar()
        self._enviar("três" * 600)
        comecou, soltar = threading.Event(), threading.Event()
        self.addCleanup(soltar.set)

        def resumir(pedido):
            comecou.set()
            soltar.wait(5)
            return [Texto("novo")]

        self.backend.resumir = resumir
        self.cliente.compactar(self.cid)
        self.assertTrue(comecou.wait(5))
        self.cliente.descompactar(self.cid)
        self.assertEqual(self.cliente.janela(self.cid)["resumo"]["texto"], RESUMO)

    # -- o mesmo verbo cancela e desfaz: o cancelamento que chega tarde não pode virar um desfazer ---------

    def test_cancelar_logo_depois_de_gravar_nao_apaga_o_resumo_novo(self):
        self._subir()
        self._enviar("um" * 600)
        self._enviar("dois")
        self.cliente.compactar(self.cid)
        self._esperar_fim_no_servidor()
        self.assertEqual(self.cliente.descompactar(self.cid), {"ok": True, "cancelada": False, "terminou_antes": True})
        self.assertEqual(self.cliente.janela(self.cid)["resumo"]["texto"], RESUMO)

    def test_cancelar_logo_depois_de_falhar_nao_apaga_o_resumo_anterior(self):
        def falhar(pedido):
            raise ErroBackend("falhou")

        self._subir()
        self._enviar("um" * 600)
        self._enviar("dois")
        self._compactar_e_esperar()
        self._enviar("três" * 600)
        self.backend.resumir = falhar
        with self.assertLogs("duo_provedor.compactacao", "WARNING"):
            self.cliente.compactar(self.cid)
            self._esperar_fim_no_servidor()
        self.assertEqual(self.cliente.descompactar(self.cid), {"ok": True, "cancelada": False, "terminou_antes": True})
        janela = self.cliente.janela(self.cid)
        self.assertEqual(janela["resumo"]["texto"], RESUMO)
        self.assertIsNone(janela["compactacao"])

    def test_depois_de_ver_o_resumo_o_mesmo_delete_desfaz(self):
        self._subir()
        self._enviar("um" * 600)
        self._enviar("dois")
        self.cliente.compactar(self.cid)
        self._esperar_fim_no_servidor()
        self.assertEqual(self._esperar()["resumo"]["texto"], RESUMO)
        self.assertEqual(self.cliente.descompactar(self.cid), {"ok": True})
        self.assertIsNone(self.cliente.janela(self.cid)["resumo"])

    def test_a_compactacao_que_ninguem_viu_terminar_nao_prende_a_conversa(self):
        self._subir()
        self._enviar("um" * 600)
        self._enviar("dois")
        self.cliente.compactar(self.cid)
        self._esperar_fim_no_servidor()
        self.assertIsNone(self._enviar("três" * 600).erro)
        self.assertEqual(self.srv.pedir("PUT", f"/api/conversas/{self.cid}/janela", {"excluidos": []})[0], 200)
        self.assertEqual(self._status_compactar()[0], 202)
        self._esperar()

    def test_o_texto_dos_autos_nao_fica_na_memoria_depois_de_terminar(self):
        def falhar(pedido):
            raise ErroBackend("falhou")

        self._subir(BackendResumidor(resumir=falhar))
        self._enviar("um" * 600)
        self._enviar("dois")
        with self.assertLogs("duo_provedor.compactacao", "WARNING"):
            self.cliente.compactar(self.cid)
            self._esperar_fim_no_servidor()
        self.assertEqual(self.srv.servico.compactacoes[self.cid].plano.partes, [])

    def test_excluir_a_conversa_cancela_a_compactacao(self):
        soltar = self._rodando()
        self.assertEqual(self.srv.pedir("DELETE", f"/api/conversas/{self.cid}")[0], 200)
        self.assertEqual(self.srv.servico.compactacoes, {})
        soltar.set()
        time.sleep(0.2)
        self.assertEqual(self.srv.pedir("GET", f"/api/conversas/{self.cid}/janela")[0], 404)
        with self.arm._leitura() as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM resumos").fetchone(), (0,))

    def test_encerrar_o_servidor_com_compactacao_rodando(self):
        self._rodando()
        self.srv.servico.fechar()  # o __exit__ fecha de novo; a thread não pode achar o banco fechado e quebrar
        self.assertEqual(self.srv.servico.compactacoes, {})


class TestFalhas(Base):
    def _preparar(self, resumir) -> None:
        self._subir(BackendResumidor(resumir=resumir))
        self._enviar("um" * 600)
        self._enviar("dois")

    def _conferir_erro(self, trecho: str) -> dict:
        antes = self.arm.ler_conversa(self.cid)
        janela = self._falhar_e_esperar()
        self.assertEqual(janela["compactacao"]["estado"], "erro")
        self.assertIn(trecho, janela["compactacao"]["erro"])
        self.assertIsNone(janela["resumo"])
        self.assertEqual(self.arm.ler_conversa(self.cid)["chat_messages"], antes["chat_messages"])
        return janela

    def test_falha_do_backend(self):
        def resumir(pedido):
            raise ErroBackend("O Ollama caiu.")

        self._preparar(resumir)
        self._conferir_erro("O Ollama caiu.")

    def test_resposta_vazia(self):
        for vazio in ([], [Texto("   \n ")], [Raciocinio("só pensei")]):
            with self.subTest(resposta=vazio):
                self._preparar(lambda p, vazio=vazio: vazio)
                self._conferir_erro("vazio")

    def test_resumo_que_nao_encurta(self):
        self._preparar(lambda p: [Texto("x" * 50000)])
        self._conferir_erro("não ficou menor")

    def test_resumo_cortado_pelo_limite_nao_e_gravado_como_completo(self):
        self._preparar(lambda p: [Texto("O processo tem"), Cortada(), Uso(10, 5)])
        self._conferir_erro("no limite da janela")

    def test_erro_inesperado_nao_prende_a_conversa_nem_vai_ao_log(self):
        def resumir(pedido):
            raise ValueError("CONTEUDOSIGILOSO")

        self._preparar(resumir)
        with self.assertLogs("duo_provedor.compactacao", logging.INFO) as logs:
            janela = self._compactar_e_esperar()
        self.assertIn("ValueError", janela["compactacao"]["erro"])
        self.assertNotIn("CONTEUDOSIGILOSO", janela["compactacao"]["erro"])
        self.assertNotIn("CONTEUDOSIGILOSO", "\n".join(logs.output))
        self.assertIsNone(self._enviar("seguiu").erro)

    def test_teto_de_tempo_libera_a_conversa_mesmo_com_o_motor_parado(self):
        soltar = threading.Event()
        self.addCleanup(soltar.set)

        def resumir(pedido):
            soltar.wait(5)  # um motor que ignora o cancelamento
            return [Texto(RESUMO)]

        self._preparar(resumir)
        with mock.patch.object(compactacao, "TETO_COMPACTACAO", 0.2):
            self._conferir_erro("limite")
        self.assertIsNone(self._enviar("seguiu").erro)
        soltar.set()
        time.sleep(0.2)
        self.assertIsNone(self.arm.estado_janela(self.cid)["resumo"])

    def test_erro_some_ao_tentar_de_novo_ao_desfazer_e_ao_seguir_a_conversa(self):
        def falhar(pedido):
            raise ErroBackend("falhou")

        self._preparar(falhar)
        self._conferir_erro("falhou")
        self.backend.resumir = lambda p: [Texto(RESUMO)]
        self.assertIsNone(self._compactar_e_esperar()["compactacao"])

        self._enviar("três" * 600)
        self.backend.resumir = falhar
        self.assertEqual(self._falhar_e_esperar()["compactacao"]["estado"], "erro")
        self._enviar("quatro")
        self.assertIsNone(self.cliente.janela(self.cid)["compactacao"])

        self.backend.resumir = falhar
        self._falhar_e_esperar()
        self.cliente.descompactar(self.cid)
        janela = self.cliente.janela(self.cid)
        self.assertIsNone(janela["compactacao"])
        self.assertIsNone(janela["resumo"])

    def test_modelo_de_nuvem_bloqueado(self):
        nuvem = InfoModelo("nuvem:cloud", "Nuvem", True, False, False, True, None)
        local = InfoModelo(MODELO, "Local", True, False, False, False, None)
        self._subir(BackendResumidor(modelos=[local, nuvem]))
        self._enviar("um" * 600)
        self._enviar("dois")
        status, corpo = self._status_compactar({"modelo": "nuvem:cloud"})
        self.assertEqual(status, 400)
        self.assertIn("de nuvem", corpo["error"]["message"])
        self.assertEqual(self.backend.resumos, [])
        self.assertIsNone(self.cliente.janela(self.cid)["compactacao"])

    def test_nuvem_liberada_compacta(self):
        nuvem = InfoModelo("nuvem:cloud", "Nuvem", True, False, False, True, None)
        local = InfoModelo(MODELO, "Local", True, False, False, False, None)
        self._subir(BackendResumidor(modelos=[local, nuvem]), {"permitir_modelos_nuvem": True})
        self._enviar("um" * 600)
        self._enviar("dois")
        self.assertEqual(self._status_compactar({"modelo": "nuvem:cloud"})[0], 202)
        self.assertEqual(self._esperar()["resumo"]["modelo"], "nuvem:cloud")

    def test_motor_fora_do_ar_ao_iniciar(self):
        self._subir()
        self._enviar("um")
        self._enviar("dois")

        def fora(modelo):
            raise ErroBackend("Não consegui falar com o Ollama.")

        self.backend.info_modelo = fora
        status, corpo = self._status_compactar()
        self.assertEqual(status, 502)
        self.assertIn("Não consegui falar", corpo["error"]["message"])


class TestSimulado(Base):
    def test_compacta_no_backend_simulado(self):
        # É com ele que se testa o Duo sem LLM: o eco devolveria o texto inteiro, e a compactação sempre falharia.
        self._subir(BackendSimulado())
        self.cliente.enviar(self.cid, "pergunta um " * 50, self.cliente.raiz)
        self.cliente.enviar(self.cid, "pergunta dois", self._folha())
        self.assertEqual(self._compactar_e_esperar()["resumo"]["texto"], RESUMO_SIMULADO)


class TestFatias(Base):
    def test_agrupar_junta_turnos_e_so_parte_o_que_nao_cabe(self):
        self.assertEqual(agrupar(["a" * 40, "b" * 40], 100), ["a" * 40 + "\n\n" + "b" * 40])
        self.assertEqual(agrupar(["a" * 300, "b" * 300], 100), ["a" * 300, "b" * 300])
        partido = agrupar(["c" * 3000], 100)
        self.assertEqual("".join(partido), "c" * 3000)
        self.assertTrue(all(estimar_tokens(f) <= 100 for f in partido))
        self.assertEqual(agrupar([], 100), [])

    def test_entrada_maior_que_a_janela_vai_em_passadas_sem_passar_do_orcamento(self):
        def resumir(pedido):
            return [Texto(f"resumo parcial {len(self.backend.resumos)}")]

        self._subir(BackendResumidor(resumir=resumir), {"contexto_tokens": 4096})
        for i in range(6):
            self.assertIsNone(self._enviar(f"pergunta {i} " + "p" * 7000, system="Regras.").erro)
        janela = self._compactar_e_esperar()
        orcamento = 4096 - 1024
        pesos = [sum(estimar_tokens(m["texto"]) for m in p.mensagens) for p in self.backend.resumos]
        self.assertTrue(all(peso <= orcamento for peso in pesos), pesos)
        # Uma passada com as fatias e uma última que junta os resumos parciais.
        self.assertGreater(len(self.backend.resumos), 2)
        ultima = self.backend.resumos[-1].mensagens[1]["texto"]
        self.assertIn("resumo parcial", ultima)
        self.assertEqual(janela["resumo"]["texto"], f"resumo parcial {len(self.backend.resumos)}")
        # Todo turno anterior ao último passou por alguma fatia, inteiro e na ordem.
        fatias = "".join(p.mensagens[1]["texto"] for p in self.backend.resumos[:-1])
        for i in range(5):
            self.assertIn(f"pergunta {i} ", fatias)
        self.assertNotIn("pergunta 5", fatias)

    def test_um_turno_maior_que_a_janela_tambem_e_partido(self):
        self._subir(BackendResumidor(resumir=lambda p: [Texto("parcial")]), {"contexto_tokens": 4096},
                    roteiro=None)
        self.backend.roteiro_turnos = [[ChamadaFerramenta("listar_documentos", {})], [Texto("li")], [Texto("ok")]]
        self._enviar("leia", "d" * 40000, opcoes={"contexto": 32768})
        self._enviar("agora", opcoes={"contexto": 4096})
        janela = self._compactar_e_esperar()
        self.assertIsNotNone(janela["resumo"])
        self.assertGreater(len(self.backend.resumos), 3)
        partes = re.findall(r"d{50,}", "".join(p.mensagens[1]["texto"] for p in self.backend.resumos[:-1]))
        self.assertEqual(sum(map(len, partes)), 40000)

    def test_modelo_que_nao_encurta_nas_passadas_vira_erro(self):
        self._subir(BackendResumidor(resumir=lambda p: [Texto(p.mensagens[1]["texto"])]), {"contexto_tokens": 4096})
        for i in range(6):
            self.assertIsNone(self._enviar("p" * 7000, system="Regras.").erro)
        janela = self._falhar_e_esperar()
        self.assertEqual(janela["compactacao"]["estado"], "erro")
        self.assertIn("encurtar", janela["compactacao"]["erro"])
        self.assertIsNone(janela["resumo"])

    def test_contagem_acima_do_orcamento_parte_a_fatia_em_vez_de_deixar_o_motor_cortar(self):
        class Contador(BackendResumidor):
            contagens = 0

            def contar_tokens(self, pedido, cancelado=None):
                if not eh_resumo(pedido):
                    return 10
                type(self).contagens += 1
                return estimar_tokens(pedido.mensagens[1]["texto"]) * 2  # a estimativa fica na metade

        self._subir(Contador(resumir=lambda p: [Texto("r")]), {"contexto_tokens": 8192})
        self._enviar("p" * 16000)
        self._enviar("q" * 8000)
        self._enviar("ultimo")
        janela = self._compactar_e_esperar()
        self.assertIsNotNone(janela["resumo"])
        self.assertGreater(Contador.contagens, 1)
        for pedido in self.backend.resumos:
            self.assertLessEqual(estimar_tokens(pedido.mensagens[1]["texto"]) * 2, 6144)

    def test_fatia_que_o_motor_recusa_no_envio_e_partida(self):
        # A contagem falhou: quem prova que não coube é o envio, com `truncate: false`. Sem isso, o motor cortaria o
        # começo da fatia em silêncio, e o resumo sairia sem ele, gravado como bom.
        class Recusa(BackendResumidor):
            def contar_tokens(self, pedido, cancelado=None):
                raise ErroBackend("não deu")

            def conversar(self, pedido, cancelado):
                if eh_resumo(pedido) and len(pedido.mensagens[1]["texto"]) > 6000:
                    raise ErroAcimaDaJanela(9000)
                return super().conversar(pedido, cancelado)

        self._subir(Recusa(resumir=lambda p: [Texto("r")]))
        self._enviar("p" * 8000)
        self._enviar("último")
        self.assertEqual(self._compactar_e_esperar()["resumo"]["texto"], "r")
        self.assertGreater(len(self.backend.resumos), 2)
        partes = re.findall(r"p{50,}", "".join(p.mensagens[1]["texto"] for p in self.backend.resumos[:-1]))
        self.assertEqual(sum(map(len, partes)), 8000)

    def test_fatia_que_o_motor_recusa_na_contagem_e_partida(self):
        # A recusa levantada na contagem é a mesma prova da recusa do envio, e não uma falha dela.
        class Recusa(BackendResumidor):
            def contar_tokens(self, pedido, cancelado=None):
                if eh_resumo(pedido) and len(pedido.mensagens[1]["texto"]) > 6000:
                    raise ErroAcimaDaJanela(None)
                return 10

        self._subir(Recusa(resumir=lambda p: [Texto("r")]))
        self._enviar("p" * 8000)
        self._enviar("último")
        self.assertEqual(self._compactar_e_esperar()["resumo"]["texto"], "r")
        for pedido in self.backend.resumos:
            self.assertLessEqual(len(pedido.mensagens[1]["texto"]), 6000)

    def test_falha_na_contagem_nao_impede_o_resumo(self):
        class Falho(BackendResumidor):
            def contar_tokens(self, pedido, cancelado=None):
                if eh_resumo(pedido):
                    raise ErroBackend("não deu")
                return 10

        self._subir(Falho())
        self._enviar("um" * 600)
        self._enviar("dois")
        self.assertEqual(self._compactar_e_esperar()["resumo"]["texto"], RESUMO)


class TestEntrada(Base):
    def test_o_que_o_usuario_tirou_nao_vai_ao_resumo(self):
        self._subir(roteiro=[[ChamadaFerramenta("listar_documentos", {})], [Texto("resposta um")],
                             [ChamadaFerramenta("listar_documentos", {})], [Texto("resposta dois")], [Texto("três")]])
        self.backend.resumir = lambda pedido: [Texto("Curto.")]
        self._enviar("pergunta retirada", "resultado do turno retirado " * 50)
        self._enviar("pergunta dois", "resultado retirado sozinho " * 50)
        self._enviar("pergunta três")
        nos = self.arm.ler_conversa(self.cid)["chat_messages"]
        r2 = next(b["tool_use_id"] for b in nos[3]["content"] if b.get("type") == "tool_result")
        self.cliente.janela_excluir(self.cid, [nos[0]["uuid"], r2])
        self.assertEqual(self._compactar_e_esperar()["resumo"]["texto"], "Curto.")
        entrada = self.backend.resumos[-1].mensagens[1]["texto"]
        self.assertNotIn("pergunta retirada", entrada)
        self.assertNotIn("resultado do turno retirado", entrada)
        self.assertNotIn("resultado retirado sozinho", entrada)
        self.assertIn("pergunta dois", entrada)
        self.assertIn(historico.MARCA_RETIRADO, entrada)

    def test_tudo_tirado_nao_tem_o_que_resumir(self):
        self._subir()
        self._enviar("um")
        self._enviar("dois")
        self.cliente.janela_excluir(self.cid, [self.arm.ler_conversa(self.cid)["chat_messages"][0]["uuid"]])
        self.assertEqual(self._status_compactar()[0], 400)

    def test_transcricao_nao_leva_o_que_e_interno(self):
        turno = [
            {"papel": "usuario", "texto": "Analise"},
            {"papel": "assistente", "texto": "Vou ver", "chamadas": [
                {"id": "a", "nome": "carregar_ferramentas", "argumentos": {}},
                {"id": "b", "nome": "ler_trecho_resultado", "argumentos": {"id": "c"}},
                {"id": "c", "nome": "listar", "argumentos": {"pagina": "1"}}]},
            {"papel": "ferramenta", "id_chamada": "a", "nome": "carregar_ferramentas", "texto": "META", "interno": True},
            {"papel": "ferramenta", "id_chamada": "b", "nome": "ler_trecho_resultado", "texto": "LEITURA", "interno": True},
            {"papel": "ferramenta", "id_chamada": "c", "nome": "listar", "texto": "doc 77"},
            {"papel": "usuario", "texto": LEMBRETE_RODADA_VAZIA},
            {"papel": "assistente", "texto": "Achei o 77"},
        ]
        texto = transcrever(turno)
        for presente in ("Usuário: Analise", "Assistente: Vou ver", 'listar com {"pagina": "1"}',
                         "Resultado de listar: doc 77", "Assistente: Achei o 77"):
            self.assertIn(presente, texto)
        for ausente in ("META", "LEITURA", "carregar_ferramentas", "ler_trecho_resultado", LEMBRETE_RODADA_VAZIA):
            self.assertNotIn(ausente, texto)

    def test_log_so_tem_metadados(self):
        self._subir(BackendResumidor(resumir=lambda p: [Texto("RESUMOSIGILOSO")]))
        self._enviar("PERGUNTASIGILOSA " * 100)
        self._enviar("dois")
        with self.assertLogs("duo_provedor.compactacao", logging.INFO) as logs:
            self._compactar_e_esperar()
        juntos = "\n".join(logs.output)
        self.assertIn(": fim |", juntos)
        self.assertNotIn("RESUMOSIGILOSO", juntos)
        self.assertNotIn("PERGUNTASIGILOSA", juntos)

    def test_contexto_vem_do_ultimo_envio(self):
        self._subir()
        self._enviar("um" * 600, opcoes={"contexto": 8192, "temperatura": 0.1})
        self._enviar("dois", opcoes={"contexto": 8192, "temperatura": 0.1})
        self._compactar_e_esperar()
        (pedido,) = self.backend.resumos
        self.assertEqual((pedido.contexto_tokens, pedido.temperatura), (8192, 0.1))

    def test_cliente_do_duo_recebe_o_erro_com_o_status(self):
        self._subir()
        with self.assertRaises(ErroCliente) as erro:
            self.cliente.compactar(UUID_INEXISTENTE)
        self.assertEqual(erro.exception.status, 404)


if __name__ == "__main__":
    unittest.main()
