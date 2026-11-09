"""Abertura cancelável de uma resposta HTTP em streaming, compartilhada pelos backends.

Motores locais podem passar minutos carregando o modelo e fazendo o prefill antes do
primeiro byte. Esperar em `getresponse()` deixaria o "parar" do Duo preso nesse intervalo,
então a espera é feita com `select` em fatias curtas, só com API pública.
"""

from __future__ import annotations

import http.client
import select
import threading
from typing import Callable

from .base import ErroBackend

# Fatia de espera do select antes da resposta; limita a latência do cancelamento.
PERIODO_ESPERA = 0.05


def abrir_resposta(conexao: http.client.HTTPConnection, caminho: str, corpo: bytes,
                   cabecalhos: dict, cancelado: threading.Event,
                   erro_conexao: Callable[[], ErroBackend]) -> http.client.HTTPResponse | None:
    """Conecta, envia o POST e espera os cabeçalhos; devolve None se cancelado antes.

    Falhas de rede viram o `ErroBackend` montado por `erro_conexao` (mensagem do backend).
    """
    try:
        conexao.connect()
    except OSError as e:
        raise erro_conexao() from e
    if cancelado.is_set():
        return None
    soquete = conexao.sock
    # Depois de conectar, a leitura não tem prazo: o primeiro byte só chega após o prefill.
    soquete.settimeout(None)
    try:
        conexao.request("POST", caminho, body=corpo, headers=cabecalhos)
        while not select.select([soquete], [], [], PERIODO_ESPERA)[0]:
            if cancelado.is_set():
                return None
        return conexao.getresponse()
    except (OSError, http.client.HTTPException) as e:
        raise erro_conexao() from e
