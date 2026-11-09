import json
import threading
import unittest

from duo_provedor.backends.base import ChamadaFerramenta, Pedido, Raciocinio, Texto, Uso
from duo_provedor.backends.simulado import BackendSimulado
from duo_provedor.catalogo import META
from duo_provedor.emulacao import LINGUAGEM_CHAMADA, MARCA_RESULTADOS, catalogo_texto, chamada_como_texto

LER = {"name": "ler_peca", "description": "Lê uma peça", "input_schema": {"type": "object", "properties": {}}}
EXIGE = {"name": "buscar", "description": "Busca", "input_schema": {"type": "object", "required": ["q"]}}
META_DEF = {"name": META, "description": "Carrega.\n• outra — Faz outra coisa\n• mais — Mais uma",
            "input_schema": {"type": "object", "required": ["nomes"]}}


def _eventos(mensagens, ferramentas=(), pensar=False, cancelado=None):
    pedido = Pedido("simulado", mensagens, list(ferramentas), pensar, 8192, None)
    return list(BackendSimulado().conversar(pedido, cancelado or threading.Event()))


def _usuario(texto):
    return {"papel": "usuario", "texto": texto}


def _resultado(nome, texto="ok"):
    return {"papel": "ferramenta", "id_chamada": "1", "nome": nome, "texto": texto}


def _textos(eventos):
    return "".join(e.texto for e in eventos if isinstance(e, Texto))


class TestSimulado(unittest.TestCase):
    def test_modelo_unico(self) -> None:
        modelos = BackendSimulado().listar_modelos()
        self.assertEqual([m.id for m in modelos], ["simulado"])

    def test_eco_em_pedacos_de_8_e_uso(self) -> None:
        eventos = _eventos([_usuario("olá mundo, tudo bem?")])
        textos = [e.texto for e in eventos if isinstance(e, Texto)]
        self.assertEqual("".join(textos), "Você disse: olá mundo, tudo bem?")
        self.assertTrue(all(len(t) <= 8 for t in textos))
        self.assertIsInstance(eventos[-1], Uso)
        self.assertGreater(eventos[-1].entrada, 0)
        self.assertGreater(eventos[-1].saida, 0)

    def test_pensar_emite_raciocinio_primeiro(self) -> None:
        eventos = _eventos([_usuario("oi")], pensar=True)
        self.assertEqual(eventos[0], Raciocinio("Pensando no pedido…"))

    def test_vazio_nao_emite_texto(self) -> None:
        eventos = _eventos([_usuario("responda vazio")])
        self.assertEqual(_textos(eventos), "")
        self.assertEqual(len(eventos), 1)
        self.assertIsInstance(eventos[0], Uso)

    def test_ferramenta_chama_a_primeira_sem_required(self) -> None:
        eventos = _eventos([_usuario("use ferramenta")], [EXIGE, LER])
        self.assertIn(ChamadaFerramenta("ler_peca", {}), eventos)

    def test_ferramenta_so_meta_carrega_o_primeiro_listado(self) -> None:
        eventos = _eventos([_usuario("use ferramenta")], [META_DEF])
        self.assertIn(ChamadaFerramenta(META, {"nomes": ["outra"]}), eventos)

    def test_resultado_de_ferramenta_vira_texto(self) -> None:
        mensagens = [_usuario("use ferramenta"), {"papel": "assistente", "texto": ""},
                     _resultado("ler_peca", "x" * 500)]
        self.assertEqual(_textos(_eventos(mensagens, [LER])), "Recebi o resultado de ler_peca: " + "x" * 200)

    def test_palavra_chave_vem_do_pedido_humano_e_nao_da_ultima_mensagem(self) -> None:
        # O resultado traz "vazio" no conteúdo, mas a palavra-chave é a do pedido humano.
        mensagens = [_usuario("use ferramenta"), _resultado("ler_peca", "vazio")]
        self.assertIn("Recebi o resultado de ler_peca", _textos(_eventos(mensagens, [LER])))

    def test_resultado_da_meta_nao_conta_e_chama_a_carregada(self) -> None:
        mensagens = [_usuario("use ferramenta"), _resultado(META, "Ferramentas carregadas")]
        outra = {"name": "outra", "description": "x", "input_schema": {"type": "object"}}
        self.assertIn(ChamadaFerramenta("outra", {}), _eventos(mensagens, [outra, META_DEF]))

    def test_sempre_ferramenta_chama_mesmo_apos_resultado(self) -> None:
        mensagens = [_usuario("sempre-ferramenta"), _resultado("ler_peca")]
        self.assertIn(ChamadaFerramenta("ler_peca", {}), _eventos(mensagens, [LER]))

    def test_sempre_ferramenta_pula_a_meta(self) -> None:
        eventos = _eventos([_usuario("sempre-ferramenta")], [META_DEF, LER])
        self.assertIn(ChamadaFerramenta("ler_peca", {}), eventos)
        self.assertFalse(any(isinstance(e, ChamadaFerramenta) and e.nome == META for e in eventos))

    def test_sempre_ferramenta_so_com_meta_chama_a_meta(self) -> None:
        eventos = _eventos([_usuario("sempre-ferramenta")], [META_DEF])
        self.assertIn(ChamadaFerramenta(META, {"nomes": ["outra"]}), eventos)

    def test_ferramenta_inexistente_na_primeira_rodada(self) -> None:
        eventos = _eventos([_usuario("ferramenta-inexistente")], [LER])
        self.assertIn(ChamadaFerramenta("nao_existe", {}), eventos)

    def test_ferramenta_inexistente_nas_rodadas_seguintes_responde_direto(self) -> None:
        mensagens = [_usuario("ferramenta-inexistente"), {"papel": "assistente", "texto": "Sem texto."}]
        self.assertEqual(_textos(_eventos(mensagens, [LER])), "Sem essa ferramenta, respondo direto.")

    def test_modo_cercas_emite_chamada_como_texto(self) -> None:
        sistema = {"papel": "sistema", "texto": catalogo_texto([LER])}
        self.assertIn(LINGUAGEM_CHAMADA, sistema["texto"])
        eventos = _eventos([sistema, _usuario("use ferramenta")], [])
        self.assertEqual(_textos(eventos), chamada_como_texto("ler_peca", {}))
        self.assertFalse(any(isinstance(e, ChamadaFerramenta) for e in eventos))

    def test_resultado_em_modo_cercas_vira_texto(self) -> None:
        sistema = {"papel": "sistema", "texto": catalogo_texto([LER])}
        corpo = json.dumps({"nome": "ler_peca", "resultado": "conteudo"})
        resultado = f"{MARCA_RESULTADOS}\n```duo-resultado\n{corpo}\n```"
        texto = _textos(_eventos([sistema, _usuario("use ferramenta"), _usuario(resultado)], []))
        self.assertTrue(texto.startswith("Recebi o resultado de ler_peca: "))

    def test_json_quebrado_em_cercas_depois_chamada_valida(self) -> None:
        sistema = {"papel": "sistema", "texto": catalogo_texto([LER])}
        primeira = _textos(_eventos([sistema, _usuario("json-quebrado")], []))
        self.assertIn(LINGUAGEM_CHAMADA, primeira)
        corpo = primeira.split("\n", 1)[1].rsplit("\n```", 1)[0]
        with self.assertRaises(ValueError):
            json.loads(corpo)
        correcao = _usuario(f"{MARCA_RESULTADOS}\nO(s) bloco(s) {LINGUAGEM_CHAMADA} da sua última resposta "
                            "não puderam ser interpretados:\n- JSON inválido")
        segunda = _textos(_eventos([sistema, _usuario("json-quebrado"),
                                    {"papel": "assistente", "texto": primeira}, correcao], []))
        self.assertEqual(segunda, chamada_como_texto("ler_peca", {}))

    def test_cancelado_interrompe(self) -> None:
        cancelado = threading.Event()
        cancelado.set()
        eventos = _eventos([_usuario("texto bem longo para ser picotado")], cancelado=cancelado)
        self.assertEqual(_textos(eventos), "")


if __name__ == "__main__":
    unittest.main()
