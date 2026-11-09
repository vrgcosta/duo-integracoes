# Plugar o seu backend

Um backend traduz o formato neutro do servidor para o protocolo do seu motor de LLM e devolve eventos.
Ele não sabe nada de SSE, conversas ou ferramentas do Duo.

## A interface (`duo_provedor/backends/base.py`)

```python
class Backend(abc.ABC):
    nome: str
    def listar_modelos(self) -> list[InfoModelo]: ...
    def info_modelo(self, modelo: str) -> InfoModelo | None: ...
    def conversar(self, pedido: Pedido, cancelado: threading.Event) -> Iterator[Evento]: ...
    def contar_tokens(self, pedido: Pedido, cancelado: threading.Event | None = None) -> int | None: ...   # opcional
    def contexto_em_uso(self, modelo: str) -> int | None: ...   # opcional
    def memoria_em_uso(self, modelo: str) -> tuple[int, int] | None: ...   # opcional
```

`contexto_em_uso` e `memoria_em_uso` só alimentam o `--verificar`: o contexto realmente carregado e a memória
do modelo carregado agora, como `(total, parte na VRAM)` em bytes (com a VRAM menor que o total, o resto está
no processador e o diagnóstico avisa). Sem eles, devolva `None`, como a classe base já faz.

`contar_tokens` é consultado antes de cada rodada, para o motor não truncar em silêncio: devolva o total exato de
tokens de entrada do pedido, se o seu motor souber dizer, mesmo acima da janela. Se ele recusar o pedido sem contar,
levante `ErroAcimaDaJanela(total)`, ou `ErroAcimaDaJanela(None)` se ele não disser quanto: é a prova de que não cabe, e o
turno tira as ferramentas ou diz ao usuário o que pesa. Com `None`, que é o da classe base, ou com `ErroBackend`, o turno
fica com a estimativa, e a falha avisa o usuário. Se demorar, o turno desiste e liga `cancelado`.

- `Pedido`: `modelo`, `mensagens` (formato neutro), `ferramentas` (definições `{name, description,
  input_schema}`), `pensar`, `contexto_tokens`, `temperatura`.
- Eventos que `conversar` produz: `Texto(texto)`, `Raciocinio(texto)`, `ChamadaFerramenta(nome,
  argumentos, erro=None)`, `Uso(entrada, saida)` e `Cortada()`. Preencha `erro` quando os argumentos vierem
  inválidos; o servidor devolve o erro ao modelo. `Cortada` diz que o motor parou no limite, e não porque a
  resposta acabou: o usuário é avisado, e um resumo nessa condição não é gravado.
- `cancelado` é um `threading.Event`: se ficar ligado, pare de ler e feche a conexão com o motor.
- Falhas esperadas (motor fora do ar, modelo ausente) viram `ErroBackend("mensagem para o usuário")`.
- Se o motor recusar o pedido por passar da janela, levante `ErroAcimaDaJanela(total)`, também aqui: o turno tira as
  ferramentas e, se nem assim couber, diz ao usuário o que pesa. Se o seu motor corta calado, não há como: por isso o aviso de
  medida estimada, que diz que ele pode cortar o começo. Se ele nunca corta, ponha `pode_cortar_calado = False` na classe, e o
  aviso diz que o motor recusa. O padrão é `True`, o lado seguro.
- `versao_motor()` é opcional, e só o `--verificar` a usa, para mostrar a versão. Pode deixar o `None` da classe base: a
  comparação que há lá é com a versão do Ollama em que a contagem foi medida, e uma versão menor do seu motor cairia nela.

## O formato neutro de mensagem

- `{"papel": "sistema", "texto"}`
- `{"papel": "usuario", "texto", "imagens"?: [base64], "texto_sem_anexos"?: str, "resultado"?: True, "itens"?: [...]}`
- `{"papel": "assistente", "texto", "chamadas"?: [{"id", "nome", "argumentos"}]}`
- `{"papel": "ferramenta", "id_chamada", "nome", "texto"}`

O `texto` já vem pronto: `resultado`, `itens`, `truncado` e `omitido` são da janela, e a mensagem que
as tem vai ao modelo como qualquer outra. Ignore chaves que não conhece.

## Exemplo: backend "eco"

Fala HTTP com uma API fictícia (`POST /gerar`, resposta em linhas JSON `{"pedaco": "..."}`).

```python
"""Backend de exemplo: devolve o que uma API fictícia gerar."""
import http.client, json, threading
from urllib.parse import urlsplit
from duo_provedor.backends.base import Backend, ErroBackend, InfoModelo, Pedido, Texto, Uso


class BackendEco(Backend):
    nome = "eco"

    def __init__(self, url: str) -> None:
        partes = urlsplit(url)
        self._host, self._porta = partes.hostname, partes.port or 80

    def _info(self) -> InfoModelo:
        # id, rótulo, ferramentas, raciocínio, visão, nuvem, contexto máximo (e, opcional, conversa=False
        # para o que não gera texto: não entra em listar_modelos, como os modelos de embedding do Ollama)
        return InfoModelo("eco-1", "Eco 1", False, False, False, False, 4096)

    def listar_modelos(self) -> list[InfoModelo]:
        return [self._info()]

    def info_modelo(self, modelo: str) -> InfoModelo | None:
        return self._info() if modelo == "eco-1" else None

    def conversar(self, pedido: Pedido, cancelado: threading.Event):
        texto = "\n".join(m.get("texto", "") for m in pedido.mensagens)
        conexao = http.client.HTTPConnection(self._host, self._porta, timeout=30)
        try:
            conexao.request("POST", "/gerar", json.dumps({"prompt": texto}).encode("utf-8"),
                            {"Content-Type": "application/json"})
            resposta = conexao.getresponse()
            if resposta.status != 200:
                raise ErroBackend(f"A API eco respondeu {resposta.status}.")
            saida = 0
            for linha in resposta:
                if cancelado.is_set():  # o usuário parou: solte a conexão já
                    return
                if linha.strip():
                    yield Texto(json.loads(linha)["pedaco"])
                    saida += 1
            yield Uso(len(texto) // 4, saida)
        except OSError as erro:
            raise ErroBackend(f"Não consegui falar com a API eco: {erro}") from erro
        finally:
            conexao.close()
```

## Registrar

Em `duo_provedor/backends/__init__.py`, acrescente o import e o ramo dentro de `criar_backend` (os
backends são importados ali, e não no topo, para evitar um import circular); inclua também o nome em
`URL_PADRAO` (`config.py`) e nas `choices` de `--backend` (`servidor.py`):

```python
    from .eco import BackendEco
    ...
    if nome == "eco":
        return BackendEco(cfg["url"])
```

## Sigilo: o contrato que o seu backend precisa honrar

O blueprint diz ao Duo `destinoLocal: true` quando `destino_local` (`duo_provedor/blueprint.py`)
conclui que o texto não sai da máquina. Essa função só olha duas coisas: se a `url` do config é
loopback e se `permitir_modelos_nuvem` está desligado. Ela **não sabe** o que o seu backend faz com o
texto. Por isso:

- **Se o seu backend manda o texto para outro lugar** mesmo com `url` de loopback (um proxy, uma ponte
  para um serviço na internet, um modelo que consulta uma API), faça `destino_local` devolver `False`
  para ele, por exemplo acrescentando `and cfg.get("backend") != "eco"`. Sem isso, o Duo trata como
  local um destino que não é, e os autos saem sem o usuário saber.
- **Se só alguns modelos rodam fora**, devolva `nuvem=True` no `InfoModelo` deles (em `listar_modelos`
  e em `info_modelo`). O servidor os esconde da lista e recusa os envios para eles enquanto
  `permitir_modelos_nuvem` estiver desligado; o `--verificar` e o banner também avisam.

## Testar com o `ServidorFalso`

`testes/falso.py` sobe um servidor HTTP real que serve pedaços com `flush` e grava o que recebeu:

```python
rotas = {("POST", "/gerar"): (200, "application/x-ndjson",
                              ['{"pedaco": "Olá"}\n'.encode(), b'{"pedaco": "!"}\n'])}
with ServidorFalso(rotas) as srv:
    eventos = list(BackendEco(srv.url).conversar(pedido, threading.Event()))
assert [e.texto for e in eventos if isinstance(e, Texto)] == ["Olá", "!"]
assert srv.pedidos[0][2]["prompt"]          # corpo recebido pela API
```

Veja `testes/test_ollama.py` e `testes/test_openai.py` para casos de cancelamento, erro de HTTP e
ferramentas. Para testar o servidor inteiro com o seu backend, use `ServidorDeTeste(backend)` de
`testes/util.py` e o `testes/cliente_duo.py`.
