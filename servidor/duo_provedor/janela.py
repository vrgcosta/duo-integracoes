"""Controle da janela de contexto.

O modelo local costuma ter janela pequena. Em vez de deixar o servidor do modelo cortar o
começo da conversa em silêncio, o ajuste é feito aqui, e cada perda vira um aviso que a
interface mostra ao usuário e uma nota que o próprio modelo lê.

O corte vai por idade, do mais antigo para o mais novo (ver `ajustar`). A ordem importa mais do
que parece: numa sessão real, o corte antigo protegia o 1º turno inteiro e removia o 2º, então o
modelo voltava a ver a tentativa que falhou, deixava de ver a que deu certo e afirmava com
segurança que nada tinha sido encontrado.

Tudo o que o usuário e o modelo leem sobre tamanho está em tokens. Os caracteres ficam só por
dentro: é a posição exata de onde continuar a ler (`inicio`).
"""

import json
import math
import re
from dataclasses import dataclass, field
from typing import Callable, Collection

from .emulacao import mensagem_resultados

# Reserva para a resposta do modelo: um quarto da janela, no máximo 4096 tokens.
RESERVA_MAXIMA_RESPOSTA = 4096
# Fração do orçamento que as instruções do DUO podem ocupar antes de serem resumidas.
FRACAO_SISTEMA_DUO = 0.4
# Teto da correção da estimativa pelo uso real: um número absurdo do motor não pode esvaziar a janela.
FATOR_MAXIMO = 2.0
# Calibragem de `estimar_tokens`, medida no Ollama com o qwen3.5:4b em 30 mil caracteres de cada tipo:
# uma frase corrida repetida deu 6757 tokens (4,4 caracteres por token), linhas de IDs e datas 25075 (1,2),
# JSON 14261 (2,1), peças fictícias com vocabulário variado 7379 (4,1) e as mesmas sem acento 7653 (3,9).
# O acento não encarece: a forma sem ele é que é mais rara. Os valores ficam logo acima de todas.
TOKENS_POR_DIGITO = 1.1  # o modelo escreve número dígito a dígito
TOKENS_POR_SIMBOLOS = 1.2  # por grupo de até 4 símbolos seguidos: `","` é um token só
LETRAS_POR_TOKEN = 4.25  # letras e espaços
_SIMBOLOS = re.compile(r"[^\w\s]{1,4}")

# A ferramenta que o servidor responde sozinho (ver `leitura`); as marcas abaixo a citam.
LER_TRECHO = "ler_trecho_resultado"
# Começo da marca de um resultado limpo.
MARCA_OMITIDO = "[resultado omitido da janela"
# O mínimo que um resultado da rodada mais recente mantém, em tokens: sem isso, o modelo só veria a
# marca e leria de novo o mesmo pedaço que também não caberia.
TRECHO_MINIMO = 500
# Quanto baixar o teto de truncamento, em tokens, se a conta direta não bastou (marca, cercas).
PASSO_TRUNCAMENTO = 50
# Reservado uma vez na conta do trecho que cabe: a moldura dos resultados nas cercas e uma margem.
FOLGA_LEITURA = 150
# O que cada leitura acrescenta além do trecho: a chamada, com o id duas vezes, e o cabeçalho da resposta.
CUSTO_POR_LEITURA = 250


def truncar_meio(texto: str, limite: int) -> str:
    """Corta o miolo do texto, mantendo começo e fim: o começo costuma trazer a estrutura e o fim o desfecho."""
    if len(texto) <= limite:
        return texto
    inicio = limite * 2 // 3
    fim = limite // 3
    omitidos = estimar_tokens(texto[inicio:len(texto) - fim])
    sufixo = texto[len(texto) - fim:] if fim else ""
    return f"{texto[:inicio]}\n[… {omitidos} tokens omitidos …]\n{sufixo}"


def estimar_tokens(texto: str) -> int:
    """Estimativa pelo tipo de conteúdo; erra um pouco para mais, o que é o lado seguro.

    Uma conta só por caracteres não serve: as listas de documentos e o JSON das ferramentas, que
    são o forte do que o Duo manda, têm muito menos caracteres por token que um texto corrido, e 3
    por token ficava 2,5 vezes abaixo do real numa lista de IDs. Aí o motor corta o começo em
    silêncio, que é o que a janela existe para evitar.
    """
    sem_simbolos, simbolos = _SIMBOLOS.subn("", texto)
    digitos = sum(map(sem_simbolos.count, "0123456789"))
    letras = len(sem_simbolos) - digitos
    return math.ceil(digitos * TOKENS_POR_DIGITO + simbolos * TOKENS_POR_SIMBOLOS + letras / LETRAS_POR_TOKEN)


def caracteres_ate(texto: str, tokens: int, inicio: int = 0, medir: Callable[[str], int] = estimar_tokens) -> int:
    """A maior posição `fim` em que `texto[inicio:fim]` pesa até `tokens`, pela conta de `medir`."""
    # Nenhum trecho rende mais que LETRAS_POR_TOKEN caracteres por token, o que limita a busca:
    # num documento enorme, medir o resto todo a cada passo é desperdício.
    alto = min(len(texto), inicio + math.ceil(tokens * LETRAS_POR_TOKEN) + 1)
    if medir(texto[inicio:alto]) <= tokens:
        return alto
    baixo = inicio
    while alto - baixo > 1:
        meio = (baixo + alto) // 2
        if medir(texto[inicio:meio]) <= tokens:
            baixo = meio
        else:
            alto = meio
    return baixo


def _inicio_da_cauda(texto: str, tokens: int) -> int:
    """A menor posição em que `texto[posicao:]` pesa até `tokens`."""
    baixo = max(0, len(texto) - math.ceil(tokens * LETRAS_POR_TOKEN) - 1)
    if estimar_tokens(texto[baixo:]) <= tokens:
        return baixo
    alto = len(texto)
    while alto - baixo > 1:
        meio = (baixo + alto) // 2
        if estimar_tokens(texto[meio:]) <= tokens:
            alto = meio
        else:
            baixo = meio
    return alto


def fator_real(entrada_real: int, estimativa: int) -> float:
    """Quantas vezes o motor contou mais que a estimativa, entre 1 e FATOR_MAXIMO.

    A estimativa costuma sobrar, mas o molde que o motor põe em volta das mensagens e um texto fora
    do comum podem passar dela, e aí o motor recusa ou, se for dos que cortam calado, corta o começo. Nunca fica abaixo de 1
    porque o `prompt_eval_count` do Ollama pode excluir o prefixo em cache: um número baixo não
    prova que a conversa inteira pesa menos.
    """
    if estimativa <= 0:
        return 1.0
    return max(1.0, min(FATOR_MAXIMO, entrada_real / estimativa))


def aviso_truncado(nome: str, recebidos: int, total: int, pelo_limite: bool = False) -> str:
    motivo = "Pelo limite_resultado" if pelo_limite else "Para caber na janela"
    return f"{motivo}, o modelo recebeu {recebidos} de {total} tokens de {nome}."


@dataclass
class Truncado:
    """Um resultado do turno atual que foi ao modelo pela metade."""

    id: str
    nome: str
    recebidos: int  # tokens
    total: int  # tokens
    pelo_limite: bool = False  # o teto da configuração, e não a falta de espaço

    @property
    def aviso(self) -> str:
        return aviso_truncado(self.nome, self.recebidos, self.total, self.pelo_limite)


@dataclass(frozen=True)
class ComLeitura:
    """O sistema fixo e o peso das definições com a ferramenta de leitura no catálogo."""

    sistema_fixo: str
    peso_ferramentas: int


def reserva_resposta(contexto_tokens: int) -> int:
    return min(RESERVA_MAXIMA_RESPOSTA, contexto_tokens // 4)


@dataclass
class ResultadoJanela:
    mensagens: list[dict]
    avisos: list[str] = field(default_factory=list)
    # Tokens estimados do que vai ao motor, sem `fator_tokens`: é a base da medição seguinte,
    # e com o fator embutido ele se acumularia a cada rodada.
    estimativa: int = 0
    # Há marca de resultado limpo ou truncado na janela: é o que justifica expor a leitura.
    leitura: bool = False
    # Por id de chamada, as faixas (em caracteres do resultado) que o modelo tem à vista.
    vistos: dict[str, list[tuple[int, int]]] = field(default_factory=dict)
    truncados: list[Truncado] = field(default_factory=list)
    # Tokens que uma leitura pode trazer na próxima rodada, se tudo o que dá para limpar sair.
    espaco_resultado: int = 0
    # O que ficou de cada turno, na ordem de entrada (lista vazia: saiu inteiro). É por aqui que o medidor sabe
    # o que está fora; as mensagens enviadas já não dizem de que turno cada uma veio.
    turnos: list[list[dict]] = field(default_factory=list)
    # As partes de `estimativa`, para o medidor mostrar as seções.
    peso_sistema: int = 0
    peso_ferramentas: int = 0
    # O espaço para o pedido, e o multiplicador com que a conta foi feita.
    orcamento: int = 0
    fator: float = 1.0
    # Algo saiu ou foi truncado, ou seja: a conta com um fator menor poderia levar mais.
    cortou: bool = False

    @property
    def total(self) -> int:
        """Os tokens do pedido, pela estimativa corrigida pelo fator."""
        return math.ceil(self.estimativa * self.fator)

    @property
    def excede(self) -> bool:
        """Nem todos os cortes bastaram: o pedido é maior que o espaço útil da janela."""
        return self.total > self.orcamento


def _peso(mensagem: dict) -> int:
    peso = estimar_tokens(mensagem.get("texto", ""))
    if mensagem.get("chamadas"):
        # As chamadas estruturadas também vão no prompt, então entram na conta.
        peso += estimar_tokens(json.dumps(mensagem["chamadas"], ensure_ascii=False))
    return peso


class _Pesos:
    """O peso das mensagens, calculado uma vez só: `excede` roda a cada passo do corte."""

    def __init__(self) -> None:
        self._memoria: dict[int, tuple[dict, int]] = {}

    def de(self, mensagem: dict) -> int:
        achado = self._memoria.get(id(mensagem))
        if achado is None:
            # Guardar a mensagem junto impede o id de ser reaproveitado por outra mais tarde.
            achado = self._memoria[id(mensagem)] = (mensagem, _peso(mensagem))
        return achado[1]

    def dos(self, turnos: list[list[dict]]) -> int:
        return sum(self.de(m) for t in turnos for m in t)


def _eh_resultado(mensagem: dict) -> bool:
    # Nas cercas o resultado é uma mensagem `usuario`, que o `historico` marca: sem a marca, a
    # janela poderia apagar um pedido humano achando que é resultado.
    return mensagem.get("papel") == "ferramenta" or bool(mensagem.get("resultado"))


def _resultados(mensagem: dict) -> list[dict]:
    """Os resultados que a mensagem leva, em forma única: {"id", "nome", "conteudo", ...}."""
    if mensagem.get("papel") == "ferramenta":
        return [{"id": mensagem.get("id_chamada"), "nome": mensagem.get("nome"),
                 "conteudo": mensagem.get("texto", ""),
                 **{k: mensagem[k] for k in ("omitido", "truncado", "interno", "excluido") if k in mensagem}}]
    return (mensagem.get("itens") or []) if mensagem.get("resultado") else []


def _nome(item: dict) -> str:
    # Um resultado cuja chamada não está no ramo chega sem nome; o aviso não pode sair em branco.
    return item.get("nome") or "ferramenta"


def _com_resultado(mensagem: dict, indice: int, conteudo: str, **marcas: object) -> dict:
    """Cópia da mensagem com um resultado trocado.

    No modo nativo, `id_chamada` e `nome` ficam: os motores rejeitam chamada sem resposta, então o
    par chamada/resultado continua válido. Nas cercas, a mensagem é refeita inteira, porque é pela
    marca e pelas cercas que o modelo reconhece a resposta ao que pediu.
    """
    if mensagem.get("papel") == "ferramenta":
        copia = {k: v for k, v in mensagem.items() if k not in ("omitido", "truncado")}
        return {**copia, "texto": conteudo, **marcas}
    itens = list(mensagem["itens"])
    item = {k: v for k, v in itens[indice].items() if k not in ("omitido", "truncado")}
    itens[indice] = {**item, "conteudo": conteudo, **marcas}
    return {**mensagem, "itens": itens, "texto": mensagem_resultados(itens)}


def marca_omitido(id_: str, pode_ler: bool = True) -> str:
    # Diz o que fazer, e não só que sumiu: é a saída que o modelo tem para não afirmar que não existe. Na rodada final
    # não há o que fazer: a ferramenta não vai, e quem obedecesse teria a chamada descartada e ficaria sem resposta.
    return f'{MARCA_OMITIDO}; leia com {LER_TRECHO}(id="{id_}", inicio=0)]' if pode_ler else f"{MARCA_OMITIDO}]"


def marca_truncado(id_: str, inicio: int, omitidos: int, total: int, pode_ler: bool = True) -> str:
    if not pode_ler:
        return f"[… {omitidos} de {total} tokens omitidos …]"
    return f'[… {omitidos} de {total} tokens omitidos; leia com {LER_TRECHO}(id="{id_}", inicio={inicio}) …]'


# Como o cabeçalho de uma leitura manda continuar (ver `leitura.ler`).
_CONTINUE_LENDO = re.compile(rf': continue com {LER_TRECHO}\(id="[^"]*", inicio=\d+\)\.\]')


def _para_a_rodada_final(mensagem: dict) -> dict:
    """A mensagem sem nada que mande chamar ferramenta: na rodada final, a chamada seria descartada."""
    def sem_ordem(texto: str) -> str:
        return _CONTINUE_LENDO.sub(".]", texto, count=1)

    if mensagem.get("papel") == "ferramenta" and mensagem.get("interno"):
        return {**mensagem, "texto": sem_ordem(mensagem.get("texto", ""))}
    if mensagem.get("resultado"):
        itens = [{**item, "conteudo": sem_ordem(item["conteudo"])} if item.get("interno") else item
                 for item in mensagem["itens"]]
        return {**mensagem, "itens": itens, "texto": mensagem_resultados(itens, pode_chamar=False)}
    return mensagem


def _tokens_originais(item: dict) -> int:
    truncado = item.get("truncado")
    return truncado["tokens"] if truncado else estimar_tokens(item["conteudo"])


def _limpar_resultados(turno: list[dict], excede: Callable[[], bool], protegidos: Collection[int] = (),
                       pode_ler: bool = True) -> None:
    """Limpa os resultados do turno, do mais antigo ao mais novo, até o pedido caber."""
    for i in range(len(turno)):
        if i in protegidos:
            continue
        for j, item in enumerate(_resultados(turno[i])):
            if not excede():
                return
            marca = marca_omitido(item["id"], pode_ler)
            # O que o usuário tirou já é uma marca, e esta diria ao modelo como ler o que ele não deve ler.
            if "excluido" in item:
                continue
            # Um resultado que pesa menos que a marca não libera nada; trocá-lo só contaria uma perda falsa.
            if "omitido" not in item and estimar_tokens(marca) < estimar_tokens(item["conteudo"]):
                turno[i] = _com_resultado(turno[i], j, marca, omitido=_tokens_originais(item))


def _pedido(turno: list[dict]) -> dict | None:
    """O pedido humano do turno; um turno pode não ter (histórico que não começa num pedido)."""
    return next((m for m in turno if m.get("papel") == "usuario" and not _eh_resultado(m)), None)


# Para o medidor e o erro do que não cabe, que precisam ver os turnos como a janela os vê.
resultados_de = _resultados
peso_mensagem = _peso
nome_de = _nome
pedido_de = _pedido


def _eh_do_resumo(turno: list[dict]) -> bool:
    return any(m.get("resumo") for m in turno)


def _ultima_rodada(turno: list[dict]) -> set[int]:
    """Índices dos resultados da rodada mais recente: o último bloco contínuo de resultados."""
    fim = next((i for i in reversed(range(len(turno))) if _eh_resultado(turno[i])), None)
    if fim is None:
        return set()
    inicio = fim
    while inicio > 0 and _eh_resultado(turno[inicio - 1]):
        inicio -= 1
    return set(range(inicio, fim + 1))


def _truncado(item: dict, teto: float, pode_ler: bool = True) -> tuple[str, dict] | None:
    """O conteúdo reduzido a `teto` tokens: começo, marca e fim. None se já couber."""
    conteudo = item["conteudo"]
    total = estimar_tokens(conteudo)
    if total <= teto:
        return None
    # A marca mais longa possível: o tamanho real depende de onde o corte cair.
    util = max(0, int(teto) - estimar_tokens(marca_truncado(item["id"], len(conteudo), total, total, pode_ler)))
    # Dois terços para o começo, que costuma trazer a estrutura; o resto para o fim, com o desfecho.
    inicio = caracteres_ate(conteudo, util * 2 // 3)
    fim = max(inicio, _inicio_da_cauda(conteudo, util - util * 2 // 3))
    omitidos = estimar_tokens(conteudo[inicio:fim])
    texto = f"{conteudo[:inicio]}\n{marca_truncado(item['id'], inicio, omitidos, total, pode_ler)}\n{conteudo[fim:]}"
    if estimar_tokens(texto) >= total:
        return None
    return texto, {"inicio": inicio, "fim": fim, "tamanho": len(conteudo), "tokens": total,
                   "recebidos": max(0, total - omitidos)}


def _com_teto(mensagem: dict, teto: float, limite: int = 0, pode_ler: bool = True) -> dict:
    """A mensagem com cada resultado limitado a `teto` tokens e ao `limite` da configuração.

    O limite não vale para a leitura: ali o modelo pediu a parte que faltava, e o tamanho já veio
    pela janela. O teto, sim, porque ele é o que de fato cabe.
    """
    saida = mensagem
    for j, item in enumerate(_resultados(mensagem)):
        teto_item = min(teto, limite) if limite and item.get("nome") != LER_TRECHO else teto
        reduzido = _truncado(item, teto_item, pode_ler)
        if reduzido is not None:
            pelo_limite = bool(limite) and teto_item == limite
            saida = _com_resultado(saida, j, reduzido[0], truncado={**reduzido[1], "pelo_limite": pelo_limite})
    return saida


def _teto_comum(pesos: list[int], disponivel: float) -> int:
    """O maior teto T em que a soma de min(peso, T) cabe em `disponivel`."""
    restante = disponivel
    ordenados = sorted(pesos)
    for i, peso in enumerate(ordenados):
        quota = restante / (len(ordenados) - i)
        if peso > quota:
            return int(quota)
        restante -= peso
    return ordenados[-1] if ordenados else 0


def _truncar_ultima_rodada(turno: list[dict], originais: list[dict], protegidos: Collection[int],
                           sobra: Callable[[], float], limite: int, pode_ler: bool = True) -> None:
    """Acha o maior teto comum com que a rodada mais recente cabe, e o aplica a todos dela.

    Teto comum, e não o mais velho primeiro: o que é pequeno fica inteiro e os grandes dividem o
    espaço, então o modelo vê um pouco de cada documento que pediu.
    """
    pesos = [estimar_tokens(item["conteudo"]) for i in protegidos for item in _resultados(turno[i])]
    teto = max(TRECHO_MINIMO, _teto_comum(pesos, sum(pesos) + sobra()))
    while True:
        for i in protegidos:
            turno[i] = _com_teto(originais[i], teto, limite, pode_ler)
        falta = -sobra()
        if falta <= 0 or teto <= TRECHO_MINIMO:
            return  # coube, ou nem o mínimo cabe e resta o aviso de excesso
        teto = max(TRECHO_MINIMO, teto - max(PASSO_TRUNCAMENTO, int(falta)))


def _nota(resultados: int, truncados: int, turnos: int, resposta: bool, anexos: bool, pode_consultar: bool = True) -> str:
    """O aviso ao modelo do que saiu; "" se nada saiu.

    O usuário lê os avisos, mas quem responde é o modelo: sem saber do corte, ele trata a falta
    como ausência e afirma que algo não existe. Foi o que aconteceu na sessão real.
    """
    primeiro = {(True, True): "a resposta e os anexos do primeiro pedido",
                (True, False): "a resposta do primeiro pedido",
                (False, True): "os anexos do primeiro pedido"}.get((resposta, anexos))
    partes = []
    if resultados:
        partes.append(f"{resultados} resultado(s) de ferramenta")
    if truncados:
        partes.append(f"parte de {truncados} resultado(s)")
    if turnos:
        partes.append(f"{turnos} turno(s) antigo(s)")
    if primeiro:
        partes.append(primeiro)
    if not partes:
        return ""
    lista = f"{', '.join(partes[:-1])} e {partes[-1]}" if len(partes) > 1 else partes[0]
    verbo = "foi omitida" if lista == "a resposta do primeiro pedido" else "foram omitidos"
    # A ferramenta só é exposta quando há marca na janela: a nota não pode citar o que o modelo não tem.
    if not pode_consultar:
        conselho = "Nesta resposta não há como consultar de novo: diga ao usuário o que ficou de fora"
    elif resultados or truncados:
        conselho = f"Leia o que precisar com {LER_TRECHO}, pelo id da marca, ou consulte de novo com as ferramentas"
    else:
        conselho = "Se precisar disso, consulte de novo com as ferramentas"
    return (f"[Nota do servidor: para caber na janela, {lista} {verbo}. {conselho}; não afirme que algo não "
            "existe ou não foi encontrado só porque não está visível aqui, e não conclua sobre um documento "
            "com parte omitida sem lê-la.]")


# O maior tamanho que a nota pode ter. Reservado assim que algo precisa sair, porque o tamanho
# real só se sabe no fim: sem a reserva, o próprio aviso poderia estourar a janela.
PESO_NOTA = estimar_tokens("\n\n" + _nota(9999, 9999, 9999, True, True))


def _aviso_primeiro(resposta: bool, anexos: bool) -> str:
    if resposta and anexos:
        return "Os anexos e a resposta do primeiro pedido saíram da janela (o texto do pedido foi mantido)."
    if anexos:
        return "Os anexos do primeiro pedido saíram da janela (o texto do pedido foi mantido)."
    return "A resposta do primeiro pedido saiu da janela (o texto do pedido foi mantido)."


def _peso_sem_resultados(mensagem: dict, pesos: _Pesos) -> int:
    """O que a mensagem pesa se todo resultado dela virar marca."""
    if not _eh_resultado(mensagem):
        return pesos.de(mensagem)
    itens = _resultados(mensagem)
    if mensagem.get("papel") == "ferramenta":
        return sum(estimar_tokens(marca_omitido(item["id"])) for item in itens)
    # Nas cercas conta o que vai ao modelo: a moldura e cada marca escapada dentro do JSON.
    return estimar_tokens(mensagem_resultados([{**item, "conteudo": marca_omitido(item["id"])} for item in itens]))


def ajustar(
    sistema_fixo: str,
    sistema_duo: str,
    turnos: list[list[dict]],
    contexto_tokens: int,
    fixar_primeiro: bool,
    peso_ferramentas: int = 0,
    fator_tokens: float = 1.0,
    limite_resultado: int = 0,
    com_leitura: ComLeitura | None = None,
    sem_ferramentas: bool = False,
) -> ResultadoJanela:
    """Monta as mensagens a enviar dentro do orçamento da janela, com avisos do que se perdeu.

    O corte vai por idade e não guarda estado. Enquanto o pedido não couber, e cada passo só até
    onde precisar, percorre os turnos anteriores do mais antigo ao mais novo e, em cada um:

    a. limpa os resultados de ferramenta, do mais antigo ao mais novo. É o que mais pesa e o que o
       modelo recupera pela marca; o texto do assistente fica, porque é o resumo do que já se
       descobriu;
    b. se ainda não couber, tira o texto do turno. O 1º turno, com `fixar_primeiro`, fica reduzido ao
       1º pedido, sem os anexos, porque o que se fixa é o texto que traz a tarefa, e não o turno
       inteiro; os demais saem inteiros.

    Só então passa ao turno seguinte. O resumo da compactação, que é o mais velho, fica para o fim dos
    anteriores: quem compactou quer manter aquilo, e é na janela apertada que se compacta. No turno atual, limpa os
    resultados mais velhos: o pedido humano
    e os resultados da rodada mais recente nunca saem, porque sem eles o modelo repetiria a chamada.
    Por último, trunca os resultados da rodada mais recente, que são os únicos ainda inteiros: é o
    único lugar onde um resultado é truncado, e só depois de todos os outros cortes. Se nem assim
    couber, o resultado sai com `excede`, e quem decide o que fazer é o turno: o que a janela não faz é
    tirar a mensagem atual, que é o que o usuário pediu.

    A garantia é esta: a conversa só cresce, a ordem do corte só acrescenta itens no fim (a rodada
    protegida agora entra na fila, depois de tudo, quando deixa de ser a mais recente) e o peso
    reservado no corte é sempre o mesmo. Por isso, com o mesmo `fator_tokens` e o mesmo sistema e
    catálogo, o que saiu não volta, e nada sai antes de tudo o que é mais velho que ele. Sem o corte
    por idade, a rodada que deixava de ser protegida era limpa antes de o turno mais velho sair, e aí
    o turno velho voltava à janela no lugar dos resultados mais novos. As exceções são benignas: um
    resultado truncado pode voltar inteiro se sair um mais velho que lhe tomava o espaço, e o resumo que
    saiu pode voltar quando sai o último turno, que na conta seguinte sai antes dele.

    `peso_ferramentas` é o peso, em tokens, das definições de ferramentas que vão fora do texto
    (o campo `tools` do modo nativo). O motor as escreve no prompt, então elas ocupam a janela
    como o sistema: um peso fixo, que não se corta.

    `fator_tokens` corrige as estimativas pelo que o motor mediu (ver `fator_real`). O teto das
    instruções do Duo fica fora da correção: é uma regra de divisão da janela, e resumi-las só
    porque a medição subiu mudaria as regras do modelo no meio do turno.

    `limite_resultado`, se positivo, é um teto em tokens por resultado, escolhido na configuração:
    vale mesmo com a janela sobrando.

    `com_leitura` é como ficam o sistema e o peso das definições com a ferramenta de leitura, usados só
    se houver marca na janela. O peso dela é reservado assim que algo precisa sair, haja marca ou não,
    como o da nota. Se a reserva dependesse de haver marca, o peso sumiria da conta no envio em que
    o único turno com marca saísse, e o que tinha saído por causa dele voltaria à janela.

    `sem_ferramentas` é a rodada final do turno: a nota, as marcas, a mensagem de resultados e o cabeçalho das leituras
    não mandam consultar nada, porque o modelo não tem como.
    """
    avisos: list[str] = []
    orcamento = contexto_tokens - reserva_resposta(contexto_tokens)

    peso_duo = estimar_tokens(sistema_duo)
    if peso_duo > FRACAO_SISTEMA_DUO * orcamento:
        # O corte é em caracteres e o teto em tokens: a proporção leva em conta o que há no texto.
        sistema_duo = truncar_meio(sistema_duo, int(len(sistema_duo) * FRACAO_SISTEMA_DUO * orcamento / peso_duo))
        avisos.append("As instruções do DUO foram resumidas para caber na janela.")
    sufixo_duo = "\n\n" + sistema_duo if sistema_duo else ""
    sistema = sistema_fixo + sufixo_duo
    peso_fixo = estimar_tokens(sistema) + peso_ferramentas
    # O que pesa assim que algo precisa sair: tudo o que o corte pode fazer aparecer.
    peso_no_corte = peso_fixo
    if com_leitura is not None:
        peso_no_corte = max(peso_fixo, estimar_tokens(com_leitura.sistema_fixo + sufixo_duo)
                            + com_leitura.peso_ferramentas)
    peso_no_corte += PESO_NOTA

    pode_ler = not sem_ferramentas
    if limite_resultado > 0:
        mantidos = [[_com_teto(m, math.inf, limite_resultado, pode_ler) for m in t] for t in turnos]
    else:
        mantidos = [list(t) for t in turnos]
    removidos = 0
    resposta_primeiro = anexos_primeiro = resumo_fora = False
    pesos = _Pesos()

    def sobra() -> float:
        return orcamento / fator_tokens - (peso_no_corte + pesos.dos(mantidos))

    def excede() -> bool:
        return sobra() < 0

    encurtou = any("truncado" in item for t in mantidos for m in t for item in _resultados(m))
    if encurtou or (peso_fixo + pesos.dos(mantidos)) * fator_tokens > orcamento:
        # Com um só turno, ele é o atual: só o último recurso o alcança.
        for i in sorted(range(len(mantidos) - 1), key=lambda i: _eh_do_resumo(mantidos[i])):
            _limpar_resultados(mantidos[i], excede, pode_ler=pode_ler)
            if not excede():
                break
            pedido = _pedido(mantidos[i]) if i == 0 and fixar_primeiro else None
            if pedido is None:
                if _eh_do_resumo(mantidos[i]):
                    resumo_fora = True
                else:
                    removidos += 1
                mantidos[i] = []
            else:
                reduzido = dict(pedido)
                reduzido["texto"] = pedido.get("texto_sem_anexos", pedido["texto"])
                anexos_primeiro = reduzido["texto"] != pedido["texto"]
                resposta_primeiro = len(mantidos[i]) > 1
                mantidos[i] = [reduzido]

        if mantidos and excede():
            protegidos = _ultima_rodada(mantidos[-1])
            _limpar_resultados(mantidos[-1], excede, protegidos, pode_ler)
            if excede():
                _truncar_ultima_rodada(mantidos[-1], turnos[-1], protegidos, sobra, limite_resultado, pode_ler)

    # Só os limpos ainda visíveis: o que saiu com um turno inteiro já está na conta do turno.
    limpos: list[dict] = []
    n_truncados = 0
    truncados_atuais: list[Truncado] = []
    truncados_antigos: list[dict] = []
    vistos: dict[str, list[tuple[int, int]]] = {}
    for posicao, turno in enumerate(mantidos):
        for mensagem in turno:
            for item in _resultados(mensagem):
                if "excluido" in item:
                    continue  # não é perda: foi escolha do usuário, e ele sabe
                if "omitido" in item:
                    limpos.append(item)
                    vistos[item["id"]] = []
                elif "truncado" in item:
                    n_truncados += 1
                    corte = item["truncado"]
                    vistos[item["id"]] = [(0, corte["inicio"]), (corte["fim"], corte["tamanho"])]
                    if item.get("interno"):
                        pass  # o usuário nem sabe dessas chamadas; o que importa a ele é o resultado original
                    elif posicao == len(mantidos) - 1:
                        truncados_atuais.append(Truncado(item["id"], _nome(item), corte["recebidos"], corte["tokens"],
                                                         corte["pelo_limite"]))
                    else:
                        truncados_antigos.append(item)
                else:
                    vistos[item["id"]] = [(0, len(item["conteudo"]))]

    if sem_ferramentas:
        # Por último, para valer sobre as marcas e o que `vistos` já mediu: as faixas são do resultado como foi guardado.
        mantidos = [[_para_a_rodada_final(m) for m in t] for t in mantidos]
    if resumo_fora:
        avisos.append(f"O resumo da conversa ficou fora da janela de contexto ({contexto_tokens} tokens).")
    if removidos:
        avisos.append(f"{removidos} turno(s) antigo(s) ficaram fora da janela de contexto ({contexto_tokens} tokens).")
    limpos_do_usuario = [item for item in limpos if not item.get("interno")]
    if limpos_do_usuario:
        nomes = ", ".join(dict.fromkeys(_nome(item) for item in limpos_do_usuario))
        tokens = sum(item["omitido"] for item in limpos_do_usuario)
        avisos.append(f"{len(limpos_do_usuario)} resultado(s) antigo(s) de ferramenta ({nomes}; {tokens} tokens) "
                      f"ficaram fora da janela de contexto ({contexto_tokens} tokens).")
    if truncados_antigos:
        nomes = ", ".join(dict.fromkeys(_nome(item) for item in truncados_antigos))
        recebidos = sum(item["truncado"]["recebidos"] for item in truncados_antigos)
        tokens = sum(item["truncado"]["tokens"] for item in truncados_antigos)
        resto = f" e pode ler o resto com {LER_TRECHO}" if pode_ler else ""
        avisos.append(f"{len(truncados_antigos)} resultado(s) antigo(s) de ferramenta ({nomes}): pelo limite_resultado, "
                      f"o modelo recebeu {recebidos} de {tokens} tokens{resto}.")
    if resposta_primeiro or anexos_primeiro:
        avisos.append(_aviso_primeiro(resposta_primeiro, anexos_primeiro))
    avisos.extend(t.aviso for t in truncados_atuais)

    leitura = bool(limpos) or n_truncados > 0
    if leitura and com_leitura is not None:
        sistema, peso_ferramentas = com_leitura.sistema_fixo + sufixo_duo, com_leitura.peso_ferramentas
    nota = _nota(len(limpos), n_truncados, removidos + resumo_fora, resposta_primeiro, anexos_primeiro,
                 not sem_ferramentas)
    com_nota = "\n\n".join(parte for parte in (sistema, nota) if parte)
    mensagens = [{"papel": "sistema", "texto": com_nota}] + [m for t in mantidos for m in t]
    estimativa = estimar_tokens(com_nota) + peso_ferramentas + pesos.dos(mantidos)
    # Com todos os resultados virando marca e o texto das conversas ficando: reler não precisa
    # empurrar para fora o que o modelo escreveu.
    piso = peso_no_corte + FOLGA_LEITURA + sum(_peso_sem_resultados(m, pesos) for t in mantidos for m in t)
    return ResultadoJanela(mensagens=mensagens, avisos=avisos, estimativa=estimativa,
                           leitura=leitura, vistos=vistos, truncados=truncados_atuais,
                           espaco_resultado=max(TRECHO_MINIMO, int(orcamento / fator_tokens) - piso),
                           turnos=mantidos, peso_sistema=estimar_tokens(com_nota), peso_ferramentas=peso_ferramentas,
                           orcamento=orcamento, fator=fator_tokens, cortou=bool(nota))
