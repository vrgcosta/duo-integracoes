"""A ferramenta `ler_trecho_resultado`: a saída quando um resultado não coube na janela."""

from __future__ import annotations

import json
import re
import unittest

from duo_provedor import leitura
from duo_provedor.backends.base import ChamadaFerramenta, Texto
from duo_provedor.catalogo import Catalogo
from duo_provedor.emulacao import chamada_como_texto
from duo_provedor.ferramentas import para_funcoes
from duo_provedor.janela import estimar_tokens
from duo_provedor.turno import _resposta_repetida
from testes.amostras import JSON_DUO, LISTA_IDS, PROSA, amostra
from testes.cliente_duo import ClienteDuo
from testes.util import BackendGravador, ServidorDeTeste

LISTAR = {"name": "listar_documentos", "description": "Lista os documentos do processo.",
          "input_schema": {"type": "object", "properties": {}}}
NOME = "ler_trecho_resultado"
# ~12 mil tokens, sem repetição: dá para conferir qual pedaço chegou.
DOCUMENTO = "".join(f"Parágrafo {i} do documento em segredo de justiça. " for i in range(1000))
MARCA = re.compile(NOME + r'\(id="([^"]+)", inicio=(\d+)\)')


def nos_com(texto: object, nome: str = "obter_texto_documento", id_: str = "t1") -> list[dict]:
    return [{"uuid": "h", "sender": "human", "content": []},
            {"uuid": "a", "sender": "assistant", "content": [
                {"type": "tool_use", "id": id_, "name": nome, "input": {}},
                {"type": "tool_result", "tool_use_id": id_, "content": texto}]}]


class TestLer(unittest.TestCase):
    def test_a_medida_decide_o_tamanho_do_trecho(self):
        # Nas cercas o trecho vai escapado dentro de JSON, e é isso que tem de caber.
        lido = leitura.ler(nos_com(amostra(JSON_DUO, 40000)), {"id": "t1", "inicio": 0}, 1000, leitura.peso_nas_cercas)
        corpo = lido.texto[lido.cabecalho:]
        self.assertLessEqual(leitura.peso_nas_cercas(corpo), 1000)
        self.assertGreater(leitura.peso_nas_cercas(corpo), 950)
        self.assertLess(estimar_tokens(corpo), leitura.peso_nas_cercas(corpo))

    def test_devolve_o_que_cabe_em_tokens_e_diz_como_continuar(self):
        lido = leitura.ler(nos_com(DOCUMENTO), {"id": "t1", "inicio": 100}, 1000)
        self.assertIsNone(lido.erro)
        cabecalho, corpo = lido.texto[:lido.cabecalho], lido.texto[lido.cabecalho:]
        self.assertEqual(corpo, DOCUMENTO[100:lido.fim])
        self.assertLessEqual(estimar_tokens(corpo), 1000)
        self.assertGreater(estimar_tokens(corpo), 950)
        self.assertEqual((lido.alvo, lido.inicio), ("t1", 100))
        self.assertIn(f"{estimar_tokens(corpo)} de {estimar_tokens(DOCUMENTO)} tokens", cabecalho)
        self.assertIn(f'{NOME}(id="t1", inicio={lido.fim})', cabecalho)
        self.assertIn(f"Faltam {estimar_tokens(DOCUMENTO[lido.fim:])} tokens", cabecalho)
        self.assertIn("obter_texto_documento", cabecalho)
        self.assertNotIn("caracteres", cabecalho)  # o usuário e o modelo só veem tokens

    def test_texto_denso_rende_menos_caracteres_com_os_mesmos_tokens(self):
        denso = "ID 123456789 | 14/10/2026 | 0001234-56.2026.8.99.0000\n" * 1000
        corrido = "a parte autora ajuizou a presente demanda " * 1000
        fim_denso = leitura.ler(nos_com(denso), {"id": "t1", "inicio": 0}, 1000).fim
        fim_corrido = leitura.ler(nos_com(corrido), {"id": "t1", "inicio": 0}, 1000).fim
        self.assertLess(fim_denso * 2, fim_corrido)

    def test_fim_do_resultado(self):
        lido = leitura.ler(nos_com("abcdef"), {"id": "t1", "inicio": 2}, 1000)
        self.assertTrue(lido.texto.endswith("\ncdef"))
        self.assertIn("É o fim do resultado", lido.texto)
        self.assertNotIn(f"{NOME}(", lido.texto)
        self.assertEqual(lido.fim, 6)

    def test_inicio_ausente_vale_zero(self):
        self.assertEqual(leitura.ler(nos_com("abc"), {"id": "t1"}, 1000).inicio, 0)

    def test_conteudo_em_blocos(self):
        lido = leitura.ler(nos_com([{"type": "text", "text": "abc"}, {"type": "text", "text": "def"}]),
                           {"id": "t1", "inicio": 0}, 1000)
        self.assertTrue(lido.texto.endswith("\nabcdef"))

    def test_sempre_avanca(self):
        # Sem espaço nenhum, devolver nada faria o modelo repetir a mesma leitura para sempre.
        lido = leitura.ler(nos_com("1234567890"), {"id": "t1", "inicio": 0}, 0)
        self.assertGreater(lido.fim, 0)

    def test_pedido_ruim_vira_mensagem_clara(self):
        tamanho = len(DOCUMENTO)
        casos = [(None, "id"), ([], "id"), ({}, "id"), ({"id": 5}, "id"), ({"id": ""}, "id"),
                 ({"id": "zz", "inicio": 0}, '"zz"'),
                 ({"id": "t1", "inicio": "abc"}, "inicio"), ({"id": "t1", "inicio": True}, "inicio"),
                 ({"id": "t1", "inicio": 1.5}, "inicio"), ({"id": "t1", "inicio": None}, "inicio"),
                 ({"id": "t1", "inicio": -1}, "inicio=-1"), ({"id": "t1", "inicio": tamanho}, f"inicio={tamanho}"),
                 ({"id": "t1", "inicio": 10**12}, "inicio=")]
        for argumentos, trecho in casos:
            with self.subTest(argumentos=argumentos):
                lido = leitura.ler(nos_com(DOCUMENTO), argumentos, 1000)
                self.assertIsNotNone(lido.erro)
                self.assertIn(trecho, lido.texto)
                self.assertNotIn(DOCUMENTO[:50], lido.texto)
                self.assertNotIn("caracteres", lido.texto)

    def test_chamada_sem_resultado_gravado(self):
        nos = [{"uuid": "a", "sender": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "a", "input": {}}]}]
        self.assertIsNotNone(leitura.ler(nos, {"id": "t1", "inicio": 0}, 1000).erro)

    def test_resultado_vazio(self):
        lido = leitura.ler(nos_com(""), {"id": "t1", "inicio": 0}, 1000)
        self.assertIsNotNone(lido.erro)
        self.assertIn("vazio", lido.texto)

    def test_ler_uma_leitura_vai_ao_original(self):
        # A marca de uma leitura limpa ou truncada aponta para ela; o que interessa é o original.
        lido = leitura.ler(nos_com(DOCUMENTO), {"id": "t1", "inicio": 500}, 300)
        nos = nos_com(DOCUMENTO)
        nos[1]["content"] += [{"type": "tool_use", "id": "t2", "name": NOME, "input": {"id": "t1", "inicio": 500}},
                              {"type": "tool_result", "tool_use_id": "t2", "content": lido.texto, "interno": True,
                               "trecho": lido.trecho()}]
        de_novo = leitura.ler(nos, {"id": "t2", "inicio": 0}, 300)
        self.assertEqual((de_novo.alvo, de_novo.inicio, de_novo.fim), ("t1", 500, lido.fim))
        adiante = leitura.ler(nos, {"id": "t2", "inicio": lido.cabecalho + 40}, 300)
        self.assertEqual((adiante.alvo, adiante.inicio), ("t1", 540))


class TestRespostaRepetida(unittest.TestCase):
    """Pedir de novo um trecho já lido: o que dizer depende de como ficou a leitura anterior."""

    @staticmethod
    def _lido():
        return leitura.ler(nos_com(DOCUMENTO), {"id": "t1", "inicio": 500}, 300)

    def test_primeira_vez_segue(self):
        self.assertIsNone(_resposta_repetida([]))

    def test_anterior_inteira_a_vista(self):
        lido = self._lido()
        resposta = _resposta_repetida([(lido, [(0, len(lido.texto))])])
        self.assertIn("já foi lido", resposta)
        self.assertIn("está visível acima", resposta)
        self.assertIn(f"Para continuar, use inicio={lido.fim}.", resposta)

    def test_anterior_que_chegou_ao_fim(self):
        lido = leitura.ler(nos_com("abcdef"), {"id": "t1", "inicio": 2}, 1000)
        resposta = _resposta_repetida([(lido, [(0, len(lido.texto))])])
        self.assertIn("É o fim do resultado", resposta)
        self.assertNotIn("Para continuar", resposta)

    def test_anterior_truncada_nao_esta_visivel_e_diz_onde_continuar(self):
        lido = self._lido()
        faixas = [(0, lido.cabecalho + 100), (len(lido.texto) - 50, len(lido.texto))]
        resposta = _resposta_repetida([(lido, faixas)])
        self.assertNotIn("visível acima", resposta)
        self.assertIn("parte dele foi omitida", resposta)
        self.assertIn(f"inicio={lido.inicio + 100}", resposta)

    def test_anterior_truncada_sem_nada_do_trecho_a_vista_pode_reler(self):
        lido = self._lido()
        self.assertIsNone(_resposta_repetida([(lido, [(0, lido.cabecalho), (len(lido.texto), len(lido.texto))])]))

    def test_anterior_fora_da_janela_pode_reler_uma_vez(self):
        lido = self._lido()
        self.assertIsNone(_resposta_repetida([(lido, [])]))
        resposta = _resposta_repetida([(lido, []), (lido, [])])
        self.assertIn("já foi lido 2 vez(es)", resposta)

    def test_vale_a_mais_recente_a_vista(self):
        velha, nova = self._lido(), leitura.ler(nos_com(DOCUMENTO), {"id": "t1", "inicio": 500}, 900)
        resposta = _resposta_repetida([(velha, []), (nova, [(0, len(nova.texto))])])
        self.assertIn(f"inicio={nova.fim}", resposta)


class TestCatalogo(unittest.TestCase):
    def test_so_aparece_quando_pedida(self):
        catalogo = Catalogo([LISTAR], None, [], [])
        self.assertEqual([d["name"] for d in catalogo.visiveis()], ["listar_documentos"])
        self.assertEqual([d["name"] for d in catalogo.visiveis(True)], ["listar_documentos", NOME])

    def test_definicao_tem_id_e_inicio(self):
        esquema = leitura.DEFINICAO["input_schema"]
        self.assertEqual(esquema["properties"]["id"]["type"], "string")
        self.assertEqual(esquema["properties"]["inicio"]["type"], "integer")
        self.assertEqual(esquema["required"], ["id", "inicio"])

    def test_vale_mesmo_fora_da_lista_e_com_ferramentas_restritas(self):
        # O modelo pode chamá-la de novo numa rodada em que a marca já não está visível.
        catalogo = Catalogo([LISTAR], None, ["outra"], [])
        self.assertTrue(catalogo.eh_leitura(NOME))
        self.assertFalse(catalogo.eh_leitura("listar_documentos"))


class BackendPassos(BackendGravador):
    """Cada rodada chama `responder(indice, pedido)`, que decide olhando o pedido de verdade."""

    def __init__(self, responder) -> None:
        super().__init__()
        self.responder = responder

    def conversar(self, pedido, cancelado):
        indice = len(self.pedidos)
        self.pedidos.append(pedido)
        yield from self.responder(indice, pedido)


def ultima_marca(pedido) -> tuple[str, int] | None:
    achadas = MARCA.findall(pedido.mensagens[-1]["texto"].replace('\\"', '"'))
    return (achadas[-1][0], int(achadas[-1][1])) if achadas else None


def chamar(id_: object, inicio: object, cercas: bool = False):
    argumentos = {"id": id_, "inicio": inicio}
    return Texto(chamada_como_texto(NOME, argumentos)) if cercas else ChamadaFerramenta(NOME, argumentos)


def listar(cercas: bool = False):
    return Texto(chamada_como_texto("listar_documentos", {})) if cercas else ChamadaFerramenta("listar_documentos", {})


def nomes(pedido) -> list[str]:
    return [d["name"] for d in pedido.ferramentas]


def peso_enviado(pedido) -> int:
    peso = sum(estimar_tokens(m.get("texto") or "") for m in pedido.mensagens)
    peso += sum(estimar_tokens(json.dumps(m["chamadas"], ensure_ascii=False))
                for m in pedido.mensagens if m.get("chamadas"))
    return peso + (estimar_tokens(json.dumps(para_funcoes(pedido.ferramentas), ensure_ascii=False))
                   if pedido.ferramentas else 0)


class TestNoTurno(unittest.TestCase):
    def setUp(self) -> None:
        self.srv = None
        self.addCleanup(self._descer)

    def _subir(self, backend, cfg=None) -> None:
        self._descer()
        self.srv = ServidorDeTeste(backend, cfg).__enter__()
        self.cliente = ClienteDuo(self.srv.url, self.srv.blueprint)

    def _descer(self) -> None:
        if self.srv is not None:
            self.srv.__exit__(None, None, None)
            self.srv = None

    def _enviar(self, responder, documento=DOCUMENTO, contexto=8192, cercas=False, cfg=None, logs=False):
        backend = BackendPassos(responder)
        self._subir(backend, cfg)
        self.cid = self.cliente.criar_conversa()
        opcoes = {"contexto": contexto}
        if cercas:
            opcoes["ferramentas"] = "cercas"
        enviar = lambda: self.cliente.enviar(self.cid, "leia o documento", self.cliente.raiz, [LISTAR],
                                             lambda nome, entrada: documento, extras={"opcoes": opcoes})
        if logs:
            with self.assertLogs("duo_provedor.turno", "INFO") as capturados:
                resposta = enviar()
            self.logs = capturados.output
        else:
            resposta = enviar()
        self.assertIsNone(resposta.erro)
        return backend, resposta

    @staticmethod
    def _le_ate_o_fim(cercas=False):
        def responder(indice, pedido):
            if indice == 0:
                return [listar(cercas)]
            marca = ultima_marca(pedido)
            return [chamar(*marca, cercas)] if marca else [Texto("li tudo")]
        return responder

    def _no_assistente(self) -> dict:
        conversa = self.srv.servico.armazenamento.ler_conversa(self.cid)
        return next(n for n in conversa["chat_messages"] if n["sender"] == "assistant")

    def test_le_o_resto_e_nao_avisa_que_faltou(self):
        backend, r = self._enviar(self._le_ate_o_fim())
        self.assertGreater(len(backend.pedidos), 2)
        self.assertNotIn("Atenção", r.texto)
        self.assertNotIn("o modelo recebeu", r.texto)
        self.assertIn("É o fim do resultado", backend.pedidos[-1].mensagens[-1]["texto"])
        # O que chegou pelas leituras, emendado, é o documento a partir da 1ª marca, sem buracos.
        blocos = [b for b in self._no_assistente()["content"] if b.get("trecho")]
        inicio = blocos[0]["trecho"]["inicio"]
        lido = "".join(b["content"][b["trecho"]["cabecalho"]:] for b in blocos)
        self.assertEqual(lido, DOCUMENTO[inicio:])

    def test_cada_pedido_cabe_na_janela_com_a_definicao_da_ferramenta(self):
        backend, _ = self._enviar(self._le_ate_o_fim())
        for pedido in backend.pedidos:
            self.assertLessEqual(peso_enviado(pedido), 8192 - 2048)

    def test_nao_chega_ao_duo(self):
        backend, r = self._enviar(self._le_ate_o_fim())
        self.assertEqual([c[1] for c in r.chamadas], ["listar_documentos"])
        self.assertNotIn(NOME, json.dumps(r.eventos))
        ferramentas = [f for m in self.cliente.ler(self.cid) for f in m["ferramentas"]]
        self.assertEqual([f[1] for f in ferramentas], ["listar_documentos"])
        # No nó ela fica, marcada interna, para o modelo lembrar o que já leu.
        internos = [b for b in self._no_assistente()["content"] if b.get("interno")]
        self.assertTrue(internos)
        self.assertEqual({b.get("name") for b in internos if b["type"] == "tool_use"}, {NOME})

    def test_cada_aviso_fala_em_tokens(self):
        _, r = self._enviar(lambda i, p: [listar()] if i == 0 else [Texto("resposta sem ler")])
        avisos = re.findall(r"_\[Servidor: (.*?)\]_", r.texto)
        self.assertTrue(avisos)
        for aviso in avisos:
            self.assertIn("tokens", aviso)
            self.assertNotIn("caracteres", aviso)

    def test_sem_ler_avisa_quanto_faltou(self):
        backend, r = self._enviar(lambda i, p: [listar()] if i == 0 else [Texto("resposta sem ler")])
        total = estimar_tokens(DOCUMENTO)
        achado = re.search(r"(\d+) de (\d+) tokens omitidos", backend.pedidos[1].mensagens[-1]["texto"])
        faltam = int(achado[1])
        self.assertEqual(int(achado[2]), total)
        self.assertIn(f"Atenção: o modelo respondeu sem ler {faltam} de {total} tokens de listar_documentos.", r.texto)
        self.assertIn(f"Para caber na janela, o modelo recebeu {total - faltam} de {total} tokens de "
                      "listar_documentos.", r.texto)

    def test_leu_uma_parte_avisa_so_o_que_faltou(self):
        def responder(indice, pedido):
            if indice == 0:
                return [listar()]
            return [chamar(*ultima_marca(pedido))] if indice == 1 else [Texto("li um pedaço")]

        _, r = self._enviar(responder, documento=DOCUMENTO * 3)
        total = estimar_tokens(DOCUMENTO * 3)
        achado = re.search(rf"Atenção: o modelo respondeu sem ler (\d+) de {total} tokens de listar_documentos\.", r.texto)
        self.assertIsNotNone(achado, r.texto)
        self.assertLess(int(achado[1]), total - 8000)  # recebeu a 1ª janela e a leitura

    def test_resultado_inteiro_nao_gera_aviso_nem_ferramenta(self):
        backend, r = self._enviar(lambda i, p: [listar()] if i == 0 else [Texto("ok")], documento="doc curto")
        self.assertNotIn("Servidor:", r.texto)
        for pedido in backend.pedidos:
            self.assertNotIn(NOME, nomes(pedido))
            self.assertNotIn(NOME, pedido.mensagens[0]["texto"])

    def test_ferramenta_exposta_so_com_algo_cortado(self):
        backend, _ = self._enviar(lambda i, p: [listar()] if i == 0 else [Texto("ok")])
        self.assertNotIn(NOME, nomes(backend.pedidos[0]))
        self.assertIn(NOME, nomes(backend.pedidos[1]))

    def test_resultado_de_turno_anterior_pode_ser_lido(self):
        chamadas = []

        def responder(indice, pedido):
            if indice == 0:
                return [listar()]
            if indice == 1:
                return [Texto("li o começo")]
            if indice == 2:
                chamadas.append(MARCA.search("\n".join(m["texto"] for m in pedido.mensagens[1:])))
                return [chamar(chamadas[0][1], int(chamadas[0][2]))]
            return [Texto("agora li")]

        backend, r = self._enviar(responder, documento=DOCUMENTO * 3)
        folha = self.srv.servico.armazenamento.ler_conversa(self.cid)["chat_messages"][-1]["uuid"]
        # O documento do turno anterior não cabe: sai inteiro, com a marca, e a leitura continua valendo.
        segundo = self.cliente.enviar(self.cid, "e o resto?", folha, [LISTAR], extras={"opcoes": {"contexto": 8192}})
        self.assertIsNone(segundo.erro)
        self.assertIn(NOME, nomes(backend.pedidos[2]))
        self.assertEqual(chamadas[0][2], "0")
        self.assertIn("Trecho de listar_documentos", backend.pedidos[3].mensagens[-1]["texto"])
        self.assertEqual(segundo.texto.count("Atenção"), 0)  # o aviso é do turno em que o resultado chegou

    def test_o_que_saiu_nao_volta_pelo_peso_da_definicao(self):
        # O caso da revisão, num turno de verdade: o 2º resultado saía no 4º envio e voltava no 5º,
        # porque o único turno com marca tinha saído e o peso da ferramenta sumia da conta.
        class Roteiro(BackendGravador):
            def conversar(self, pedido, cancelado):
                self.pedidos.append(pedido)
                ultimo = pedido.mensagens[-1]
                if ultimo.get("papel") == "usuario" and ultimo["texto"][:3] in ("T1 ", "T2 "):
                    yield ChamadaFerramenta("listar_documentos", {"n": ultimo["texto"][1]})
                else:
                    yield Texto("curta")

        backend = Roteiro()
        self._subir(backend)
        for r2 in (49480, 49560, 49640):
            with self.subTest(r2=r2):
                tamanhos = {"1": 12000, "2": r2}
                cid, folha = self.cliente.criar_conversa(), self.cliente.raiz
                saiu: set[str] = set()
                anterior: set[str] = set()
                for prompt in ("T1 curto", "T2 curto", "T3 p", "T4 " + "q" * 400, "T5 " + "q" * 200):
                    r = self.cliente.enviar(cid, prompt, folha, [LISTAR],
                                            lambda nome, e: f"R{e['n']}| " + "x" * tamanhos[e["n"]],
                                            extras={"opcoes": {"contexto": 16384}})
                    self.assertIsNone(r.erro)
                    folha = self.srv.servico.armazenamento.ler_conversa(cid)["chat_messages"][-1]["uuid"]
                    texto = "\n".join(m["texto"] for m in backend.pedidos[-1].mensagens[1:])
                    vista = set(re.findall(r"T\d |R\d\|", texto))
                    saiu |= anterior - vista
                    self.assertFalse(saiu & vista, prompt[:2])
                    anterior = vista

    def test_cercas(self):
        backend, r = self._enviar(self._le_ate_o_fim(cercas=True), cercas=True)
        self.assertEqual([c[1] for c in r.chamadas], ["listar_documentos"])
        self.assertNotIn("Atenção", r.texto)
        self.assertEqual(backend.pedidos[0].ferramentas, [])
        self.assertNotIn(NOME, backend.pedidos[0].mensagens[0]["texto"])
        self.assertIn(f"• {NOME} —", backend.pedidos[1].mensagens[0]["texto"])  # no catálogo em texto
        self.assertTrue(backend.pedidos[-1].mensagens[-1]["texto"].startswith("⟦DUO:RESULTADOS⟧"))
        self.assertIn("É o fim do resultado", backend.pedidos[-1].mensagens[-1]["texto"])

    def test_leitura_nas_cercas_nao_e_truncada_de_novo(self):
        # A moldura, as aspas e as quebras de linha escapadas pesam: sem contá-los, quase toda leitura
        # voltava truncada na rodada seguinte e gastava as rodadas relendo.
        muitas = {"max_rodadas": 60}
        for nome, base in (("prosa", PROSA), ("ids", LISTA_IDS), ("json", JSON_DUO)):
            for contexto in (8192, 16384):
                with self.subTest(nome=nome, contexto=contexto):
                    documento = amostra(base, 40000)
                    nativo, _ = self._enviar(self._le_ate_o_fim(), documento=documento, contexto=contexto, cfg=muitas)
                    cercas, r = self._enviar(self._le_ate_o_fim(cercas=True), documento=documento, contexto=contexto,
                                             cercas=True, cfg=muitas)
                    self.assertNotIn("Atenção", r.texto)
                    for pedidos in (nativo.pedidos, cercas.pedidos):
                        # A partir da 3ª rodada, o que chega é leitura.
                        truncadas = [i for i, p in enumerate(pedidos) if i >= 2 and "tokens omitidos" in p.mensagens[-1]["texto"]]
                        self.assertEqual(truncadas, [])
                    self.assertLessEqual(len(cercas.pedidos), len(nativo.pedidos) * 3 // 2 + 1)

    def test_pedido_ruim_nao_derruba_o_turno(self):
        ruins = [{"id": 5}, {"id": "nao-existe", "inicio": 0}, {"id": "x", "inicio": "abc"}, {}]

        def responder(indice, pedido):
            if indice < len(ruins):
                return [ChamadaFerramenta(NOME, ruins[indice])]
            return [Texto("desisti")]

        backend, r = self._enviar(responder, documento="doc curto")
        self.assertEqual(r.texto, "desisti")
        self.assertIn("id", backend.pedidos[1].mensagens[-1]["texto"])
        self.assertIn('"nao-existe"', backend.pedidos[2].mensagens[-1]["texto"])

    def test_leitura_repetida_e_visivel_diz_que_ja_leu(self):
        def responder(indice, pedido):
            if indice == 0:
                return [listar()]
            if indice == 1:
                self.marca = ultima_marca(pedido)
                return [chamar(*self.marca)]
            return [chamar(*self.marca)] if indice == 2 else [Texto("fim")]

        backend, _ = self._enviar(responder)
        resposta = backend.pedidos[3].mensagens[-1]["texto"]
        self.assertIn("já foi lido", resposta)
        self.assertNotIn(DOCUMENTO[self.marca[1]:self.marca[1] + 50], resposta)

    def test_repeticao_aponta_o_fim_da_leitura_anterior_e_nao_o_da_que_caberia_agora(self):
        # O caso da revisão: a 1ª leitura veio pequena e a repetição, com a janela mais livre, diria
        # para continuar de um ponto adiante, pulando texto que o modelo nunca viu.
        def responder(indice, pedido):
            if indice == 0:
                return [listar()]
            if indice == 1:
                self.marca = ultima_marca(pedido)
                # Duas leituras na rodada dividem o espaço: a 1ª vem menor do que caberia sozinha.
                return [chamar(*self.marca), chamar(self.marca[0], self.marca[1] + 9000)]
            return [chamar(*self.marca)] if indice == 2 else [Texto("fim")]

        backend, _ = self._enviar(responder, documento=DOCUMENTO * 3)
        primeira = next(m["texto"] for m in backend.pedidos[2].mensagens if m["texto"].startswith("[Trecho de"))
        fim_da_primeira = int(MARCA.search(primeira)[2])
        resposta = backend.pedidos[3].mensagens[-1]["texto"]
        self.assertIn("já foi lido", resposta)
        self.assertIn(f"Para continuar, use inicio={fim_da_primeira}.", resposta)

    def test_mesma_leitura_duas_vezes_na_rodada(self):
        def responder(indice, pedido):
            if indice == 0:
                return [listar()]
            if indice == 1:
                marca = ultima_marca(pedido)
                return [chamar(*marca), chamar(*marca)]
            return [Texto("fim")]

        backend, _ = self._enviar(responder)
        self.assertIn("já foi lido", backend.pedidos[2].mensagens[-1]["texto"])

    def test_leitura_que_saiu_da_janela_pode_voltar_e_depois_para(self):
        # Janela apertada: cada leitura empurra a anterior. Reler é legítimo, até o limite.
        def responder(indice, pedido):
            if indice == 0:
                return [listar()]
            if indice == 1:
                self.primeira = ultima_marca(pedido)
            if indice in (1, 3, 5):
                return [chamar(*self.primeira)]
            if indice in (2, 4):
                return [chamar(self.primeira[0], self.primeira[1] + 20000)]
            return [Texto("fim")]

        backend, _ = self._enviar(responder, documento=DOCUMENTO * 3)
        respostas = [p.mensagens[-1]["texto"] for p in backend.pedidos]
        self.assertIn("Trecho de", respostas[2])  # 1ª leitura
        self.assertIn("Trecho de", respostas[4])  # a anterior saiu da janela: relê
        self.assertIn("já foi lido 2 vez(es)", respostas[6])  # e aí basta

    def test_rodadas_continuam_com_teto(self):
        def responder(indice, pedido):
            return [listar()] if indice == 0 else [chamar(*(ultima_marca(pedido) or ("t", 0)))]

        backend = BackendPassos(responder)
        self._subir(backend)
        self.cid = self.cliente.criar_conversa()
        r = self.cliente.enviar(self.cid, "leia", self.cliente.raiz, [LISTAR], lambda n, e: DOCUMENTO,
                                extras={"opcoes": {"contexto": 8192, "max_rodadas": 4}})
        self.assertIsNone(r.erro)
        # As 4 do teto, a final sem ferramentas e, como nela só veio a chamada descartada, mais uma chance.
        self.assertEqual(len(backend.pedidos), 6)
        self.assertIn("limite de 4 rodadas", r.texto)

    def test_limite_explicito_em_tokens(self):
        backend, r = self._enviar(lambda i, p: [listar()] if i == 0 else [Texto("ok")], contexto=32768,
                                  cfg={"limite_resultado": 1000})
        texto = backend.pedidos[1].mensagens[-1]["texto"]
        self.assertLess(estimar_tokens(texto), 1100)
        self.assertIn(NOME, nomes(backend.pedidos[1]))
        self.assertIn("Atenção: o modelo respondeu sem ler", r.texto)
        self.assertIn("Pelo limite_resultado, o modelo recebeu", r.texto)
        self.assertNotIn("Para caber na janela", r.texto)

    def test_leitura_nao_fica_presa_ao_limite_explicito(self):
        # O modelo pediu a parte que faltava: o teto é para o que chega sem ele pedir.
        backend, r = self._enviar(self._le_ate_o_fim(), contexto=32768, cfg={"limite_resultado": 1000})
        self.assertNotIn("Atenção", r.texto)
        self.assertEqual(len(backend.pedidos), 3)

    def test_log_so_tem_metadados(self):
        _, _ = self._enviar(self._le_ate_o_fim(), logs=True)
        linhas = [l for l in self.logs if "leitura interna" in l]
        self.assertTrue(linhas)
        self.assertIn("inicio=", linhas[0])
        self.assertIn("tokens", linhas[0])
        for linha in self.logs:
            self.assertNotIn("Parágrafo", linha)
            self.assertNotIn("segredo de justiça", linha)

    def test_log_de_pedido_ruim_nao_repete_o_que_o_modelo_mandou(self):
        def responder(indice, pedido):
            return [ChamadaFerramenta(NOME, {"id": "CONTEUDO-SIGILOSO"})] if indice == 0 else [Texto("fim")]

        self._enviar(responder, documento="doc", logs=True)
        self.assertTrue(any("leitura interna recusada" in l for l in self.logs))
        for linha in self.logs:
            self.assertNotIn("CONTEUDO-SIGILOSO", linha)


if __name__ == "__main__":
    unittest.main()
