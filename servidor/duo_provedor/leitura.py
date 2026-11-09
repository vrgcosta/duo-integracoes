"""A ferramenta `ler_trecho_resultado`: a saída quando um resultado não coube na janela.

Truncar sem dar como ler o resto é um beco sem saída: as ferramentas do Duo não têm parâmetro de
faixa, e o modelo ou inventava o trecho que faltava ou concluía sem ele. O resultado inteiro continua
guardado no nó da conversa, porque o corte só muda o que vai ao modelo; esta ferramenta o lê de lá.

O servidor a responde sozinho, como a meta-ferramenta do catálogo: não passa pelo Duo, não pede
autorização e fica marcada `interno` no nó. O tamanho é sempre em tokens, porque é o que o modelo
e o usuário entendem da janela; `inicio` é a posição exata, em caracteres, de onde continuar.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Callable

from .historico import texto_resultado
from .janela import LER_TRECHO, caracteres_ate, estimar_tokens

NOME = LER_TRECHO

DEFINICAO = {
    "name": NOME,
    "description": ("Lê a parte omitida de um resultado de ferramenta já recebido nesta conversa, usando o id e o "
                    "inicio indicados na marca \"leia com ler_trecho_resultado(...)\". Devolve o trecho que cabe na "
                    "janela, o tamanho total em tokens e o inicio seguinte. Não consulta o PJe nem pede autorização."),
    "input_schema": {
        "type": "object",
        "properties": {
            "id": {"type": "string", "description": "O id que aparece na marca."},
            "inicio": {"type": "integer", "description": "A posição de onde ler, como aparece na marca."},
        },
        "required": ["id", "inicio"],
    },
}


@dataclass
class Lido:
    """A resposta de uma leitura; com `erro` preenchido, `texto` só explica o que houve."""

    texto: str
    erro: str | None = None  # motivo curto, sem nada do que o modelo mandou: é o que vai ao log
    alvo: str | None = None  # id do resultado original
    inicio: int = 0
    fim: int = 0
    cabecalho: int = 0  # caracteres antes do trecho, em `texto`
    tokens: int = 0  # do trecho
    total: int = 0  # tokens do resultado inteiro
    tamanho: int = 0  # caracteres do resultado inteiro

    def trecho(self) -> dict:
        """O que o bloco gravado precisa guardar para uma leitura desta leitura ir ao original."""
        return {"alvo": self.alvo, "inicio": self.inicio, "cabecalho": self.cabecalho}


def achar(nos: list[dict], id_: str) -> tuple[str, dict] | None:
    """(nome da ferramenta, bloco tool_result) do resultado com esse id, no ramo da conversa."""
    nome, bloco = "", None
    for no in nos:
        conteudo = no.get("content")
        for b in conteudo if isinstance(conteudo, list) else []:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "tool_use" and b.get("id") == id_:
                nome = b.get("name") or ""
            elif b.get("type") == "tool_result" and b.get("tool_use_id") == id_:
                bloco = b
    return (nome, bloco) if bloco is not None else None


def peso_nas_cercas(texto: str) -> int:
    """O peso do texto como ele vai nas cercas: dentro de JSON, com aspas e quebras de linha escapadas."""
    return estimar_tokens(json.dumps(texto, ensure_ascii=False))


def ler(nos: list[dict], argumentos: object, espaco: int, medir: Callable[[str], int] = estimar_tokens) -> Lido:
    """O trecho pedido, com até `espaco` tokens pela conta de `medir`, a do modo em que ele vai ao modelo.

    Nunca levanta: pedido ruim vira mensagem ao modelo. Os tamanhos informados são os do resultado.
    """
    args = argumentos if isinstance(argumentos, dict) else {}
    id_, inicio = args.get("id"), args.get("inicio", 0)
    if not isinstance(id_, str) or not id_:
        return Lido('Informe em "id" o texto que aparece na marca do resultado.', erro="id inválido")
    if isinstance(inicio, bool) or not isinstance(inicio, int):
        return Lido('Informe em "inicio" o número inteiro que aparece na marca do resultado.', erro="inicio inválido")
    achado = achar(nos, id_)
    if achado is None:
        return Lido(f'Não há resultado com o id "{id_}" nesta conversa. Use o id que aparece na marca.',
                    erro="id desconhecido")

    # Marca de uma leitura limpa ou truncada aponta para ela, mas o que interessa é o original.
    trecho = achado[1].get("trecho")
    if isinstance(trecho, dict) and inicio >= 0:
        id_, inicio = trecho.get("alvo"), trecho.get("inicio", 0) + max(inicio - trecho.get("cabecalho", 0), 0)
        achado = achar(nos, id_)
        if achado is None:
            return Lido(f'O resultado "{id_}" não está mais nesta conversa.', erro="id desconhecido")

    nome, bloco = achado
    if bloco.get("excluido"):
        return Lido(f'O usuário retirou o resultado "{id_}" da janela: não há o que ler. Se precisar dele, peça-o ao '
                    "usuário.", erro="retirado pelo usuário")
    texto = texto_resultado(bloco.get("content"))
    tamanho, total = len(texto), estimar_tokens(texto)
    if not texto:
        return Lido(f'O resultado "{id_}" está vazio.', erro="resultado vazio")
    if inicio < 0 or inicio >= tamanho:
        return Lido(f'Não há o que ler em inicio={inicio}: o resultado "{id_}" tem {total} tokens e as posições vão '
                    f"de 0 a {tamanho - 1}. Use o inicio que aparece na marca.", erro="inicio fora do resultado")

    # `max` garante que a leitura avança, mesmo sem espaço nenhum: senão o modelo repetiria o pedido.
    fim = max(caracteres_ate(texto, espaco, inicio, medir), inicio + 1)
    tokens = estimar_tokens(texto[inicio:fim])
    prefixo = f'[Trecho de {nome} (id="{id_}"): {tokens} de {total} tokens, a partir de inicio={inicio}.'
    if fim < tamanho:
        prefixo += (f" Faltam {estimar_tokens(texto[fim:])} tokens depois deste trecho: continue com "
                    f'{NOME}(id="{id_}", inicio={fim}).]\n')
    else:
        prefixo += " É o fim do resultado.]\n"
    return Lido(prefixo + texto[inicio:fim], alvo=id_, inicio=inicio, fim=fim, cabecalho=len(prefixo),
                tokens=tokens, total=total, tamanho=tamanho)
