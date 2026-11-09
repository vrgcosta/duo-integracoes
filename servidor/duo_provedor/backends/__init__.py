"""Fábrica de backends."""

from __future__ import annotations

from .base import Backend, ErroBackend


def criar_backend(cfg: dict, simulado: bool) -> Backend:
    # Os backends concretos são importados aqui, e não no topo do pacote: `ferramentas` importa
    # `backends.base`, e os backends importam `ferramentas`. No topo, importar `ferramentas`
    # primeiro carregaria este pacote no meio e fecharia o ciclo.
    from .ollama import BackendOllama
    from .openai import BackendOpenAI
    from .simulado import BackendSimulado

    if simulado:
        return BackendSimulado()
    nome = cfg.get("backend")
    if nome == "ollama":
        return BackendOllama(cfg["url"], permitir_nuvem=bool(cfg.get("permitir_modelos_nuvem")))
    if nome == "openai":
        return BackendOpenAI(cfg["url"], chave=cfg.get("chave_backend") or "",
                             permitir_nuvem=bool(cfg.get("permitir_modelos_nuvem")))
    raise ErroBackend(f"backend desconhecido: {nome!r} (use ollama ou openai)")
