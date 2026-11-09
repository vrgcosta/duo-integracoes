"""Geração do blueprint que o usuário importa no Duo, e as regras de destino (local ou não).

O molde (blueprint/modelo.json) é versionado e não tem segredo. O arquivo gerado
leva a chave de acesso e por isso só existe em dados/ (gitignored).
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
from urllib.parse import urlsplit

from .backends.base import InfoModelo
from .config import gravar_json

NOME_ARQUIVO = "duo-provedor-local.blueprint.json"

_RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_MOLDE = os.path.join(_RAIZ, "blueprint", "modelo.json")

# Só nomes MAIÚSCULOS são do servidor; as variáveis do Duo ({convId}, {prompt}...)
# têm minúsculas e precisam chegar intactas ao motor.
_MARCADOR = re.compile(r"\{([A-Z][A-Z_]*)\}")


def eh_loopback(url: str) -> bool:
    try:
        host = urlsplit(url).hostname
    except ValueError:
        return False
    if not host:
        return False
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        # Um nome qualquer (ex.: localhost.exemplo.com) pode resolver para fora.
        return False


def destino_local(cfg: dict, simulado: bool = False) -> bool:
    """True quando o texto da conversa não sai da máquina do usuário."""
    if simulado:
        return True
    return eh_loopback(cfg.get("url", "")) and not cfg.get("permitir_modelos_nuvem", False)


def nuvem_bloqueada(cfg: dict, info: InfoModelo | None) -> bool:
    """O modelo roda fora da máquina e a configuração não liberou a nuvem?

    É o que sustenta o `destinoLocal: true`: esconder o modelo da lista não basta, porque o Duo
    pode pedir um id guardado ou o modelo padrão pode ser de nuvem. Por isso o turno também recusa.
    """
    return bool(info and info.nuvem and not cfg.get("permitir_modelos_nuvem", False))


def mensagem_nuvem_bloqueada(modelo: str) -> str:
    return (f"O modelo {modelo} é de nuvem: com ele, o conteúdo da conversa (inclusive os autos) sairia "
            "desta máquina. Por isso está bloqueado (permitir_modelos_nuvem desligado em "
            "dados/config.json). Use um modelo local ou, para liberar a nuvem, ligue essa opção, "
            "reinicie o servidor e reimporte no DUO o blueprint regenerado.")


def ler_blueprint(caminho: str) -> dict | None:
    """O blueprint gerado antes, se existir e for legível (para saber se o Duo precisa reimportar)."""
    try:
        with open(caminho, encoding="utf-8") as f:
            dados = json.load(f)
    except (OSError, ValueError):
        return None
    return dados if isinstance(dados, dict) else None


def preencher(valor: object, variaveis: dict) -> object:
    if isinstance(valor, str):
        exato = _MARCADOR.fullmatch(valor)
        if exato and exato.group(1) in variaveis:
            # Marcador sozinho preserva o tipo (porta int, destinoLocal bool).
            return variaveis[exato.group(1)]
        return _MARCADOR.sub(
            lambda m: str(variaveis[m.group(1)]) if m.group(1) in variaveis else m.group(0), valor
        )
    if isinstance(valor, list):
        return [preencher(item, variaveis) for item in valor]
    if isinstance(valor, dict):
        return {chave: preencher(item, variaveis) for chave, item in valor.items()}
    return valor


def gerar_blueprint(cfg: dict, destino: str, simulado: bool = False) -> dict:
    with open(_MOLDE, encoding="utf-8") as f:
        molde = json.load(f)
    variaveis = {
        "PORTA": int(cfg["porta"]),
        "CHAVE": cfg["chave"],
        "DESTINO_LOCAL": destino_local(cfg, simulado),
    }
    pronto = preencher(molde, variaveis)
    os.makedirs(os.path.dirname(os.path.abspath(destino)), exist_ok=True)
    gravar_json(destino, pronto)
    return pronto  # type: ignore[return-value]
