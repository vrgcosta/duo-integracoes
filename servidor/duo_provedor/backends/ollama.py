"""Backend Ollama pela API nativa (`/api/chat`, NDJSON).

Conferido contra o Ollama 0.30.10 (qwen3:0.6b): o raciocínio vem em `message.thinking`;
`tool_calls[]` traz `id` e `function.index` além de `name`/`arguments` (já um objeto);
o chunk final leva `prompt_eval_count`/`eval_count`; `/api/show` lista `capabilities`
(`completion`, `tools`, `thinking`, `vision`) e `model_info["<arq>.context_length"]`;
`/api/ps` informa o `context_length` realmente carregado.

Usamos a API nativa, e não a compatível com a OpenAI, porque só ela aceita `num_ctx` por
requisição (sem isso o Ollama trunca o prompt no contexto padrão, em silêncio) e `think`.
"""

from __future__ import annotations

import http.client
import json
import re
import threading
from typing import Iterator
from urllib.parse import urlsplit

from ..ferramentas import para_funcoes
from .base import (
    Backend, ChamadaFerramenta, Cortada, ErroAcimaDaJanela, ErroBackend, Evento, InfoModelo, Pedido, Raciocinio, Texto,
    Uso,
)
from .transporte import abrir_resposta

# Como o `/api/chat` recusa, com `truncate: false`, o pedido que passa de `num_ctx`. O corpo traz o tipo e o número em
# campos; o texto é só a reserva, porque é ele que mais pode mudar de uma versão para outra.
_TIPO_ACIMA_DA_JANELA = "exceed_context_size_error"
_ACIMA_DA_JANELA = re.compile(r"request \((\d+) tokens\) exceeds")


def _acima_da_janela(mensagem: str) -> ErroAcimaDaJanela | None:
    """A recusa por excesso, se é disso que a mensagem do motor trata."""
    try:
        corpo = json.loads(mensagem)
    except ValueError:
        corpo = None
    erro = corpo.get("error") if isinstance(corpo, dict) else None
    do_tipo = isinstance(erro, dict) and erro.get("type") == _TIPO_ACIMA_DA_JANELA
    if do_tipo and isinstance(erro.get("n_prompt_tokens"), int):
        return ErroAcimaDaJanela(erro["n_prompt_tokens"])
    achado = _ACIMA_DA_JANELA.search(mensagem)
    if achado:
        return ErroAcimaDaJanela(int(achado.group(1)))
    return ErroAcimaDaJanela(None) if do_tipo else None


def com_etiqueta(modelo: str) -> str:
    """O nome como as listas do Ollama o mostram: sem etiqueta, ele vale `:latest`.

    `gemma3` e `gemma3:latest` são o mesmo modelo, mas o `/api/tags` e o `/api/ps` sempre trazem a
    etiqueta; comparar o nome cru deixaria de achar o modelo. A etiqueta é o ":" do último trecho
    do nome: em `localhost:5000/modelo`, o ":" é da porta do registro.
    """
    return modelo if ":" in modelo.rsplit("/", 1)[-1] else modelo + ":latest"


class BackendOllama(Backend):
    nome = "ollama"
    pode_cortar_calado = False  # todo envio vai com `truncate: false`: o que não cabe volta como 400

    def __init__(self, url: str, permitir_nuvem: bool = False, timeout: float = 30.0) -> None:
        self.url = url.rstrip("/")
        self.permitir_nuvem = permitir_nuvem
        self.timeout = timeout
        partes = urlsplit(self.url)
        self._ssl = partes.scheme == "https"
        self._host = partes.hostname or "127.0.0.1"
        self._porta = partes.port or (443 if self._ssl else 80)
        self._cache: dict[str, InfoModelo | None] = {}
        self._trava = threading.Lock()

    # --- HTTP -----------------------------------------------------------------------

    def _conexao(self) -> http.client.HTTPConnection:
        if self._ssl:
            return http.client.HTTPSConnection(self._host, self._porta, timeout=self.timeout)
        return http.client.HTTPConnection(self._host, self._porta, timeout=self.timeout)

    def _erro_conexao(self) -> ErroBackend:
        return ErroBackend(f"Não consegui falar com o Ollama em {self.url}. Ele está rodando? "
                           "Abra o app do Ollama ou rode `ollama serve`.")

    @staticmethod
    def _mensagem_de_erro(bruto: bytes) -> str:
        texto = bruto.decode("utf-8", "replace").strip()
        try:
            obj = json.loads(texto)
            if isinstance(obj, dict) and obj.get("error"):
                # O Ollama 0.30.10 manda a recusa por excesso como um JSON dentro de um texto, mas podia vir sem isso.
                return obj["error"] if isinstance(obj["error"], str) else json.dumps({"error": obj["error"]})
        except ValueError:
            pass
        return texto

    def _erro_http(self, status: int, bruto: bytes, modelo: str | None) -> ErroBackend:
        mensagem = self._mensagem_de_erro(bruto)
        acima = _acima_da_janela(mensagem)
        if acima is not None:
            return acima
        if modelo and (status == 404 or "not found" in mensagem.lower()):
            return ErroBackend(f"O modelo {modelo} não está instalado. Rode: ollama pull {modelo}")
        return ErroBackend(f"O Ollama respondeu {status}: {mensagem}")

    def _json(self, metodo: str, caminho: str, corpo: dict | None = None,
              modelo: str | None = None) -> dict:
        conexao = self._conexao()
        try:
            dados = json.dumps(corpo, ensure_ascii=False).encode("utf-8") if corpo is not None else None
            cabecalhos = {"Content-Type": "application/json"} if dados is not None else {}
            try:
                conexao.request(metodo, caminho, body=dados, headers=cabecalhos)
                resposta = conexao.getresponse()
                bruto = resposta.read()
            except (OSError, http.client.HTTPException) as e:
                raise self._erro_conexao() from e
            if resposta.status != 200:
                raise self._erro_http(resposta.status, bruto, modelo)
            try:
                return json.loads(bruto)
            except ValueError as e:
                raise ErroBackend(f"O Ollama devolveu uma resposta inválida em {caminho}.") from e
        finally:
            conexao.close()

    # --- Modelos --------------------------------------------------------------------

    def listar_modelos(self) -> list[InfoModelo]:
        modelos = []
        for item in self._json("GET", "/api/tags").get("models") or []:
            nome = item.get("name") or item.get("model")
            if not nome:
                continue
            nuvem = bool(item.get("remote_host") or item.get("remote_model"))
            if nuvem and not self.permitir_nuvem:
                continue
            info = self.info_modelo(nome) or self._montar(nome, [], None, nuvem)
            info.nuvem = nuvem or info.nuvem
            if info.conversa:
                modelos.append(info)
        return modelos

    def info_modelo(self, modelo: str) -> InfoModelo | None:
        with self._trava:
            if modelo in self._cache:
                return self._cache[modelo]
        try:
            show = self._json("POST", "/api/show", {"model": modelo}, modelo=modelo)
        except ErroBackend as e:
            if "não está instalado" in str(e):
                return None  # ausência não entra no cache: o usuário pode instalar depois
            raise
        contexto = next((v for k, v in (show.get("model_info") or {}).items()
                         if k.endswith(".context_length") and isinstance(v, int)), None)
        nuvem = bool(show.get("remote_host") or show.get("remote_model"))
        info = self._montar(modelo, show.get("capabilities") or [], contexto, nuvem)
        with self._trava:
            self._cache[modelo] = info
        return info

    @staticmethod
    def _montar(modelo: str, capacidades: list[str], contexto: int | None, nuvem: bool) -> InfoModelo:
        ferramentas = "tools" in capacidades
        raciocinio = "thinking" in capacidades
        visao = "vision" in capacidades
        rotulo = " · ".join([modelo]
                            + (["ferramentas"] if ferramentas else [])
                            + (["raciocínio"] if raciocinio else [])
                            + (["visão"] if visao else []))
        # Modelo de embedding (qwen3-embedding, por exemplo) está em /api/tags, mas o /api/chat o recusa.
        # Lista vazia não prova nada: Ollama antigo não manda `capabilities`, e o modelo fica.
        conversa = not capacidades or "completion" in capacidades
        return InfoModelo(modelo, rotulo, ferramentas, raciocinio, visao, nuvem, contexto, conversa)

    def _carregado(self, modelo: str) -> dict:
        """A entrada do modelo no `/api/ps` (os modelos na memória agora), ou {}."""
        alvo = com_etiqueta(modelo)
        for item in self._json("GET", "/api/ps").get("models") or []:
            if isinstance(item, dict) and alvo in {com_etiqueta(n) for n in (item.get("name"), item.get("model"))
                                                   if isinstance(n, str)}:
                return item
        return {}

    def versao_motor(self) -> str | None:
        try:
            versao = self._json("GET", "/api/version").get("version")
        except ErroBackend:
            return None
        return versao if isinstance(versao, str) else None

    def contexto_em_uso(self, modelo: str) -> int | None:
        valor = self._carregado(modelo).get("context_length")
        return valor if isinstance(valor, int) else None

    def memoria_em_uso(self, modelo: str) -> tuple[int, int] | None:
        item = self._carregado(modelo)
        total, vram = item.get("size"), item.get("size_vram")
        if isinstance(total, int) and isinstance(vram, int) and total > 0:
            return total, vram
        return None

    # --- Conversa -------------------------------------------------------------------

    @staticmethod
    def _converter(mensagens: list[dict]) -> list[dict]:
        saida = []
        for m in mensagens:
            papel, texto = m.get("papel"), m.get("texto") or ""
            if papel == "sistema":
                saida.append({"role": "system", "content": texto})
            elif papel == "usuario":
                msg = {"role": "user", "content": texto}
                if m.get("imagens"):
                    msg["images"] = list(m["imagens"])
                saida.append(msg)
            elif papel == "assistente":
                msg = {"role": "assistant", "content": texto}
                if m.get("chamadas"):
                    msg["tool_calls"] = [
                        {"function": {"name": c["nome"], "arguments": c.get("argumentos") or {}}}
                        for c in m["chamadas"]]
                saida.append(msg)
            elif papel == "ferramenta":
                saida.append({"role": "tool", "content": texto, "tool_name": m.get("nome", "")})
        return saida

    def _corpo(self, pedido: Pedido) -> dict:
        opcoes: dict = {"num_ctx": pedido.contexto_tokens}
        if pedido.temperatura is not None:
            opcoes["temperature"] = pedido.temperatura
        # `truncate: false`, inclusive no envio: sem ele, o Ollama corta calado ao passar de `num_ctx`. Tira as
        # mensagens mais antigas, ou o começo de uma só, e o modelo responde sem elas. Com ele, recusa com o total, e
        # quem decide o que fazer é o turno.
        corpo: dict = {"model": pedido.modelo, "messages": self._converter(pedido.mensagens),
                       "stream": True, "truncate": False, "options": opcoes}
        if pedido.ferramentas:
            corpo["tools"] = para_funcoes(pedido.ferramentas)
        # `think` explícito (inclusive false): omitido, o Ollama liga o raciocínio por padrão
        # nos modelos que o têm, e o "desligado" do Duo não teria efeito.
        info = self.info_modelo(pedido.modelo)
        if info and info.raciocinio:
            corpo["think"] = bool(pedido.pensar)
        return corpo

    @staticmethod
    def _chamada(item: dict) -> ChamadaFerramenta:
        fn = item.get("function") or {}
        nome, argumentos = fn.get("name", ""), fn.get("arguments")
        if argumentos is None:
            return ChamadaFerramenta(nome, {})
        if isinstance(argumentos, str):
            try:
                argumentos = json.loads(argumentos) if argumentos.strip() else {}
            except ValueError as e:
                return ChamadaFerramenta(nome, None, erro=f"argumentos não são JSON válido: {e}")
        if not isinstance(argumentos, dict):
            return ChamadaFerramenta(nome, None, erro="argumentos não são um objeto JSON")
        return ChamadaFerramenta(nome, argumentos)

    def conversar(self, pedido: Pedido, cancelado: threading.Event) -> Iterator[Evento]:
        yield from self._fluxo(self._corpo(pedido), cancelado, pedido.modelo)

    def contar_tokens(self, pedido: Pedido, cancelado: threading.Event | None = None) -> int | None:
        """O total exato, pelo próprio `/api/chat`, com o pedido idêntico e um só token de saída.

        O Ollama não tem rota de contagem. Medido no 0.30.10: com `num_predict: 1`, o `prompt_eval_count` é
        o total, com as ferramentas e o prefixo em cache, e o prefill fica para o envio em seguida aproveitar.
        Com 0 ele não gera nada, mas custa de 3 a 4 s a mais.

        O `truncate: false` de `_corpo` é o que faz o número ser verdade acima de `num_ctx`. Sem ele, o Ollama corta
        calado: uma mensagem só satura em `num_ctx - 1`, e em várias ele tira as mais antigas e conta o que sobrou
        (6586 tokens em `num_ctx` 4096 deram 3304), o que parece caber. Com ele, responde 400 em 0,2 s, sem prefill,
        com o total. Um Ollama antigo ignora o campo, e aí o número pode enganar (ver `envio.contagem_suspeita`).
        """
        corpo = self._corpo(pedido)
        corpo["options"]["num_predict"] = 1
        cancelado = cancelado or threading.Event()
        total = None
        try:
            for evento in self._fluxo(corpo, cancelado, pedido.modelo):
                if isinstance(evento, Uso):
                    total = evento.entrada
        except ErroAcimaDaJanela as e:
            if e.total is None:
                raise  # recusou sem dizer quanto: é a prova, e não há número a devolver
            return e.total
        if not total and not cancelado.is_set():
            # None é de quem nunca teve como contar; aqui é uma falha, e o usuário é avisado.
            raise ErroBackend("O Ollama não informou a contagem de tokens.")
        return total or None

    def _fluxo(self, corpo: dict, cancelado: threading.Event, modelo: str) -> Iterator[Evento]:
        dados = json.dumps(corpo, ensure_ascii=False).encode("utf-8")
        conexao = self._conexao()
        try:
            # O Ollama (net/http do Go) só escreve os cabeçalhos junto do primeiro chunk,
            # então o carregamento e o prefill acontecem antes de qualquer byte; a abertura
            # cancelável cobre essa espera. Depois que o stream começa, o cancelamento
            # espera a próxima linha NDJSON (ver `_ler`).
            resposta = abrir_resposta(conexao, "/api/chat", dados,
                                      {"Content-Type": "application/json"},
                                      cancelado, self._erro_conexao)
            if resposta is None:
                return
            try:
                if resposta.status != 200:
                    raise self._erro_http(resposta.status, resposta.read(), modelo)
                yield from self._ler(resposta, cancelado, modelo)
            except (OSError, http.client.HTTPException) as e:
                raise self._erro_conexao() from e
        finally:
            conexao.close()

    def _ler(self, resposta: http.client.HTTPResponse, cancelado: threading.Event,
             modelo: str) -> Iterator[Evento]:
        while not cancelado.is_set():
            linha = resposta.readline()
            if not linha:
                return
            linha = linha.strip()
            if not linha:
                continue
            try:
                obj = json.loads(linha)
            except ValueError as e:
                raise ErroBackend("O Ollama enviou uma linha que não é JSON.") from e
            if obj.get("error"):
                erro = str(obj["error"])
                acima = _acima_da_janela(erro)
                if acima is not None:
                    raise acima
                if "not found" in erro.lower():
                    raise ErroBackend(f"O modelo {modelo} não está instalado. Rode: ollama pull {modelo}")
                raise ErroBackend(f"O Ollama respondeu com erro: {erro}")
            mensagem = obj.get("message") or {}
            if mensagem.get("thinking"):
                yield Raciocinio(mensagem["thinking"])
            if mensagem.get("content"):
                yield Texto(mensagem["content"])
            for item in mensagem.get("tool_calls") or []:
                yield self._chamada(item)
            if obj.get("done"):
                if obj.get("done_reason") == "length":
                    yield Cortada()
                entrada, saida = obj.get("prompt_eval_count"), obj.get("eval_count")
                if entrada is not None or saida is not None:
                    yield Uso(entrada or 0, saida or 0)
                return
