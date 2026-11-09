"""Backend para servidores compatíveis com a API da OpenAI (`/v1/chat/completions`, SSE).

Serve Ollama (`/v1`), LM Studio, llama.cpp, vLLM e a própria OpenAI. Conferido contra o
Ollama 0.30.10 (qwen3:0.6b): o raciocínio vem em `delta.reasoning` (DeepSeek e vLLM usam
`reasoning_content`; aceitamos os dois); cada `tool_calls[]` chega inteiro, com `id` e
`index`, e os argumentos já como string JSON; o `usage` vem num chunk final com `choices`
vazio, pedido por `stream_options.include_usage`.

A API não tem campo para o tamanho de contexto (`contexto_tokens` do pedido é ignorado):
quem define a janela é o servidor. O README explica como configurá-la em cada um. Também não tem rota
de contagem de tokens, e gerar um token para ler o `usage` custaria dinheiro num serviço pago: o
`contar_tokens` fica com o None da classe base.

Também não tem campo que diga onde o modelo roda. A única pista é a convenção do Ollama, que
serve os modelos de nuvem pela mesma API local com o sufixo `-cloud` ou `:cloud` no nome
(`gpt-oss:120b-cloud`, `glm-4.6:cloud`). Esses nomes são tratados como nuvem em qualquer
servidor, por precaução; num servidor que repassa a outro lugar sem essa pista, não há como saber.
"""

from __future__ import annotations

import http.client
import json
import threading
from typing import Iterator
from urllib.parse import urlsplit

from ..ferramentas import AcumuladorChamadas, para_funcoes
from .base import (
    Backend, Cortada, ErroBackend, Evento, InfoModelo, Pedido, Raciocinio, Texto, Uso,
)
from .transporte import abrir_resposta

_ABRE, _FECHA = "<think>", "</think>"
_SUFIXOS_NUVEM = ("-cloud", ":cloud")


class SeparadorThink:
    """Separa `<think>…</think>` do texto de um stream, mesmo com as tags partidas em deltas.

    Modelos que raciocinam dentro do conteúdo (sem campo próprio) precisam disso para o
    pensamento não aparecer como resposta. Só retém o sufixo que ainda pode virar uma tag.
    """

    def __init__(self) -> None:
        self._dentro = False
        self._pendente = ""

    def _evento(self, texto: str) -> list[Texto | Raciocinio]:
        if not texto:
            return []
        return [Raciocinio(texto) if self._dentro else Texto(texto)]

    def alimentar(self, s: str) -> list[Texto | Raciocinio]:
        buf, saida = self._pendente + s, []
        while True:
            tag = _FECHA if self._dentro else _ABRE
            posicao = buf.find(tag)
            if posicao < 0:
                break
            saida += self._evento(buf[:posicao])
            buf = buf[posicao + len(tag):]
            self._dentro = not self._dentro
        retido = next((n for n in range(min(len(buf), len(tag) - 1), 0, -1)
                       if tag.startswith(buf[-n:])), 0)
        saida += self._evento(buf[:len(buf) - retido])
        self._pendente = buf[len(buf) - retido:]
        return saida

    def finalizar(self) -> list[Texto | Raciocinio]:
        saida = self._evento(self._pendente)
        self._pendente = ""
        return saida


class BackendOpenAI(Backend):
    nome = "openai"

    def __init__(self, url: str, chave: str = "", timeout: float = 30.0,
                 permitir_nuvem: bool = False) -> None:
        self.url = url.rstrip("/")
        self.chave = chave
        self.timeout = timeout
        self.permitir_nuvem = permitir_nuvem
        partes = urlsplit(self.url)
        self._ssl = partes.scheme == "https"
        self._host = partes.hostname or "127.0.0.1"
        self._porta = partes.port or (443 if self._ssl else 80)
        self._base = partes.path.rstrip("/")

    # --- HTTP -----------------------------------------------------------------------

    def _conexao(self) -> http.client.HTTPConnection:
        if self._ssl:
            return http.client.HTTPSConnection(self._host, self._porta, timeout=self.timeout)
        return http.client.HTTPConnection(self._host, self._porta, timeout=self.timeout)

    def _cabecalhos(self, com_corpo: bool) -> dict:
        cabecalhos = {"Content-Type": "application/json"} if com_corpo else {}
        if self.chave:
            cabecalhos["Authorization"] = f"Bearer {self.chave}"
        return cabecalhos

    def _erro_conexao(self) -> ErroBackend:
        return ErroBackend(f"Não consegui falar com o servidor em {self.url}. Ele está rodando?")

    @staticmethod
    def _mensagem_de_erro(bruto: bytes) -> str:
        texto = bruto.decode("utf-8", "replace").strip()
        try:
            erro = json.loads(texto).get("error")
        except (ValueError, AttributeError):
            return texto
        if isinstance(erro, dict):
            erro = erro.get("message")
        return str(erro) if erro else texto

    def _erro_http(self, status: int, bruto: bytes) -> ErroBackend:
        if status in (401, 403):
            return ErroBackend("O servidor recusou a chave do backend (chave_backend no config.json).")
        return ErroBackend(f"O servidor respondeu {status}: {self._mensagem_de_erro(bruto)}")

    # --- Modelos --------------------------------------------------------------------

    def listar_modelos(self) -> list[InfoModelo]:
        conexao = self._conexao()
        try:
            try:
                conexao.request("GET", f"{self._base}/models", headers=self._cabecalhos(False))
                resposta = conexao.getresponse()
                bruto = resposta.read()
            except (OSError, http.client.HTTPException) as e:
                raise self._erro_conexao() from e
            if resposta.status != 200:
                raise self._erro_http(resposta.status, bruto)
            try:
                itens = json.loads(bruto).get("data") or []
            except (ValueError, AttributeError) as e:
                raise ErroBackend("O servidor devolveu uma lista de modelos inválida.") from e
        finally:
            conexao.close()
        modelos = [self._info(i["id"]) for i in itens if isinstance(i, dict) and i.get("id")]
        return [m for m in modelos if self.permitir_nuvem or not m.nuvem]

    @staticmethod
    def _info(modelo: str) -> InfoModelo:
        # A API não informa capacidades nem janela; assume ferramentas e deixa o resto desligado.
        nuvem = modelo.endswith(_SUFIXOS_NUVEM)
        return InfoModelo(modelo, modelo, True, False, False, nuvem, None)

    def info_modelo(self, modelo: str) -> InfoModelo | None:
        return self._info(modelo)

    # --- Conversa -------------------------------------------------------------------

    @staticmethod
    def _converter(mensagens: list[dict]) -> list[dict]:
        saida = []
        for m in mensagens:
            papel, texto = m.get("papel"), m.get("texto") or ""
            if papel == "sistema":
                saida.append({"role": "system", "content": texto})
            elif papel == "usuario":
                if m.get("imagens"):
                    conteudo = [{"type": "text", "text": texto}] + [
                        {"type": "image_url",
                         "image_url": {"url": f"data:image/png;base64,{b64}"}}
                        for b64 in m["imagens"]]
                    saida.append({"role": "user", "content": conteudo})
                else:
                    saida.append({"role": "user", "content": texto})
            elif papel == "assistente":
                msg = {"role": "assistant", "content": texto}
                if m.get("chamadas"):
                    msg["tool_calls"] = [
                        {"id": c["id"], "type": "function",
                         "function": {"name": c["nome"],
                                      "arguments": json.dumps(c.get("argumentos") or {},
                                                              ensure_ascii=False)}}
                        for c in m["chamadas"]]
                saida.append(msg)
            elif papel == "ferramenta":
                saida.append({"role": "tool", "tool_call_id": m.get("id_chamada", ""),
                              "content": texto})
        return saida

    def _corpo(self, pedido: Pedido) -> dict:
        corpo: dict = {"model": pedido.modelo, "messages": self._converter(pedido.mensagens),
                       "stream": True, "stream_options": {"include_usage": True}}
        if pedido.temperatura is not None:
            corpo["temperature"] = pedido.temperatura
        if pedido.ferramentas:
            corpo["tools"] = para_funcoes(pedido.ferramentas)
        return corpo

    def conversar(self, pedido: Pedido, cancelado: threading.Event) -> Iterator[Evento]:
        corpo = json.dumps(self._corpo(pedido), ensure_ascii=False).encode("utf-8")
        conexao = self._conexao()
        try:
            # Os cabeçalhos podem só chegar depois do prefill; a abertura cancelável cobre essa
            # espera (ver `transporte`). Essa premissa foi medida só no Ollama (`/v1`). Num servidor
            # que mande os cabeçalhos antes, a espera do prefill passa para a leitura do stream, e o
            # "parar" do Duo só vale quando chegar a primeira linha.
            resposta = abrir_resposta(conexao, f"{self._base}/chat/completions", corpo,
                                      self._cabecalhos(True), cancelado, self._erro_conexao)
            if resposta is None:
                return
            try:
                if resposta.status != 200:
                    raise self._erro_http(resposta.status, resposta.read())
                yield from self._ler(resposta, cancelado)
            except (OSError, http.client.HTTPException) as e:
                raise self._erro_conexao() from e
        finally:
            conexao.close()

    def _ler(self, resposta: http.client.HTTPResponse, cancelado: threading.Event) -> Iterator[Evento]:
        separador, acumulador = SeparadorThink(), AcumuladorChamadas()
        uso_final = None  # o `usage` chega antes do fim; sai por último, como no Ollama
        cortada = False
        while not cancelado.is_set():
            linha = resposta.readline()
            if not linha:
                break
            linha = linha.decode("utf-8", "replace").strip()
            if not linha.startswith("data:"):
                continue  # linhas em branco e comentários (`: keep-alive`) do SSE
            dados = linha[5:].strip()
            if dados == "[DONE]":
                break
            try:
                obj = json.loads(dados)
            except ValueError as e:
                raise ErroBackend("O servidor enviou um evento que não é JSON.") from e
            if not isinstance(obj, dict):
                raise ErroBackend("O servidor enviou um evento inválido (esperado um objeto JSON).")
            if obj.get("error"):
                erro = obj["error"]
                raise ErroBackend(f"O servidor respondeu com erro: "
                                  f"{erro.get('message') if isinstance(erro, dict) else erro}")
            escolhas = obj.get("choices") or []
            primeira = escolhas[0] if isinstance(escolhas, list) and escolhas else {}
            delta = (primeira.get("delta") if isinstance(primeira, dict) else None) or {}
            cortada = cortada or (isinstance(primeira, dict) and primeira.get("finish_reason") == "length")
            uso = obj.get("usage")
            # Um servidor fora do formato não pode virar AttributeError ("erro interno"): o usuário
            # precisa saber que o problema está na resposta do motor.
            if not isinstance(delta, dict) or (uso is not None and not isinstance(uso, dict)):
                raise ErroBackend("O servidor enviou um evento inválido (fora do formato da API).")
            raciocinio = delta.get("reasoning_content") or delta.get("reasoning")
            if isinstance(raciocinio, str) and raciocinio:
                yield Raciocinio(raciocinio)
            if isinstance(delta.get("content"), str) and delta["content"]:
                yield from separador.alimentar(delta["content"])
            if delta.get("tool_calls"):
                acumulador.alimentar(delta["tool_calls"])
            if uso:
                uso_final = Uso(uso.get("prompt_tokens") or 0, uso.get("completion_tokens") or 0)
        if cancelado.is_set():
            return
        yield from separador.finalizar()
        yield from acumulador.finalizar()
        if cortada:
            yield Cortada()
        if uso_final:
            yield uso_final
