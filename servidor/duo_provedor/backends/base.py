"""Contrato dos backends de LLM.

Formato neutro de mensagem (contrato entre `historico` e os backends); cada backend o
traduz para o protocolo do seu motor:

- ``{"papel": "sistema", "texto"}``
- ``{"papel": "usuario", "texto", "imagens"?: [base64], "texto_sem_anexos"?: str,
  "resultado"?: True, "itens"?: [...]}`` (com ``resultado``, é a mensagem que leva os resultados das
  ferramentas no modo cercas; ``texto`` já traz tudo, e ``itens`` é o que a janela usa para refazê-lo)
- ``{"papel": "assistente", "texto", "chamadas"?: [{"id", "nome", "argumentos"}]}``
- ``{"papel": "ferramenta", "id_chamada", "nome", "texto"}``

Um resultado pode ter ``"truncado"`` (parte foi trocada por uma marca para caber na janela) ou
``"omitido"`` (foi trocado inteiro por uma marca). Os backends ignoram chaves desconhecidas, para
o formato poder crescer.

`ChamadaFerramenta.erro` preenchido significa que os argumentos vieram inválidos: o
chamador devolve o erro ao modelo em vez de executar a ferramenta.
"""

from __future__ import annotations

import abc
import threading
from dataclasses import dataclass
from typing import Iterator


@dataclass(frozen=True)
class Texto:
    texto: str


@dataclass(frozen=True)
class Raciocinio:
    texto: str


@dataclass(frozen=True)
class ChamadaFerramenta:
    nome: str
    argumentos: dict | None
    erro: str | None = None


@dataclass(frozen=True)
class Uso:
    entrada: int
    saida: int


@dataclass(frozen=True)
class Cortada:
    """O motor parou a resposta no limite, e não porque ela acabou. Só sai quando acontece."""


Evento = Texto | Raciocinio | ChamadaFerramenta | Uso | Cortada


@dataclass
class InfoModelo:
    id: str
    rotulo: str
    ferramentas: bool
    raciocinio: bool
    visao: bool
    nuvem: bool
    contexto_max: int | None
    # False para o que o motor tem instalado mas não gera texto (modelo de embedding): `listar_modelos`
    # não o devolve, e `info_modelo` ainda responde por ele para o diagnóstico dizer o motivo.
    conversa: bool = True


@dataclass
class Pedido:
    modelo: str
    mensagens: list[dict]
    ferramentas: list[dict]
    pensar: bool
    contexto_tokens: int
    temperatura: float | None


class ErroBackend(Exception):
    """Falha ao falar com o motor (indisponível, modelo ausente, resposta inválida)."""


class ErroAcimaDaJanela(ErroBackend):
    """O motor recusou o pedido por passar da janela: é a prova de que não cabe, e não uma falha dele.

    `total` é o número de tokens que ele informou; None, se a recusa não disse.
    """

    def __init__(self, total: int | None) -> None:
        super().__init__(f"O pedido tem {total if total is not None else 'mais'} tokens, além da janela de contexto.")
        self.total = total


class Backend(abc.ABC):
    nome: str
    # Sem contagem, o motor pode cortar um pedido grande sem avisar? O que não corta recusa, como o Ollama com
    # `truncate: false`, ou não tem o que cortar, como o simulado. Muda o texto dos avisos de medida estimada.
    pode_cortar_calado: bool = True

    @abc.abstractmethod
    def listar_modelos(self) -> list[InfoModelo]:
        ...

    @abc.abstractmethod
    def info_modelo(self, modelo: str) -> InfoModelo | None:
        ...

    @abc.abstractmethod
    def conversar(self, pedido: Pedido, cancelado: threading.Event) -> Iterator[Evento]:
        ...

    def contar_tokens(self, pedido: Pedido, cancelado: threading.Event | None = None) -> int | None:
        """Os tokens de entrada que o motor contaria para este pedido, antes de ele processar.

        Pode passar da janela do pedido: é assim que o turno fica sabendo que não cabe. Se o motor recusar o pedido
        sem contar, levante `ErroAcimaDaJanela`, que vale como a mesma prova. None: não há como contar, e o turno fica
        com a estimativa. Uma falha pode levantar `ErroBackend`, com o mesmo efeito. `cancelado` ligado significa que
        ninguém mais espera o número.
        """
        return None

    def versao_motor(self) -> str | None:
        """A versão do motor, quando ele sabe informar; só para o `--verificar`."""
        return None

    def contexto_em_uso(self, modelo: str) -> int | None:
        """Tamanho de contexto realmente carregado, quando o motor sabe informar."""
        return None

    def memoria_em_uso(self, modelo: str) -> tuple[int, int] | None:
        """(total, parte na VRAM), em bytes, do modelo carregado agora, quando o motor sabe informar.

        Com o total maior que a parte na VRAM, o resto está no processador e a resposta fica
        bem mais lenta; o `--verificar` avisa. None: o motor não informa ou o modelo não está
        carregado.
        """
        return None
