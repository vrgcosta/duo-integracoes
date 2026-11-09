"""Turno: uma resposta do modelo ao Duo, com o laço de ferramentas.

Cada `completion` vira um Turno. Ele grava o pedido humano, conversa com o backend em rodadas e
devolve tudo num único SSE. Quando o modelo pede ferramentas, o SSE pausa sem fechar até o Duo
mandar os resultados por `/tool_result`; a rodada seguinte continua na mesma resposta. O laço
foi adaptado do servidor da Ponte 0.2.0 (ver créditos no README).
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
import uuid
from collections import Counter
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from . import emulacao, envio, historico, janela, leitura
from .armazenamento import RAIZ
from .backends.base import ChamadaFerramenta, Cortada, ErroAcimaDaJanela, ErroBackend, Pedido, Raciocinio, Texto, Uso
from .blueprint import mensagem_nuvem_bloqueada, nuvem_bloqueada
from .catalogo import Catalogo
from .envio import MODOS_FERRAMENTAS, Opcoes, mesclar_opcoes, montar_janela  # noqa: F401 - parte da interface do turno
from .ferramentas import (LEMBRETE_FINAL_VAZIA, LEMBRETE_RODADA_VAZIA, LEMBRETE_SEM_ESPACO, LEMBRETE_SEM_PROGRESSO,
                          LEMBRETE_TETO, erro_chamada_repetida, erro_parametros_faltando, limpar_resultado,
                          mensagem_correcao_nativa, parametros_faltando)
from .sse import ClienteDesconectou, EmissorSSE

if TYPE_CHECKING:
    from .servico import Servico

log = logging.getLogger(__name__)

# Segundos. Módulo-nível para os testes trocarem por valores curtos.
INTERVALO_PING = 15
ESPERA_ENTRE_EVENTOS = 300
ESPERA_RESULTADO = 600
# Na espera, olha o socket com esta frequência. O ping sozinho não basta: a 1ª escrita depois de
# o Duo fechar costuma passar, e a conversa ficaria presa (409) por até 2 × INTERVALO_PING.
VERIFICAR_CONEXAO = 0.5

AVISO_LIMITE_JANELA = "A entrada chegou perto do limite da janela; o modelo pode ter perdido o começo."
AVISO_POUCO_PARA_RESPONDER = "A entrada chegou perto do limite da janela: sobra pouco para a resposta."
AVISO_RESPOSTA_CORTADA = ("O modelo parou a resposta no limite da janela de contexto, antes de terminar. Aumente a janela "
                          "nas opções do plugin ou compacte a conversa.")
AVISO_SEM_ESPACO = ("O pedido não coube na janela de contexto com as definições de ferramentas: o modelo respondeu com "
                    "o que já tinha, sem poder chamar ferramentas. Para ele ir mais longe, aumente a janela nas opções "
                    "do plugin ou compacte a conversa.")
AVISO_SEM_PROGRESSO = ("O modelo repetiu chamadas de ferramenta que não deram resultado. A repetição foi interrompida, e ele "
                       "respondeu com o que já tinha: confira se a resposta cobre o que você pediu.")
# Rodadas seguidas sem nenhum resultado até o turno ir para a resposta final. Um modelo pequeno em círculos não sai
# sozinho, e cada rodada a mais só gasta tempo e consulta o PJe à toa; quem falha uma ou duas vezes costuma se corrigir.
LIMITE_SEM_PROGRESSO = 3
# Repetir a mesma chamada depois de outra ferramenta é legítimo (etiquetar e listar de novo): até aqui, no turno.
EXECUCOES_DA_MESMA_CHAMADA = 2
# Reler o mesmo trecho no turno é legítimo se ele saiu da janela; depois disso, é o modelo em círculos.
LEITURAS_DO_MESMO_TRECHO = 2
# Quantas vezes contar e ajustar antes de uma rodada. Cada contagem custa um prefill (que o envio aproveita, mas
# só o da última). Na seguinte, vale o último fator: em duas ou três a conta já se acertou.
TENTATIVAS_CONTAGEM = 3
# A proporção medida vale para o que foi contado, e o que sobra depois do novo corte varia um pouco.
FOLGA_NOVO_AJUSTE = 1.02
# Até aqui do fator atual, a medição mais baixa não muda nada: o fator não oscila a cada rodada por pouca coisa.
DESCIDA_MINIMA_DO_FATOR = 0.95


# O que a thread da contagem devolve quando ela falha; None é o motor dizendo que não conta.
_FALHOU = object()


class ErroTurno(Exception):
    """Falha do turno com mensagem pronta para o usuário (vai no evento `error`)."""


class ErroNaoCabe(ErroTurno):
    """O motor provou que não cabe. A mensagem nomeia o anexo, e nome de arquivo pode citar as partes: não vai ao log."""


class _SemEspaco(Exception):
    """O pedido com as ferramentas não cabe: resta a rodada final, sem elas."""


def _agora() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _novo_id() -> str:
    return "toolu_local_" + uuid.uuid4().hex[:24]


def _no(pai: str, remetente: str, texto: str, content: list, attachments: list, files: list) -> dict:
    return {"uuid": str(uuid.uuid4()), "parent_message_uuid": pai, "sender": remetente,
            "created_at": _agora(), "text": texto, "content": content,
            "attachments": attachments, "files": files}


class Turno:
    def __init__(self, servico: Servico, conv_id: str, corpo: dict, emissor: EmissorSSE) -> None:
        self.s = servico
        self.conv_id = conv_id
        self._id = conv_id[:8]  # no log basta o começo: acha a conversa em dados/duo.sqlite3
        self.corpo = corpo
        self.emissor = emissor
        # Sinal para a thread do backend parar de gerar quando ninguém mais vai ler.
        self.cancelado = threading.Event()
        self._resultados: queue.Queue[dict] = queue.Queue()
        self._pendentes: set[str] = set()
        self._trava = threading.Lock()
        self._no: dict | None = None  # nó do assistente, regravado a cada rodada
        self._catalogo: Catalogo | None = None
        self._modelo: str | None = None
        self._uso: Uso | None = None
        self._entrada_rodada = 0  # tokens de entrada que o motor reportou na rodada em curso
        self._fator_tokens = 1.0  # vem da conversa, em `_laco`
        self._fator_saturado: float | None = None  # o que funcionou, se o turno passou de FATOR_MAXIMO: `_conferir_envio`
        self._ultimo_uso: int | None = None  # idem; o que vai ao disco no fim do turno
        self._maior_uso = 0  # a maior entrada medida neste turno
        self._rodada_contada = False  # esta rodada foi conferida pelo número do motor?
        self._pode_contar = True  # contagem que falhou ou demorou não é tentada de novo neste turno
        self._sem_contagem = "nao_conta"  # por que não há número: "nao_conta" ou "falhou" (ver `envio.aviso_sem_contagem`)
        self._primeiro_envio = False  # é o 1º envio da conversa?
        self._avisos: list[str] = []
        # Só os da última rodada: a cada rodada o corte avança, e o usuário quer o saldo final,
        # não "1 resultado saiu", depois "2 resultados saíram".
        self._avisos_janela: list[str] = []
        self._avisos_leitura: list[str] = []
        self._ramo: list[dict] = []
        self._cercas = False
        self._excluidos: set[str] = set()  # o que o usuário tirou da janela, relido a cada rodada
        self._resumo: dict | None = None  # o resumo da compactação, idem
        # Leitura interna: o que já foi lido neste turno, e por qual chamada.
        self._leituras: dict[tuple[str, int], list[str]] = {}  # (alvo, inicio) -> ids das leituras
        self._feitas: Counter[tuple[str, str]] = Counter()  # (nome, argumentos) -> vezes neste turno
        self._ultima_leva: set[tuple[str, str]] = set()  # as chamadas da última rodada que executou algo
        self._trechos: dict[str, leitura.Lido] = {}  # id da leitura -> o que ela leu
        self._vistos_rodada: dict[str, list[tuple[int, int]]] = {}  # o que o modelo tem à vista agora
        # O que o modelo chegou a ver dos resultados, para saber o que ficou sem ler.
        self._truncados: dict[str, janela.Truncado] = {}
        self._vistos: dict[str, list[tuple[int, int]]] = {}
        self._falou = False  # algum texto já saiu ao Duo neste turno?
        self._falou_rodada = False  # e nesta rodada?
        self._cortada_rodada = False  # o motor parou esta rodada no limite?
        self._gravado = False
        self._rodadas = 0
        self._verificado_em = time.monotonic()

    # -- ciclo ---------------------------------------------------------------

    def executar(self) -> None:
        """Nunca propaga exceção: o SSE já começou, então todo erro vira evento `error`."""
        desfecho = "erro"
        try:
            self.emissor.iniciar()
            self._laco()
            desfecho = "concluído"
        except ClienteDesconectou:
            desfecho = "cancelado"
            self.cancelado.set()
            log.info("turno %s: o DUO fechou a conexão; turno cancelado", self._id)
        except ErroNaoCabe as e:
            self.emissor.erro(str(e))
        except (ErroTurno, ErroBackend) as e:
            log.warning("turno %s: %s", self._id, e)
            self.emissor.erro(str(e))
        except Exception as e:  # noqa: BLE001 - só resta avisar o Duo por evento
            log.exception("turno %s: erro interno", self._id)
            self.emissor.erro(f"erro interno do servidor: {e}")
        finally:
            with self._trava:
                # Um /tool_result atrasado recebe 409, não um "ok" de um turno que ninguém lê.
                self._pendentes.clear()
            self.cancelado.set()
            if not self._gravado:
                try:
                    self._persistir()
                except ErroTurno as e:
                    # O turno já terminou em erro ou o DUO saiu: não há a quem mandar outro evento,
                    # então a falha fica pelo menos no console.
                    log.warning("turno %s: %s", self._id, e)
            uso = self._uso
            log.info("turno %s: fim (%s) | %d rodada(s) | tokens: %s | %d aviso(s) | fator de tokens: %.2f",
                     self._id, desfecho, self._rodadas,
                     f"{uso.entrada} entrada, {uso.saida} saída" if uso else "não informados",
                     len(self._avisos_janela) + len(self._avisos_leitura) + len(self._avisos), self._fator_tokens)

    def entregar_resultado(self, corpo: dict) -> bool:
        """Entrega um resultado do Duo. False se o turno não espera esse `tool_use_id`."""
        tid = corpo.get("tool_use_id")
        with self._trava:
            if not isinstance(tid, str) or tid not in self._pendentes:
                return False
            self._pendentes.discard(tid)
        self._resultados.put(corpo)
        return True

    def _laco(self) -> None:
        s, corpo, cfg = self.s, self.corpo, self.s.cfg
        opcoes = mesclar_opcoes(cfg, corpo.get("opcoes"))
        self._modelo = modelo = envio.resolver_modelo(cfg, corpo.get("model"))
        info = s.backend.info_modelo(modelo)
        # Antes de gravar ou chamar o backend: com a nuvem desligada, o blueprint diz ao Duo que o
        # destino é local, e um modelo de nuvem desmentiria isso com os autos já fora da máquina.
        if nuvem_bloqueada(cfg, info):
            raise ErroTurno(mensagem_nuvem_bloqueada(modelo))

        conversa = s.armazenamento.ler_conversa(self.conv_id)
        if conversa is None:
            raise ErroTurno("A conversa não existe mais.")
        preparo = envio.preparar(cfg, s.sistema_padrao, s.armazenamento, conversa, corpo, opcoes, modelo, info)
        cercas, catalogo = self._cercas, self._catalogo = preparo.cercas, preparo.catalogo
        self._registrar_envio(modelo, cercas, opcoes)
        # O fator vem do turno anterior: recomeçar em 1 fazia a 1ª rodada de cada turno ir além da janela
        # (e o motor recusar, ou cortar o começo se for dos que cortam calado) e trazia de volta o que tinha saído
        # por causa dele.
        estado = s.armazenamento.estado_janela(self.conv_id)
        self._fator_tokens, self._ultimo_uso = envio.medicao_da_conversa(conversa, estado, modelo)
        self._primeiro_envio = estado is not None and estado.get("ultimo_envio") is None
        prompt = corpo.get("prompt") or ""
        humano = _no(corpo.get("parent_message_uuid") or RAIZ, "human", prompt,
                     [{"type": "text", "text": prompt}], corpo.get("attachments") or [], corpo.get("files") or [])
        self._gravar(humano)
        self._no = _no(humano["uuid"], "assistant", "", [], [], [])
        ramo = self._ramo = historico.ramo(conversa, humano["parent_message_uuid"]) + [humano]
        pensar = conversa.get("pensamento") is True
        lembrete_usado = False
        sem_progresso = 0

        def rodada(com_ferramentas: bool = True) -> tuple[list, list, janela.ResultadoJanela]:
            self._falou_rodada = self._cortada_rodada = False
            # Relê a cada rodada: o usuário pode tirar um item no meio do turno. O pedido em curso não sai.
            estado = s.armazenamento.estado_janela(self.conv_id) or {}
            self._excluidos = set(estado.get("excluidos") or []) - {humano["uuid"]}
            self._resumo = estado.get("resumo")
            jan, pedido = self._conferir_envio(preparo, humano, modelo, pensar, com_ferramentas)
            self._rodadas += 1
            self._anotar_janela(jan)
            try:
                chamadas, invalidas = self._rodada(pedido, opcoes, catalogo, cercas)
            except ErroAcimaDaJanela as e:
                # Sem contagem, é o envio que prova que não cabe: vale como o número dela, se ele o disser.
                if e.total is not None:
                    self._medir_contagem(e.total, jan, opcoes.contexto_tokens)
                self._sem_espaco(jan, humano, e.total, opcoes.contexto_tokens, com_ferramentas)
            self._medir_fator(jan)
            return chamadas, invalidas, jan

        def rodada_final(lembrete: str) -> None:
            """Sem ferramentas, para responder com o que já foi obtido; vazia, ganha mais uma chance.

            Os lembretes de antes saem: "chame a ferramenta agora" contradiz a rodada, e o que obedece fica sem resposta.
            """
            self._no["content"] = [b for b in self._no["content"] if b.get("type") != "correcao"]
            self._lembrar_modelo(lembrete)
            rodada(com_ferramentas=False)
            if not self._falou_rodada:
                log.info("turno %s: rodada final sem texto; o modelo ganha mais uma chance", self._id)
                self._lembrar_modelo(LEMBRETE_FINAL_VAZIA)
                rodada(com_ferramentas=False)

        for _ in range(opcoes.max_rodadas):
            try:
                chamadas, invalidas, jan = rodada()
            except _SemEspaco:
                # O turno não morre por falta de espaço: o trabalho das rodadas anteriores vira resposta.
                log.info("turno %s: não coube com as ferramentas; última rodada sem elas", self._id)
                rodada_final(LEMBRETE_SEM_ESPACO)
                if self._falou_rodada:
                    self._avisar(AVISO_SEM_ESPACO)
                break
            chamadas, repetidas = self._tirar_repetidas(chamadas, catalogo)
            invalidas = invalidas + repetidas
            if not chamadas and not invalidas:
                # Modelo pequeno às vezes raciocina, anuncia o que vai fazer e para sem fazer.
                # Vale mesmo se ele já falou antes de uma ferramenta: aquele "vou consultar" não é resposta.
                if not self._falou_rodada and not lembrete_usado:
                    lembrete_usado = True
                    log.info("turno %s: rodada sem texto nem ferramenta; o modelo ganha mais uma chance", self._id)
                    self._lembrar_modelo(LEMBRETE_RODADA_VAZIA)
                    continue
                break
            reais = self._registrar_chamadas(chamadas, catalogo, jan) if chamadas else []
            if reais:
                self._executar_no_duo(reais)
            sem_progresso = 0 if chamadas and self._deu_resultado(reais) else sem_progresso + 1
            if sem_progresso >= LIMITE_SEM_PROGRESSO:
                log.info("turno %s: %d rodadas sem resultado; última rodada sem ferramentas", self._id, sem_progresso)
                rodada_final(LEMBRETE_SEM_PROGRESSO)
                if self._falou_rodada:
                    self._avisar(AVISO_SEM_PROGRESSO)
                break
            if invalidas:
                self._lembrar_modelo(emulacao.mensagem_correcao(invalidas) if cercas
                                     else mensagem_correcao_nativa(invalidas))
        else:
            # Sem esta rodada, o trabalho das anteriores se perderia e o usuário ficaria só com o aviso.
            # Chamada feita aqui não vai ao Duo: ela não teria como voltar ao modelo.
            log.info("turno %s: teto de %d rodadas; última rodada sem ferramentas", self._id, opcoes.max_rodadas)
            rodada_final(LEMBRETE_TETO.format(rodadas=opcoes.max_rodadas))
            self._avisar(f"limite de {opcoes.max_rodadas} rodadas de ferramentas atingido.")

        # Pela última rodada, e não pelo turno: o "vou consultar" de antes da ferramenta não é resposta.
        if not self._falou_rodada:
            self._texto(("\n\n" if self._falou else "") + self._sem_resposta())
        # Só a última: a resposta que o usuário lê é a dela, e "antes de terminar" seria falso numa rodada do meio.
        if self._cortada_rodada:
            self._avisar(AVISO_RESPOSTA_CORTADA)
        self._avisos_leitura = self._avisos_de_leitura()
        for aviso in self._avisos_janela + self._avisos_leitura + self._avisos:
            self._texto(f"\n\n_[Servidor: {aviso}]_")
        # Grava antes do message_stop: quem relê a conversa logo depois já encontra a resposta.
        self._persistir()
        self.emissor.finalizar(self._uso)

    def _sem_resposta(self) -> str:
        """O aviso de que o turno acabou sem resposta: o que foi feito, e o que tentar."""
        consultas = sum(1 for b in self._no["content"] if b.get("type") == "tool_result" and not b.get("interno"))
        if not consultas:
            return "_[O modelo não produziu resposta. Tente de novo, ou reformule o pedido.]_"
        return (f"_[O modelo não produziu resposta: ele fez {consultas} consulta(s), e parou sem escrever a conclusão. Os "
                "resultados ficaram na conversa: peça de novo, por exemplo \"responda com o que já leu\".]_")

    def _conferir_envio(self, preparo: envio.Preparo, humano: dict, modelo: str, pensar: bool,
                        com_ferramentas: bool) -> tuple[janela.ResultadoJanela, Pedido]:
        """O pedido da rodada, com o tamanho conferido antes de o modelo processar.

        O motor corta ou recusa o que passa de `num_ctx`, então conta, e se passar do orçamento refaz o ajuste com a
        proporção medida. Ela vale só para esta rodada, sem o teto de `fator_real`, porque aqui o número é exato e
        não há risco de ser absurdo. O fator que fica é o da medição do pedido que foi (`_medir_contagem`), para as
        rodadas seguintes não herdarem a proporção de um documento denso que já saiu: com ela, os resultados normais
        chegavam truncados com metade da janela livre.

        Se a medição mostra que sobrou espaço por causa do fator, há uma contagem extra para levar mais: com o fator
        medido, ou, se ele acabou de passar do orçamento, com o ponto médio entre o que passou e o que coube. Se não
        couber, vai o pedido que já tinha cabido. Custa um prefill, e o ganho é o aviso "para caber na janela" não mentir.

        Uma proporção acima de FATOR_MAXIMO não cabe no fator gravado: toda rodada recomeçaria abaixo dela e passaria do
        orçamento de novo, com três contagens e quatro prefills, enquanto o documento ficasse na janela. Por isso, depois
        disso o turno segue com o fator do pedido que foi (`_fator_saturado`), e paga um prefill de contagem por rodada, à
        custa de não levar mais o que caberia. Se o pedido cortado deixar mais da metade do orçamento livre, o documento
        já saiu: o fator desce até o meio do caminho para a medida, para a rodada seguinte. Se a contagem passar do
        orçamento, cabendo na janela, o pedido vai como está, com o aviso de que sobra pouco, e o fator sobe. Acima da janela
        conta de novo: o motor recusa antes do prefill, e esse número é a prova para recusar. Imagem fica de fora: a
        proporção dela não é a do texto, e segurá-la cortaria à toa o que viesse depois.

        Deixar de enviar exige prova de que o motor cortaria, e prova só há com o número dele acima da janela.
        Entre o espaço útil e a janela nada é cortado, e vai com aviso de que sobra pouco para a resposta. Sem
        contagem (backend que não conta, ou contagem que falhou), vai sempre, com aviso: a estimativa costuma errar
        para mais, e no backend `openai` o tamanho configurado nem chega ao servidor.

        Com prova, ainda resta tirar as ferramentas (`_SemEspaco`), porque um turno em andamento não deve morrer por
        falta de espaço. Só o pedido que nem sem elas cabe é recusado, com o aviso de qual item pesa.
        """
        opcoes, exato = preparo.opcoes, None
        fator = self._fator_saturado or self._fator_tokens
        segurando = self._fator_saturado is not None  # esta rodada não tenta de novo o que o turno já pagou
        contexto = opcoes.contexto_tokens
        motivo: str | None = None  # por que foi sem o número, quando foi
        acima: float | None = None  # o fator com que o pedido passou do orçamento nesta rodada
        cabia: tuple | None = None  # o último pedido que coube, enquanto se tenta levar mais
        medido: tuple | None = None  # a última contagem que vale para o fator
        # Cada volta monta com o `fator` da vez e conta. O laço acaba de um destes jeitos, com `jan`, `pedido` e `exato` do
        # que vai ser enviado:
        # - coube, e o fator não tem culpa de nenhum corte: vai;
        # - coube, mas o fator cortou de mais: guarda em `cabia`, tenta um fator menor uma vez, e fica com o que couber;
        # - passou do orçamento: sobe o fator e conta de novo, salvo se já havia um que coube ou se não há mais o que cortar;
        # - sem número (o motor não conta, a contagem falhou ou veio suspeita, ou as tentativas acabaram): `exato` None.
        for tentativa in range(TENTATIVAS_CONTAGEM + 1):
            jan, visiveis = envio.montar(preparo, self._ramo + [self._no], self._excluidos, self._resumo, fator,
                                         com_ferramentas)
            pedido = Pedido(modelo, jan.mensagens, [] if preparo.cercas else visiveis, pensar,
                            contexto, opcoes.temperatura)
            if cabia is not None and jan.mensagens == cabia[0].mensagens:
                jan, pedido, exato = cabia  # o fator novo dá no mesmo pedido: não há por que contar de novo
                break
            if tentativa == TENTATIVAS_CONTAGEM:
                exato, motivo = None, "esgotou"
                break
            try:
                exato = self._contar(pedido, opcoes)
            except ErroAcimaDaJanela:  # recusou sem dizer quanto
                if cabia is None:
                    self._sem_espaco(jan, humano, None, contexto, com_ferramentas)
                jan, pedido, exato = cabia
                break
            if exato is None or envio.contagem_suspeita(exato, jan, contexto):
                if cabia is not None:
                    jan, pedido, exato = cabia  # a tentativa de levar mais não deu em nada, e já havia um que coube
                elif exato is None:
                    motivo = self._sem_contagem
                else:
                    # Um número que não é de todo o pedido: não mede nada, e o usuário fica sabendo.
                    log.warning("turno %s: contagem suspeita | %d tokens | estimativa %d | janela de %d", self._id, exato,
                                jan.estimativa, contexto)
                    jan.avisos.append(envio.aviso_de_suspeita(jan, exato))
                    exato = None
                break
            log.info("turno %s: contagem exata | %d tokens | cabem %d", self._id, exato, jan.orcamento)
            # Passou do orçamento.
            if exato > jan.orcamento:
                if cabia is not None:
                    jan, pedido, exato = cabia
                    break
                medido = (exato, jan)  # se as tentativas acabarem, é a última medição que se tem
                # Com a estimativa também acima, os cortes já acabaram: um fator maior não tiraria mais nada.
                if jan.excede:
                    break
                proporcao = exato / max(jan.estimativa, 1)
                acima, fator = fator, proporcao * FOLGA_NOVO_AJUSTE
                if proporcao > janela.FATOR_MAXIMO and not any(m.get("imagens") for m in jan.mensagens):
                    self._fator_saturado = fator
                if segurando and exato < contexto - 1:
                    break
                continue
            # Coube. Tentar levar mais só se houve corte e o fator ficou acima do que o motor mediu.
            medida = janela.fator_real(exato, jan.estimativa)
            if (segurando or cabia is not None or not jan.cortou or tentativa == TENTATIVAS_CONTAGEM - 1
                    or medida >= fator * DESCIDA_MINIMA_DO_FATOR):
                break
            cabia = (jan, pedido, exato)
            fator = medida if acima is None else (acima + fator) / 2
        # Daqui em diante, é o pedido que vai: guarda as medições e decide entre enviar, tirar as ferramentas e recusar.
        if self._fator_saturado is not None:
            self._fator_saturado = max(jan.fator, fator)  # `fator` é o que mediu por último, se passou do orçamento
            if exato is not None and jan.cortou and exato < jan.orcamento / 2:
                self._fator_saturado = (jan.fator + janela.fator_real(exato, jan.estimativa)) / 2
        if exato is not None:
            medido = (exato, jan)
        if medido is not None:
            self._medir_contagem(*medido, contexto)
        self._rodada_contada = exato is not None
        # `- 1`: um Ollama sem `truncate: false` satura aí, e nenhum pedido que caiba com resposta chega a isso.
        if exato is not None and exato >= contexto - 1:
            self._sem_espaco(jan, humano, exato, contexto, com_ferramentas)
        if exato is not None:
            aviso = envio.aviso_com_contagem(jan, exato, contexto)
        elif motivo is not None:
            aviso = envio.aviso_sem_contagem(jan, contexto, motivo, self._primeiro_envio, self.s.backend.pode_cortar_calado)
        else:
            aviso = None  # a contagem suspeita já avisou
        if aviso:
            jan.avisos.append(aviso)
        return jan, pedido

    def _sem_espaco(self, jan: janela.ResultadoJanela, humano: dict, total: int | None, contexto: int,
                    com_ferramentas: bool) -> None:
        """O motor provou que o pedido não cabe: resta a rodada sem ferramentas e, sem elas, a recusa."""
        if com_ferramentas:
            raise _SemEspaco
        log.warning("turno %s: pedido não enviado, não cabe na janela | %s tokens | janela de %d", self._id,
                    "?" if total is None else total, contexto)
        raise ErroNaoCabe(envio.mensagem_nao_cabe(jan, humano, total, contexto))

    def _contar(self, pedido: Pedido, opcoes: envio.Opcoes) -> int | None:
        """O total exato do pedido, ou None para seguir pela estimativa.

        Roda numa thread, como a rodada, para o SSE continuar com ping: a contagem pode levar o tempo do
        prefill, que o envio em seguida aproveita. Falha ou demora nunca travam o envio, e não se tenta de
        novo neste turno: cada espera dessas seria somada à da próxima rodada. A recusa do motor por passar da
        janela não é falha, e sim a prova: vale pelo total dela, e sem total ela sobe a quem chamou.
        """
        if not self._pode_contar:
            return None
        resposta: queue.Queue = queue.Queue()
        parar = threading.Event()

        def trabalhar() -> None:
            try:
                resposta.put(self.s.backend.contar_tokens(pedido, parar))
            except ErroAcimaDaJanela as e:
                resposta.put(e)
            except Exception as e:  # noqa: BLE001 - qualquer falha aqui só significa ficar com a estimativa
                log.info("turno %s: falha na contagem (%s); segue pela estimativa", self._id, type(e).__name__)
                resposta.put(_FALHOU)

        threading.Thread(target=trabalhar, daemon=True, name=f"contagem-{self._id}").start()
        limite = time.monotonic() + opcoes.espera_primeiro_token
        try:
            while True:
                try:
                    total = resposta.get(timeout=min(VERIFICAR_CONEXAO, INTERVALO_PING))
                    break
                except queue.Empty:
                    self._manter_conexao(sempre=True)
                if time.monotonic() > limite:
                    log.info("turno %s: a contagem passou de %s s; segue pela estimativa", self._id,
                             opcoes.espera_primeiro_token)
                    total = _FALHOU
                    break
        finally:
            parar.set()
        if isinstance(total, ErroAcimaDaJanela):
            if total.total is None:
                raise total
            return total.total
        if total is None or isinstance(total, bool) or not isinstance(total, int) or total <= 0:
            self._pode_contar = False
            # None é o motor dizendo que não tem como contar; o resto é falha, e o usuário é avisado a cada turno.
            self._sem_contagem = "nao_conta" if total is None else "falhou"
            return None
        return total

    def _medir_contagem(self, exato: int, jan: janela.ResultadoJanela, contexto: int) -> None:
        """O fator passa a ser o da última medição, para os dois lados, salvo com imagem, que a janela não pesa.

        Só crescer deixava uma leitura alta encarecendo o resto da conversa para sempre: o fator multiplica também
        o sistema e as ferramentas, e a janela útil chegava a zero, com o motor contando bem menos. Em
        `num_ctx - 1` (Ollama sem `truncate: false`) o número é só um piso, que não mostra que o fator estava alto.
        """
        if any(m.get("imagens") for m in jan.mensagens):
            return
        medida = janela.fator_real(exato, jan.estimativa)
        desce = medida < self._fator_tokens * DESCIDA_MINIMA_DO_FATOR and exato != contexto - 1
        if medida > self._fator_tokens or desce:
            self._fator_tokens = medida
        self._ultimo_uso = exato

    def _lembrar_modelo(self, texto: str) -> None:
        """Fala só com o modelo, e só neste turno: não aparece para o usuário nem vai aos turnos seguintes."""
        self._no["content"].append({"type": "correcao", "interno": True, "texto": texto})

    def _anotar_janela(self, jan: janela.ResultadoJanela) -> None:
        """Guarda os avisos da rodada e o que o modelo tem à vista nela.

        Os avisos dos truncados do turno saem daqui e voltam no fim: uma leitura feita depois pode
        ter trazido o resto, e aí o aviso desta rodada já estaria errado.
        """
        linhas_truncados = {t.aviso for t in jan.truncados}
        self._avisos_janela = [a for a in jan.avisos if a not in linhas_truncados]
        self._vistos_rodada = dict(jan.vistos)
        for truncado in jan.truncados:
            self._truncados[truncado.id] = truncado
        for id_, faixas in jan.vistos.items():
            lido = self._trechos.get(id_)
            if lido is None:
                self._vistos.setdefault(id_, []).extend(faixas)
            else:
                # As faixas de uma leitura são da resposta dela: traduz para as do resultado original.
                self._vistos.setdefault(lido.alvo, []).extend(
                    (lido.inicio + max(a - lido.cabecalho, 0), lido.inicio + max(b - lido.cabecalho, 0))
                    for a, b in faixas)

    def _avisos_de_leitura(self) -> list[str]:
        """O que o modelo deixou de ler dos resultados truncados neste turno."""
        avisos = []
        for truncado in self._truncados.values():
            achado = leitura.achar(self._ramo + [self._no], truncado.id)
            if achado is None:
                continue
            texto = historico.texto_resultado(achado[1].get("content"))
            faltam = min(truncado.total, sum(janela.estimar_tokens(texto[a:b])
                                             for a, b in _lacunas(self._vistos.get(truncado.id, []), len(texto))))
            if faltam:
                avisos.append(janela.aviso_truncado(truncado.nome, truncado.total - faltam, truncado.total,
                                                    truncado.pelo_limite))
                avisos.append(f"Atenção: o modelo respondeu sem ler {faltam} de {truncado.total} tokens "
                              f"de {truncado.nome}.")
        return avisos

    def _medir_fator(self, jan: janela.ResultadoJanela) -> None:
        """Sem contagem prévia, corrige a próxima rodada, e o próximo turno, pelo uso que o motor informou.

        Guarda o maior valor: a partir da 2ª rodada o motor costuma reaproveitar o prefixo em cache e
        reportar só o trecho novo, o que não mostra que a estimativa melhorou.
        Rodada com imagem não mede: o motor as conta na entrada e a janela não as pesa, então a
        razão sairia inflada sem dizer nada sobre a estimativa do texto.
        """
        entrada, self._entrada_rodada = self._entrada_rodada, 0
        if entrada > 0 and not self._rodada_contada and not any(m.get("imagens") for m in jan.mensagens):
            self._fator_tokens = max(self._fator_tokens, janela.fator_real(entrada, jan.estimativa))
            self._maior_uso = max(self._maior_uso, entrada)
            self._ultimo_uso = self._maior_uso

    def _registrar_envio(self, modelo: str, cercas: bool, opcoes: Opcoes) -> None:
        """Uma linha de log por envio, para conferir o que o DUO mandou e o que valeu.

        Só tamanhos e contagens: o texto do pedido, do sistema, dos anexos e dos resultados de
        ferramentas nunca vai para o log, porque pode estar sob segredo de justiça.
        """
        corpo = self.corpo
        catalogo = "não"
        if corpo.get("catalogo"):
            favoritas = corpo["catalogo"].get("favoritas")
            catalogo = f"sim ({len(favoritas) if isinstance(favoritas, list) else 0} favoritas)"
        log.info("turno %s: envio | modelo=%s ferramentas=%s | contexto=%d temperatura=%s max_rodadas=%d "
                 "| system=%d tokens | catalogo=%s | tools=%d",
                 self._id, modelo, "cercas" if cercas else "nativo", opcoes.contexto_tokens,
                 opcoes.temperatura, opcoes.max_rodadas, janela.estimar_tokens(corpo.get("system") or ""), catalogo,
                 len(corpo.get("tools") or []))

    # -- uma rodada com o backend --------------------------------------------

    def _rodada(self, pedido: Pedido, opcoes: Opcoes, catalogo: Catalogo,
                cercas: bool) -> tuple[list[tuple[str, dict]], list[tuple[str, str]]]:
        fila: queue.Queue = queue.Queue()
        # O backend roda numa thread para o laço poder mandar ping enquanto o modelo pensa.
        threading.Thread(target=self._ler_backend, args=(pedido, fila), daemon=True,
                         name=f"backend-{self.conv_id[:8]}").start()
        parser = emulacao.ParserCercas() if cercas else None
        chamadas: list[tuple[str, dict]] = []
        invalidas: list[tuple[str, str]] = []  # (bruto, erro), como emulacao.mensagem_correcao espera

        def classificar(nome: str, argumentos: dict | None, erro: str | None = None) -> None:
            # O Duo só pode receber ferramentas que ele mesmo ofereceu.
            if erro:
                invalidas.append((nome, f"{nome}: {erro}"))
            elif catalogo.eh_meta(nome) or catalogo.eh_leitura(nome):
                chamadas.append((nome, argumentos or {}))
            elif catalogo.adiada(nome) or catalogo.conhece(nome):
                # Viu o nome na lista e chamou sem carregar. A ferramenta existe: dizer que não
                # existe faz o modelo desistir de um caminho certo.
                catalogo.carregar(nome) if catalogo.adiada(nome) else None
                # Fora do histórico, e não só recusada. Medido: com uma chamada vazia à vista, o modelo a imita
                # mesmo raciocinando que precisa do argumento, e nem um erro mais claro o tira disso.
                faltando = parametros_faltando(catalogo.definicao(nome) or {}, argumentos)
                if faltando:
                    invalidas.append((nome, erro_parametros_faltando(nome, faltando)))
                else:
                    chamadas.append((nome, argumentos or {}))
            else:
                invalidas.append((nome, f"a ferramenta {nome} não existe"))

        def do_parser(saidas: list[tuple]) -> None:
            for saida in saidas:
                if saida[0] == "texto":
                    self._texto(saida[1])
                elif saida[0] == "chamada":
                    classificar(saida[1]["nome"], saida[1]["argumentos"])
                else:
                    invalidas.append((saida[1], saida[2]))

        espera = opcoes.espera_primeiro_token
        while True:
            tipo, valor = self._proximo(fila, time.monotonic() + espera,
                                        f"O modelo não respondeu em {espera} s.")
            if tipo == "fim":
                break
            if tipo == "erro":
                raise valor
            espera = ESPERA_ENTRE_EVENTOS
            if isinstance(valor, Texto):
                if parser:
                    do_parser(parser.alimentar(valor.texto))
                else:
                    self._texto(valor.texto)
            elif isinstance(valor, Raciocinio):
                if pedido.pensar:
                    self._raciocinio(valor.texto)
            elif isinstance(valor, ChamadaFerramenta):
                classificar(valor.nome, valor.argumentos, valor.erro)
            elif isinstance(valor, Uso):
                self._somar_uso(valor, opcoes.contexto_tokens)
            elif isinstance(valor, Cortada):
                self._cortada_rodada = True
            # Raciocínio oculto ou cercas retidas podem deixar o SSE mudo por muito tempo.
            self._manter_conexao(sempre=False)
        if parser:
            do_parser(parser.finalizar())
        return chamadas, invalidas

    def _ler_backend(self, pedido: Pedido, fila: queue.Queue) -> None:
        eventos = None
        try:
            eventos = self.s.backend.conversar(pedido, self.cancelado)
            for evento in eventos:
                if self.cancelado.is_set():
                    return
                fila.put(("ev", evento))
            fila.put(("fim", None))
        except ErroBackend as e:
            fila.put(("erro", e))
        except Exception as e:  # noqa: BLE001 - a thread não tem a quem propagar; o laço avisa o Duo
            log.exception("turno %s: falha inesperada no backend", self._id)
            fila.put(("erro", ErroBackend(f"erro interno do servidor: {e}")))
        finally:
            # Fecha o gerador já: o backend real solta a conexão com o motor sem esperar o GC.
            fechar = getattr(eventos, "close", None)
            if fechar:
                fechar()

    # -- ferramentas ----------------------------------------------------------

    def _tirar_repetidas(self, chamadas: list[tuple[str, dict]],
                         catalogo: Catalogo) -> tuple[list[tuple[str, dict]], list[tuple[str, str]]]:
        """Separa o que pode rodar do que é a mesma chamada de novo; o segundo grupo no formato de `invalidas`.

        Sem isso, o modelo em círculos consulta o PJe a cada rodada até o teto. A leitura interna tem a própria conta.
        """
        novas: list[tuple[str, dict]] = []
        repetidas: list[tuple[str, str]] = []
        leva: set[tuple[str, str]] = set()
        for nome, argumentos in chamadas:
            if catalogo.eh_leitura(nome):
                novas.append((nome, argumentos))
                continue
            # Com as chaves em ordem: a mesma chamada não muda por vir com os argumentos em outra ordem.
            chave = (nome, json.dumps(argumentos, ensure_ascii=False, sort_keys=True, default=str))
            if chave in leva:
                continue
            if chave in self._ultima_leva or self._feitas[chave] >= EXECUCOES_DA_MESMA_CHAMADA:
                repetidas.append((nome, erro_chamada_repetida(nome)))
                continue
            leva.add(chave)
            self._feitas[chave] += 1
            novas.append((nome, argumentos))
        if novas:
            self._ultima_leva = leva
        return novas, repetidas

    def _deu_resultado(self, reais: list[tuple[str, str, dict]]) -> bool:
        """Se a rodada avançou: chamada só interna (carregar, ler) avança; a do Duo, se alguma não voltou com erro."""
        if not reais:
            return True
        ids = {tid for tid, _, _ in reais}
        return any(b.get("type") == "tool_result" and b.get("tool_use_id") in ids
                   and not historico.resultado_com_erro(b) for b in self._no["content"])

    def _registrar_chamadas(self, chamadas: list[tuple[str, dict]], catalogo: Catalogo,
                            jan: janela.ResultadoJanela) -> list[tuple[str, str, dict]]:
        """Põe os tool_use no nó e resolve as internas aqui mesmo; devolve só as chamadas para o Duo."""
        blocos = self._no["content"]
        # As chamadas da rodada dividem o espaço: os resultados delas chegam todos na próxima.
        espaco = max(janela.TRECHO_MINIMO, jan.espaco_resultado // max(len(chamadas), 1) - janela.CUSTO_POR_LEITURA)
        reais, internas = [], []
        for nome, argumentos in chamadas:
            tid = _novo_id()
            bloco = {"type": "tool_use", "id": tid, "name": nome, "input": argumentos}
            if catalogo.eh_meta(nome):
                # Interna: o modelo precisa vê-la no histórico, o Duo nunca.
                bloco["interno"] = True
                internas.append({"type": "tool_result", "tool_use_id": tid, "is_error": False,
                                 "content": catalogo.responder_meta(argumentos), "interno": True})
            elif catalogo.eh_leitura(nome):
                bloco["interno"] = True
                internas.append(self._responder_leitura(tid, argumentos, espaco))
            else:
                reais.append((tid, nome, argumentos))
            blocos.append(bloco)
        blocos.extend(internas)
        return reais

    def _responder_leitura(self, tid: str, argumentos: object, espaco: int) -> dict:
        """O bloco com a resposta de `ler_trecho_resultado`. No log, só metadados: nunca o trecho."""
        medir = leitura.peso_nas_cercas if self._cercas else janela.estimar_tokens
        # Pelos nós sem o que o usuário tirou: o que ele tirou não volta nem por este caminho.
        nos = historico.sem_excluidos(self._ramo + [self._no], self._excluidos)
        lido = leitura.ler(nos, argumentos, espaco, medir)
        resposta = {"type": "tool_result", "tool_use_id": tid, "is_error": lido.erro is not None,
                    "content": lido.texto, "interno": True}
        if lido.erro:
            log.info("turno %s: leitura interna recusada | %s", self._id, lido.erro)
            return resposta
        anteriores = self._leituras.setdefault((lido.alvo, lido.inicio), [])
        repetida = _resposta_repetida([(self._trechos[a], self._vistos_rodada.get(a, [])) for a in anteriores])
        if repetida:
            log.info("turno %s: leitura interna repetida | id=%s inicio=%d", self._id, lido.alvo, lido.inicio)
            resposta["content"] = repetida
            return resposta
        anteriores.append(tid)
        # Outra chamada igual na mesma rodada também é repetição: esta chega inteira na próxima.
        self._vistos_rodada[tid] = [(0, len(lido.texto))]
        self._trechos[tid] = lido
        resposta["trecho"] = lido.trecho()
        log.info("turno %s: leitura interna | id=%s inicio=%d fim=%d | %d de %d tokens", self._id, lido.alvo,
                 lido.inicio, lido.fim, lido.tokens, lido.total)
        return resposta

    def _executar_no_duo(self, reais: list[tuple[str, str, dict]]) -> None:
        ids = [tid for tid, _, _ in reais]
        # Registra antes de emitir: o resultado pode chegar antes de `chamadas` retornar.
        with self._trava:
            self._pendentes = set(ids)
        # Grava já com os tool_use: se o Duo sumir na espera, a chamada fica no histórico.
        self._gravar(self._no)
        self.emissor.chamadas(reais)
        log.info("turno %s: ferramentas pedidas ao DUO: %s", self._id, ", ".join(n for _, n, _ in reais))
        obtidos = self._aguardar_resultados(ids)
        for tid in ids:
            r = obtidos[tid]
            self._no["content"].append({
                "type": "tool_result", "tool_use_id": tid,
                "content": limpar_resultado(historico.texto_resultado(r.get("content"))),
                "is_error": bool(r.get("is_error")),
            })
        self._gravar(self._no)

    def _aguardar_resultados(self, ids: list[str]) -> dict[str, dict]:
        obtidos: dict[str, dict] = {}
        limite = time.monotonic() + ESPERA_RESULTADO
        while len(obtidos) < len(ids):
            r = self._proximo(self._resultados, limite, "O DUO não devolveu os resultados das ferramentas a tempo.")
            obtidos[r["tool_use_id"]] = r
        return obtidos

    # -- espera e conexão ------------------------------------------------------

    def _proximo(self, fila: queue.Queue, limite: float, mensagem: str):
        """Próximo item da fila, cuidando da conexão na folga; ErroTurno(mensagem) passado o `limite`."""
        while True:
            try:
                return fila.get(timeout=min(VERIFICAR_CONEXAO, INTERVALO_PING))
            except queue.Empty:
                pass
            self._manter_conexao(sempre=True)
            if time.monotonic() > limite:
                raise ErroTurno(mensagem)

    def _manter_conexao(self, sempre: bool) -> None:
        """Descobre cedo que o Duo saiu (ClienteDesconectou) e manda ping se o SSE ficou mudo."""
        agora = time.monotonic()
        # Entre eventos do modelo, a sonda é espaçada: não vale uma chamada de sistema por token.
        if sempre or agora - self._verificado_em >= VERIFICAR_CONEXAO:
            self._verificado_em = agora
            self.emissor.verificar_conexao()
        if agora - self.emissor.ultima_escrita >= INTERVALO_PING:
            self.emissor.ping()

    # -- nó do assistente -----------------------------------------------------

    def _texto(self, s: str) -> None:
        if not s:
            return
        blocos = self._no["content"]
        if blocos and blocos[-1].get("type") == "text":
            blocos[-1]["text"] += s
        else:
            blocos.append({"type": "text", "text": s})
        self.emissor.texto(s)
        self._falou = True
        self._falou_rodada = True

    def _raciocinio(self, s: str) -> None:
        if not s:
            return
        blocos = self._no["content"]
        if blocos and blocos[-1].get("type") == "thinking":
            blocos[-1]["summaries"][0]["summary"] += s
        else:
            blocos.append({"type": "thinking", "summaries": [{"summary": s}]})
        self.emissor.raciocinio(s)

    def _somar_uso(self, uso: Uso, contexto_tokens: int) -> None:
        self._entrada_rodada = uso.entrada
        atual = self._uso
        self._uso = uso if atual is None else Uso(atual.entrada + uso.entrada, atual.saida + uso.saida)
        # Com a contagem na mão, já se sabe que nada foi cortado.
        if uso.entrada >= 0.95 * contexto_tokens and not self._rodada_contada:
            # Em `num_ctx - 1` só chega quem cortou: é onde satura um Ollama antigo, que ignora `truncate: false`.
            cortou = self.s.backend.pode_cortar_calado or uso.entrada >= contexto_tokens - 1
            self._avisar(AVISO_LIMITE_JANELA if cortou else AVISO_POUCO_PARA_RESPONDER)

    def _avisar(self, *avisos: str) -> None:
        for aviso in avisos:
            if aviso not in self._avisos:
                self._avisos.append(aviso)

    def _gravar(self, no: dict) -> None:
        try:
            self.s.armazenamento.gravar_mensagem(self.conv_id, no)
        except KeyError:
            raise ErroTurno("A conversa foi excluída durante a resposta.") from None
        except OSError as e:
            raise _erro_gravacao(e) from e

    def _persistir(self) -> None:
        """Grava o que houver: o nó (se tiver conteúdo), o modelo e as ferramentas carregadas.

        Falha de disco vira ErroTurno: o Duo recebe `error` em vez de um `message_stop` de uma
        resposta que não foi salva.
        """
        self._gravado = True
        if self._no is None:
            return
        try:
            # Sem nó só com lembrete: um pedido sem nó do assistente é o que diz ao Duo que o envio seguinte sai do pai
            # dele, senão o que falhou, ou o que não coube, ficaria na conversa.
            if not historico.so_lembretes(self._no):
                self.s.armazenamento.gravar_mensagem(self.conv_id, self._no)
            campos: dict = {"model": self._modelo, "fator_tokens": self._fator_tokens,
                            "ultimo_uso": self._ultimo_uso, "ultimo_envio": envio.para_guardar(self.corpo)}
            if self._catalogo is not None:
                campos["ferramentas_carregadas"] = self._catalogo.carregadas
            self.s.armazenamento.atualizar_conversa(self.conv_id, **campos)
        except KeyError:
            # Excluída durante a resposta: foi o usuário que pediu, não há o que salvar.
            log.info("turno %s: a conversa foi excluída durante a resposta", self._id)
        except OSError as e:
            raise _erro_gravacao(e) from e


def _resposta_repetida(anteriores: list[tuple[leitura.Lido, list[tuple[int, int]]]]) -> str | None:
    """A mensagem no lugar do trecho, se esta leitura for repetição; None se ela deve seguir.

    `anteriores` são as leituras do mesmo trecho neste turno, cada uma com as faixas dela à vista na
    rodada. Sem isso o modelo poderia pedir o mesmo trecho até acabarem as rodadas, sem nunca avançar.
    O ponto de continuar é o da leitura anterior, que foi a que o modelo viu: a que caberia agora
    pode ir mais longe, e apontar para o fim dela pularia texto nunca lido.
    """
    if not anteriores:
        return None
    destino = f'id="{anteriores[0][0].alvo}", inicio={anteriores[0][0].inicio}'
    for lido, faixas in reversed(anteriores):
        if not faixas:
            continue
        if faixas == [(0, len(lido.texto))]:
            continuar = f" Para continuar, use inicio={lido.fim}." if lido.fim < lido.tamanho else " É o fim do resultado."
            return f"O trecho pedido ({destino}) já foi lido nesta resposta e está visível acima.{continuar}"
        # Truncada: a 1ª faixa é o começo da resposta, com o cabeçalho; o que falta vem logo depois dela.
        omitido = lido.inicio + max(faixas[0][1] - lido.cabecalho, 0)
        if omitido > lido.inicio:
            return (f"O trecho pedido ({destino}) já foi lido nesta resposta, mas parte dele foi omitida para caber "
                    f"na janela. Para ler essa parte, use inicio={omitido}.")
    if len(anteriores) >= LEITURAS_DO_MESMO_TRECHO:
        return (f"O trecho pedido ({destino}) já foi lido {len(anteriores)} vez(es) nesta resposta e não fica na "
                "janela junto com o resto. Não leia de novo: responda com o que tem e diga ao usuário o que "
                "ficou sem ler.")
    return None


def _lacunas(faixas: list[tuple[int, int]], tamanho: int) -> list[tuple[int, int]]:
    """As partes de 0 a `tamanho` que nenhuma faixa cobre."""
    lacunas, posicao = [], 0
    for inicio, fim in sorted(faixas):
        if inicio > posicao:
            lacunas.append((posicao, inicio))
        posicao = max(posicao, fim)
    if posicao < tamanho:
        lacunas.append((posicao, tamanho))
    return lacunas


def _erro_gravacao(e: OSError) -> ErroTurno:
    return ErroTurno(f"Não foi possível gravar a conversa no disco ({e.strerror or e}); a resposta não "
                     "foi salva. Confira o espaço em disco e se outro programa (antivírus, editor) está "
                     "com o banco dados/duo.sqlite3 aberto, e envie de novo.")
