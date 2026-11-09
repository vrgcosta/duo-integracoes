"""A janela de contexto pela API: o que o medidor do Duo lê e o que o usuário tira.

O teste central é o da soma: o número que o Duo mostra é o que o próximo envio usa de fato, conferido
no pedido que chega ao backend.
"""

import itertools
import json
import math
import re
import threading
import unittest

from duo_provedor import envio, historico
from duo_provedor.backends.base import ChamadaFerramenta, ErroBackend, InfoModelo, Texto, Uso
from duo_provedor.emulacao import LINGUAGEM_CHAMADA, mensagem_resultados
from duo_provedor.ferramentas import para_funcoes
from duo_provedor.janela import ajustar, estimar_tokens
from duo_provedor.servico import _MSG_COMPACTANDO
from testes.cliente_duo import ClienteDuo, ErroCliente
from testes.util import BackendGravador, ServidorDeTeste, ferramentas_do_duo

LISTAR = {"name": "listar_documentos", "description": "Lista os documentos do processo.",
          "input_schema": {"type": "object", "properties": {}}}
SISTEMA_DUO = "Instruções do preset de assessor. " * 40
MODELO = "gravador"
UUID_INEXISTENTE = "11111111-2222-4333-8444-555555555555"


def peso_do_pedido(pedido) -> int:
    """A conta de `janela.ajustar` refeita sobre o que chegou ao backend: é o que o motor recebe."""
    textos = sum(estimar_tokens(m["texto"]) for m in pedido.mensagens)
    chamadas = sum(estimar_tokens(json.dumps(m["chamadas"], ensure_ascii=False))
                   for m in pedido.mensagens if m.get("chamadas"))
    ferramentas = estimar_tokens(json.dumps(para_funcoes(pedido.ferramentas), ensure_ascii=False))
    return textos + chamadas + (ferramentas if pedido.ferramentas else 0)


_NUMERO = itertools.count(1)


def chamada(cercas: bool = False, nome: str = "listar_documentos"):
    # Válida e com argumento próprio: `pagina` é o obrigatório em `ferramentas_do_duo`, e a mesma chamada, de novo, é
    # barrada pela trava contra repetição.
    n = str(next(_NUMERO))
    if cercas:
        return Texto(f'```{LINGUAGEM_CHAMADA}\n{{"nome": "{nome}", "argumentos": {{"pagina": "{n}"}}}}\n```')
    return ChamadaFerramenta(nome, {"pagina": n})


class Base(unittest.TestCase):
    def _subir(self, roteiro=None, modelos=None, cfg_extra=None, backend=None) -> None:
        self.backend = backend or BackendGravador(roteiro or [], modelos=modelos)
        self.srv = ServidorDeTeste(self.backend, {"modelo_padrao": MODELO, **(cfg_extra or {})}).__enter__()
        self.addCleanup(self.srv.__exit__, None, None, None)
        self.cliente = ClienteDuo(self.srv.url, self.srv.blueprint)
        self.cid = self.cliente.criar_conversa()
        self.arm = self.srv.servico.armazenamento

    def _folha(self) -> str:
        nos = self.arm.ler_conversa(self.cid)["chat_messages"]
        return nos[-1]["uuid"] if nos else self.cliente.raiz

    def _enviar(self, prompt, resultado="x", ferramentas=(LISTAR,), **extras):
        resposta = self.cliente.enviar(self.cid, prompt, self._folha(), list(ferramentas),
                                       lambda nome, entrada: resultado, extras=extras)
        self.assertIsNone(resposta.erro)
        return resposta

    def _itens(self, janela: dict, tipo: str) -> list[dict]:
        return [i for i in janela["itens"] if i["tipo"] == tipo]

    def _ids_resultado(self) -> list[str]:
        return [b["tool_use_id"] for n in self.arm.ler_conversa(self.cid)["chat_messages"]
                for b in n["content"] if b.get("type") == "tool_result"]

    def _ids_turno(self) -> list[str]:
        return [n["uuid"] for n in self.arm.ler_conversa(self.cid)["chat_messages"] if n["sender"] == "human"]


class TestSomaIgualAoProximoEnvio(Base):
    """O número do medidor é o do envio seguinte, nos dois modos, com e sem corte, com e sem fator."""

    CASOS = {
        "sem corte": {"contexto": 32768, "uso": None, "ferramentas": "nativo"},
        "com corte": {"contexto": 16384, "uso": None, "ferramentas": "nativo"},
        "com fator medido": {"contexto": 16384, "uso": Uso(12000, 1), "ferramentas": "nativo"},
        "cercas sem corte": {"contexto": 32768, "uso": None, "ferramentas": "cercas"},
        "cercas com corte": {"contexto": 16384, "uso": None, "ferramentas": "cercas"},
        "catálogo compacto": {"contexto": 16384, "uso": None, "ferramentas": "nativo",
                              "catalogo": {"favoritas": ["listar_documentos"], "resumos": {}}},
    }

    def _conferir(self, contexto: int, uso: Uso | None, ferramentas: str, catalogo: dict | None = None) -> None:
        cercas = ferramentas == "cercas"
        roteiro = [[chamada(cercas)], [Texto("Li os autos."), *([uso] if uso else [])],
                   [chamada(cercas)], [Texto("Li de novo."), *([uso] if uso else [])], [Texto("fim")]]
        self._subir(roteiro)
        extras = {"system": SISTEMA_DUO, "opcoes": {"contexto": contexto, "ferramentas": ferramentas},
                  "model": MODELO, **({"catalogo": catalogo} if catalogo else {})}
        defs = ferramentas_do_duo()
        self._enviar("Analise o processo", "alfa " * 4000, defs, **extras)
        self._enviar("Agora a contestação", "beta " * 4000, defs, **extras)

        janela = self.cliente.janela(self.cid, MODELO, contexto)
        fator = self.arm.estado_janela(self.cid)["fator_tokens"]
        self._enviar("", ferramentas=defs, **extras)
        pedido = self.backend.pedidos[-1]

        self.assertEqual(janela["usado"], math.ceil(peso_do_pedido(pedido) * fator))
        self.assertEqual(sum(janela["secoes"].values()), janela["usado"])
        self.assertEqual(janela["limite"], contexto)
        self.assertEqual(janela["reserva"], min(4096, contexto // 4))
        self.assertGreater(janela["secoes"]["ferramentas"], 100 if catalogo else 1000)
        # O que o medidor diz que saiu é o que saiu mesmo.
        enviado = json.dumps(pedido.mensagens, ensure_ascii=False)
        alfa, beta = self._itens(janela, "resultado")
        self.assertEqual(alfa["incluido"], "alfa alfa" in enviado)
        self.assertEqual(beta["incluido"], "beta beta" in enviado)
        fora = [i for i in janela["itens"] if not i["incluido"]]
        if contexto < 32768 and not catalogo:
            self.assertTrue(fora, "a janela estreita precisa cortar algo")
            self.assertTrue(all(i["motivo"] == "corte" for i in fora))
        elif contexto == 32768:
            self.assertEqual(fora, [])

    def test_casos(self) -> None:
        for nome, caso in self.CASOS.items():
            with self.subTest(caso=nome):
                self._conferir(**caso)

    def test_mesma_lista_de_mensagens_ao_montar_com_o_ultimo_envio_gravado(self) -> None:
        # A mesma função dos dois lados: nada de uma conta paralela no medidor que só coincida nos testes.
        self._subir([[chamada()], [Texto("Li.")], [Texto("fim")]])
        extras = {"system": SISTEMA_DUO, "opcoes": {"contexto": 8192}, "model": MODELO}
        self._enviar("Analise", "documento " * 3000, ferramentas_do_duo(), **extras)
        self._enviar("", ferramentas=ferramentas_do_duo(), **extras)
        conversa = self.arm.ler_conversa(self.cid)
        estado = self.arm.estado_janela(self.cid)
        cfg = self.srv.servico.cfg
        opcoes = envio.mesclar_opcoes(cfg, estado["ultimo_envio"]["opcoes"])
        preparo = envio.preparar(cfg, self.srv.servico.sistema_padrao, self.arm, conversa, estado["ultimo_envio"],
                                 opcoes, MODELO, self.backend.info_modelo(MODELO))
        ultimo_no = next(n for n in reversed(conversa["chat_messages"]) if n["sender"] == "human")
        nos = historico.ramo(conversa, ultimo_no["uuid"])
        jan, _ = envio.montar(preparo, nos, set(), None, estado["fator_tokens"])
        self.assertEqual(jan.mensagens, self.backend.pedidos[-1].mensagens)

    def test_motor_fora_do_ar_nao_derruba_a_leitura(self) -> None:
        class Fora(BackendGravador):
            def info_modelo(self, modelo):
                raise ErroBackend("fora do ar")

        self._subir(backend=Fora())
        self.assertEqual(self.cliente.janela(self.cid)["itens"], [])


class TestResposta(Base):
    def test_conversa_nova(self) -> None:
        self._subir()
        janela = self.cliente.janela(self.cid, MODELO, 8192)
        self.assertEqual(janela["limite"], 8192)
        self.assertEqual(janela["fonte"], "estimado")
        self.assertEqual(janela["itens"], [])
        self.assertIsNone(janela["resumo"])
        self.assertIsNone(janela["compactacao"])
        self.assertIs(janela["provisorio"], True)
        self.assertEqual(janela["usado"], sum(janela["secoes"].values()))
        self.assertEqual(set(janela["secoes"]), {"sistema", "ferramentas", "historico"})

    def test_depois_do_1o_envio_o_numero_ja_nao_e_provisorio(self) -> None:
        # Antes dele, o servidor não conhece o `system` nem as ferramentas: o número sai baixo, e a tela tem de saber.
        self._subir([[Texto("ok")]])
        self._enviar("oi", ferramentas=ferramentas_do_duo(), system=SISTEMA_DUO)
        janela = self.cliente.janela(self.cid)
        self.assertIs(janela["provisorio"], False)
        self.assertGreater(janela["secoes"]["ferramentas"], 1000)

    def test_a_janela_configurada_vale_e_nao_a_do_ultimo_envio(self) -> None:
        # O motor manda `?contexto=` a partir de `campoContexto`: quem mudou a opção vê o efeito antes de enviar.
        self._subir([[chamada()], [Texto("Li.")]])
        self._enviar("leia", "x" * 34000, opcoes={"contexto": 32768})
        janela = self.cliente.janela(self.cid, contexto=8192)
        self.assertEqual((janela["limite"], janela["reserva"]), (8192, 2048))
        self.assertFalse(self._itens(janela, "resultado")[0]["incluido"])
        self.assertTrue(self._itens(self.cliente.janela(self.cid), "resultado")[0]["incluido"])
        self.assertEqual(self.cliente.janela(self.cid)["limite"], 32768)

    def test_campo_de_contexto_aponta_para_a_opcao_do_envio(self) -> None:
        self._subir()
        campo = self.srv.blueprint["janelaContexto"]["ler"]["campoContexto"]
        opcao = next(c for c in self.srv.blueprint["configuraveis"] if c["caminho"] == f"enviar.corpo.{campo}")
        self.assertEqual(opcao["tipo"], "numero")

    def test_conversa_inexistente_responde_404(self) -> None:
        self._subir()
        self.assertEqual(self.srv.pedir("GET", f"/api/conversas/{UUID_INEXISTENTE}/janela")[0], 404)

    def test_sem_contexto_na_consulta_vale_o_do_ultimo_envio_e_antes_dele_o_da_config(self) -> None:
        self._subir([[Texto("ok")]], cfg_extra={"contexto_tokens": 4096})
        self.assertEqual(self.cliente.janela(self.cid)["limite"], 4096)
        self._enviar("oi", opcoes={"contexto": 8192})
        self.assertEqual(self.cliente.janela(self.cid)["limite"], 8192)
        self.assertEqual(self.cliente.janela(self.cid, contexto=16384)["limite"], 16384)

    def test_itens_dizem_o_que_e_cada_coisa(self) -> None:
        self._subir([[chamada()], [Texto("Resposta.")]])
        pergunta = "analise   o processo\n" + "e diga tudo o que houver de relevante " * 5
        self._enviar(pergunta, "conteúdo do documento", opcoes={"contexto": 32768})
        janela = self.cliente.janela(self.cid)
        (turno,), (resultado,) = self._itens(janela, "turno"), self._itens(janela, "resultado")
        self.assertEqual(turno["id"], self._ids_turno()[0])
        self.assertTrue(turno["rotulo"].startswith("analise o processo e diga"))
        self.assertLessEqual(len(turno["rotulo"]), 80)
        self.assertTrue(turno["incluido"])
        self.assertNotIn("motivo", turno)
        self.assertEqual(resultado["id"], self._ids_resultado()[0])
        self.assertEqual(resultado["turno"], turno["id"])
        self.assertEqual(resultado["rotulo"], "listar_documentos")
        self.assertEqual(resultado["tokens"], estimar_tokens("conteúdo do documento"))
        self.assertTrue(resultado["incluido"])

    def test_rotulo_nao_traz_o_texto_do_anexo(self) -> None:
        self._subir([[Texto("ok")]])
        anexo = {"file_name": "peticao.txt", "extracted_content": "conteúdo do anexo"}
        self._enviar("resuma", attachments=[anexo])
        (turno,) = self._itens(self.cliente.janela(self.cid), "turno")
        self.assertEqual(turno["rotulo"], "resuma")
        self.assertGreater(turno["tokens"], estimar_tokens("conteúdo do anexo"))

    def test_item_cortado_fica_fora_com_o_tamanho_que_voltaria_a_ocupar(self) -> None:
        self._subir([[chamada()], [Texto("Li.")], [Texto("ok")]])
        documento = "x" * 34000
        self._enviar("leia", documento, opcoes={"contexto": 16384})
        janela = self.cliente.janela(self.cid, contexto=8192)
        (resultado,) = self._itens(janela, "resultado")
        self.assertFalse(resultado["incluido"])
        self.assertEqual(resultado["motivo"], "corte")
        self.assertEqual(resultado["tokens"], estimar_tokens(documento))
        self.assertLessEqual(janela["usado"], 8192 - janela["reserva"])

    def test_primeiro_turno_fixado_continua_dentro_mesmo_sem_a_resposta(self) -> None:
        # Sem instruções do Duo, o 1º pedido fica na janela e sai o resto dele.
        self._subir([[chamada()], [Texto("r" * 6000)], [chamada()], [Texto("t" * 3000)]])
        self._enviar("primeiro", "x" * 12000)
        self._enviar("segundo", "y" * 18000)
        janela = self.cliente.janela(self.cid, contexto=8192)
        primeiro = self._itens(janela, "turno")[0]
        self.assertTrue(primeiro["incluido"])
        self.assertTrue(primeiro["parcial"])
        self.assertEqual(primeiro["tokens"], estimar_tokens("primeiro"))

    def test_nas_cercas_turno_inteiro_com_varios_resultados_nao_e_parcial(self) -> None:
        # Nas cercas, os resultados de uma rodada dividem uma mensagem: o turno tem menos mensagens, e está inteiro.
        duas = Texto("\n".join(chamada(True).texto for _ in range(2)))
        self._subir([[duas], [Texto("Li.")], [Texto("ok")]])
        extras = {"opcoes": {"ferramentas": "cercas"}, "system": SISTEMA_DUO}
        self._enviar("um", "resultado", **extras)
        self._enviar("dois", **extras)
        janela = self.cliente.janela(self.cid)
        self.assertEqual(len(self._itens(janela, "resultado")), 2)
        self.assertTrue(all(i["incluido"] and "parcial" not in i for i in janela["itens"]))

    def test_fonte_real_depois_de_uso_medido_com_o_mesmo_modelo(self) -> None:
        outro = InfoModelo("outro", "Outro", True, False, False, False, None)
        gravador = InfoModelo(MODELO, "Gravador", True, False, False, False, None)
        self._subir([[Texto("ok"), Uso(50, 1)]], modelos=[gravador, outro])
        self.assertEqual(self.cliente.janela(self.cid, MODELO)["fonte"], "estimado")
        self._enviar("oi")
        real = self.cliente.janela(self.cid, MODELO)
        self.assertEqual(real["fonte"], "real")
        self.assertEqual(real["ultimo_uso"], 50)
        self.assertEqual(self.cliente.janela(self.cid, "padrao")["fonte"], "real")  # "padrao" é o mesmo modelo
        self.assertEqual(self.cliente.janela(self.cid, "outro")["fonte"], "estimado")

    def test_ler_a_janela_nao_chama_o_modelo(self) -> None:
        self._subir([[Texto("ok")]])
        self._enviar("oi")
        pedidos = len(self.backend.pedidos)
        self.cliente.janela(self.cid)
        self.assertEqual(len(self.backend.pedidos), pedidos)

    def test_pedido_sem_resposta_nao_conta(self) -> None:
        # Parado ou com erro, ele fica na árvore, mas o Duo manda o envio seguinte pelo pai dele.
        class Falha(BackendGravador):
            def conversar(self, pedido, cancelado):
                self.pedidos.append(pedido)
                if len(self.pedidos) == 1:
                    yield Texto("ok")
                else:
                    raise ErroBackend("o motor caiu")

        self._subir(backend=Falha())
        self._enviar("primeiro")
        antes = self.cliente.janela(self.cid)
        with self.assertLogs("duo_provedor.turno", "WARNING"):
            self.cliente.enviar(self.cid, "z" * 4000, self._folha(), [LISTAR])
        depois = self.cliente.janela(self.cid)
        self.assertEqual(len(self._itens(depois, "turno")), 1)
        self.assertEqual(depois["usado"], antes["usado"])

    def test_blueprint_declara_as_rotas(self) -> None:
        self._subir()
        rotas = self.srv.blueprint["janelaContexto"]
        self.assertEqual(rotas["ler"], {"caminho": "/api/conversas/{convId}/janela", "campoContexto": "opcoes.contexto"})
        self.assertEqual(rotas["excluir"], {"metodo": "PUT", "caminho": "/api/conversas/{convId}/janela"})
        self.assertEqual(rotas["compactar"], {"caminho": "/api/conversas/{convId}/compactar"})
        self.assertEqual(rotas["descompactar"], {"metodo": "DELETE", "caminho": "/api/conversas/{convId}/compactar"})


class TestExclusoes(Base):
    def _dois_turnos(self, roteiro_extra=()):
        self._subir([[chamada()], [Texto("Resposta um.")], [chamada()], [Texto("Resposta dois.")], *roteiro_extra])
        self._enviar("primeiro pedido", "resultado um")
        self._enviar("segundo pedido", "resultado dois")

    def test_janela_devolve_a_lista_gravada(self) -> None:
        self._dois_turnos()
        self.assertEqual(self.cliente.janela(self.cid)["excluidos"], [])
        t1, _ = self._ids_turno()
        r1, _ = self._ids_resultado()
        self.cliente.janela_excluir(self.cid, [r1, t1])
        self.assertEqual(self.cliente.janela(self.cid)["excluidos"], [r1, t1])

    def test_religar_o_turno_nao_devolve_o_resultado_tirado_direto(self) -> None:
        # O caso do Duo depois de recarregar a página: tirar r1, tirar t1, religar t1. Pelos `itens`, r1 parece ter saído
        # com o turno; é a lista que mostra que foi tirado à parte, e o PUT dele sai dela.
        self._dois_turnos([[Texto("três")]])
        t1, _ = self._ids_turno()
        r1, _ = self._ids_resultado()
        self.cliente.janela_excluir(self.cid, [r1, t1])
        lista = self.cliente.janela(self.cid)["excluidos"]
        self.cliente.janela_excluir(self.cid, [item for item in lista if item != t1])
        janela = self.cliente.janela(self.cid)
        self.assertTrue(next(i for i in janela["itens"] if i["id"] == t1)["incluido"])
        self.assertEqual(next(i for i in janela["itens"] if i["id"] == r1)["motivo"], "usuario")
        self._enviar("terceiro")
        enviado = json.dumps(self.backend.pedidos[-1].mensagens, ensure_ascii=False)
        self.assertIn("primeiro pedido", enviado)
        self.assertNotIn("resultado um", enviado)

    def test_lista_invalida_responde_400_e_conversa_inexistente_404(self) -> None:
        self._subir()
        for corpo in ({}, {"excluidos": "a"}, {"excluidos": [1]}, {"excluidos": None}):
            self.assertEqual(self.srv.pedir("PUT", f"/api/conversas/{self.cid}/janela", corpo)[0], 400)
        self.assertEqual(self.srv.pedir("PUT", f"/api/conversas/{UUID_INEXISTENTE}/janela", {"excluidos": []})[0], 404)

    def test_resultado_tirado_vira_marca_e_o_par_continua_valido(self) -> None:
        self._dois_turnos([[Texto("três")]])
        r1, r2 = self._ids_resultado()
        self.cliente.janela_excluir(self.cid, [r1])

        janela = self.cliente.janela(self.cid)
        fora = next(i for i in janela["itens"] if i["id"] == r1)
        self.assertEqual((fora["incluido"], fora["motivo"]), (False, "usuario"))
        self.assertEqual(fora["tokens"], estimar_tokens("resultado um"))
        self.assertTrue(next(i for i in janela["itens"] if i["id"] == r2)["incluido"])

        self._enviar("terceiro")
        mensagens = self.backend.pedidos[-1].mensagens
        enviado = json.dumps(mensagens, ensure_ascii=False)
        self.assertNotIn("resultado um", enviado)
        self.assertIn("resultado dois", enviado)
        marca = next(m for m in mensagens if m.get("id_chamada") == r1)
        self.assertEqual(marca["texto"], historico.MARCA_RETIRADO)
        # Sem nota de corte: o modelo não é orientado a ler o que o usuário tirou.
        self.assertNotIn("Nota do servidor", mensagens[0]["texto"])

    def test_turno_tirado_sai_inteiro_e_os_resultados_dele_tambem(self) -> None:
        self._dois_turnos([[Texto("três")]])
        t1, t2 = self._ids_turno()
        self.cliente.janela_excluir(self.cid, [t1])

        janela = self.cliente.janela(self.cid)
        turno, resultado = janela["itens"][0], janela["itens"][1]
        self.assertEqual((turno["id"], turno["incluido"], turno["motivo"]), (t1, False, "usuario"))
        self.assertEqual((resultado["turno"], resultado["incluido"], resultado["motivo"]), (t1, False, "usuario"))
        self.assertTrue(next(i for i in janela["itens"] if i["id"] == t2)["incluido"])

        self._enviar("terceiro")
        enviado = json.dumps(self.backend.pedidos[-1].mensagens, ensure_ascii=False)
        for texto in ("primeiro pedido", "Resposta um", "resultado um"):
            self.assertNotIn(texto, enviado)
        self.assertIn("segundo pedido", enviado)

    def test_original_continua_gravado_e_lista_vazia_devolve_tudo(self) -> None:
        self._dois_turnos([[Texto("três")], [Texto("quatro")]])
        t1, _ = self._ids_turno()
        r1, r2 = self._ids_resultado()
        self.cliente.janela_excluir(self.cid, [t1, r2])
        # O Duo relê a conversa inteira: tirar da janela não mexe no que ele mostra.
        lida = self.srv.pedir("GET", f"/api/conversas/{self.cid}")[1]
        self.assertIn("resultado um", json.dumps(lida, ensure_ascii=False))
        self.assertIn("resultado dois", json.dumps(lida, ensure_ascii=False))

        self._enviar("terceiro")
        self.cliente.janela_excluir(self.cid, [])
        self.assertTrue(all(i["incluido"] for i in self.cliente.janela(self.cid)["itens"]))
        self._enviar("quarto")
        enviado = json.dumps(self.backend.pedidos[-1].mensagens, ensure_ascii=False)
        self.assertIn("resultado um", enviado)
        self.assertIn("resultado dois", enviado)

    def test_o_que_o_usuario_tirou_nao_volta_mesmo_com_a_janela_sobrando(self) -> None:
        self._dois_turnos([[Texto("três")], [Texto("quatro")]])
        r1, _ = self._ids_resultado()
        self.cliente.janela_excluir(self.cid, [r1])
        self._enviar("terceiro", opcoes={"contexto": 262144})
        self._enviar("quarto", opcoes={"contexto": 262144})
        self.assertNotIn("resultado um", json.dumps(self.backend.pedidos[-1].mensagens, ensure_ascii=False))
        self.assertEqual(self.arm.estado_janela(self.cid)["excluidos"], [r1])

    def test_marca_do_usuario_nao_vira_marca_de_corte(self) -> None:
        # Numa janela apertada, o corte passa por cima sem a trocar pela marca que manda ler, nos dois modos.
        retirado = {"id": "r", "nome": "ler", "conteudo": historico.MARCA_RETIRADO, "excluido": True}
        nativo = [{"papel": "usuario", "texto": "um"},
                  {"papel": "assistente", "texto": "", "chamadas": [{"id": "r", "nome": "ler", "argumentos": {}}]},
                  {"papel": "ferramenta", "id_chamada": "r", "nome": "ler", "texto": retirado["conteudo"],
                   "excluido": True},
                  {"papel": "assistente", "texto": "a" * 9000}]
        cercas = [{"papel": "usuario", "texto": "um"},
                  {"papel": "usuario", "texto": mensagem_resultados([retirado]), "resultado": True, "itens": [retirado]},
                  {"papel": "assistente", "texto": "a" * 9000}]
        for nome, turno in (("nativo", nativo), ("cercas", cercas)):
            with self.subTest(modo=nome):
                r = ajustar("", "", [turno, [{"papel": "usuario", "texto": "x" * 2000}]], 2048, True)
                self.assertFalse(r.leitura)
                self.assertNotIn("r", r.vistos)
                self.assertNotIn("omitido", json.dumps(r.mensagens, ensure_ascii=False))
                self.assertFalse([a for a in r.avisos if "resultado" in a])

    def test_exclusao_vale_para_o_medidor_e_para_o_envio_na_mesma_conta(self) -> None:
        self._dois_turnos([[Texto("três")]])
        r1, _ = self._ids_resultado()
        antes = self.cliente.janela(self.cid)["usado"]
        self.cliente.janela_excluir(self.cid, [r1])
        janela = self.cliente.janela(self.cid)
        self.assertNotEqual(janela["usado"], antes)
        self._enviar("")
        self.assertEqual(janela["usado"], peso_do_pedido(self.backend.pedidos[-1]))

    def test_tirar_no_meio_do_turno_vale_na_rodada_seguinte(self) -> None:
        self._subir([[chamada()], [Texto("Li.")], [chamada()], [Texto("fim")]])
        self._enviar("um", "resultado um")
        r1 = self._ids_resultado()[0]

        def executar(nome, entrada):
            self.cliente.janela_excluir(self.cid, [r1])
            return "resultado dois"

        resposta = self.cliente.enviar(self.cid, "dois", self._folha(), [LISTAR], executar)
        self.assertIsNone(resposta.erro)
        self.assertIn("resultado um", json.dumps(self.backend.pedidos[2].mensagens, ensure_ascii=False))
        self.assertNotIn("resultado um", json.dumps(self.backend.pedidos[3].mensagens, ensure_ascii=False))

    def test_modelo_nao_le_de_volta_o_que_o_usuario_tirou(self) -> None:
        self._dois_turnos([[ChamadaFerramenta("ler_trecho_resultado", {"id": "?", "inicio": 0})], [Texto("ok")]])
        r1, _ = self._ids_resultado()
        self.cliente.janela_excluir(self.cid, [r1])
        self.backend.roteiro[4] = [ChamadaFerramenta("ler_trecho_resultado", {"id": r1, "inicio": 0})]
        self._enviar("releia")
        enviado = json.dumps(self.backend.pedidos[-1].mensagens, ensure_ascii=False)
        self.assertNotIn("resultado um", enviado)
        self.assertIn("retirou", enviado)

    def test_modelo_nao_le_resultado_de_turno_tirado(self) -> None:
        self._dois_turnos([[ChamadaFerramenta("ler_trecho_resultado", {"id": "?", "inicio": 0})], [Texto("ok")]])
        t1, _ = self._ids_turno()
        r1, _ = self._ids_resultado()
        self.cliente.janela_excluir(self.cid, [t1])
        self.backend.roteiro[4] = [ChamadaFerramenta("ler_trecho_resultado", {"id": r1, "inicio": 0})]
        self._enviar("releia")
        self.assertNotIn("resultado um", json.dumps(self.backend.pedidos[-1].mensagens, ensure_ascii=False))

    def test_trecho_lido_antes_de_tirar_tambem_sai(self) -> None:
        # A leitura guarda um pedaço do resultado num bloco interno: não pode ser o caminho de volta.
        self._subir([[chamada()], [Texto("Li.")], [Texto("Bem.")], [Texto("fim")]])
        self._enviar("um", "resultado sigiloso", opcoes={"contexto": 32768})
        r1 = self._ids_resultado()[0]
        self.backend.roteiro[2] = [ChamadaFerramenta("ler_trecho_resultado", {"id": r1, "inicio": 0})]
        self.backend.roteiro[3] = [Texto("Reli.")]
        self._enviar("dois")
        self.assertIn("resultado sigiloso", json.dumps(self.backend.pedidos[3].mensagens, ensure_ascii=False))
        self.cliente.janela_excluir(self.cid, [r1])
        self.backend.roteiro.append([Texto("ok")])
        self._enviar("três")
        self.assertNotIn("resultado sigiloso", json.dumps(self.backend.pedidos[-1].mensagens, ensure_ascii=False))

    def test_sem_excluidos_nao_copia_o_ramo(self) -> None:
        nos = [{"uuid": "a", "sender": "human", "content": []}]
        self.assertIs(historico.sem_excluidos(nos, set()), nos)

    def test_ids_dos_turnos_seguem_para_turnos(self) -> None:
        nos = [{"uuid": "x", "sender": "assistant", "content": []},
               {"uuid": "a", "sender": "human", "text": "um", "content": []},
               {"uuid": "b", "sender": "assistant", "content": []},
               {"uuid": "c", "sender": "human", "text": "dois", "content": []}]
        self.assertEqual(len(historico.ids_dos_turnos(nos)), len(historico.para_turnos(nos, "nativo")))
        self.assertEqual(historico.ids_dos_turnos(nos), ["x", "a", "c"])


class TestRetentativaDoEnvio(Base):
    """Logo depois de parar, o turno ainda leva um instante para soltar a conversa: o motor tenta de novo o 409 dele."""

    def _molde(self) -> re.Pattern:
        retentativa = self.srv.blueprint["retryTransitorio"]
        self.assertEqual(retentativa["tentativas"], 10)
        return re.compile(retentativa["padrao"], re.IGNORECASE)  # como o motor: new RegExp(padrao, 'i')

    def test_casa_com_o_409_do_turno_aberto_e_nao_com_o_da_compactacao(self) -> None:
        pronto, soltar = threading.Event(), threading.Event()
        self.addCleanup(soltar.set)

        class Parado(BackendGravador):
            def conversar(self, pedido, cancelado):
                self.pedidos.append(pedido)
                pronto.set()
                soltar.wait(5)
                yield Texto("Resposta de um resumo bem curto.")

        self._subir(backend=Parado())
        thread = threading.Thread(target=lambda: self.cliente.enviar(self.cid, "um " * 300, self.cliente.raiz))
        thread.start()
        self.addCleanup(thread.join, 5)
        self.assertTrue(pronto.wait(5))
        status, corpo = self.srv.pedir("POST", f"/api/conversas/{self.cid}/completion", {"prompt": "dois"})
        self.assertEqual(status, 409)
        molde = self._molde()
        self.assertTrue(molde.search(corpo["error"]["message"]), corpo["error"]["message"])
        for mensagem in (_MSG_COMPACTANDO, "Já há uma compactação em andamento nesta conversa."):
            self.assertIsNone(molde.search(mensagem), mensagem)
        status, corpo = self.srv.pedir("POST", f"/api/conversas/{self.cid}/compactar", {})
        self.assertEqual(status, 409)
        self.assertIsNone(molde.search(corpo["error"]["message"]), "compactar com turno aberto não é o envio")


class TestConcorrencia(Base):
    def test_ler_a_janela_com_turno_aberto(self) -> None:
        pronto, liberar = threading.Event(), threading.Event()

        class Parado(BackendGravador):
            def conversar(self, pedido, cancelado):
                self.pedidos.append(pedido)
                pronto.set()
                liberar.wait(5)
                yield Texto("ok")

        self._subir(backend=Parado())
        resultado: dict = {}
        thread = threading.Thread(target=lambda: resultado.update(r=self.cliente.enviar(
            self.cid, "pergunta", self.cliente.raiz)))
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(liberar.set)
        self.assertTrue(pronto.wait(5))
        status, janela = self.srv.pedir("GET", f"/api/conversas/{self.cid}/janela")
        self.assertEqual(status, 200)
        self.assertIn("usado", janela)
        liberar.set()
        thread.join(5)
        self.assertIsNone(resultado["r"].erro)


class TestClienteErro(Base):
    def test_erro_do_cliente_traz_o_status(self) -> None:
        self._subir()
        with self.assertRaises(ErroCliente):
            self.cliente.janela(UUID_INEXISTENTE)


if __name__ == "__main__":
    unittest.main()
