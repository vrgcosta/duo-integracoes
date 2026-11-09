"""Configuração persistente em dados/config.json.

A chave de acesso é gerada na primeira execução e fica no arquivo: é ela que a
extensão envia no cabeçalho para provar que quem chama é o navegador do usuário.
"""

import json
import os
import secrets
import time

from .hardware import Hardware, PERFIS, detectar, escolher_perfil

PORTA_PADRAO = 8766
CABECALHO_CHAVE = "x-duo-provedor-chave"
URL_PADRAO = {"ollama": "http://127.0.0.1:11434", "openai": "http://127.0.0.1:1234/v1"}

# No Windows, trocar um arquivo que outro processo mantém aberto (antivírus, indexador
# de busca, um editor) falha com PermissionError [WinError 5]. Quase sempre o arquivo é solto em
# milissegundos, então vale tentar de novo algumas vezes antes de desistir.
TENTATIVAS = 5
ESPERA_ENTRE_TENTATIVAS = 0.05  # segundos


class ErroConfig(Exception):
    """Configuração ilegível; a mensagem diz qual arquivo e o que fazer."""


def _com_tentativas(operacao, *args) -> None:
    for tentativa in range(1, TENTATIVAS + 1):
        try:
            operacao(*args)
            return
        except PermissionError:
            # Na última tentativa o erro sobe: quem chamou decide como avisar o usuário.
            if tentativa == TENTATIVAS:
                raise
            time.sleep(ESPERA_ENTRE_TENTATIVAS)


def gravar_json(caminho: str, dados: object) -> None:
    # Escreve num temporário e troca: queda no meio da gravação não deixa um
    # JSON pela metade no lugar do arquivo bom.
    temporario = caminho + ".tmp"
    with open(temporario, "w", encoding="utf-8") as f:
        json.dump(dados, f, ensure_ascii=False, indent=2)
    _com_tentativas(os.replace, temporario, caminho)


def _caminho_config(pasta_dados: str) -> str:
    return os.path.join(pasta_dados, "config.json")


def carregar_config(pasta_dados: str, hw: Hardware | None = None) -> dict:
    os.makedirs(pasta_dados, exist_ok=True)
    caminho = _caminho_config(pasta_dados)
    cfg: dict = {}
    if os.path.exists(caminho):
        try:
            with open(caminho, encoding="utf-8") as f:
                cfg = json.load(f)
        except (json.JSONDecodeError, UnicodeDecodeError) as erro:
            raise ErroConfig(f"Não foi possível ler {caminho} ({erro}). Apague ou corrija o arquivo.") from erro
        if not isinstance(cfg, dict):
            raise ErroConfig(f"{caminho} deve conter um objeto JSON. Apague ou corrija o arquivo.")
    original = json.loads(json.dumps(cfg))

    # Só mede o hardware se o perfil ainda não foi decidido: o usuário pode ter
    # fixado o perfil à mão e a detecção chama processos externos.
    if "perfil" not in cfg:
        cfg["perfil"] = escolher_perfil(hw or detectar())
    if cfg["perfil"] not in PERFIS:
        raise ErroConfig(
            f"Perfil inválido {cfg['perfil']!r} em {caminho}. Use um destes: {', '.join(PERFIS)}."
        )
    perfil = PERFIS[cfg["perfil"]]
    cfg.setdefault("backend", "ollama")
    padroes = {
        "chave": secrets.token_urlsafe(24),
        "porta": PORTA_PADRAO,
        "url": URL_PADRAO.get(cfg["backend"], URL_PADRAO["ollama"]),
        "modelo_padrao": perfil["modelo_padrao"],
        "contexto_tokens": perfil["contexto_tokens"],
        "espera_primeiro_token": perfil["espera_primeiro_token"],
        "temperatura": 0.3,
        "ferramentas": "auto",
        "max_rodadas": 15,
        # Teto em tokens por resultado de ferramenta; 0 = sem teto: a janela só trunca o que não
        # couber mesmo depois de todos os outros cortes.
        "limite_resultado": 0,
        "permitir_modelos_nuvem": False,
        "ferramentas_por_modelo": {},
        "ferramentas_ativas": [],
        "chave_backend": "",
    }
    for nome, valor in padroes.items():
        cfg.setdefault(nome, valor)

    if cfg != original:
        gravar_json(caminho, cfg)
    return cfg


def salvar_config(pasta_dados: str, cfg: dict) -> None:
    os.makedirs(pasta_dados, exist_ok=True)
    gravar_json(_caminho_config(pasta_dados), cfg)
