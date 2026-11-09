"""A trava contra o laço de ferramentas: nenhum turno gasta rodadas repetindo o que não dá resultado.

Medido no `qwen3.5:4b`: com uma chamada vazia no histórico, o modelo a imita, mesmo raciocinando que precisa do
argumento (0 de 12 com o argumento; 12 de 12 sem ela no histórico). Avisar não resolve: lembrete ou erro mais forte,
com a vazia ainda lá, davam 0 a 2 de 12. Por isso a chamada inválida não entra no histórico.
"""

import json
import unittest

from duo_provedor import historico
from duo_provedor.backends.base import ChamadaFerramenta, Texto
from duo_provedor.catalogo import META, Catalogo
from duo_provedor.ferramentas import parametros_faltando
from testes.test_turno_recuperacao import Base, BackendPassos

PROCESSOS = {"name": "listar_processos", "description": "Lista os processos de uma tarefa.",
             "input_schema": {"type": "object", "required": ["tarefa"],
                              "properties": {"tarefa": {"type": "string", "description": "Nome exato da tarefa."},
                                             "limite": {"type": "number"}}}}
TAREFAS = {"name": "listar_tarefas", "description": "Lista as tarefas do painel.",
           "input_schema": {"type": "object", "properties": {}}}
SO_TAREFAS_FAVORITA = {"catalogo": {"favoritas": ["listar_tarefas"], "resumos": {}}}
ERRO = json.dumps({"erro": "Tarefa não encontrada."}, ensure_ascii=False)


def chamar(nome, **argumentos):
    return ChamadaFerramenta(nome, argumentos)


class Trava(Base):
    def _turno(self, responder, executar=None, extras=None, ferramentas=(PROCESSOS, TAREFAS)):
        self._subir(BackendPassos(responder))
        self.cid = self.cliente.criar_conversa()
        self.executadas = []

        def registrar(nome, entrada):
            self.executadas.append((nome, entrada))
            return executar(nome, entrada) if executar else "resultado"

        return self.cliente.enviar(self.cid, "pergunta", self.cliente.raiz, list(ferramentas), registrar, extras=extras)

    def _usos_gravados(self) -> list:
        nos = self.servidor.servico.armazenamento.ler_conversa(self.cid)["chat_messages"]
        return [(b["name"], b["input"]) for n in nos for b in n["content"] if b.get("type") == "tool_use"]


class TestParametroObrigatorio(Trava):
    def test_chamada_sem_obrigatorio_nao_vai_ao_duo_nem_ao_historico(self) -> None:
        def responder(indice, pedido):
            return [chamar("listar_processos")] if indice == 0 else [Texto("Fim.")]

        r = self._turno(responder)

        self.assertEqual(self.executadas, [])
        self.assertEqual(self._usos_gravados(), [])
        self.assertEqual(r.chamadas, [])
        ultimo = self._texto_do_ultimo_pedido()
        self.assertIn("`tarefa`", ultimo)
        self.assertIn("Nome exato da tarefa.", ultimo)
        self.assertNotIn('"ferramenta"', ultimo)

    def test_o_caso_real_o_modelo_que_imita_a_vazia_chega_ao_argumento(self) -> None:
        # O modelo real: enquanto houver chamada vazia no que ele vê, ele a repete.
        def responder(indice, pedido):
            historico_visto = json.dumps(pedido.mensagens, ensure_ascii=False)
            if '"Tarefa X"' in historico_visto or "resultado" in historico_visto and indice > 2:
                return [Texto("Os processos são estes.")]
            if indice == 0:
                return [chamar("listar_processos")]
            if '"chamadas"' in historico_visto and '"argumentos": {}' in historico_visto:
                return [chamar("listar_processos")]
            return [chamar("listar_processos", tarefa="Tarefa X")]

        r = self._turno(responder, extras=SO_TAREFAS_FAVORITA)

        self.assertEqual(self.executadas, [("listar_processos", {"tarefa": "Tarefa X"})])
        self.assertIn("Os processos são estes.", r.texto)
        self.assertLessEqual(len(self.backend.pedidos), 3)

    def test_adiada_sem_obrigatorio_e_carregada_e_nao_executada(self) -> None:
        def responder(indice, pedido):
            return [chamar("listar_processos")] if indice == 0 else [Texto("Fim.")]

        self._turno(responder, extras=SO_TAREFAS_FAVORITA)

        self.assertEqual(self.executadas, [])
        self.assertEqual(self._usos_gravados(), [])
        self.assertIn("listar_processos", [f["name"] for f in self.backend.pedidos[-1].ferramentas])

    def test_adiada_com_o_obrigatorio_executa_na_hora(self) -> None:
        def responder(indice, pedido):
            return [chamar("listar_processos", tarefa="X")] if indice == 0 else [Texto("Fim.")]

        self._turno(responder, extras=SO_TAREFAS_FAVORITA)

        self.assertEqual(self.executadas, [("listar_processos", {"tarefa": "X"})])

    def test_adiada_sem_nenhum_obrigatorio_executa_na_hora(self) -> None:
        def responder(indice, pedido):
            return [chamar("listar_tarefas")] if indice == 0 else [Texto("Fim.")]

        self._turno(responder, extras={"catalogo": {"favoritas": ["listar_processos"], "resumos": {}}})

        self.assertEqual(self.executadas, [("listar_tarefas", {})])

    def test_na_mesma_rodada_a_valida_executa_e_a_invalida_vira_lembrete(self) -> None:
        def responder(indice, pedido):
            if indice == 0:
                return [chamar("listar_tarefas"), chamar("listar_processos")]
            return [Texto("Fim.")]

        self._turno(responder)

        self.assertEqual(self.executadas, [("listar_tarefas", {})])
        self.assertIn("`tarefa`", self._texto_do_ultimo_pedido())

    def test_valor_vazio_conta_como_faltando(self) -> None:
        self.assertEqual(parametros_faltando(PROCESSOS, {"tarefa": ""}), [("tarefa", "Nome exato da tarefa.")])
        self.assertEqual(parametros_faltando(PROCESSOS, {"tarefa": None}), [("tarefa", "Nome exato da tarefa.")])
        self.assertEqual(parametros_faltando(PROCESSOS, {"tarefa": "X"}), [])
        self.assertEqual(parametros_faltando(TAREFAS, {}), [])

    def test_zero_e_falso_nao_contam_como_faltando(self) -> None:
        definicao = {"name": "x", "input_schema": {"required": ["n", "b"], "properties": {}}}
        self.assertEqual(parametros_faltando(definicao, {"n": 0, "b": False}), [])

    def test_nas_cercas_tambem(self) -> None:
        bloco = '```duo-ferramenta\n{"nome": "listar_processos", "argumentos": {}}\n```'
        self._turno(lambda i, p: [Texto(bloco)] if i == 0 else [Texto("Fim.")],
                    extras={"opcoes": {"ferramentas": "cercas"}})

        self.assertEqual(self.executadas, [])
        self.assertIn("`tarefa`", self._texto_do_ultimo_pedido())


class TestRepeticao(Trava):
    def test_a_mesma_chamada_nao_roda_de_novo_e_o_turno_acaba_cedo(self) -> None:
        r = self._turno(lambda i, p: [chamar("listar_processos", tarefa="X")] if p.ferramentas else [Texto("Com o que li.")])

        self.assertEqual(self.executadas, [("listar_processos", {"tarefa": "X"})])
        self.assertEqual(len(self.backend.pedidos), 5)  # 1 executada, 3 recusadas, 1 sem ferramentas
        self.assertEqual(self.backend.pedidos[-1].ferramentas, [])
        self.assertIn("Com o que li.", r.texto)
        self.assertIn("repetiu", r.texto)
        self.assertNotIn("limite de", r.texto)

    def test_o_lembrete_de_repeticao_diz_para_nao_repetir(self) -> None:
        def responder(indice, pedido):
            return [chamar("listar_processos", tarefa="X")] if indice < 2 else [Texto("Fim.")]

        self._turno(responder)

        self.assertIn("mesmos argumentos", self._texto_do_ultimo_pedido())

    def test_a_ordem_dos_argumentos_nao_engana(self) -> None:
        pares = iter([{"tarefa": "X", "limite": 1}, {"limite": 1, "tarefa": "X"}])

        def responder(indice, pedido):
            return [ChamadaFerramenta("listar_processos", next(pares))] if indice < 2 else [Texto("Fim.")]

        self._turno(responder)

        self.assertEqual(len(self.executadas), 1)

    def test_argumentos_diferentes_nao_sao_repeticao(self) -> None:
        def responder(indice, pedido):
            return [chamar("listar_processos", tarefa=f"T{indice}")] if indice < 4 else [Texto("Fim.")]

        self._turno(responder)

        self.assertEqual(len(self.executadas), 4)

    def test_repetir_depois_de_outra_ferramenta_vale_mas_nao_sem_fim(self) -> None:
        # Etiquetar e listar de novo é legítimo: o estado mudou. Mas só até certo ponto.
        roteiro = iter([chamar("listar_tarefas", pagina=1), chamar("listar_processos", tarefa="X"),
                        chamar("listar_tarefas", pagina=1), chamar("listar_processos", tarefa="X"),
                        chamar("listar_tarefas", pagina=1)])

        def responder(indice, pedido):
            return [next(roteiro)] if indice < 5 else [Texto("Fim.")]

        self._turno(responder)

        self.assertEqual(self.executadas.count(("listar_tarefas", {"pagina": 1})), 2)
        self.assertEqual(self.executadas.count(("listar_processos", {"tarefa": "X"})), 2)

    def test_duas_iguais_na_mesma_rodada_rodam_uma_vez(self) -> None:
        def responder(indice, pedido):
            return [chamar("listar_tarefas"), chamar("listar_tarefas")] if indice == 0 else [Texto("Fim.")]

        self._turno(responder)

        self.assertEqual(self.executadas, [("listar_tarefas", {})])

    def test_a_meta_repetida_tambem_para(self) -> None:
        def responder(indice, pedido):
            return [ChamadaFerramenta(META, {"nomes": ["listar_processos"]})] if p_tem(pedido) else [Texto("Fim.")]

        def p_tem(pedido):
            return bool(pedido.ferramentas)

        self._turno(responder, extras=SO_TAREFAS_FAVORITA)

        self.assertEqual(len(self.backend.pedidos), 5)

    def test_em_outro_turno_a_conta_recomeca(self) -> None:
        def responder(indice, pedido):
            return [chamar("listar_processos", tarefa="X")] if indice in (0, 2) else [Texto("Fim.")]

        self._turno(responder)
        nos = self.servidor.servico.armazenamento.ler_conversa(self.cid)["chat_messages"]
        self.cliente.enviar(self.cid, "de novo", nos[-1]["uuid"], [PROCESSOS, TAREFAS],
                            lambda n, e: self.executadas.append((n, e)) or "resultado")

        self.assertEqual(len(self.executadas), 2)


class TestSemProgresso(Trava):
    def test_ferramenta_que_so_falha_com_argumentos_diferentes_para_em_tres(self) -> None:
        def responder(indice, pedido):
            return [chamar("listar_processos", tarefa=f"T{indice}")] if pedido.ferramentas else [Texto("Não achei.")]

        r = self._turno(responder, executar=lambda n, e: ERRO)

        self.assertEqual(len(self.executadas), 3)
        self.assertEqual(len(self.backend.pedidos), 4)
        self.assertEqual(self.backend.pedidos[-1].ferramentas, [])
        self.assertIn("Não achei.", r.texto)
        self.assertIn("repetiu", r.texto)

    def test_um_acerto_zera_a_conta(self) -> None:
        def executar(nome, entrada):
            return "resultado" if entrada.get("tarefa") == "T2" else ERRO

        def responder(indice, pedido):
            return [chamar("listar_processos", tarefa=f"T{indice}")] if indice < 6 else [Texto("Fim.")]

        self._turno(responder, executar=executar)

        self.assertEqual(len(self.executadas), 6)

    def test_o_modelo_que_insiste_sem_o_obrigatorio_para_em_tres(self) -> None:
        r = self._turno(lambda i, p: [chamar("listar_processos")] if p.ferramentas else [Texto("Faltou a tarefa.")])

        self.assertEqual(self.executadas, [])
        self.assertEqual(len(self.backend.pedidos), 4)
        self.assertIn("Faltou a tarefa.", r.texto)

    def test_a_rodada_final_nao_manda_chamar_ferramenta(self) -> None:
        self._turno(lambda i, p: [chamar("listar_processos")] if p.ferramentas else [Texto("Fim.")])

        ultimo = self._texto_do_ultimo_pedido()
        self.assertIn("Não chame", ultimo)
        self.assertNotIn("Chame de novo", ultimo)

    def test_quem_vai_bem_nao_e_interrompido(self) -> None:
        def responder(indice, pedido):
            return [chamar("listar_processos", tarefa=f"T{indice}")] if indice < 10 else [Texto("Fim.")]

        r = self._turno(responder)

        self.assertEqual(len(self.executadas), 10)
        self.assertNotIn("repetiu", r.texto)


class TestChamadaVaziaDeConversaAntiga(unittest.TestCase):
    """Conversa gravada antes da trava: a vazia que falhou no histórico faria o modelo imitá-la de novo."""

    @staticmethod
    def _no(*blocos) -> dict:
        return {"uuid": "a", "sender": "assistant", "text": "", "content": list(blocos), "attachments": [], "files": []}

    @staticmethod
    def _uso(id_, **entrada) -> dict:
        return {"type": "tool_use", "id": id_, "name": "listar_processos", "input": entrada}

    @staticmethod
    def _resultado(id_, texto, erro=False) -> dict:
        return {"type": "tool_result", "tool_use_id": id_, "content": texto, "is_error": erro}

    def _chamadas(self, *blocos) -> list:
        turnos = historico.para_turnos([self._no(*blocos)], "nativo")
        return [c for m in turnos[0] for c in (m.get("chamadas") or [])]

    def test_vazia_que_falhou_nao_volta_ao_modelo(self) -> None:
        self.assertEqual(self._chamadas(self._uso("1"), self._resultado("1", ERRO)), [])
        self.assertEqual(self._chamadas(self._uso("1"), self._resultado("1", "x", erro=True)), [])

    def test_vazia_que_deu_certo_continua(self) -> None:
        self.assertEqual(len(self._chamadas(self._uso("1"), self._resultado("1", "ok"))), 1)

    def test_com_argumento_que_falhou_continua(self) -> None:
        # Não ensina a imitar nada: o modelo vê o argumento e pode tentar outro.
        self.assertEqual(len(self._chamadas(self._uso("1", tarefa="X"), self._resultado("1", ERRO))), 1)

    def test_a_vazia_sem_resultado_ainda_continua(self) -> None:
        self.assertEqual(len(self._chamadas(self._uso("1"))), 1)


class TestCatalogoDaTrava(unittest.TestCase):
    def test_definicao_de_adiada_e_visivel(self) -> None:
        catalogo = Catalogo([PROCESSOS, TAREFAS], {"favoritas": ["listar_tarefas"], "resumos": {}}, [], [])

        self.assertEqual(catalogo.definicao("listar_processos"), PROCESSOS)
        self.assertEqual(catalogo.definicao("listar_tarefas"), TAREFAS)
        self.assertIsNone(catalogo.definicao("inexistente"))

    def test_fora_das_ativas_nao_tem_definicao(self) -> None:
        catalogo = Catalogo([PROCESSOS, TAREFAS], None, ["listar_tarefas"], [])

        self.assertIsNone(catalogo.definicao("listar_processos"))


if __name__ == "__main__":
    unittest.main()
