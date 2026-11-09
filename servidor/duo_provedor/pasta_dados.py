"""Onde fica `dados/`: avisa quando a pasta leva o histórico para fora da máquina sem ninguém notar.

O histórico (texto dos autos e anexos) mora em `dados/`, ao lado do `servidor.py`. Quem descompacta
o servidor na Área de Trabalho ou em Documentos costuma, no Windows, estar dentro do OneDrive (o
backup dessas pastas vem ligado em muitas máquinas), e o aplicativo do OneDrive envia o banco para a
nuvem a cada mudança. Numa pasta de rede, o banco passa pela rede e fica no servidor de arquivos; e
o modo WAL do SQLite nem funciona direito ali, porque depende de memória compartilhada entre os
processos que abrem o banco, coisa que uma pasta de rede não oferece.

A detecção cobre o que o Windows deixa ver sem dependências: as variáveis de ambiente que o próprio
OneDrive cria, o caminho UNC (`\\\\servidor\\pasta`) e a unidade mapeada para a rede. Dropbox e
Google Drive não deixam um rastro assim; o README avisa deles. Tudo aqui é melhor esforço: errar
para menos só tira um aviso, e nada impede o servidor de subir.
"""

from __future__ import annotations

import ntpath
import os
from typing import Callable, Mapping

# As três que o OneDrive cria: a conta em uso, a corporativa (OneDrive for Business) e a pessoal.
VARIAVEIS_ONEDRIVE = ("OneDrive", "OneDriveCommercial", "OneDriveConsumer")
_UNIDADE_DE_REDE = 4  # DRIVE_REMOTE, o que GetDriveTypeW devolve para uma unidade mapeada


def _tipo_unidade_windows(raiz: str) -> int | None:
    import ctypes  # só no Windows chega aqui

    return ctypes.windll.kernel32.GetDriveTypeW(raiz)


def _normalizar(caminho: str) -> str:
    # Sem acessar o disco: no Windows, caixa e "/" ou "\" não distinguem caminhos.
    return ntpath.normcase(ntpath.normpath(caminho))


def pasta_que_sai_da_maquina(caminho: str, ambiente: Mapping[str, str] | None = None,
                             windows: bool | None = None,
                             tipo_unidade: Callable[[str], int | None] | None = None) -> str | None:
    """"OneDrive" ou "rede" quando `caminho` está num desses lugares; None num disco local comum.

    `caminho` deve vir resolvido (`os.path.realpath`): uma junção dentro de C:\\ que aponta para o
    OneDrive só se revela assim. `ambiente`, `windows` e `tipo_unidade` existem para os testes.
    """
    windows = os.name == "nt" if windows is None else windows
    if not windows:
        return None
    ambiente = os.environ if ambiente is None else ambiente
    alvo = _normalizar(caminho)
    # "\\?\C:\..." é a forma longa de um caminho local; "\\?\UNC\srv\pasta" é a de "\\srv\pasta".
    if alvo.startswith(("\\\\?\\", "\\\\.\\")):
        alvo = alvo[4:]
        if alvo.startswith("unc\\"):
            alvo = "\\\\" + alvo[4:]
    for nome in VARIAVEIS_ONEDRIVE:
        base = ambiente.get(nome)
        if not base:
            continue  # variável vazia casaria com qualquer caminho
        base = _normalizar(base).rstrip("\\")
        # O separador no fim impede que "OneDrive-backup" passe por "OneDrive".
        if alvo == base or alvo.startswith(base + "\\"):
            return "OneDrive"
    if alvo.startswith("\\\\"):
        return "rede"
    unidade, _ = ntpath.splitdrive(alvo)
    if len(unidade) == 2 and unidade[1] == ":":
        try:
            tipo = (tipo_unidade or _tipo_unidade_windows)(unidade + "\\")
        except (OSError, AttributeError):  # AttributeError: ctypes sem windll
            tipo = None
        if tipo == _UNIDADE_DE_REDE:
            return "rede"
    return None


def mensagem_pasta(caminho: str, onde: str) -> tuple[str, str]:
    """O aviso para o usuário: (o risco, começando em minúscula, e o que fazer).

    Em duas partes porque o banner põe cada uma numa linha, depois de "ATENÇÃO:", e o --verificar
    junta as duas numa linha só.
    """
    if onde == "OneDrive":
        risco = (f"a pasta de dados ({caminho}) está dentro do OneDrive, que envia o histórico, com o "
                 "texto dos autos e os anexos, para a nuvem.")
    else:
        risco = (f"a pasta de dados ({caminho}) está numa pasta de rede: o histórico, com o texto dos "
                 "autos e os anexos, fica no servidor de arquivos, e o SQLite não funciona direito ali.")
    return risco, "Mova a pasta do servidor para um disco desta máquina (por exemplo, C:\\duo-provider-server)."
