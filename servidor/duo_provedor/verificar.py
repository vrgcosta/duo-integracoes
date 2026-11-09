"""Diagnóstico `servidor.py --verificar`: confere o ambiente antes de o usuário abrir o Duo.

Cada linha tem um nível (`ok`, `aviso`, `erro`) e, quando há problema, diz o que fazer. Só `erro`
impede o uso (o comando sai com 1); `aviso` é algo que o usuário deveria saber, mas que não trava.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .backends.base import Backend, ErroBackend, InfoModelo
from .blueprint import eh_loopback, mensagem_nuvem_bloqueada, nuvem_bloqueada
from .hardware import PERFIS, Hardware


# A versão em que o `truncate: false` foi medido; ver `BackendOllama.contar_tokens`.
VERSAO_MEDIDA = "0.30.10"


@dataclass
class Linha:
    nivel: str  # "ok" | "aviso" | "erro"
    texto: str


def _gb(valor: float | None) -> str:
    return "não detectada" if valor is None else f"{valor:g} GB"


def verificar(cfg: dict, backend: Backend, hw: Hardware) -> list[Linha]:
    modelo = cfg["modelo_padrao"]
    linhas = [Linha("ok", f"Máquina: RAM {_gb(hw.ram_gb)}, VRAM {_gb(hw.vram_gb)}"
                          f"{f' ({hw.gpu})' if hw.gpu else ''}; perfil escolhido: {cfg['perfil']}")]

    try:
        modelos = backend.listar_modelos()
    except ErroBackend as erro:
        # Sem backend não há o que dizer do modelo, do contexto ou das capacidades.
        linhas.append(Linha("erro", str(erro)))
        return linhas + _destino(cfg)
    linhas.append(Linha("ok", f"Backend {backend.nome} respondeu ({len(modelos)} modelo(s) disponível(is))"))
    linhas += _versao(backend)

    info = next((m for m in modelos if m.id == modelo), None)
    # A lista esconde os modelos de nuvem e os que não conversam: fora dela, pergunta direto ao
    # backend, para não mandar instalar um modelo que existe mas está bloqueado ou não serve.
    direta = info or _info_direta(backend, modelo)
    if nuvem_bloqueada(cfg, direta):
        linhas.append(Linha("erro", mensagem_nuvem_bloqueada(modelo)))
    elif direta is not None and not direta.conversa:
        linhas.append(Linha("erro", f"O modelo padrão {modelo} está instalado, mas não conversa (é de embedding, "
                                    "por exemplo). Troque com --modelo"))
    elif info is None:
        dica = (f"Rode: ollama pull {modelo}" if cfg.get("backend") == "ollama"
                else "Carregue-o no servidor ou troque com --modelo")
        linhas.append(Linha("erro", f"O modelo padrão {modelo} não está instalado. {dica}"))
    else:
        if info.nuvem:
            linhas.append(Linha("aviso", f"O modelo padrão {modelo} é de nuvem: o conteúdo dos autos "
                                         "sai desta máquina"))
        linhas.append(Linha("ok", f"Modelo padrão {modelo} instalado"))
        linhas.append(Linha("ok", "Ferramentas: o modelo as aceita nativamente") if info.ferramentas else
                      Linha("aviso", f"{modelo} não declara suporte a ferramentas: usará emulação por cercas "
                                     "(menos confiável que o modo nativo)"))
        linhas.append(Linha("ok", "Raciocínio: disponível (botão de pensamento do Duo)") if info.raciocinio else
                      Linha("aviso", f"{modelo} não raciocina: o botão de pensamento não terá efeito"))
        linhas += _contexto(cfg, backend, modelo, info.contexto_max)
        linhas += _memoria(cfg, backend, modelo)
    return linhas + _destino(cfg)


def _numeros_da_versao(versao: str) -> tuple[int, ...] | None:
    numeros = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+).*", versao)
    return tuple(map(int, numeros.groups())) if numeros else None


def _versao(backend: Backend) -> list[Linha]:
    """A versão do motor. Não diz em qual o campo surgiu, porque isso não foi conferido: só em qual foi medido."""
    versao = backend.versao_motor()
    if versao is None:
        return []
    numeros = _numeros_da_versao(versao)
    if numeros is not None and numeros < _numeros_da_versao(VERSAO_MEDIDA):
        return [Linha("aviso", f"Ollama, versão {versao}: a contagem de tokens foi medida na {VERSAO_MEDIDA}. Numa anterior, "
                               "confira se ele recusa o pedido acima da janela com `truncate: false`. Se ignorar o "
                               "campo, ele corta o pedido sem avisar. Na dúvida, atualize")]
    return [Linha("ok", f"Motor na versão {versao}")]


def _info_direta(backend: Backend, modelo: str) -> InfoModelo | None:
    try:
        return backend.info_modelo(modelo)
    except ErroBackend:
        return None


def _contexto(cfg: dict, backend: Backend, modelo: str, maximo: int | None) -> list[Linha]:
    pedido = cfg["contexto_tokens"]
    linhas = []
    if maximo is not None and maximo < pedido:
        linhas.append(Linha("aviso", f"O contexto configurado ({pedido} tokens) passa do máximo do modelo "
                                     f"({maximo}); reduza contexto_tokens ou a opção no Duo"))
    try:
        em_uso = backend.contexto_em_uso(modelo)
    except ErroBackend:
        em_uso = None  # dado opcional: o modelo pode nem estar carregado ainda
    if em_uso is not None:
        # O Ollama recarrega o modelo quando o num_ctx pedido difere do carregado; por isso o aviso
        # de "carregado agora" não é um problema, só informação.
        linhas.append(Linha("ok", f"Contexto carregado agora: {em_uso} tokens; o servidor pede "
                                  f"num_ctx={pedido} a cada requisição"
                                  f"{'' if em_uso == pedido else ' (o Ollama recarrega o modelo com esse valor)'}"))
    return linhas


def _gb_bytes(valor: int) -> str:
    return f"{valor / 10**9:.1f}".replace(".", ",")


def _memoria(cfg: dict, backend: Backend, modelo: str) -> list[Linha]:
    """Avisa quando o modelo padrão, já carregado, ficou em parte no processador.

    É o caso do qwen3.5:9b numa placa de 8 GB com a área de trabalho e o navegador ocupando parte
    dela: o Ollama põe na CPU as camadas que não couberam, e cada token passa a esperar por elas.
    Só dá para saber com o modelo carregado (depois de uma conversa); antes disso, nada a dizer.
    """
    try:
        carga = backend.memoria_em_uso(modelo)
    except ErroBackend:
        carga = None
    if carga is None:
        return []
    total, vram = carga
    # Sem nada na VRAM, no perfil cpu, o processador é o esperado (a máquina não tem GPU útil).
    if vram >= total or (vram <= 0 and cfg.get("perfil") != "gpu"):
        return []
    menor = PERFIS["cpu"]["modelo_padrao"]
    dica = (f"use um modelo menor: --modelo {menor}" if modelo != menor else
            "reduza contexto_tokens em dados/config.json (ou a janela de contexto no Duo)")
    return [Linha("aviso", f"{modelo} está com {1 - vram / total:.0%} no processador ({_gb_bytes(vram)} "
                           f"de {_gb_bytes(total)} GB na VRAM): as respostas ficam mais lentas. Feche "
                           f"programas que usam a placa de vídeo ou {dica}")]


def _destino(cfg: dict) -> list[Linha]:
    linhas = []
    if not eh_loopback(cfg.get("url", "")):
        linhas.append(Linha("aviso", f"O backend em {cfg['url']} não é local: o conteúdo dos autos "
                                     "sai desta máquina"))
    if cfg.get("permitir_modelos_nuvem"):
        linhas.append(Linha("aviso", "permitir_modelos_nuvem está ligado: modelos de nuvem processam "
                                     "o conteúdo fora da sua máquina"))
    return linhas or [Linha("ok", "Destino: tudo local")]
