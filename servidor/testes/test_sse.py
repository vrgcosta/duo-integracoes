import json
import re
import unittest

from duo_provedor.backends.base import Uso
from duo_provedor.sse import ClienteDesconectou, EmissorSSE


class Gravador:
    """Faz o papel do socket: guarda cada escrita separada, para contar quantas houve."""

    def __init__(self) -> None:
        self.escritas: list[bytes] = []

    def __call__(self, dados: bytes) -> None:
        self.escritas.append(dados)

    def eventos(self) -> list[dict]:
        bruto = b"".join(self.escritas).decode("utf-8")
        return [json.loads(q[len("data: "):]) for q in bruto.split("\n\n") if q.startswith("data: ")]


def _tipos(eventos: list[dict]) -> list[str]:
    return [e["type"] for e in eventos]


class TestEmissorSSE(unittest.TestCase):
    def setUp(self) -> None:
        self.gravador = Gravador()
        self.emissor = EmissorSSE(self.gravador)

    def test_texto_abre_um_bloco_e_reusa(self) -> None:
        self.emissor.iniciar()
        self.emissor.texto("Olá, ")
        self.emissor.texto("mundo")
        self.emissor.texto("")  # vazio não gera evento
        eventos = self.gravador.eventos()
        self.assertEqual(_tipos(eventos), ["message_start", "content_block_start",
                                           "content_block_delta", "content_block_delta"])
        self.assertRegex(eventos[0]["message"]["id"], r"^msg_local_[0-9a-f]{16}$")
        self.assertEqual(eventos[0]["message"]["role"], "assistant")
        self.assertEqual(eventos[1], {"type": "content_block_start", "index": 0,
                                      "content_block": {"type": "text", "text": ""}})
        self.assertEqual(eventos[2]["delta"], {"type": "text_delta", "text": "Olá, "})
        self.assertEqual(eventos[3], {"type": "content_block_delta", "index": 0,
                                      "delta": {"type": "text_delta", "text": "mundo"}})

    def test_trocar_de_texto_para_raciocinio_fecha_bloco(self) -> None:
        self.emissor.texto("a")
        self.emissor.raciocinio("pensando")
        self.emissor.texto("b")
        eventos = self.gravador.eventos()
        self.assertEqual(eventos, [
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "a"}},
            {"type": "content_block_stop", "index": 0},
            {"type": "content_block_start", "index": 1, "content_block": {"type": "thinking", "thinking": ""}},
            {"type": "content_block_delta", "index": 1,
             "delta": {"type": "thinking_delta", "thinking": "pensando"}},
            {"type": "content_block_stop", "index": 1},
            {"type": "content_block_start", "index": 2, "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 2, "delta": {"type": "text_delta", "text": "b"}},
        ])

    def test_chamadas_numa_unica_escrita(self) -> None:
        self.emissor.texto("Vou consultar.")
        antes = len(self.gravador.escritas)
        self.emissor.chamadas([("toolu_local_1", "listar_documentos", {}),
                               ("toolu_local_2", "ler_documento", {"id": "doc 1", "página": 2})])
        # O Duo pausa ao ver a chamada completa: as duas (e o fechamento do texto) vão juntas.
        self.assertEqual(len(self.gravador.escritas), antes + 1)
        eventos = self.gravador.eventos()[2:]
        self.assertEqual(eventos, [
            {"type": "content_block_stop", "index": 0},
            {"type": "content_block_start", "index": 1,
             "content_block": {"type": "tool_use", "id": "toolu_local_1", "name": "listar_documentos", "input": {}}},
            {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": "{}"}},
            {"type": "content_block_stop", "index": 1},
            {"type": "content_block_start", "index": 2,
             "content_block": {"type": "tool_use", "id": "toolu_local_2", "name": "ler_documento", "input": {}}},
            {"type": "content_block_delta", "index": 2,
             "delta": {"type": "input_json_delta", "partial_json": '{"id": "doc 1", "página": 2}'}},
            {"type": "content_block_stop", "index": 2},
        ])
        # Depois das chamadas, um texto novo abre outro bloco com o índice seguinte.
        self.emissor.texto("pronto")
        self.assertEqual(self.gravador.eventos()[-2]["index"], 3)

    def test_finalizar_com_e_sem_uso(self) -> None:
        self.emissor.texto("fim")
        self.emissor.finalizar(Uso(120, 7))
        self.assertEqual(self.gravador.eventos()[-3:], [
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta", "delta": {"stop_reason": "end_turn"},
             "usage": {"input_tokens": 120, "output_tokens": 7}},
            {"type": "message_stop"},
        ])

        outro = Gravador()
        EmissorSSE(outro).finalizar(None)
        self.assertEqual(outro.eventos(), [
            {"type": "message_delta", "delta": {"stop_reason": "end_turn"}},
            {"type": "message_stop"},
        ])
        self.assertEqual(len(outro.escritas), 1)

    def test_erro_fecha_bloco_e_nao_tem_message_stop(self) -> None:
        self.emissor.texto("começo")
        self.emissor.erro("O modelo não respondeu.")
        self.assertEqual(self.gravador.eventos()[-2:], [
            {"type": "content_block_stop", "index": 0},
            {"type": "error", "error": {"type": "duo-provedor", "message": "O modelo não respondeu."}},
        ])

    def test_ping_e_comentario_sse(self) -> None:
        self.emissor.ping()
        self.assertEqual(self.gravador.escritas, [b": ping\n\n"])

    def test_escrita_quebrada_vira_cliente_desconectou(self) -> None:
        for erro in (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            with self.subTest(erro=erro.__name__):
                def quebrada(_dados: bytes, erro=erro) -> None:
                    raise erro("o Duo fechou a conexão")

                emissor = EmissorSSE(quebrada)
                for acao in (emissor.iniciar, lambda: emissor.texto("x"), emissor.ping,
                             lambda: emissor.chamadas([("t", "n", {})]), lambda: emissor.finalizar(None)):
                    with self.assertRaises(ClienteDesconectou):
                        acao()
                # O aviso de erro é a última palavra: sem ninguém para ouvir, não há o que fazer.
                emissor.erro("tarde demais")

    def test_verificar_conexao_sem_escrever(self) -> None:
        EmissorSSE(self.gravador).verificar_conexao()  # sem sonda, nada a verificar
        estado = {"vivo": True}
        emissor = EmissorSSE(self.gravador, lambda: estado["vivo"])
        emissor.verificar_conexao()
        estado["vivo"] = False
        with self.assertRaises(ClienteDesconectou):
            emissor.verificar_conexao()
        self.assertEqual(self.gravador.escritas, [])

    def test_utf8_e_emoji_intactos(self) -> None:
        self.emissor.texto("ação 😀")
        bruto = b"".join(self.gravador.escritas)
        # ensure_ascii=False: o acento e o emoji vão como UTF-8, não como \\u escapado.
        self.assertIn("ação 😀".encode("utf-8"), bruto)
        self.assertNotIn(b"\\u", bruto)
        self.assertEqual(self.gravador.eventos()[-1]["delta"]["text"], "ação 😀")
        self.assertTrue(re.fullmatch(rb"(data: [^\n]+\n\n)+", bruto))


if __name__ == "__main__":
    unittest.main()
