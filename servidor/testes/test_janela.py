import json
import random
import re
import unicodedata
import unittest

from duo_provedor import emulacao, historico
from duo_provedor.catalogo import Catalogo
from duo_provedor.ferramentas import para_funcoes
from duo_provedor.janela import ajustar, caracteres_ate, estimar_tokens, fator_real, truncar_meio
from duo_provedor.turno import Opcoes, montar_janela
from testes.amostras import JSON_DUO, LISTA_IDS, PROSA, amostra, texto_juridico_acentuado
from testes.util import ferramentas_do_duo


LISTAR = {"name": "listar_documentos", "description": "Lista os documentos do processo.",
          "input_schema": {"type": "object", "properties": {}}}
LER_TRECHO = "ler_trecho_resultado"


def omitido(id_):
    return f'[resultado omitido da janela; leia com ler_trecho_resultado(id="{id_}", inicio=0)]'


def turno(texto_usuario, texto_assistente="ok"):
    return [{"papel": "usuario", "texto": texto_usuario},
            {"papel": "assistente", "texto": texto_assistente}]


def total(resultado):
    return resultado.estimativa


def usuario(texto):
    return {"papel": "usuario", "texto": texto}


def assistente(texto):
    return {"papel": "assistente", "texto": texto}


def rodada(id_, *resultados, texto=""):
    """Uma ida e volta de ferramenta no modo nativo: `id_` numera as chamadas da rodada."""
    ids = [f"{id_}{i}" for i in range(len(resultados))]
    chamadas = [{"id": i, "nome": "ler", "argumentos": {}} for i in ids]
    return [{"papel": "assistente", "texto": texto, "chamadas": chamadas}] + [
        {"papel": "ferramenta", "id_chamada": i, "nome": "ler", "texto": r} for i, r in zip(ids, resultados)]


def textos_de(resultado):
    return [m["texto"] for m in resultado.mensagens[1:]]


def no_(uuid, pai, sender, content, text=""):
    return {"uuid": uuid, "parent_message_uuid": pai, "sender": sender, "text": text, "content": content}


class TestEstimar(unittest.TestCase):
    def test_arredonda_para_cima(self):
        self.assertEqual(estimar_tokens(""), 0)
        self.assertEqual(estimar_tokens("abc"), 1)
        self.assertEqual(estimar_tokens("abcde"), 2)

    def test_nao_fica_abaixo_do_medido_nem_muito_acima(self):
        # Abaixo do real, o motor corta o começo em silêncio; muito acima, sai da janela o que cabia.
        for nome, base, real in (("prosa", PROSA, 6757), ("ids", LISTA_IDS, 25075), ("json", JSON_DUO, 14261)):
            with self.subTest(nome):
                estimado = estimar_tokens(amostra(base))
                self.assertGreaterEqual(estimado, real)
                self.assertLessEqual(estimado, real * 1.1)

    def test_texto_juridico_acentuado_nao_fica_abaixo_do_medido(self):
        # Vocabulário variado, nomes próprios e maiúsculas rendem menos caracteres por token que uma
        # frase corrida repetida. O acento não pesa: tirá-lo é que piora, porque a forma sem acento é
        # mais rara. Por isso a calibragem está nas letras, e não num peso por letra acentuada.
        acentuado = texto_juridico_acentuado()
        sem_acento = "".join(c for c in unicodedata.normalize("NFD", acentuado) if not unicodedata.combining(c))
        for nome, texto, real in (("completo", acentuado, 7379), ("sem acento", sem_acento, 7653),
                                  ("minúsculas", acentuado.lower(), 7303),
                                  ("sem acento e minúsculas", sem_acento.lower(), 7567)):
            with self.subTest(nome):
                self.assertEqual(len(texto), 30000)
                estimado = estimar_tokens(texto)
                self.assertGreaterEqual(estimado, real)
                self.assertLessEqual(estimado, real * 1.1)

    def test_digitos_contam_um_a_um_e_letras_em_grupo(self):
        self.assertGreater(estimar_tokens("1234567890"), estimar_tokens("abcdefghij") * 3)

    def test_simbolos_seguidos_contam_como_um_grupo(self):
        # `","` do JSON é um token só no modelo: contá-los um a um passaria 46% do real.
        self.assertLess(estimar_tokens('","'), estimar_tokens(", . ;"))

    def test_simbolos_repetidos_nao_viram_um_token_so(self):
        # Uma linha de "=====" ocupa tokens: um grupo sem limite a contaria quase de graça.
        self.assertGreaterEqual(estimar_tokens("=" * 100), 25)

    def test_caracteres_ate_acha_a_posicao_pelos_tokens(self):
        for base in (PROSA, LISTA_IDS, JSON_DUO):
            with self.subTest(base[:10]):
                texto = amostra(base)
                for inicio in (0, 1234):
                    fim = caracteres_ate(texto, 500, inicio)
                    self.assertLessEqual(estimar_tokens(texto[inicio:fim]), 500)
                    self.assertGreater(estimar_tokens(texto[inicio:fim + 8]), 500)
        self.assertEqual(caracteres_ate("abc", 500), 3)  # tudo cabe
        self.assertEqual(caracteres_ate("abcdef", 0), 0)

    def test_nao_e_mais_pesado_junto_do_que_em_partes(self):
        # A nota e o sistema são somados assim: a reserva da nota não pode faltar ao juntá-los.
        for a, b in (("abc", "def"), ("1,", ",2"), (PROSA, LISTA_IDS), (JSON_DUO, JSON_DUO)):
            self.assertLessEqual(estimar_tokens(a + b), estimar_tokens(a) + estimar_tokens(b))


class TestTruncarMeio(unittest.TestCase):
    def test_no_limite_nao_corta(self):
        self.assertEqual(truncar_meio("a" * 30, 30), "a" * 30)

    def test_limite_zero_so_avisa(self):
        saida = truncar_meio("abcdef", 0)
        self.assertIn("2 tokens omitidos", saida)
        self.assertNotIn("abc", saida)

    def test_proporcao_dois_tercos_e_um_terco(self):
        texto = "A" * 100 + "B" * 100
        saida = truncar_meio(texto, 90)
        self.assertTrue(saida.startswith("A" * 60 + "\n"))
        self.assertTrue(saida.endswith("\n" + "B" * 30))

    def test_truncar_meio_conta_omitidos(self):
        saida = truncar_meio("x" * 1000, 300)
        self.assertIn("165 tokens omitidos", saida)  # 700 caracteres só de letras


def texto_entre_marcas(texto):
    return texto.split("\n[…", 1)[0]


class TestAjustar(unittest.TestCase):
    def test_cabe_tudo_sem_avisos(self):
        r = ajustar("fixo", "duo", [turno("oi"), turno("tchau")], 16384, True)
        self.assertEqual(r.avisos, [])
        self.assertEqual(r.mensagens[0], {"papel": "sistema", "texto": "fixo\n\nduo"})
        self.assertEqual(len(r.mensagens), 5)

    def test_sem_sistema_duo_nao_deixa_separador(self):
        r = ajustar("fixo", "", [turno("oi")], 16384, True)
        self.assertEqual(r.mensagens[0]["texto"], "fixo")

    def test_remove_antigos_preservando_primeiro_e_atual(self):
        # contexto 8192 -> orçamento 6144 tokens (~18 mil caracteres)
        turnos = [turno("PRIMEIRO"), turno("b" * 15000), turno("c" * 15000), turno("d" * 15000),
                  turno("ATUAL")]
        r = ajustar("", "", turnos, 8192, True)
        textos = [m["texto"] for m in r.mensagens]
        self.assertIn("PRIMEIRO", textos)
        self.assertIn("ATUAL", textos)
        self.assertNotIn("b" * 15000, textos)  # o mais antigo sai primeiro
        self.assertIn("d" * 15000, textos)
        self.assertEqual(r.avisos[0],
                         "2 turno(s) antigo(s) ficaram fora da janela de contexto (8192 tokens).")
        self.assertLessEqual(total(r), 6144)

    def test_sem_fixar_remove_o_primeiro(self):
        turnos = [turno("PRIMEIRO" + "a" * 15000), turno("b" * 15000), turno("ATUAL")]
        r = ajustar("", "", turnos, 8192, False)
        textos = " ".join(m["texto"] for m in r.mensagens)
        self.assertNotIn("PRIMEIRO", textos)
        self.assertIn("ATUAL", textos)
        self.assertTrue(r.avisos[0].startswith("1 turno(s)"))

    def test_primeiro_fixo_perde_anexos_antes_de_sumir(self):
        primeiro = [
            {"papel": "usuario", "texto": "pergunta\n" + "anexo" * 7500, "texto_sem_anexos": "pergunta"},
            {"papel": "assistente", "texto": "resposta"},
        ]
        r = ajustar("", "", [primeiro, turno("ATUAL")], 8192, True)
        textos = [m["texto"] for m in r.mensagens[1:]]
        self.assertEqual(textos[0], "pergunta")
        self.assertNotIn("resposta", textos)
        self.assertIn("ATUAL", textos)
        # nenhum turno foi removido, mas a perda do primeiro é avisada
        self.assertEqual(r.avisos, ["Os anexos e a resposta do primeiro pedido saíram da janela "
                                    "(o texto do pedido foi mantido)."])
        self.assertLessEqual(total(r), 6144)

    def test_primeiro_sem_nada_a_tirar_nao_avisa_reducao(self):
        # O 1º pedido é só o texto (sem anexos, sem resposta): "reduzi-lo" não muda nada, e o
        # aviso de que anexos e resposta saíram seria falso. Resta o excesso, que o turno recusa.
        primeiro = [{"papel": "usuario", "texto": "pergunta"}]
        r = ajustar("", "", [primeiro, turno("x" * 60000)], 4096, True)
        self.assertEqual(r.mensagens[1], primeiro[0])
        self.assertEqual(r.avisos, [])
        self.assertTrue(r.excede)

    def test_sistema_duo_grande_resumido(self):
        r = ajustar("fixo", "d" * 45000, [turno("oi")], 8192, True)
        self.assertIn("As instruções do DUO foram resumidas para caber na janela.", r.avisos)
        self.assertIn("tokens omitidos", r.mensagens[0]["texto"])
        # Até 0,4 * 6144 = 2458 tokens, mais o aviso de omissão e o "fixo".
        self.assertLess(estimar_tokens(r.mensagens[0]["texto"]), 2480)

    def test_sistema_duo_denso_e_medido_em_tokens(self):
        # Uma lista de IDs passa do teto com bem menos caracteres que um texto corrido, e o corte
        # acompanha: resumir por caracteres deixaria o sistema acima do teto.
        r = ajustar("", amostra(LISTA_IDS, 10000), [turno("oi")], 8192, True)
        self.assertIn("As instruções do DUO foram resumidas para caber na janela.", r.avisos)
        self.assertLess(estimar_tokens(r.mensagens[0]["texto"]), 2480)

    def test_excesso_nao_tira_o_turno_atual(self):
        r = ajustar("", "", [turno("x" * 60000)], 4096, True)
        self.assertEqual([m["papel"] for m in r.mensagens], ["sistema", "usuario", "assistente"])
        self.assertTrue(r.excede)
        # A conta é contra o espaço útil: o resto da janela fica reservado para a resposta.
        self.assertEqual(r.orcamento, 3072)

    def test_chamadas_pesam_no_total(self):
        com = [{"papel": "assistente", "texto": "",
                "chamadas": [{"id": "1", "nome": "ler", "argumentos": {"p": "z" * 45000}}]}]
        r = ajustar("", "", [com, turno("ATUAL")], 4096, False)
        self.assertTrue(any("turno(s)" in a for a in r.avisos))

    def test_definicoes_de_ferramentas_pesam_na_janela(self):
        # As 21 ferramentas do Duo no modo nativo: vão no campo `tools`, mas o motor as põe no prompt.
        peso = estimar_tokens(json.dumps(para_funcoes(ferramentas_do_duo()), ensure_ascii=False))
        self.assertGreater(peso, 3000)
        # contexto 16384 -> orçamento 12288; os turnos somam ~9 mil tokens e cabem sozinhos.
        turnos = [turno("PRIMEIRO"), turno("b" * 13500), turno("c" * 13500), turno("d" * 13500),
                  turno("ATUAL")]
        sem = ajustar("sistema", "", turnos, 16384, True)
        self.assertEqual(sem.avisos, [])
        com = ajustar("sistema", "", turnos, 16384, True, peso_ferramentas=peso)
        self.assertEqual(com.avisos,
                         ["1 turno(s) antigo(s) ficaram fora da janela de contexto (16384 tokens).",
                          "A resposta do primeiro pedido saiu da janela (o texto do pedido foi mantido)."])
        textos = [m["texto"] for m in com.mensagens]
        self.assertIn("PRIMEIRO", textos)
        self.assertIn("ATUAL", textos)
        self.assertNotIn("b" * 13500, textos)
        self.assertLessEqual(total(com), 16384 - 4096)

    def test_ferramentas_maiores_que_a_janela_contam_no_excesso(self):
        r = ajustar("", "", [turno("oi")], 4096, True, peso_ferramentas=5654)
        self.assertTrue(r.excede)
        self.assertEqual(r.peso_ferramentas, 5654)

    def test_documento_enorme_em_janela_pequena(self):
        def no(uuid, pai, sender, content, text=""):
            return {"uuid": uuid, "parent_message_uuid": pai, "sender": sender,
                    "text": text, "content": content}
        nos = [
            no("1", None, "human", [], "leia o documento"),
            no("2", "1", "assistant", [
                {"type": "tool_use", "id": "t1", "name": "ler_doc", "input": {"id": 1}},
                {"type": "tool_result", "tool_use_id": "t1",
                 "content": [{"type": "text", "text": "z" * 450000}]},
                {"type": "text", "text": "li"},
            ]),
            no("3", "2", "human", [], "e agora?"),
        ]
        turnos = historico.para_turnos(nos, "nativo")
        r = ajustar("sistema", "duo", turnos, 16384, True)
        self.assertLessEqual(total(r), 16384 - 4096)
        self.assertIn(omitido("t1"), textos_de(r))  # é de um turno anterior: sai, com a marca de como ler


class TestOrdemDeCorte(unittest.TestCase):
    """contexto 8192 -> orçamento 6144 tokens (~18 mil caracteres, 3 por token)."""

    def test_limpa_resultados_anteriores_do_mais_antigo_so_o_necessario(self):
        turnos = [[usuario("t0"), *rodada("a", "A" * 9450), *rodada("b", "B" * 9450), assistente("conclusão 0")],
                  [usuario("t1"), *rodada("c", "C" * 9450), assistente("conclusão 1")],
                  [usuario("ATUAL")]]
        original = json.dumps(turnos)
        r = ajustar("", "", turnos, 8192, True)
        textos = textos_de(r)
        self.assertIn(omitido("a0"), textos)
        self.assertNotIn("A" * 9450, textos)
        self.assertIn("B" * 9450, textos)  # um só bastou: os mais novos ficam
        self.assertIn("C" * 9450, textos)
        self.assertIn("conclusão 0", textos)  # o texto do assistente não é resultado
        self.assertIn("conclusão 1", textos)
        self.assertEqual(r.avisos, ["1 resultado(s) antigo(s) de ferramenta (ler; 2224 tokens) ficaram fora "
                                    "da janela de contexto (8192 tokens)."])
        self.assertLessEqual(total(r), 6144)
        self.assertEqual(json.dumps(turnos), original)  # quem chama continua com os turnos intactos

    def test_limpar_mantem_o_par_chamada_resultado(self):
        turnos = [[usuario("t0"), *rodada("a", "A" * 30000), assistente("fim")], [usuario("ATUAL")]]
        r = ajustar("", "", turnos, 8192, True)
        chamada, resultado = r.mensagens[2], r.mensagens[3]
        self.assertEqual(chamada["chamadas"][0]["id"], "a0")
        self.assertEqual((resultado["papel"], resultado["id_chamada"], resultado["nome"], resultado["texto"]),
                         ("ferramenta", "a0", "ler", omitido("a0")))

    def test_resultado_menor_que_a_marca_fica_como_esta(self):
        # Trocar um resultado curto pela marca aumentaria o pedido e contaria uma perda falsa.
        turnos = [[usuario("t0"), *rodada("a", "curto"), *rodada("b", "B" * 30000), assistente("fim")],
                  [usuario("ATUAL")]]
        r = ajustar("", "", turnos, 8192, True)
        self.assertIn("curto", textos_de(r))
        self.assertEqual(textos_de(r).count(omitido("b0")), 1)
        self.assertIn("1 resultado(s) antigo(s)", r.avisos[0])

    def test_resultados_saem_antes_de_reduzir_o_primeiro_pedido(self):
        primeiro = [{"papel": "usuario", "texto": "pedido\n" + "x" * 4500, "texto_sem_anexos": "pedido"},
                    *rodada("a", "A" * 30000), assistente("RESPOSTA")]
        r = ajustar("", "", [primeiro, [usuario("ATUAL")]], 8192, True)
        textos = textos_de(r)
        self.assertIn("RESPOSTA", textos)
        self.assertIn("pedido\n" + "x" * 4500, textos)  # os anexos ainda cabem
        self.assertIn(omitido("a0"), textos)
        self.assertFalse(any("primeiro pedido" in a for a in r.avisos))

    def test_primeiro_pedido_e_reduzido_antes_de_remover_turnos(self):
        primeiro = [{"papel": "usuario", "texto": "pedido\n" + "x" * 30000, "texto_sem_anexos": "pedido"},
                    assistente("RESPOSTA")]
        r = ajustar("", "", [primeiro, turno("MEIO"), [usuario("ATUAL")]], 8192, True)
        self.assertEqual(textos_de(r), ["pedido", "MEIO", "ok", "ATUAL"])
        self.assertFalse(any("turno(s)" in a for a in r.avisos))

    def test_remove_turnos_do_mais_antigo_sem_pular(self):
        turnos = [turno("A" + "a" * 12000), turno("B" + "b" * 12000), turno("C" + "c" * 12000), [usuario("ATUAL")]]
        r = ajustar("", "", turnos, 8192, False)
        visiveis = " ".join(textos_de(r))
        self.assertNotIn("Aaaa", visiveis)
        self.assertIn("Bbbb", visiveis)
        self.assertIn("Cccc", visiveis)
        self.assertEqual(r.avisos, ["1 turno(s) antigo(s) ficaram fora da janela de contexto (8192 tokens)."])

    def test_turno_removido_nao_conta_duas_vezes_os_resultados(self):
        # O resultado limpo que depois saiu com o turno inteiro já está no "1 turno(s)".
        turnos = [[usuario("A" * 13500), *rodada("a", "R" * 4500), assistente("fim")],
                  turno("B" * 15000), [usuario("ATUAL")]]
        r = ajustar("", "", turnos, 8192, False)
        self.assertEqual(r.avisos, ["1 turno(s) antigo(s) ficaram fora da janela de contexto (8192 tokens)."])
        self.assertIn("1 turno(s) antigo(s) foram omitidos", r.mensagens[0]["texto"])
        self.assertNotIn("resultado(s)", r.mensagens[0]["texto"])

    def test_turno_atual_so_e_tocado_depois_de_esgotar_os_anteriores(self):
        anterior = [usuario("t0"), *rodada("a", "A" * 4500), assistente("fim")]
        atual = [usuario("PEDIDO"), *rodada("b", "B" * 10500), *rodada("c", "C" * 10500)]
        # O atual cresce um pouco: limpar o resultado do turno anterior basta.
        cabe = ajustar("", "", [anterior, [*atual, *rodada("d", "D" * 3000)]], 8192, True)
        self.assertNotIn("A" * 4500, textos_de(cabe))
        self.assertIn("fim", textos_de(cabe))
        self.assertIn("B" * 10500, textos_de(cabe))
        # Com o anterior já sem nada a tirar, o resultado mais velho do atual é o que resta.
        passa = ajustar("", "", [anterior, [*atual, *rodada("d", "D" * 10500)]], 8192, True)
        textos = textos_de(passa)
        self.assertEqual(textos[0], "t0")  # o anterior foi reduzido ao texto do pedido
        self.assertNotIn("fim", textos)
        self.assertNotIn("B" * 10500, textos)
        self.assertIn("C" * 10500, textos)
        self.assertIn("D" * 10500, textos)
        self.assertIn("PEDIDO", textos)

    def test_ultimo_recurso_limpa_rodadas_velhas_e_preserva_pedido_e_ultima_rodada(self):
        atual = [{"papel": "usuario", "texto": "PEDIDO", "texto_sem_anexos": "PEDIDO"},
                 *rodada("a", "A" * 10500), *rodada("b", "B" * 10500), *rodada("c", "C" * 5250, "D" * 5250)]
        r = ajustar("", "", [atual], 8192, True)
        textos = textos_de(r)
        self.assertEqual(textos[0], "PEDIDO")
        self.assertNotIn("A" * 10500, textos)
        self.assertIn("B" * 10500, textos)  # só o necessário
        self.assertIn("C" * 5250, textos)
        self.assertIn("D" * 5250, textos)  # todos os resultados da última rodada ficam
        self.assertEqual(r.mensagens[3]["id_chamada"], "a0")
        self.assertEqual(r.avisos, ["1 resultado(s) antigo(s) de ferramenta (ler; 2471 tokens) ficaram fora "
                                    "da janela de contexto (8192 tokens)."])
        self.assertLessEqual(total(r), 6144)

    @staticmethod
    def _cercas(*resultados, resposta="LI TUDO"):
        conteudo = [{"type": "tool_use", "id": f"t{i}", "name": f"f{i}", "input": {}} for i in range(len(resultados))]
        conteudo += [{"type": "tool_result", "tool_use_id": f"t{i}", "content": r} for i, r in enumerate(resultados)]
        if resposta:
            conteudo.append({"type": "text", "text": resposta})
        return [no_("1", None, "human", [], "leia"), no_("2", "1", "assistant", conteudo)]

    def test_cercas_limpa_um_resultado_por_vez_e_refaz_a_mensagem(self):
        nos = [*self._cercas("A" * 28500, "B" * 3000), no_("3", "2", "human", [], "e agora?")]
        r = ajustar("", "", historico.para_turnos(nos, "cercas"), 8192, True)
        textos = textos_de(r)
        self.assertEqual(r.mensagens[3]["papel"], "usuario")
        # A mensagem é refeita inteira: é pela marca e pelas cercas que o modelo a reconhece.
        self.assertEqual(textos[2], emulacao.mensagem_resultados([{"nome": "f0", "conteudo": omitido("t0")},
                                                                  {"nome": "f1", "conteudo": "B" * 3000}]))
        self.assertIn(emulacao.chamada_como_texto("f0", {}), textos[1])  # a chamada continua lá
        self.assertIn("LI TUDO", textos)
        self.assertIn("1 resultado(s) antigo(s) de ferramenta (f0; ", r.avisos[0])
        self.assertIn("1 resultado(s) de ferramenta", r.mensagens[0]["texto"])

    def test_cercas_conta_cada_resultado_que_sai(self):
        nos = [*self._cercas("A" * 30000, "B" * 30000), no_("3", "2", "human", [], "e agora?")]
        r = ajustar("", "", historico.para_turnos(nos, "cercas"), 8192, True)
        self.assertIn("2 resultado(s) antigo(s) de ferramenta (f0, f1; ", r.avisos[0])
        self.assertIn("2 resultado(s) de ferramenta", r.mensagens[0]["texto"])

    def test_cercas_trunca_so_o_resultado_grande_da_ultima_rodada(self):
        turnos = historico.para_turnos(self._cercas("P" * 900, "G" * 60000, resposta=""), "cercas")
        r = ajustar("", "", turnos, 8192, True)
        texto = r.mensagens[-1]["texto"]
        self.assertTrue(texto.startswith(emulacao.MARCA_RESULTADOS))
        self.assertIn("P" * 900, texto)
        self.assertNotIn("G" * 60000, texto)
        self.assertIn('leia com ler_trecho_resultado(id=\\"t1\\", inicio=', texto)
        self.assertLessEqual(total(r), 6144)
        self.assertEqual(len(r.avisos), 1)
        self.assertIn("de f1.", r.avisos[0])

    def test_correcao_nao_e_resultado(self):
        correcao = usuario(f"{emulacao.MARCA_RESULTADOS}\ncorrija" + "z" * 30000)
        r = ajustar("", "", [[usuario("t0"), assistente("x"), correcao], [usuario("ATUAL")]], 8192, False)
        self.assertNotIn("resultado omitido", " ".join(textos_de(r)))
        self.assertEqual(textos_de(r), ["ATUAL"])


class TestNotaAoModelo(unittest.TestCase):
    def test_sem_omissao_nao_ha_nota(self):
        r = ajustar("fixo", "", [[usuario("oi"), *rodada("a", "curto"), assistente("fim")], [usuario("ATUAL")]],
                    8192, True)
        self.assertEqual(r.mensagens[0]["texto"], "fixo")

    def test_nota_diz_o_que_saiu_e_manda_consultar_de_novo(self):
        turnos = [[usuario("t0"), *rodada("a", "A" * 18000), assistente("fim")],
                  [usuario("t1"), *rodada("b", "B" * 18000), assistente("fim")],
                  turno("t2"), [usuario("ATUAL")]]
        r = ajustar("fixo", "", turnos, 8192, False)
        sistema = r.mensagens[0]["texto"]
        self.assertTrue(sistema.startswith("fixo\n\n[Nota do servidor: para caber na janela, "))
        self.assertIn("consulte de novo com as ferramentas", sistema)
        self.assertIn("não afirme que algo não existe ou não foi encontrado", sistema)
        self.assertIn("não conclua sobre um documento com parte omitida sem lê-la", sistema)
        self.assertIn("1 resultado(s) de ferramenta foram omitidos", sistema)
        self.assertIn("Leia o que precisar com ler_trecho_resultado", sistema)

    def test_nota_sem_marcas_nao_oferece_a_leitura(self):
        # A ferramenta só existe quando há o que ler: a nota não pode citar o que o modelo não tem.
        r = ajustar("", "", [turno("A" * 45000), [usuario("ATUAL")]], 8192, False)
        self.assertIn("1 turno(s) antigo(s) foram omitidos", r.mensagens[0]["texto"])
        self.assertNotIn("ler_trecho_resultado", r.mensagens[0]["texto"])
        self.assertFalse(r.leitura)

    def test_nota_conta_o_resultado_truncado(self):
        r = ajustar("", "", [[usuario("PEDIDO"), *rodada("a", "A" * 60000)]], 8192, True)
        self.assertIn("parte de 1 resultado(s) foram omitidos", r.mensagens[0]["texto"])

    def test_nota_com_resultados_e_turnos(self):
        turnos = [turno("A" * 4500), [usuario("t1"), *rodada("b", "B" * 27900), assistente("fim")],
                  [usuario("ATUAL")]]
        r = ajustar("", "", turnos, 8192, False)
        # O turno mais velho sai inteiro, e só depois o resultado do mais novo é limpo.
        self.assertIn("1 resultado(s) de ferramenta e 1 turno(s) antigo(s) foram omitidos", r.mensagens[0]["texto"])

    def test_nota_cita_o_primeiro_pedido(self):
        primeiro = [usuario("pergunta"), assistente("r" * 30000)]
        r = ajustar("", "", [primeiro, [usuario("ATUAL")]], 8192, True)
        self.assertIn("a resposta do primeiro pedido foi omitida", r.mensagens[0]["texto"])

    def test_nota_conta_no_orcamento(self):
        # Sem a nota, o pedido cabe depois de limpar um resultado; a conta tem de incluí-la.
        for tamanho in range(25500, 28500, 53):
            turnos = [[usuario("t0"), *rodada("a", "A" * 300), *rodada("b", "B" * tamanho), assistente("fim")],
                      [usuario("ATUAL")]]
            with self.subTest(tamanho=tamanho):
                self.assertLessEqual(total(ajustar("", "", turnos, 8192, False)), 6144)


class TestAvisosAoUsuario(unittest.TestCase):
    def test_primeiro_so_com_resposta_nao_fala_de_anexos(self):
        r = ajustar("", "", [[usuario("pergunta"), assistente("r" * 30000)], [usuario("ATUAL")]], 8192, True)
        self.assertEqual(r.avisos, ["A resposta do primeiro pedido saiu da janela (o texto do pedido foi mantido)."])

    def test_primeiro_so_com_anexos_nao_fala_de_resposta(self):
        primeiro = [{"papel": "usuario", "texto": "pergunta\n" + "x" * 30000, "texto_sem_anexos": "pergunta"}]
        r = ajustar("", "", [primeiro, [usuario("ATUAL")]], 8192, True)
        self.assertEqual(r.avisos, ["Os anexos do primeiro pedido saíram da janela (o texto do pedido foi mantido)."])

    def test_resultado_limpo_nomeia_a_ferramenta_e_os_tokens(self):
        r = ajustar("", "", [[usuario("t0"), *rodada("a", "A" * 30000), assistente("fim")], [usuario("ATUAL")]],
                    8192, True)
        self.assertEqual(r.avisos, ["1 resultado(s) antigo(s) de ferramenta (ler; 7059 tokens) ficaram fora "
                                    "da janela de contexto (8192 tokens)."])

    def test_chamada_interna_nao_vira_aviso(self):
        # O usuário nunca viu estas chamadas; para o modelo elas continuam contando, na nota.
        interno = {"papel": "ferramenta", "id_chamada": "a0", "nome": "ler_trecho_resultado",
                   "texto": "A" * 30000, "interno": True}
        r = ajustar("", "", [[usuario("t0"), interno, assistente("fim")], [usuario("ATUAL")]], 8192, True)
        self.assertEqual(r.avisos, [])
        self.assertIn("1 resultado(s) de ferramenta foram omitidos", r.mensagens[0]["texto"])
        grande = {**interno, "texto": "A" * 60000}
        r = ajustar("", "", [[usuario("PEDIDO"), grande]], 8192, True)
        self.assertEqual((r.avisos, r.truncados), ([], []))
        self.assertIn("parte de 1 resultado(s)", r.mensagens[0]["texto"])

    def test_resultado_truncado_diz_quantos_tokens_o_modelo_recebeu(self):
        r = ajustar("", "", [[usuario("PEDIDO"), *rodada("a", "A" * 60000)]], 8192, True)
        self.assertEqual(len(r.avisos), 1)
        achado = re.fullmatch(r"Para caber na janela, o modelo recebeu (\d+) de 14118 tokens de ler\.", r.avisos[0])
        self.assertIsNotNone(achado, r.avisos[0])
        self.assertLess(int(achado[1]), 6144)
        self.assertGreater(int(achado[1]), 5500)  # aproveita a janela
        self.assertEqual([(t.id, t.nome, t.total) for t in r.truncados], [("a0", "ler", 14118)])


class TestTruncamento(unittest.TestCase):
    """Um resultado só é truncado quando não cabe mesmo depois de todos os outros cortes."""

    def test_documentos_que_cabem_chegam_inteiros(self):
        # O caso real: documentos de 12.525 e 13.356 caracteres eram cortados com a janela sobrando.
        doc1, doc2 = amostra(PROSA, 12525), amostra(LISTA_IDS, 13356)
        r = ajustar("sistema", "", [[usuario("leia"), *rodada("a", doc1, doc2)]], 32768, True)
        self.assertEqual(r.avisos, [])
        self.assertFalse(r.leitura)
        self.assertIn(doc1, textos_de(r))
        self.assertIn(doc2, textos_de(r))

    def test_antes_de_truncar_sai_todo_o_resto(self):
        doc = "D" * 25000
        turnos = [[usuario("t0"), *rodada("z", "Z" * 9000), assistente("fim")],
                  [usuario("PEDIDO"), *rodada("v", "V" * 3000), *rodada("a", doc)]]
        r = ajustar("", "", turnos, 8192, False)
        self.assertIn(doc, textos_de(r))
        self.assertEqual(r.truncados, [])

    def test_marca_e_exata(self):
        doc = amostra(PROSA, 60000)
        r = ajustar("", "", [[usuario("PEDIDO"), *rodada("a", doc)]], 8192, True)
        texto = r.mensagens[3]["texto"]
        achado = re.search(r'\n\[… (\d+) de (\d+) tokens omitidos; leia com ler_trecho_resultado\(id="a0", '
                           r'inicio=(\d+)\) …\]\n', texto)
        self.assertIsNotNone(achado, texto[:200])
        inicio, cauda = int(achado[3]), texto[achado.end():]
        fim = len(doc) - len(cauda)
        self.assertEqual(texto[:achado.start()], doc[:inicio])
        self.assertEqual(cauda, doc[fim:])
        self.assertTrue(cauda)
        self.assertEqual(int(achado[2]), estimar_tokens(doc))
        self.assertEqual(int(achado[1]), estimar_tokens(doc[inicio:fim]))
        self.assertLessEqual(total(r), 6144)
        self.assertTrue(r.leitura)
        self.assertEqual(r.vistos["a0"], [(0, inicio), (fim, len(doc))])

    def test_texto_denso_e_truncado_pelos_tokens(self):
        doc = amostra(LISTA_IDS, 30000)
        r = ajustar("", "", [[usuario("PEDIDO"), *rodada("a", doc)]], 8192, True)
        self.assertLessEqual(total(r), 6144)
        self.assertGreater(total(r), 5500)

    def test_resultado_pequeno_da_rodada_fica_inteiro(self):
        pequeno = "P" * 4500
        r = ajustar("", "", [[usuario("PEDIDO"), *rodada("a", pequeno, "G" * 90000)]], 8192, True)
        self.assertIn(pequeno, textos_de(r))
        self.assertEqual([t.id for t in r.truncados], ["a1"])
        self.assertLessEqual(total(r), 6144)

    def test_varios_grandes_dividem_o_espaco(self):
        r = ajustar("", "", [[usuario("PEDIDO"), *rodada("a", "A" * 60000, "B" * 90000)]], 8192, True)
        self.assertEqual([t.id for t in r.truncados], ["a0", "a1"])
        recebidos = [t.recebidos for t in r.truncados]
        self.assertLess(abs(recebidos[0] - recebidos[1]), 100)
        self.assertLessEqual(total(r), 6144)

    def test_sem_espaco_nem_para_o_minimo_marca_o_excesso(self):
        r = ajustar("", "", [[usuario("PEDIDO"), *rodada("a", "A" * 60000)]], 8192, True, peso_ferramentas=20000)
        self.assertTrue(r.excede)
        self.assertIn("A" * 1000, textos_de(r)[2])  # fica o mínimo, em vez de só a marca

    def test_resultado_sem_nome_nao_derruba_nem_sai_em_branco(self):
        limpo = {"papel": "ferramenta", "id_chamada": "a0", "texto": "A" * 30000}
        r = ajustar("", "", [[usuario("t0"), limpo, assistente("fim")], [usuario("ATUAL")]], 8192, True)
        self.assertIn("(ferramenta; 7059 tokens)", r.avisos[0])

    def test_vistos_marca_inteiro_e_limpo(self):
        turnos = [[usuario("t0"), *rodada("a", "A" * 30000), assistente("fim")],
                  [usuario("ATUAL"), *rodada("b", "abc")]]
        r = ajustar("", "", turnos, 8192, True)
        self.assertEqual(r.vistos, {"a0": [], "b0": [(0, 3)]})

    def test_limite_explicito_trunca_mesmo_com_espaco(self):
        doc = amostra(PROSA, 13500)
        turnos = [[usuario("t0"), *rodada("a", doc), assistente("fim")], [usuario("PEDIDO"), *rodada("b", doc)]]
        r = ajustar("", "", turnos, 32768, True, limite_resultado=1000)
        self.assertTrue(r.leitura)
        for id_ in ("a0", "b0"):
            self.assertIn(f'ler_trecho_resultado(id="{id_}"', "\n".join(textos_de(r)))
        self.assertLess(total(r), 2200)
        self.assertEqual([t.id for t in r.truncados], ["b0"])  # só o do turno atual vira aviso individual
        total_doc = estimar_tokens(doc)
        antigo = next(a for a in r.avisos if a.startswith("1 resultado(s) antigo(s)"))
        achado = re.fullmatch(r"1 resultado\(s\) antigo\(s\) de ferramenta \(ler\): pelo limite_resultado, o modelo "
                              rf"recebeu (\d+) de {total_doc} tokens e pode ler o resto com ler_trecho_resultado\.", antigo)
        self.assertIsNotNone(achado, antigo)
        self.assertLessEqual(int(achado[1]), 1000)
        self.assertGreater(int(achado[1]), 800)
        recebidos = r.truncados[0].recebidos
        self.assertIn(f"Pelo limite_resultado, o modelo recebeu {recebidos} de {total_doc} tokens de ler.", r.avisos)
        self.assertIn("parte de 2 resultado(s) foram omitidos", r.mensagens[0]["texto"])

    def test_varios_antigos_somam_os_tokens(self):
        doc = amostra(PROSA, 13500)
        turnos = [[usuario("t0"), *rodada("a", doc, doc), assistente("fim")], [usuario("PEDIDO")]]
        r = ajustar("", "", turnos, 32768, True, limite_resultado=1000)
        self.assertRegex(r.avisos[0], rf"^2 resultado\(s\) antigo\(s\) de ferramenta \(ler\): pelo limite_resultado, "
                                      rf"o modelo recebeu \d+ de {estimar_tokens(doc) * 2} tokens")

    def test_limite_que_nao_basta_diz_que_foi_para_caber(self):
        doc = amostra(PROSA, 60000)
        r = ajustar("", "", [[usuario("PEDIDO"), *rodada("a", doc)]], 8192, True, limite_resultado=12000)
        self.assertTrue(r.avisos[0].startswith("Para caber na janela, o modelo recebeu "), r.avisos[0])

    def test_limite_explicito_nao_impede_de_apertar_mais(self):
        doc = amostra(PROSA, 60000)
        r = ajustar("", "", [[usuario("PEDIDO"), *rodada("a", doc)]], 8192, True, limite_resultado=12000)
        self.assertLessEqual(total(r), 6144)
        self.assertEqual(texto_entre_marcas(r.mensagens[3]["texto"]), doc[:len(texto_entre_marcas(r.mensagens[3]["texto"]))])


class TestFatorTokens(unittest.TestCase):
    TURNOS = [[usuario("t0"), *rodada("a", "A" * 18000), assistente("fim")], [usuario("ATUAL")]]

    def test_fator_escala_as_estimativas(self):
        # ~4 mil tokens cabem nos 6144; contados a 1,6 token por estimado, não.
        self.assertEqual(ajustar("", "", self.TURNOS, 8192, True).avisos, [])
        r = ajustar("", "", self.TURNOS, 8192, True, fator_tokens=1.6)
        self.assertIn(omitido("a0"), textos_de(r))
        self.assertLessEqual(total(r) * 1.6, 6144)

    def test_fator_escala_tambem_as_definicoes_de_ferramentas(self):
        turnos = [[usuario("t0"), *rodada("a", "A" * 4500), assistente("fim")], [usuario("ATUAL")]]
        self.assertEqual(ajustar("", "", turnos, 8192, True, peso_ferramentas=3500).avisos, [])
        r = ajustar("", "", turnos, 8192, True, peso_ferramentas=3500, fator_tokens=1.6)
        self.assertIn(omitido("a0"), textos_de(r))

    def test_estimativa_devolvida_nao_leva_o_fator(self):
        # É a base do próximo fator: com o fator embutido, ele se acumularia a cada rodada.
        for fator in (1.0, 1.6):
            with self.subTest(fator=fator):
                r = ajustar("", "", self.TURNOS, 8192, True, peso_ferramentas=100, fator_tokens=fator)
                textos = sum(estimar_tokens(m["texto"]) for m in r.mensagens)
                peso_chamadas = sum(estimar_tokens(json.dumps(m["chamadas"], ensure_ascii=False))
                                    for m in r.mensagens if m.get("chamadas"))
                self.assertEqual(r.estimativa, textos + peso_chamadas + 100)

    def test_excesso_usa_o_total_com_fator(self):
        r = ajustar("", "", [[usuario("x" * 42500)]], 8192, True, fator_tokens=2.0)
        self.assertEqual(r.total, 20000)
        self.assertTrue(r.excede)

    def test_instrucoes_do_duo_nao_mudam_com_o_fator(self):
        # O teto das instruções é uma regra de divisão da janela, não uma conta de tokens:
        # resumi-las só porque a medição subiu mudaria as regras do modelo no meio do turno.
        sistema_duo = "d" * 10000
        r = ajustar("fixo", sistema_duo, [[usuario("oi")]], 8192, True, fator_tokens=2.0)
        self.assertEqual(r.avisos, [])
        self.assertEqual(r.mensagens[0]["texto"], "fixo\n\n" + sistema_duo)

    def test_fator_real_fica_entre_1_e_2(self):
        self.assertEqual(fator_real(1500, 1000), 1.5)
        self.assertEqual(fator_real(500, 1000), 1.0)  # o prompt_eval_count pode excluir o prefixo em cache
        self.assertEqual(fator_real(9000, 1000), 2.0)
        self.assertEqual(fator_real(100, 0), 1.0)


class TestTurnoQueAchouNaoSaiAntesDoQueNaoAchou(unittest.TestCase):
    """A sessão real que motivou a ordem de corte, com o tamanho dos resultados dela e texto sintético.

    Turno 1: o modelo não achou o processo originário. Turno 2: achou, por resultados de
    ferramenta. Turno 3: "ficou sem contexto?". O corte antigo tirava o turno 2 e devolvia o turno 1
    inteiro, e o modelo respondia "não localizei" e "não há falta de contexto".
    """

    CONTEXTO = 32768
    PESO_FERRAMENTAS = 5700  # ~20 definições no modo nativo
    SISTEMA = "s" * 6000
    NAO_ACHOU = "Não localizei o processo originário."
    ACHOU = "Encontrei o processo originário: 0000123-45.2020."

    @staticmethod
    def _nos_do_turno(n, pedido, resultados, resposta):
        conteudo = []
        for i, resultado in enumerate(resultados):
            tid = f"t{n}_{i}"
            conteudo.append({"type": "tool_use", "id": tid, "name": "ler", "input": {"id": i}})
            conteudo.append({"type": "tool_result", "tool_use_id": tid,
                             "content": f"R{n}.{i} " + resultado})
        conteudo.append({"type": "text", "text": resposta})
        pai = f"a{n - 1}" if n > 1 else None
        return [no_(f"h{n}", pai, "human", [], pedido), no_(f"a{n}", f"h{n}", "assistant", conteudo)]

    def _sessao(self, extras=()):
        """Os nós dos 2 turnos reais (~43k e ~23k caracteres de listas em JSON), mais `extras`
        turnos, cada um com os tamanhos de resultado dados, e um pedido atual curto."""
        nos = self._nos_do_turno(1, "ache o processo originário", [amostra(JSON_DUO, 8600)] * 5, self.NAO_ACHOU)
        nos += self._nos_do_turno(2, "procure de novo", [amostra(JSON_DUO, 7700)] * 3, self.ACHOU)
        for n, tamanhos in enumerate(extras, start=3):
            nos += self._nos_do_turno(n, f"pedido {n}", [amostra(JSON_DUO, t) for t in tamanhos], f"resposta {n}")
        atual = 3 + len(extras)
        return nos + [no_(f"h{atual}", f"a{atual - 1}", "human", [], "ficou sem contexto?")]

    def _ajustar(self, nos, fixar=True, contexto=CONTEXTO):
        turnos = historico.para_turnos(nos, "nativo")
        return ajustar(self.SISTEMA, "", turnos, contexto, fixar, peso_ferramentas=self.PESO_FERRAMENTAS)

    @staticmethod
    def _etiquetas(r):
        return re.findall(r"^R\d+\.\d+(?= )", "\n".join(m["texto"] for m in r.mensagens), re.M)

    def test_o_que_foi_achado_no_turno_2_continua_visivel(self):
        r = self._ajustar(self._sessao())
        self.assertLessEqual(total(r), self.CONTEXTO - 4096)
        textos = textos_de(r)
        self.assertIn(self.ACHOU, textos)
        self.assertIn(self.NAO_ACHOU, textos)  # só resultados saíram: a resposta curta do turno 1 fica
        visiveis = self._etiquetas(r)
        for etiqueta in ("R2.0", "R2.1", "R2.2"):
            self.assertIn(etiqueta, visiveis)
        self.assertNotIn("R1.0", visiveis)  # o resultado mais velho é o primeiro a sair
        self.assertIn("R1.4", visiveis)  # e só o necessário sai
        self.assertIn("ache o processo originário", textos)
        self.assertIn("ficou sem contexto?", textos)
        omitidos = 5 - sum(e.startswith("R1.") for e in visiveis)
        self.assertIn(f"[Nota do servidor: para caber na janela, {omitidos} resultado(s) de ferramenta foram omitidos.",
                      r.mensagens[0]["texto"])
        self.assertEqual(len(r.avisos), 1)
        self.assertTrue(r.avisos[0].startswith(f"{omitidos} resultado(s) antigo(s) de ferramenta"), r.avisos[0])

    def test_resultados_do_turno_1_saem_antes_de_qualquer_coisa_do_turno_2(self):
        # Janela mais apertada: o turno 1 vai todo, e o turno 2 fica de pé até a vez dele.
        r = self._ajustar(self._sessao(), contexto=23000)
        self.assertEqual(self._etiquetas(r), ["R2.0", "R2.1", "R2.2"])
        self.assertIn(self.ACHOU, textos_de(r))
        self.assertIn(self.NAO_ACHOU, textos_de(r))
        self.assertIn("5 resultado(s) de ferramenta foram omitidos", r.mensagens[0]["texto"])


class TestCortePorIdade(unittest.TestCase):
    """O que saiu da janela não volta, e nada sai antes de tudo o que é mais velho que ele."""

    @staticmethod
    def _bloco(n, rodadas, resposta):
        conteudo = []
        for r, resultados in enumerate(rodadas):
            ids = [f"t{n}_{r}_{i}" for i in range(len(resultados))]
            conteudo += [{"type": "tool_use", "id": t, "name": "ler", "input": {}} for t in ids]
            conteudo += [{"type": "tool_result", "tool_use_id": t, "content": f"R{t[1:]}| {texto}"}
                         for t, texto in zip(ids, resultados)]
        if resposta:
            conteudo.append({"type": "text", "text": resposta})
        return conteudo

    def _reproducao(self, fixar, modo):
        """O caso da revisão: a rodada grande do turno 2 estava protegida e, no turno 3, deixa de estar."""
        h1 = no_("h1", None, "human", [], "PEDIDO1")
        h1["attachments"] = [{"file_name": "doc.txt", "extracted_content": "ANEXO1 " + amostra(JSON_DUO, 12000)}]
        t1 = [h1, no_("a1", "h1", "assistant", self._bloco(1, [[amostra(JSON_DUO, 3000)]],
                                                           "NAO_LOCALIZEI " + amostra(JSON_DUO, 6000)))]
        grande = [[amostra(JSON_DUO, 12000)] * 3]
        h2 = no_("h2", "a1", "human", [], "PEDIDO2")
        rodada_final = t1 + [h2, no_("a2", "h2", "assistant", self._bloco(2, grande, ""))]
        h3 = no_("h3", "a2", "human", [], "PEDIDO3")
        seguinte = t1 + [h2, no_("a2", "h2", "assistant", self._bloco(2, grande, "ACHEI")), h3]
        sistema = "s" * 3000
        return (ajustar(sistema, "", historico.para_turnos(rodada_final, modo), 16384, fixar),
                ajustar(sistema, "", historico.para_turnos(seguinte, modo), 16384, fixar))

    def test_turno_1_nao_volta_quando_a_rodada_grande_deixa_de_ser_protegida(self):
        for fixar in (True, False):
            for modo in ("nativo", "cercas"):
                with self.subTest(fixar=fixar, modo=modo):
                    rodada_final, seguinte = self._reproducao(fixar, modo)
                    for r in (rodada_final, seguinte):
                        visivel = "\n".join(textos_de(r))
                        self.assertNotIn("NAO_LOCALIZEI", visivel)
                        self.assertNotIn("ANEXO1", visivel)
                        self.assertNotIn("R1_0_0|", visivel)
                        self.assertEqual("PEDIDO1" in visivel, fixar)
                    self.assertIn("ACHEI", "\n".join(textos_de(seguinte)))
                    self.assertIn("PEDIDO3", "\n".join(textos_de(seguinte)))

    def test_turno_mais_velho_sai_antes_de_tocar_no_seguinte(self):
        # Mesmo com resultados grandes no mais novo: a idade decide, não o que libera mais espaço.
        turnos = [[usuario("VELHO"), assistente("V" * 18000)],
                  [usuario("t1"), *rodada("a", "A" * 18000), assistente("NOVO")],
                  [usuario("ATUAL")]]
        r = ajustar("", "", turnos, 8192, False)
        self.assertNotIn("VELHO", textos_de(r))
        self.assertIn("A" * 18000, textos_de(r))
        self.assertIn("NOVO", textos_de(r))

    def test_texto_do_turno_so_sai_depois_dos_resultados_dele(self):
        turnos = [[usuario("T0"), *rodada("a", "A" * 28500), assistente("RESPOSTA CURTA")], [usuario("ATUAL")]]
        r = ajustar("", "", turnos, 8192, False)
        self.assertIn("RESPOSTA CURTA", textos_de(r))
        self.assertNotIn("A" * 28500, textos_de(r))

    # -- a propriedade, em conversas aleatórias crescendo rodada a rodada -----------------------

    TAMANHOS = (300, 700, 3000, 9000, 20000, 40000)
    TIPOS = (PROSA, LISTA_IDS, JSON_DUO)
    ETIQUETA = re.compile(r"(?:PED|RESP|ANEXO)\d+\||R\d+_\d+_\d+\|")

    def _tamanho(self, rng):
        # Também tamanhos quebrados: os defeitos de fronteira só aparecem numa faixa estreita.
        return rng.choice(self.TAMANHOS) if rng.random() < .5 else rng.randint(300, 40000)

    def _rodadas_aleatorias(self, rng):
        return [[amostra(rng.choice(self.TIPOS), self._tamanho(rng)) for _ in range(rng.randint(1, 3))]
                for _ in range(rng.randint(0, 3))]

    def _envios(self, rng, n_turnos):
        """Cada envio de uma conversa: uma rodada a mais por vez. Devolve (nós, ordem de corte, pinados)."""
        nos, ordem = [], []
        for n in range(1, n_turnos + 1):
            pai = f"a{n - 1}" if n > 1 else None
            humano = no_(f"h{n}", pai, "human", [], f"PED{n}|")
            texto_do_turno = [f"PED{n}|"]
            if rng.random() < .3:
                humano["attachments"] = [{"file_name": "d.txt", "extracted_content":
                                          f"ANEXO{n}| " + amostra(rng.choice(self.TIPOS), rng.choice(self.TAMANHOS))}]
                texto_do_turno.append(f"ANEXO{n}|")
            rodadas = self._rodadas_aleatorias(rng)
            anteriores = list(nos)
            for k in range(len(rodadas) + 1):
                parcial = no_(f"a{n}", f"h{n}", "assistant", self._bloco(n, rodadas[:k], ""))
                yield (anteriores + [humano] + ([parcial] if k else []), ordem + [
                    f"R{n}_{r}_{i}|" for r in range(k - 1) for i in range(len(rodadas[r]))])
            resposta = f"RESP{n}| " + amostra(rng.choice(self.TIPOS), rng.choice(self.TAMANHOS))
            nos = anteriores + [humano, no_(f"a{n}", f"h{n}", "assistant", self._bloco(n, rodadas, resposta))]
            ordem = ordem + [f"R{n}_{r}_{i}|" for r in range(len(rodadas)) for i in range(len(rodadas[r]))]
            ordem += texto_do_turno + [f"RESP{n}|"]

    def test_o_que_saiu_nao_volta_e_nada_sai_antes_do_mais_velho(self):
        # Pelo fluxo real da rodada: `montar_janela`, com a definição da leitura indo ao catálogo.
        rng = random.Random(20261104)
        for caso in range(40):
            modo = rng.choice(["nativo", "cercas"])
            contexto = rng.choice([8192, 16384, 32768])
            sistema_duo = "d" * rng.choice([0, 0, 3000])
            fixar = not sistema_duo
            fator = rng.choice([1.0, 1.0, 1.4])
            catalogo = Catalogo(rng.choice([[LISTAR], ferramentas_do_duo()]), None, [], [])
            sistema = "s" * rng.choice([0, 1500, 6000])
            opcoes = Opcoes(contexto, None, "padrao", 15, 600, 0)
            ja_saiu: set[str] = set()
            vista_antes: set[str] = set()
            for nos, ordem in self._envios(rng, rng.randint(2, 7)):
                r, visiveis = montar_janela(catalogo, modo == "cercas", [sistema], sistema_duo,
                                            historico.para_turnos(nos, modo), opcoes, fator)
                visivel = "\n".join(textos_de(r))
                agora = set(self.ETIQUETA.findall(visivel))
                with self.subTest(caso=caso, modo=modo, contexto=contexto, fixar=fixar, fator=fator):
                    ja_saiu |= vista_antes - agora
                    self.assertFalse(ja_saiu & agora, "voltou para a janela")
                    # Da ordem de corte, o que resta é sempre o fim: o 1º pedido fixado fica de fora da conta.
                    cortaveis = [e for e in ordem if not (fixar and e == "PED1|")]
                    presentes = [e in agora for e in cortaveis]
                    if True in presentes:
                        self.assertNotIn(False, presentes[presentes.index(True):], "saiu antes de um mais velho")
                    # A ferramenta aparece se, e só se, há marca dela na janela.
                    tem_marca = f"{LER_TRECHO}(id=" in visivel
                    self.assertEqual(r.leitura, tem_marca)
                    self.assertEqual(LER_TRECHO in [d["name"] for d in visiveis], tem_marca)
                    if modo == "cercas":
                        self.assertEqual(f"• {LER_TRECHO} —" in r.mensagens[0]["texto"], tem_marca)
                vista_antes = agora


    def test_com_resumo_ele_sai_por_ultimo_e_o_resto_segue_a_ordem(self):
        # A mesma varredura, com a conversa compactada: entre os anteriores, o resumo é o último a sair. Ele é a única
        # exceção à regra de idade, e pode voltar, mas com ele na conta os demais continuam não voltando.
        rng = random.Random(20261105)
        for caso in range(40):
            modo = rng.choice(["nativo", "cercas"])
            contexto = rng.choice([8192, 16384, 32768])
            fator = rng.choice([1.0, 1.0, 1.4])
            catalogo = Catalogo(rng.choice([[LISTAR], ferramentas_do_duo()]), None, [], [])
            opcoes = Opcoes(contexto, None, "padrao", 15, 600, 0)
            resumo = {"papel": "usuario", "texto": "RESUMOFIXO " + amostra(PROSA, rng.choice([300, 3000, 9000])),
                      "resumo": True}
            ja_saiu: set[str] = set()
            vista_antes: set[str] = set()
            for nos, ordem in self._envios(rng, rng.randint(2, 7)):
                turnos = [[resumo]] + historico.para_turnos(nos, modo)
                r, _ = montar_janela(catalogo, modo == "cercas", ["s" * 1500], "d" * 3000, turnos, opcoes, fator)
                visivel = "\n".join(textos_de(r))
                agora = set(self.ETIQUETA.findall(visivel))
                with self.subTest(caso=caso, modo=modo, contexto=contexto, fator=fator):
                    ja_saiu |= vista_antes - agora
                    self.assertFalse(ja_saiu & agora, "voltou para a janela")
                    presentes = [e in agora for e in ordem]
                    if True in presentes:
                        self.assertNotIn(False, presentes[presentes.index(True):], "saiu antes de um mais velho")
                    anteriores = [m for t in r.turnos[1:-1] for m in t]
                    if "RESUMOFIXO" not in visivel:
                        self.assertEqual(anteriores, [], "o resumo saiu antes de um turno anterior")
                vista_antes = agora


class TestPesoDaLeitura(unittest.TestCase):
    """A definição da leitura pesa: esse peso não pode ir e vir conforme haja marca na janela."""

    @staticmethod
    def _turno(n, pedido, tamanho):
        pai = f"a{n - 1}" if n > 1 else None
        conteudo = []
        if tamanho:
            conteudo = [{"type": "tool_use", "id": f"t{n}", "name": "ler", "input": {}},
                        {"type": "tool_result", "tool_use_id": f"t{n}", "content": f"R{n}| " + "x" * tamanho}]
        conteudo.append({"type": "text", "text": f"RESP{n}| curta"})
        return [no_(f"h{n}", pai, "human", [], f"PED{n}| " + pedido), no_(f"a{n}", f"h{n}", "assistant", conteudo)]

    def _vistas(self, r2, extra):
        """Cinco envios; o ajuste do 4º ou 5º tira o único turno com marca."""
        catalogo = Catalogo([LISTAR], None, [], [])
        opcoes = Opcoes(16384, None, "padrao", 15, 600, 0)
        pedidos = [("curto", 12000), ("curto", r2), ("p", 0), ("q" * extra, 0), ("q" * (extra // 2), 0)]
        feitos, vistas = [], []
        for n, (pedido, tamanho) in enumerate(pedidos, start=1):
            humano, assistente_ = self._turno(n, pedido, tamanho)
            envio = feitos + [humano]
            if tamanho:
                envio.append(no_(f"a{n}", f"h{n}", "assistant", assistente_["content"][:-1]))
            jan, _ = montar_janela(catalogo, False, ["Sistema."], "", historico.para_turnos(envio, "nativo"), opcoes, 1.0)
            vistas.append(set(re.findall(r"PED\d\||RESP\d\||R\d\|", "\n".join(textos_de(jan)))))
            feitos += [humano, assistente_]
        return vistas

    def test_o_que_saiu_nao_volta_quando_o_turno_com_marca_sai(self):
        # A faixa estreita em que o peso da definição (~180 tokens) decide o corte.
        for r2 in range(50300, 50700, 40):
            with self.subTest(r2=r2):
                saiu: set[str] = set()
                anterior: set[str] = set()
                for vista in self._vistas(r2, 400):
                    saiu |= anterior - vista
                    self.assertFalse(saiu & vista)
                    anterior = vista

    def test_sem_marca_a_ferramenta_nao_aparece(self):
        # Sem resultados na conversa: saem só turnos inteiros, e não há o que ler.
        turnos = [turno("A" * 45000), [usuario("ATUAL")]]
        for cercas in (False, True):
            with self.subTest(cercas=cercas):
                r, visiveis = montar_janela(Catalogo([LISTAR], None, [], []), cercas, ["Sistema."], "d", turnos,
                                            Opcoes(8192, None, "padrao", 15, 600, 0), 1.0)
                self.assertNotIn("A" * 45000, textos_de(r))
                self.assertFalse(r.leitura)
                self.assertEqual([d["name"] for d in visiveis], ["listar_documentos"])
                self.assertNotIn(LER_TRECHO, r.mensagens[0]["texto"])

    def test_definicao_conta_no_orcamento_quando_ha_marca(self):
        turnos = [[usuario("t0"), *rodada("a", "A" * 40000), assistente("fim")], [usuario("ATUAL")]]
        r, visiveis = montar_janela(Catalogo([LISTAR], None, [], []), False, ["Sistema."], "", turnos,
                                    Opcoes(8192, None, "padrao", 15, 600, 0), 1.0)
        self.assertIn(LER_TRECHO, [d["name"] for d in visiveis])
        peso = estimar_tokens(json.dumps(para_funcoes(visiveis), ensure_ascii=False))
        mensagens = sum(estimar_tokens(m["texto"]) for m in r.mensagens) + sum(
            estimar_tokens(json.dumps(m["chamadas"], ensure_ascii=False)) for m in r.mensagens if m.get("chamadas"))
        self.assertEqual(r.estimativa, mensagens + peso)
        self.assertLessEqual(r.estimativa, 6144)


if __name__ == "__main__":
    unittest.main()
