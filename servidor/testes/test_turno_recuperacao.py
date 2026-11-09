"""O turno tem de terminar com uma resposta: rodadas vazias, ferramenta não carregada e teto de rodadas.

Com um modelo pequeno, essas situações eram o fim da conversa: ele raciocinava e parava em silêncio,
ouvia que uma ferramenta existente "não existe" ou gastava as rodadas e o usuário ficava sem resposta.
"""

import json
import re
import threading
import time
import unittest

from duo_provedor.backends.base import ChamadaFerramenta, ErroBackend, Raciocinio, Texto
from duo_provedor import janela
from duo_provedor.catalogo import Catalogo, META
from duo_provedor.emulacao import MARCA_RESULTADOS, mensagem_resultados
from duo_provedor.ferramentas import LEMBRETE_FINAL_VAZIA, LEMBRETE_RODADA_VAZIA, eh_lembrete_interno
from testes.cliente_duo import ClienteDuo
from testes.util import BackendGravador, ServidorDeTeste, chamada_unica

LISTAR = {"name": "listar_documentos", "description": "Lista os documentos do processo.",
          "input_schema": {"type": "object", "properties": {}}}
TAREFAS = {"name": "listar_tarefas", "description": "Lista as tarefas do painel.",
           "input_schema": {"type": "object", "properties": {}}}
SO_LISTAR_FAVORITA = {"catalogo": {"favoritas": ["listar_documentos"], "resumos": {}}}


class BackendPassos(BackendGravador):
    """Cada rodada chama `responder(indice, pedido)`."""

    def __init__(self, responder) -> None:
        super().__init__()
        self.responder = responder

    def conversar(self, pedido, cancelado):
        indice = len(self.pedidos)
        self.pedidos.append(pedido)
        yield from self.responder(indice, pedido)


class Base(unittest.TestCase):
    def _subir(self, backend) -> None:
        self.servidor = ServidorDeTeste(backend=backend)
        self.servidor.__enter__()
        self.addCleanup(self.servidor.__exit__, None, None, None)
        self.cliente = ClienteDuo(self.servidor.url, self.servidor.blueprint)
        self.backend = backend

    def _enviar(self, responder, extras=None, ferramentas=(LISTAR, TAREFAS)):
        self._subir(BackendPassos(responder))
        self.cid = self.cliente.criar_conversa()
        self.executadas = []

        def executar(nome, entrada):
            self.executadas.append(nome)
            return "resultado"

        return self.cliente.enviar(self.cid, "pergunta", self.cliente.raiz, list(ferramentas), executar, extras=extras)

    def _texto_do_ultimo_pedido(self) -> str:
        return json.dumps(self.backend.pedidos[-1].mensagens, ensure_ascii=False)


class TestRodadaVazia(Base):
    def test_rodada_so_com_raciocinio_ganha_uma_nova_chance(self) -> None:
        r = self._enviar(lambda i, p: [Raciocinio("Vou listar os documentos.")] if i == 0 else [Texto("Resposta.")])

        self.assertIsNone(r.erro)
        self.assertEqual(len(self.backend.pedidos), 2)
        self.assertIn("Resposta.", r.texto)
        self.assertNotIn("não produziu resposta", r.texto)
        self.assertIn("sem texto", self._texto_do_ultimo_pedido())

    def test_rodada_vazia_depois_de_ferramenta_tambem(self) -> None:
        def responder(indice, pedido):
            if indice == 0:
                return [ChamadaFerramenta("listar_documentos", {})]
            return [Raciocinio("Vou ler.")] if indice == 1 else [Texto("Concluí.")]

        r = self._enviar(responder)

        self.assertEqual(self.executadas, ["listar_documentos"])
        self.assertIn("Concluí.", r.texto)

    def test_vazia_mesmo_depois_de_ter_falado_antes_da_ferramenta(self) -> None:
        # "Vou consultar" antes da ferramenta não é resposta: o usuário ficaria sem o resultado.
        def responder(indice, pedido):
            if indice == 0:
                return [Texto("Vou consultar. "), ChamadaFerramenta("listar_documentos", {})]
            return [] if indice == 1 else [Texto("Concluí.")]

        r = self._enviar(responder)

        self.assertIn("Concluí.", r.texto)

    def test_so_uma_chance_e_depois_avisa(self) -> None:
        r = self._enviar(lambda i, p: [Raciocinio("Hum.")])

        self.assertEqual(len(self.backend.pedidos), 2)
        self.assertIn("não produziu resposta", r.texto)

    def test_o_lembrete_nao_aparece_para_o_usuario(self) -> None:
        r = self._enviar(lambda i, p: [] if i == 0 else [Texto("Resposta.")])

        self.assertNotIn("sem texto", r.texto)
        self.assertEqual(self.cliente.ler(self.cid)[-1]["texto"].strip(), "Resposta.")


class TestFerramentaNaoCarregada(Base):
    def test_chamada_direta_a_adiada_executa_e_fica_carregada(self) -> None:
        r = self._enviar(lambda i, p: [ChamadaFerramenta("listar_tarefas", {})] if i == 0 else [Texto("Pronto.")],
                         extras=SO_LISTAR_FAVORITA)

        self.assertEqual(self.executadas, ["listar_tarefas"])
        self.assertIn("Pronto.", r.texto)
        self.assertNotIn("não existe", self._texto_do_ultimo_pedido())
        self.assertIn("listar_tarefas", [f["name"] for f in self.backend.pedidos[-1].ferramentas])
        conversa = self.servidor.servico.armazenamento.ler_conversa(self.cid)
        self.assertEqual(conversa["ferramentas_carregadas"], ["listar_tarefas"])

    def test_inventada_continua_recusada_e_o_duo_nao_a_recebe(self) -> None:
        r = self._enviar(lambda i, p: [ChamadaFerramenta("apagar_tudo", {})] if i == 0 else [Texto("Sem ela.")],
                         extras=SO_LISTAR_FAVORITA)

        self.assertEqual(self.executadas, [])
        self.assertIn("Sem ela.", r.texto)
        self.assertIn("apagar_tudo", self._texto_do_ultimo_pedido())

    def test_catalogo_so_trata_como_adiada_o_que_esta_escondido(self) -> None:
        catalogo = Catalogo([LISTAR, TAREFAS], {"favoritas": ["listar_documentos"], "resumos": {}}, [], [])

        self.assertTrue(catalogo.adiada("listar_tarefas"))
        self.assertFalse(catalogo.adiada("listar_documentos"))
        self.assertFalse(catalogo.adiada("inexistente"))
        self.assertFalse(catalogo.adiada(META))
        catalogo.carregar("listar_tarefas")
        self.assertFalse(catalogo.adiada("listar_tarefas"))
        self.assertEqual(catalogo.carregadas, ["listar_tarefas"])

    def test_ferramenta_fora_das_ativas_nao_e_adiada(self) -> None:
        catalogo = Catalogo([LISTAR, TAREFAS], {"favoritas": [], "resumos": {}}, ["listar_documentos"], [])

        self.assertFalse(catalogo.adiada("listar_tarefas"))


class TestCorrecaoNoModoNativo(Base):
    def test_nao_fala_em_blocos_das_cercas(self) -> None:
        self._enviar(lambda i, p: [ChamadaFerramenta("apagar_tudo", {})] if i == 0 else [Texto("ok")])

        texto = self._texto_do_ultimo_pedido()
        self.assertNotIn("duo-ferramenta", texto)
        self.assertIn("não existe", texto)
        self.assertIn("responda ao usuário", texto)

    def test_nas_cercas_a_correcao_continua_com_o_formato_dos_blocos(self) -> None:
        self._enviar(lambda i, p: [Texto("```duo-ferramenta\n{quebrado\n```")] if i == 0 else [Texto("ok")],
                     extras={"opcoes": {"ferramentas": "cercas"}})

        self.assertIn("duo-ferramenta", self._texto_do_ultimo_pedido())


class TestTetoDeRodadas(Base):
    def test_bater_no_teto_ainda_rende_uma_resposta_sem_ferramentas(self) -> None:
        def responder(indice, pedido):
            return [chamada_unica()] if pedido.ferramentas else [Texto("Com o que li: X.")]

        r = self._enviar(responder, extras={"opcoes": {"max_rodadas": 3}})

        self.assertEqual(len(self.executadas), 3)
        self.assertEqual(len(self.backend.pedidos), 4)
        self.assertEqual(self.backend.pedidos[-1].ferramentas, [])
        self.assertIn("Com o que li: X.", r.texto)
        self.assertIn("limite de 3 rodadas", r.texto)
        self.assertIn("responda", self._texto_do_ultimo_pedido())

    def test_chamada_na_rodada_final_e_ignorada_e_ganha_mais_uma_chance(self) -> None:
        r = self._enviar(lambda i, p: [chamada_unica()],
                         extras={"opcoes": {"max_rodadas": 2}})

        self.assertIsNone(r.erro)
        self.assertEqual(len(self.executadas), 2)
        self.assertEqual(len(self.backend.pedidos), 4)
        self.assertIn("limite de 2 rodadas", r.texto)
        self.assertIn("não produziu resposta", r.texto)

    def test_nas_cercas_a_rodada_final_nao_executa_ferramenta(self) -> None:
        bloco = '```duo-ferramenta\n{{"nome": "listar_documentos", "argumentos": {{"n": {}}}}}\n```'
        r = self._enviar(lambda i, p: [Texto(bloco.format(i))], extras={"opcoes": {"ferramentas": "cercas", "max_rodadas": 2}})

        self.assertIsNone(r.erro)
        self.assertEqual(len(self.executadas), 2)
        self.assertEqual(len(self.backend.pedidos), 4)
        self.assertIn("não produziu resposta", r.texto)

    def test_quem_termina_antes_do_teto_nao_ganha_rodada_extra(self) -> None:
        self._enviar(lambda i, p: [ChamadaFerramenta("listar_documentos", {})] if i == 0 else [Texto("Fim.")],
                     extras={"opcoes": {"max_rodadas": 5}})

        self.assertEqual(len(self.backend.pedidos), 2)


class TestLembreteDaUltimaChance(unittest.TestCase):
    def test_e_interno(self) -> None:
        self.assertTrue(eh_lembrete_interno(LEMBRETE_FINAL_VAZIA))


class TestSemRespostaNaoPassaPorResposta(Base):
    """"Vou consultar" não é resposta. Quando o turno acaba sem ela, o usuário fica sabendo, e sabe o que tentar."""

    @staticmethod
    def _so_final(*respostas_finais):
        """Uma rodada com ferramenta e, nas que não oferecem ferramenta, as respostas dadas, uma por vez."""
        finais = iter(respostas_finais)

        def responder(indice, pedido):
            return [chamada_unica()] if pedido.ferramentas else next(finais, [])

        return responder

    def test_vou_consultar_seguido_de_rodadas_vazias_avisa(self) -> None:
        r = self._enviar(lambda i, p: [Texto("Vou consultar. "), ChamadaFerramenta("listar_documentos", {})] if i == 0
                         else [])
        self.assertEqual(len(self.backend.pedidos), 3)
        self.assertTrue(r.texto.startswith("Vou consultar."))
        self.assertIn("não produziu resposta", r.texto)
        self.assertIn("1 consulta", r.texto)

    def test_sem_nada_antes_tambem_avisa_sem_falar_em_consulta(self) -> None:
        r = self._enviar(lambda i, p: [])
        self.assertIn("não produziu resposta", r.texto)
        self.assertNotIn("consulta", r.texto)

    def test_quem_responde_na_ultima_rodada_nao_recebe_o_aviso(self) -> None:
        r = self._enviar(lambda i, p: [Texto("Vou consultar. "), ChamadaFerramenta("listar_documentos", {})] if i == 0
                         else [Texto("Achei.")])
        self.assertNotIn("não produziu resposta", r.texto)

    def test_rodada_final_vazia_ganha_mais_uma_chance(self) -> None:
        r = self._enviar(self._so_final([Raciocinio("Hum.")], [Texto("Com o que li: X.")]),
                         extras={"opcoes": {"max_rodadas": 2}})
        self.assertEqual(len(self.backend.pedidos), 4)
        self.assertIn("Com o que li: X.", r.texto)
        self.assertNotIn("não produziu resposta", r.texto)
        self.assertIn(LEMBRETE_FINAL_VAZIA, self._texto_do_ultimo_pedido())
        self.assertEqual(self.backend.pedidos[-1].ferramentas, [])

    def test_as_duas_vazias_dizem_que_o_trabalho_foi_feito_e_o_que_tentar(self) -> None:
        r = self._enviar(self._so_final(), extras={"opcoes": {"max_rodadas": 2}})
        self.assertEqual(len(self.backend.pedidos), 4)
        self.assertIn("não produziu resposta", r.texto)
        self.assertIn("2 consulta", r.texto)
        self.assertIn("peça de novo", r.texto)
        self.assertIn("limite de 2 rodadas", r.texto)

    def test_nas_cercas_tambem(self) -> None:
        bloco = '```duo-ferramenta\n{"nome": "listar_documentos", "argumentos": {}}\n```'
        finais = iter([[Texto(bloco)], [Texto("Respondi.")]])
        r = self._enviar(lambda i, p: [Texto(bloco)] if "Limite de rodadas" not in self._mensagens(p) else next(finais),
                         extras={"opcoes": {"ferramentas": "cercas", "max_rodadas": 2}})
        self.assertIn("Respondi.", r.texto)
        self.assertNotIn("não produziu resposta", r.texto)

    @staticmethod
    def _mensagens(pedido) -> str:
        return json.dumps(pedido.mensagens, ensure_ascii=False)

    def test_sem_espaco_e_rodada_final_vazia_nao_se_contradiz(self) -> None:
        # Era: "não produziu resposta", seguido de "o modelo respondeu com o que já tinha".
        class Apertado(BackendPassos):
            def contar_tokens(self, pedido, cancelado=None):
                return 9000 if pedido.ferramentas else 100

        self._subir(Apertado(lambda i, p: []))
        self.cid = self.cliente.criar_conversa()
        r = self.cliente.enviar(self.cid, "pergunta", self.cliente.raiz, [LISTAR], extras={"opcoes": {"contexto": 8192}})
        self.assertIn("não produziu resposta", r.texto)
        self.assertNotIn("respondeu com o que já tinha", r.texto)

    def test_sem_espaco_e_resposta_na_segunda_chance_avisa_como_sempre(self) -> None:
        class Apertado(BackendPassos):
            def contar_tokens(self, pedido, cancelado=None):
                return 9000 if pedido.ferramentas else 100

        self._subir(Apertado(lambda i, p: [] if i == 0 else [Texto("Respondi.")]))
        self.cid = self.cliente.criar_conversa()
        r = self.cliente.enviar(self.cid, "pergunta", self.cliente.raiz, [LISTAR], extras={"opcoes": {"contexto": 8192}})
        self.assertIn("Respondi.", r.texto)
        self.assertIn("respondeu com o que já tinha", r.texto)


class TestRodadaFinalNaoMandaChamarFerramenta(Base):
    """A mesma classe das marcas: quem obedece tem a chamada descartada e fica sem resposta."""

    def _ultima_com_o_teto(self, responder, extras=None) -> str:
        self._enviar(responder, extras={"opcoes": {"max_rodadas": 1, **(extras or {})}})
        self.assertEqual(self.backend.pedidos[-1].ferramentas, [])
        return "".join(m["texto"] for m in self.backend.pedidos[-1].mensagens)

    def test_nas_cercas_a_mensagem_de_resultados_nao_pede_novos_blocos(self) -> None:
        bloco = '```duo-ferramenta\n{"nome": "listar_documentos", "argumentos": {}}\n```'
        texto = self._ultima_com_o_teto(lambda i, p: [Texto(bloco)] if i == 0 else [Texto("fim")],
                                        {"ferramentas": "cercas"})
        self.assertNotIn("emita novos blocos", texto)
        self.assertIn(MARCA_RESULTADOS, texto)

    def test_o_cabecalho_da_leitura_nao_manda_continuar(self) -> None:
        documento = "dado do processo. " * 4000

        def responder(indice, pedido):
            if indice == 0:
                return [ChamadaFerramenta("listar_documentos", {})]
            if pedido.ferramentas:
                return [ChamadaFerramenta("ler_trecho_resultado", {"id": self._marca(pedido), "inicio": 0})]
            return [Texto("fim")]

        self._subir(BackendPassos(responder))
        self.cid = self.cliente.criar_conversa()
        r = self.cliente.enviar(self.cid, "leia", self.cliente.raiz, [LISTAR], lambda n, e: documento,
                                extras={"opcoes": {"contexto": 8192, "max_rodadas": 2}})
        self.assertIsNone(r.erro)
        texto = "".join(m["texto"] for m in self.backend.pedidos[-1].mensagens)
        self.assertIn("[Trecho de", texto)
        self.assertNotIn("continue com", texto)
        self.assertNotIn("ler_trecho_resultado(", texto)

    @staticmethod
    def _marca(pedido) -> str:
        return re.search(r'ler_trecho_resultado\(id="([^"]+)"', json.dumps(pedido.mensagens, ensure_ascii=False)
                         .replace('\\"', '"')).group(1)

    def test_nas_cercas_o_cabecalho_tambem(self) -> None:
        item = {"id": "c1", "nome": "ler_trecho_resultado", "interno": True,
                "conteudo": '[Trecho de x (id="t"): 5 de 9 tokens, a partir de inicio=0. Faltam 4 tokens depois deste '
                            'trecho: continue com ler_trecho_resultado(id="t", inicio=7).]\nabc'}
        turnos = [[{"papel": "usuario", "texto": "um"},
                   {"papel": "usuario", "texto": mensagem_resultados([item]), "resultado": True, "itens": [item]}]]
        r = janela.ajustar("", "", turnos, 8192, False, sem_ferramentas=True)
        texto = "".join(m["texto"] for m in r.mensagens)
        self.assertNotIn("continue com", texto)
        self.assertIn("Faltam 4 tokens depois deste trecho.]", texto)
        self.assertNotIn("emita novos blocos", texto)

    def test_com_ferramentas_a_mensagem_continua_igual(self) -> None:
        self.assertIn("emita novos blocos", mensagem_resultados([{"nome": "a", "conteudo": "b"}]))

    def test_lembretes_de_antes_saem_da_rodada_final(self) -> None:
        # "Se falta um dado, chame a ferramenta agora" contradiz a rodada sem ferramentas.
        def responder(indice, pedido):
            if indice == 0:
                return []
            return [ChamadaFerramenta("listar_documentos", {})] if pedido.ferramentas else [Texto("fim")]

        self._enviar(responder, extras={"opcoes": {"max_rodadas": 2}})
        ultima = self._texto_do_ultimo_pedido()
        self.assertEqual(self.backend.pedidos[-1].ferramentas, [])
        self.assertNotIn(LEMBRETE_RODADA_VAZIA, ultima)
        self.assertIn("Limite de rodadas", ultima)


class TestLembretesSoNoTurnoEmQueNasceram(Base):
    """O que o servidor fala com o modelo vale para aquele turno: no seguinte, "não chame ferramentas" o atrapalharia."""

    def _seguinte(self) -> str:
        nos = self.servidor.servico.armazenamento.ler_conversa(self.cid)["chat_messages"]
        r = self.cliente.enviar(self.cid, "pergunta seguinte", nos[-1]["uuid"], [LISTAR, TAREFAS])
        self.assertIsNone(r.erro)
        return self._texto_do_ultimo_pedido()

    def test_teto_de_rodadas(self) -> None:
        def responder(indice, pedido):
            if pedido.mensagens[-1]["texto"] == "pergunta seguinte":
                return [Texto("ok")]
            return [ChamadaFerramenta("listar_documentos", {})] if pedido.ferramentas else [Texto("Com o que li.")]

        self._enviar(responder, extras={"opcoes": {"max_rodadas": 1}})
        self.assertIn("Limite de rodadas", self._texto_do_ultimo_pedido())
        self.assertNotIn("Limite de rodadas", self._seguinte())

    def test_rodada_vazia(self) -> None:
        self._enviar(lambda i, p: [] if i == 0 else [Texto("Resposta.")])
        self.assertIn("sem texto", self._texto_do_ultimo_pedido())
        self.assertNotIn("sem texto", self._seguinte())

    def test_correcao_nativa(self) -> None:
        self._enviar(lambda i, p: [ChamadaFerramenta("apagar_tudo", {})] if i == 0 else [Texto("ok")])
        self.assertIn("apagar_tudo", self._texto_do_ultimo_pedido())
        self.assertNotIn("apagar_tudo", self._seguinte())

    def test_correcao_nas_cercas(self) -> None:
        self._enviar(lambda i, p: [Texto("```duo-ferramenta\n{quebrado\n```")] if i == 0 else [Texto("ok")],
                     extras={"opcoes": {"ferramentas": "cercas"}})
        self.assertIn("não puderam ser interpretados", self._texto_do_ultimo_pedido())
        self.assertNotIn("não puderam ser interpretados", self._seguinte())

    def test_o_que_ja_estava_gravado_em_conversa_antiga_tambem_nao_vai(self) -> None:
        self._enviar(lambda i, p: [Texto("Resposta.")])
        armazenamento = self.servidor.servico.armazenamento
        no = armazenamento.ler_conversa(self.cid)["chat_messages"][-1]
        no["content"].append({"type": "correcao", "interno": True, "texto": LEMBRETE_RODADA_VAZIA})
        armazenamento.gravar_mensagem(self.cid, no)
        self.assertNotIn("sem texto", self._seguinte())

    def test_o_resto_do_no_continua_indo(self) -> None:
        self._enviar(lambda i, p: [Texto("Parte um. ")] if i == 0 else [Texto("Parte dois.")])
        self.assertIn("Parte um.", self._seguinte())


class TestNoSoComLembrete(Base):
    """Sem nada que o usuário veja, não há resposta: o pedido fica sem resposta, e o Duo segue pelo pai dele (R27)."""

    def _conteudo_gravado(self) -> list[str]:
        nos = self.servidor.servico.armazenamento.ler_conversa(self.cid)["chat_messages"]
        return [n["sender"] for n in nos]

    def _rodada_final_que_falha(self) -> object:
        def responder(indice, pedido):
            if pedido.ferramentas:
                return [ChamadaFerramenta("apagar_tudo", {})]  # recusada, gera a correção
            return [Texto("não chega")]

        class Falho(BackendPassos):
            def conversar(self, pedido, cancelado):
                self.pedidos.append(pedido)
                if len(self.pedidos) == 2:
                    raise ErroBackend("o motor caiu")
                yield from responder(len(self.pedidos), pedido)

        self._subir(Falho(responder))
        self.cid = self.cliente.criar_conversa()
        with self.assertLogs("duo_provedor.turno", "WARNING"):
            return self.cliente.enviar(self.cid, "pergunta", self.cliente.raiz, [LISTAR])

    def test_motor_que_cai_depois_do_lembrete_nao_deixa_no(self) -> None:
        r = self._rodada_final_que_falha()
        self.assertIn("o motor caiu", r.erro)
        self.assertEqual(self._conteudo_gravado(), ["human"])

    def test_o_pedido_que_falhou_nao_aparece_como_turno_no_medidor(self) -> None:
        self._rodada_final_que_falha()
        self.assertEqual(self.cliente.janela(self.cid)["itens"], [])

    def test_duo_que_fecha_na_rodada_final_nao_deixa_no(self) -> None:
        comecou, soltar = threading.Event(), threading.Event()
        self.addCleanup(soltar.set)

        class Parado(BackendPassos):
            def conversar(self, pedido, cancelado):
                self.pedidos.append(pedido)
                if len(self.pedidos) == 1:
                    yield ChamadaFerramenta("apagar_tudo", {})
                    return
                comecou.set()
                cancelado.wait(5)

        self._subir(Parado(None))
        self.cid = self.cliente.criar_conversa()
        conexao, resposta = self.cliente._abrir("POST", f"/api/conversas/{self.cid}/completion",
                                                {"prompt": "oi", "parent_message_uuid": self.cliente.raiz,
                                                 "tools": [LISTAR]})
        self.assertTrue(comecou.wait(5))
        resposta.close()
        conexao.close()
        limite = time.monotonic() + 5
        while self.servidor.servico.turnos and time.monotonic() < limite:
            time.sleep(0.02)
        self.assertEqual(self.servidor.servico.turnos, {})
        self.assertEqual(self._conteudo_gravado(), ["human"])

    def test_no_com_conteudo_de_verdade_continua_gravado(self) -> None:
        self._enviar(lambda i, p: [Texto("Parte um. ")] if i == 0 else [Texto("Parte dois.")])
        self.assertEqual(self._conteudo_gravado(), ["human", "assistant"])


if __name__ == "__main__":
    unittest.main()
