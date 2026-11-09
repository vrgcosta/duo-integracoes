"""Ferramentas nativas: conversão de definições, acúmulo de chamadas em streaming e modo.

As definições chegam do Duo no formato Anthropic; os motores locais falam o formato de
funções da OpenAI. As chamadas voltam em deltas fatiados por índice.
"""

from __future__ import annotations

import json

from .backends.base import ChamadaFerramenta, InfoModelo


LEMBRETE_RODADA_VAZIA = (
    "Sua última resposta veio sem texto e sem chamada de ferramenta. Se falta um dado, chame a "
    "ferramenta agora; se não, responda ao usuário em texto."
)
LEMBRETE_FINAL_VAZIA = (
    "Sua última resposta veio sem texto, e não há mais ferramentas nesta resposta. Escreva agora, em texto, a resposta ao "
    "usuário com o que você já obteve, e diga o que ficou faltando."
)
_INICIO_TETO = "Limite de rodadas de ferramentas atingido"
LEMBRETE_TETO = (
    _INICIO_TETO + " ({rodadas}). Não chame mais ferramentas: responda agora ao usuário com o que "
    "você já obteve e diga o que ficou faltando."
)
_INICIO_SEM_ESPACO = "A janela de contexto encheu"
LEMBRETE_SEM_ESPACO = (
    _INICIO_SEM_ESPACO + ": não cabem mais definições nem resultados de ferramentas. Não chame ferramentas: responda "
    "agora ao usuário com o que você já obteve e diga o que ficou faltando."
)
_INICIO_SEM_PROGRESSO = "As últimas chamadas de ferramenta não deram resultado"
LEMBRETE_SEM_PROGRESSO = (
    _INICIO_SEM_PROGRESSO + ". Não chame mais ferramentas: responda agora ao usuário com o que você já obteve e diga o "
    "que ficou faltando."
)
_INICIO_CORRECAO = "Não foi possível executar a(s) chamada(s) da sua última resposta:"


def eh_lembrete_interno(texto: str) -> bool:
    """O servidor falando com o modelo, gravado como mensagem do usuário sem que ele a tenha escrito."""
    return texto.startswith((LEMBRETE_RODADA_VAZIA, LEMBRETE_FINAL_VAZIA, _INICIO_TETO, _INICIO_SEM_ESPACO,
                             _INICIO_SEM_PROGRESSO, _INICIO_CORRECAO))


def parametros_faltando(definicao: dict, argumentos: object) -> list[tuple[str, str]]:
    """Os parâmetros obrigatórios que a chamada não trouxe, cada um com a descrição que a definição dá.

    Zero e falso são valores: só falta o que veio ausente, nulo ou como texto vazio.
    """
    esquema = definicao.get("input_schema") or {}
    exigidos = esquema.get("required")
    propriedades = esquema.get("properties") or {}
    dados = argumentos if isinstance(argumentos, dict) else {}
    return [(nome, (propriedades.get(nome) or {}).get("description") or "")
            for nome in (exigidos if isinstance(exigidos, list) else [])
            if dados.get(nome) in (None, "")]


def erro_parametros_faltando(nome: str, faltando: list[tuple[str, str]]) -> str:
    itens = ", ".join(f"`{p}` ({d})" if d else f"`{p}`" for p, d in faltando)
    return f"{nome}: faltou o parâmetro obrigatório {itens}. Informe-o na chamada."


def erro_chamada_repetida(nome: str) -> str:
    return (f"{nome}: você já fez esta chamada com estes mesmos argumentos, e o resultado está acima. Não repita: use o "
            "que já tem, mude os argumentos ou responda ao usuário.")


def mensagem_correcao_nativa(invalidas: list[tuple[str, str]]) -> str:
    """Pede ao modelo que refaça chamadas recusadas. Cada item é (bruto, erro).

    Fala só de chamadas: no modo nativo o modelo não conhece os blocos das cercas.
    """
    detalhes = "\n".join(f"- {erro}" for _, erro in invalidas)
    return (
        f"{_INICIO_CORRECAO}\n"
        f"{detalhes}\n"
        "Chame de novo só as ferramentas disponíveis, com argumentos válidos, ou responda ao usuário "
        "sem ferramentas."
    )

# Acima disso, um campo `data` em resultado de ferramenta é tratado como arquivo em base64.
_LIMITE_DATA = 1000


def para_funcoes(defs: list[dict]) -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": d["name"],
                "description": d.get("description", ""),
                "parameters": d.get("input_schema") or {"type": "object", "properties": {}},
            },
        }
        for d in defs
    ]


class AcumuladorChamadas:
    """Junta os deltas de `tool_calls` do streaming; o JSON só é válido ao final."""

    def __init__(self) -> None:
        self._por_indice: dict[int, dict] = {}

    def alimentar(self, deltas: list[dict]) -> None:
        for d in deltas:
            atual = self._por_indice.setdefault(d.get("index", 0), {"nome": "", "args": ""})
            fn = d.get("function") or {}
            # O nome vem inteiro no primeiro delta; concatenar protegeria de motores que o fatiam.
            if fn.get("name"):
                atual["nome"] += fn["name"]
            if fn.get("arguments"):
                atual["args"] += fn["arguments"]

    def finalizar(self) -> list[ChamadaFerramenta]:
        chamadas = []
        for indice in sorted(self._por_indice):
            item = self._por_indice[indice]
            bruto = item["args"].strip()
            if not bruto:
                chamadas.append(ChamadaFerramenta(item["nome"], {}))
                continue
            try:
                argumentos = json.loads(bruto)
            except json.JSONDecodeError as e:
                chamadas.append(ChamadaFerramenta(
                    item["nome"], None, erro=f"argumentos não são JSON válido: {e}"))
                continue
            if not isinstance(argumentos, dict):
                chamadas.append(ChamadaFerramenta(
                    item["nome"], None, erro="argumentos não são JSON válido: esperado um objeto"))
                continue
            chamadas.append(ChamadaFerramenta(item["nome"], argumentos))
        return chamadas


def modo_ferramentas(pedido: str, cfg: dict, modelo: str, info: InfoModelo | None) -> str:
    """Decide entre ferramentas nativas ("nativo") e emuladas por cercas ("cercas")."""
    if pedido and pedido != "padrao":
        modo = pedido
    else:
        modo = (cfg.get("ferramentas_por_modelo") or {}).get(modelo) or cfg.get("ferramentas")
    if modo == "auto":
        return "nativo" if info is None or info.ferramentas else "cercas"
    return modo


def _limpar(valor):
    if isinstance(valor, list):
        return [_limpar(v) for v in valor]
    if isinstance(valor, dict):
        dados = valor.get("data")
        mime = valor.get("mediaType") or valor.get("mimeType") or valor.get("mime_type")
        if isinstance(dados, str) and len(dados) > _LIMITE_DATA and mime:
            nome = valor.get("name") or valor.get("nome") or valor.get("filename") or valor.get("fileName")
            return {"arquivo_omitido": nome, "tipo": mime}
        return {k: _limpar(v) for k, v in valor.items()}
    return valor


def limpar_resultado(texto: str) -> str:
    """Tira base64 de arquivos do resultado: estoura o contexto sem ajudar o modelo."""
    try:
        valor = json.loads(texto)
    except (json.JSONDecodeError, TypeError):
        return texto
    return json.dumps(_limpar(valor), ensure_ascii=False)
