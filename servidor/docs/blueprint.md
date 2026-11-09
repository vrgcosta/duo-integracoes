# Contrato do blueprint

Este documento descreve o blueprint `duo-provedor-local`: o que o Duo faz com cada chave do molde
(`blueprint/modelo.json`), qual rota do servidor responde e a forma exata das mensagens trocadas.
É a fonte de `testes/cliente_duo.py`, que simula o Duo a partir daqui.

O arquivo que o usuário importa é gerado por `duo_provedor/blueprint.py` e fica em
`dados/duo-provedor-local.blueprint.json`. Ele contém a chave de acesso e por isso nunca é versionado.

## Convenções

- Marcadores em MAIÚSCULAS (`{PORTA}`, `{CHAVE}`, `{DESTINO_LOCAL}`) são preenchidos pelo servidor na
  geração. Um marcador que ocupa o valor inteiro mantém o tipo (número, booleano); dentro de um texto
  vira texto.
- Marcadores em minúsculas (`{convId}`, `{prompt}`, `{leaf}`, `{sistema}`, `{modelo}`, `{timezone}`,
  `{titulo}`, `{termo}`, `{projetoId}`, `{toolCallId}`, `{content}`, `{nome}`, `{tamanho}`,
  `{conteudo}`) são do Duo e ficam intactos no arquivo gerado. O Duo os resolve a cada chamada.
- Todo corpo e toda resposta (exceto o SSE) é JSON em UTF-8. Datas em ISO 8601 UTC com `Z`.
- Erros de rota respondem JSON com o status HTTP adequado (403 sem chave, 404 rota ou recurso inexistente, 400 corpo inválido, 409 conflito).

## Identidade e segurança

| Chave | Valor | O que o Duo faz |
|---|---|---|
| `id` | `duo-provedor-local` | Identifica o provedor; separa conversas e configurações dele. |
| `rotulo` | `Plugin (LLM local)` | Nome mostrado na interface. |
| `icone` | `nossos-robot` | Ícone do provedor. |
| `baseURL` | `http://127.0.0.1:{PORTA}` | Prefixo de todas as chamadas. O servidor escuta só em loopback. |
| `sessao` | `false` | Não há login nem cookie de sessão a reaproveitar; a autenticação é pelo cabeçalho. |
| `headers` | `{"x-duo-provedor-chave": "{CHAVE}"}` | Enviado em toda requisição. Sem a chave certa, ou com `Origin` de página web (`http:`/`https:`), o servidor responde 403; `OPTIONS` também responde 403 (sem CORS). |
| `identidade` | `["contaId"]` | Só essa variável do contexto identifica a "conta". Trocar a chave não troca de conta; mudar `contaId` sim. |

A chave é gerada na primeira execução e vive em `dados/config.json`. O servidor só aceita conexões de
`127.0.0.1`.

## Contexto e modelos

**`contexto`** (lista de chamadas executadas ao abrir o plugin): `GET /api/contexto` e a variável
`contaId` é extraída do campo `conta` da resposta.

```json
{"conta": "local", "backend": "ollama", "versao": "0.1.0"}
```

**`modelos`**: lista fixa com um único item, `{"id": "padrao", "label": "Modelo padrão do servidor"}`.
Serve de reserva quando a descoberta falha.

**`modelosEndpoint`**: `GET /api/modelos`. O Duo lê `modelos` (`itens`) e, de cada item, `id` e `label`.

```json
{"modelos": [{"id": "padrao", "label": "Padrão do servidor (qwen3.5:9b)"}, {"id": "llama3.1:8b", "label": "llama3.1:8b"}]}
```

Modelos de nuvem só aparecem quando `permitir_modelos_nuvem` está ligado na configuração. Com a opção
desligada, um envio para um deles (pedido pelo id ou como modelo padrão) termina em evento `error`
antes de qualquer gravação, sem chamar o modelo: é o que mantém verdadeiro o `destinoLocal: true`.

## Conversas (árvore)

O servidor guarda cada conversa como uma árvore de nós, e o Duo reconstrói o histórico seguindo
`parent_message_uuid` da folha até a raiz.

### Forma da conversa

```json
{
  "uuid": "…", "name": "…", "model": "…", "project_uuid": null,
  "created_at": "2026-10-22T12:00:00Z", "updated_at": "2026-10-22T12:00:00Z",
  "pensamento": null,
  "ferramentas_carregadas": [],
  "chat_messages": []
}
```

`pensamento` é `null` (padrão), `true` ou `false`. `ferramentas_carregadas` lista as ferramentas que o
modelo pediu ao catálogo naquela conversa.

### Forma do nó (item de `chat_messages`)

```json
{
  "uuid": "…", "parent_message_uuid": "…", "sender": "human",
  "created_at": "…", "text": "…", "content": [], "attachments": [], "files": []
}
```

- `sender` é `human` ou `assistant`.
- O primeiro nó tem como pai a raiz `00000000-0000-4000-8000-000000000000`.
- `content` é uma lista de blocos: `{"type":"text","text":…}`, `{"type":"thinking","summaries":[{"summary":…}]}`,
  `{"type":"tool_use","id","name","input"}` e `{"type":"tool_result","tool_use_id","content","is_error"}`.
- Chamadas internas do servidor (meta-ferramenta do catálogo e `ler_trecho_resultado`) carregam
  `"interno": true` e são filtradas na leitura: o Duo nunca as vê.

### Chaves de `conversas`

| Chave | Rota | O que o Duo faz |
|---|---|---|
| `criar` | `POST /api/conversas` com `{"name": "", "model": "{modelo}"}` (e `project_uuid` quando há projeto, via `campoProjeto`) | Cria a conversa; o id vem em `uuid` na resposta (`{"uuid": "…"}`). |
| `renomear` | `PUT /api/conversas/{convId}` com `{"name": "{titulo}"}` | Responde `{"ok": true}`. |
| `ler` | `GET /api/conversas/{convId}` | Lê a árvore (ver abaixo). 404 se não existir. |
| `listar` | `GET /api/conversas` | Lista de resumos `{uuid, name, model, project_uuid, created_at, updated_at}`, mais recentes primeiro. Mapeamento: `id`←`uuid`, `titulo`←`name`, `atualizadoEm`←`updated_at`, `projetoId`←`project_uuid`. |
| `projetos` | `GET /api/projetos` | Lista `[{"uuid": "civel", "name": "Cível"}]`. Cada projeto é uma pasta com `instrucoes.md`. |
| `projetoConversas` | `GET /api/projetos/{projetoId}/conversas` | Resumos das conversas do projeto. |
| `buscar` | `GET /api/conversas/busca?termo={termo}` | Resumos cujo título ou texto casam com o termo, sem distinguir acento nem caixa. |
| `excluir` | `DELETE /api/conversas/{convId}` | Responde `{"ok": true}`. |

### Como o Duo lê (`conversas.ler`)

- `itens: "chat_messages"`: onde está a lista de nós.
- `arvore`: `uuid` e `parent` dizem quais campos ligam nó e pai; `raiz` é o pai do primeiro nó.
- `role`: o campo `sender` é traduzido por `human` → `user` e `assistant` → `assistant`.
- `texto`: usa o campo `text` do nó; se vier vazio, junta os blocos `content` com `type = text`.
- `pensamento`: junta os blocos `type = thinking`, percorrendo `summaries[].summary`, cada um terminado
  em `"\n"`.
- `ferramentas`: dentro de `content`, `call` reconhece blocos `type = tool_use` (`id`, `name`, `input`) e
  `result` reconhece blocos `type = tool_result`, ligados à chamada por `tool_use_id`, com o texto em
  `content` e o indicador de erro em `is_error`.

## Envio

`POST /api/conversas/{convId}/completion` devolve um fluxo SSE (ver a seção seguinte). Corpo:

```json
{
  "model": "{modelo}",
  "prompt": "{prompt}",
  "parent_message_uuid": "{leaf}",
  "timezone": "{timezone}",
  "system": "{sistema}",
  "tools": [],
  "files": [],
  "attachments": [],
  "opcoes": {"contexto": 0, "temperatura": -1, "ferramentas": "padrao", "max_rodadas": 0}
}
```

| Campo | Significado |
|---|---|
| `model` | `padrao` usa o modelo da configuração; qualquer outro id é repassado ao backend. |
| `prompt` | Texto da mensagem do usuário. |
| `parent_message_uuid` | Folha de onde a nova mensagem se ramifica (a raiz na primeira vez). Permite regenerar e editar. |
| `timezone` | Fuso do navegador. Aceito e ainda não usado: o servidor não o repassa ao modelo. |
| `system` | Instruções do Duo para este turno (ver "Campos opt-in"). |
| `tools` | Definições de ferramentas do Duo no formato `{name, description, input_schema}`. |
| `files` | Ids devolvidos por `/api/upload`. |
| `attachments` | Anexos de texto embutidos: `{file_name, file_type, file_size, extracted_content}`. |
| `opcoes` | Ajustes do usuário (ver "Opções do usuário"). |
| `catalogo` | Opcional; só existe quando o Duo envia o catálogo (ver "Campos opt-in"). |

Só uma resposta por conversa pode estar em andamento: um segundo envio na mesma conversa recebe 409
(`"Já há um envio em andamento nesta conversa."`); conversa inexistente dá 404. Campo com tipo errado
(por exemplo `prompt` que não é texto ou `tools` que não é lista) dá 400, antes de o SSE começar.

**`retryTransitorio`** (`{"padrao": "Já há um envio em andamento", "tentativas": 10}`, na raiz): logo depois de o usuário parar
uma resposta, o turno ainda leva um instante para soltar a conversa, e um novo envio recebe 409. O motor tenta de novo a cada
400 ms, enquanto a mensagem de erro casar com `padrao`. Ele não casa com o 409 de compactação em andamento, que não se resolve em
segundos: esse volta ao usuário na hora.

## Stream

O fluxo é `text/event-stream`. Cada evento é uma linha `data: <json>` seguida de linha em branco; o
tipo vem em `type`. Linhas `: ping` são comentários de manutenção e devem ser ignoradas.

| Evento | JSON |
|---|---|
| início | `{"type":"message_start","message":{"id":"msg_local_<hex16>","role":"assistant"}}` |
| abre bloco de texto | `{"type":"content_block_start","index":i,"content_block":{"type":"text","text":""}}` |
| abre bloco de raciocínio | `{"type":"content_block_start","index":i,"content_block":{"type":"thinking","thinking":""}}` |
| texto | `{"type":"content_block_delta","index":i,"delta":{"type":"text_delta","text":"…"}}` |
| raciocínio | `{"type":"content_block_delta","index":i,"delta":{"type":"thinking_delta","thinking":"…"}}` |
| chamada de ferramenta | `content_block_start` com `{"type":"tool_use","id":"toolu_local_<hex24>","name":"…","input":{}}`, depois `content_block_delta` com `{"type":"input_json_delta","partial_json":"<JSON completo dos argumentos>"}`, depois `{"type":"content_block_stop","index":i}` |
| fim | `{"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"input_tokens":e,"output_tokens":s}}` e então `{"type":"message_stop"}` |
| erro | `{"type":"error","error":{"type":"duo-provedor","message":"…"}}` |

Observações:

- `usage` é omitido quando o backend não informou contagem.
- Todo bloco aberto é fechado com `content_block_stop` antes de abrir outro de tipo diferente.
- A chamada de ferramenta vai inteira: o argumento chega num único `partial_json`.
- Após `error` o fluxo termina; não há `message_stop`.

### Como o Duo lê (`stream`)

- `texto`: eventos `content_block_delta` com `delta.type = text_delta`; o texto está em `delta.text`.
- `usage`: no evento `message_delta`, `usage.input_tokens` (entrada) e `usage.output_tokens` (saída).
- O Duo exibe a mensagem do evento `error` ao usuário.

## Ferramentas (pausa e retomada)

Chaves de `ferramentas`:

| Chave | Valor | O que o Duo faz |
|---|---|---|
| `campoTools` | `tools` | Campo do corpo do envio onde vão as definições de ferramentas. |
| `campoCatalogo` | `catalogo` | Campo do corpo onde vai o catálogo, quando ele existe (opt-in). |
| `toolCall` | `content_block_start` com `content_block.type = tool_use` | Reconhece o início de uma chamada; nome em `content_block.name`, id em `content_block.id`. |
| `toolCallInput` | `content_block_delta` com `delta.type = input_json_delta` | Acumula `delta.partial_json`. |
| `toolCallFim` | `content_block_stop` | Fecha a chamada; o Duo faz o parse do JSON acumulado. |
| `enviarResultado` | `POST /api/conversas/{convId}/tool_result` | Devolve o resultado de uma chamada. |

Não há chave `emulacao`: o servidor cuida sozinho do modo "cercas" (texto com blocos) e entrega ao Duo
sempre `tool_use` estruturado.

### Ciclo

1. O modelo decide chamar uma ou mais ferramentas. O servidor emite as chamadas no SSE, todas numa
   única escrita, e **pausa** o turno sem fechá-lo.
2. O Duo processa o trecho recebido inteiro, executa cada ferramenta e faz, para **cada** chamada:

   ```json
   POST /api/conversas/{convId}/tool_result
   {"tool_use_id": "toolu_local_…", "content": [{"type": "text", "text": "…"}], "is_error": false}
   ```

   Resposta: `{"ok": true}`. Sem turno aberto na conversa, ou com um `tool_use_id` que o turno não
   está aguardando (repetido ou desconhecido), 409; sem `tool_use_id` em texto, 400.
3. Com todos os resultados, o servidor **continua na mesma resposta SSE**: o texto seguinte chega pelo
   mesmo fluxo, sem novo `completion`.
4. Enquanto espera, o servidor envia `: ping` periodicamente para a conexão não cair. Se os resultados
   não chegarem no prazo, o turno termina com evento `error`.
5. Se o Duo fechar a conexão durante a espera, o turno é cancelado, o que já foi gerado fica gravado e a
   conversa fica livre para um novo envio.

O resultado gravado no nó é `{"type":"tool_result","tool_use_id","content":"<texto>","is_error":false}`,
com o texto inteiro (só o base64 de arquivos embutidos é trocado por uma referência). O que não couber na
janela é limpo ou truncado ao montar o pedido ao modelo, não no nó, e o modelo pode ler o resto com a
ferramenta interna `ler_trecho_resultado`. O Duo não vê nem precisa autorizar essa leitura.

## Pensamento

| Chave | Rota | O que o Duo faz |
|---|---|---|
| `ligar` | `PUT /api/conversas/{convId}/pensamento` com `{"ativo": true}` | Pede que o modelo raciocine nesta conversa. |
| `desligar` | o mesmo, com `{"ativo": false}` | Desliga. Ambos respondem `{"ok": true}`. |
| `stream` | eventos `content_block_delta` com `delta.type = thinking_delta` | Texto do raciocínio em `delta.thinking`, mostrado à parte da resposta. |

O raciocínio é gravado no nó como bloco `thinking` e relido por `conversas.ler.pensamento`.

## Anexos

- `upload`: `POST /api/upload`, multipart com o campo `file`. A resposta é `{"file_uuid": "<32 hex>"}`,
  e o Duo lê o id em `file_uuid`.
- `campoFiles` (`files`): onde o Duo coloca os ids enviados no corpo do `completion`.
- `campoAttachments` (`attachments`) e `attachment`: para texto, o Duo manda direto no corpo um objeto
  `{file_name, file_type, file_size, extracted_content}` por documento.
- `maxArquivoMB` (30) e `maxArquivos` (10): limites aplicados pelo Duo antes de enviar.

O servidor entrega o texto extraído ao modelo, delimitado. Imagens, quando o modelo não tem visão,
viram uma nota avisando que não puderam ser lidas.

## Opções do usuário

A lista `configuraveis` faz o Duo mostrar campos na tela do plugin. O valor escolhido é gravado no
caminho indicado do blueprint, e portanto vai no corpo de cada `completion`, em `opcoes`.

| Caminho | Rótulo | Tipo | Faixa / opções | Efeito |
|---|---|---|---|---|
| `enviar.corpo.opcoes.contexto` | Janela de contexto (tokens) | numero | 0–262144 | 0 = padrão do servidor (`config.json`). |
| `enviar.corpo.opcoes.temperatura` | Temperatura | numero | -1–2 | -1 = padrão do servidor. |
| `enviar.corpo.opcoes.ferramentas` | Modo de ferramentas | opcoes | `padrao` Padrão do servidor, `auto` Automático, `nativo` Nativo (tool-calling do modelo), `cercas` Emulado (blocos de texto) | `padrao` segue a configuração do servidor. |
| `enviar.corpo.opcoes.max_rodadas` | Máximo de rodadas de ferramentas | numero | 0–50 | 0 = padrão do servidor. |

Valor fora da faixa é ignorado pelo Duo (vale o padrão do blueprint). O servidor também aceita número
em texto e ignora valores inválidos, caindo no padrão da configuração.

## Janela de contexto (`janelaContexto`)

Em plugin, quem tem o histórico é o servidor, então o medidor e o painel do Duo mostram o que vem destas
rotas. Blueprint sem o campo fica como antes.

```json
"janelaContexto": {
  "ler": { "caminho": "/api/conversas/{convId}/janela", "campoContexto": "opcoes.contexto" },
  "excluir": { "metodo": "PUT", "caminho": "/api/conversas/{convId}/janela" },
  "compactar": { "caminho": "/api/conversas/{convId}/compactar" },
  "descompactar": { "metodo": "DELETE", "caminho": "/api/conversas/{convId}/compactar" }
}
```

**`GET /api/conversas/{id}/janela?modelo=&contexto=`** devolve a conta do próximo envio, a mesma do turno, antes de o motor
conferir: não chama o modelo. Com um documento denso no histórico, ele aparece dentro e `usado` fica acima do que irá de fato,
até a contagem do envio o tirar; o número exato do último envio vem em `ultimo_uso`. Com `contexto`, vale ele; sem, o do último
envio e, antes dele, o da configuração. `ler.campoContexto` diz de qual campo de `enviar.corpo` o Duo tira esse valor, para a
janela escolhida nas opções valer no medidor antes de enviar. Um Duo que não o conheça o ignora, e o medidor fica com o do último envio.

```json
{
  "limite": 32768, "reserva": 4096, "usado": 23154, "fonte": "real", "ultimo_uso": 21800,
  "secoes": { "sistema": 300, "ferramentas": 5700, "historico": 17154 },
  "itens": [
    { "id": "<uuid do nó>", "tipo": "turno", "rotulo": "analise o processo...", "tokens": 14400, "incluido": true },
    { "id": "<id da chamada>", "tipo": "resultado", "turno": "<uuid do nó>", "rotulo": "obter_texto_documento",
      "tokens": 4000, "incluido": false, "motivo": "corte" }
  ],
  "compactacao": null,
  "resumo": null
}
```

| Campo | O que é |
|---|---|
| `limite`, `reserva` | O tamanho da janela e a parte guardada para a resposta. O que cabe no pedido é a diferença. |
| `usado` | A estimativa do próximo envio, corrigida pelo fator, sem contar o texto dele; `secoes` soma exatamente isso. |
| `fonte` | `real` quando há uso medido pelo motor gravado na conversa, com este modelo (`usado` é a estimativa corrigida por ele); senão `estimado`. |
| `ultimo_uso` | Os tokens de entrada medidos no último envio, ou `null`. Com contagem, é o número exato do que foi. |
| `itens` | Os turnos e os resultados do ramo atual, na ordem da conversa; o ramo é o do último nó gravado. |
| `itens[].id` | O uuid do nó que abre o turno, ou o id da chamada: é o que vai em `excluidos`. |
| `itens[].tokens` | O que o item ocupa ou, se está fora, o que voltaria a ocupar. O de um turno não inclui os resultados, que têm itens próprios. |
| `itens[].motivo` | Por que está fora: `corte` (foi o servidor) ou `usuario`. Sem `incluido: false`, não vem. |
| `itens[].parcial` | `true` num turno de que só restou o pedido: é o 1º, fixado quando o Duo não manda `system`. |
| `excluidos` | A lista gravada do que o usuário tirou, inclusive o que não aparece em `itens`. Pelos `itens`, um resultado tirado à parte não se distingue do que saiu com o turno: é desta lista que o Duo monta o `PUT`, para religar o turno sem devolver o resultado. |
| `compactacao` | `null`, ou `{"estado": "rodando" \| "erro", "inicio", "erro"}`. O Duo consulta a cada ~3 s enquanto estiver `rodando`. |
| `resumo` | `null`, ou `{"texto", "tokens_antes", "tokens_depois", "ate", "modelo", "incluido"}`: o que substitui os turnos até o nó `ate`. Só vem se vale no ramo atual. Com `incluido: false` e `motivo: "corte"`, a janela o tirou, e ele não vai ao modelo. |
| `provisorio` | `true` antes do 1º envio da conversa: o servidor ainda não conhece o `system` nem as ferramentas, e o número sai baixo. |

Chamadas internas (`ler_trecho_resultado`, `carregar_ferramentas`) não são itens: o usuário nunca as viu, e o
peso delas entra no do turno.

**`PUT /api/conversas/{id}/janela`** com `{"excluidos": ["<id>", ...]}` grava a lista completa do que o
usuário tirou **do que está em `itens` agora**; lista vazia devolve tudo isso. Responde `{"ok": true}`. O que já
estava tirado e não aparece em `itens` (por trás de um resumo, ou em outro ramo) continua tirado sem precisar vir na
lista, e volta a poder ser religado quando aparecer. Id que não existe é ignorado. O efeito:

- um turno sai inteiro, com os resultados dele;
- um resultado vira a marca `[resultado retirado da janela pelo usuário]`, porque os motores nativos
  rejeitam chamada sem resposta;
- o corte respeita isso, e o que o usuário tirou não volta sozinho: nem com a janela sobrando, nem por
  `ler_trecho_resultado`, que recusa o pedido e também perde o que tinha lido antes dele;
- a árvore não muda, e a releitura da conversa no Duo continua igual;
- vale a partir da rodada seguinte, mesmo no meio de uma resposta.

Enquanto uma compactação roda, responde 409: o item tirado agora já pode ter ido ao modelo.

**`POST /api/conversas/{id}/compactar`** com `{"modelo": "...", "ate": "<uuid opcional>"}` **inicia** o resumo dos
turnos anteriores ao último, numa thread do servidor, e responde na hora `202 {"estado": "rodando"}`. Com `ate`, o
resumo vai até o fim do turno daquele nó, que tem de vir antes do último turno.

| Status | Quando |
|---|---|
| 202 | Começou. O resultado vem no `GET /janela`. |
| 400 | Não há turnos anteriores ao último, ou nada novo desde o resumo atual; `ate` ou `modelo` inválidos; modelo de nuvem com `permitir_modelos_nuvem` desligado. |
| 404 | A conversa não existe. |
| 409 | Há turno aberto ou compactação em andamento. Enquanto ela roda, novos turnos e `PUT /janela` também recebem 409. |
| 502 | O motor não respondeu ao consultar o modelo. |

O teto é de 30 minutos. Falha, resposta vazia, resumo cortado pelo limite do motor, resumo que não ficou menor que o
original, cancelamento ou teto não mudam nada na conversa, e o motivo aparece em `compactacao.erro`. Esse erro some ao tentar de novo, ao desfazer ou
no próximo envio. A compactação vive no servidor e sobrevive a recarregar a aba; reiniciar o servidor a interrompe,
sem efeito na conversa. O contexto usado é o do último envio.

O que vai ao modelo, sem ferramentas e sem raciocínio, é o texto dos turnos, sem o que o usuário tirou da janela. A
instrução manda preservar número do processo, IDs de documentos, datas, nomes e pedidos, marcar dúvidas e não
acrescentar nada. Se não couber de uma vez, vai por fatias de turnos, em passadas: cada fatia vira um resumo, e os
resumos são resumidos. A cada passada, o tamanho é conferido pela contagem do motor quando há.

Do envio seguinte em diante, os turnos até `ate` vão ao modelo numa só mensagem, que começa com `Resumo da conversa
até aqui, gerado pelo modelo; confira nos autos antes de citar:`. A árvore e a releitura no Duo ficam iguais, e o
modelo só conhece aquele trecho pelo resumo: para ele ver o original, é preciso desfazer. Quem volta a um ponto anterior e segue por outro ramo
conversa sem o resumo. Compactar de novo resume o resumo junto com os turnos novos. Entre os turnos anteriores, ele é o
último a sair da janela; se sair, o `GET` diz (`resumo.incluido`), e a resposta avisa.

**`DELETE /api/conversas/{id}/compactar`** cancela ou desfaz, com o mesmo verbo:

| Situação | O que faz | Resposta |
|---|---|---|
| Compactação em andamento | Cancela. | `{"ok": true, "cancelada": true}` |
| Terminou (pronta ou com erro) e nenhum `GET /janela` viu o desfecho | Nada: é um cancelamento que chegou tarde, e o resumo fica. O erro, se houve, some. | `{"ok": true, "cancelada": false, "terminou_antes": true}` |
| Nenhuma, ou o desfecho já foi visto | Volta aos turnos originais. | `{"ok": true}` |

O resumo sai junto com a conversa, sem resíduo.

## Campos opt-in (`destinoLocal`, `{sistema}`, `campoCatalogo`)

São extensões que o motor do Duo só aplica quando o blueprint as declara; um blueprint sem elas
continua funcionando como antes.

- **`destinoLocal`** (booleano na raiz): declara se o texto da conversa permanece na máquina do usuário.
  O servidor calcula o valor ao gerar o arquivo: `true` somente quando a URL do backend é loopback
  (`localhost`, `127.0.0.0/8`, `::1`) **e** `permitir_modelos_nuvem` está desligado. Com `true` (e a
  `baseURL` também em loopback, que o Duo confere), a única mudança é na confirmação de envio de
  processo em segredo de justiça: o Duo deixa de chamar o provedor de "serviço externo". Nenhuma outra
  proteção é dispensada. O modo simulado (backend de testes) também gera `true`. O Duo guarda a cópia
  importada: quando o valor muda, é preciso reimportar (o banner do servidor avisa).
- **`{sistema}`** (no `enviar.corpo.system`): o Duo preenche com as instruções de sistema do turno
  (regras da extensão). O servidor monta o sistema nesta ordem: o seu prompt padrão, as instruções
  do projeto, o catálogo de ferramentas (só no modo cercas) e, por último, estas instruções. Num Duo
  sem o ajuste a variável não existe e o prompt de sistema do Duo **não chega** ao servidor (o Duo
  antigo o descarta no modo plugin): só o preâmbulo do processo vai, no texto do 1º pedido. Por isso,
  sem `system`, a janela fixa o texto desse 1º pedido e o `prompts/sistema.md` vale sozinho.
- **`campoCatalogo`** (`ferramentas.campoCatalogo = "catalogo"`): nome do campo do corpo onde o Duo
  envia o catálogo de ferramentas, no formato `{"favoritas": [nomes], "resumos": {nome: resumo}}`. Com ele,
  o servidor manda ao modelo só as favoritas e uma meta-ferramenta para carregar as demais sob demanda.
  A meta-ferramenta é resolvida no servidor (chamada e resultado marcados `interno`) e nunca aparece no
  SSE nem na leitura da conversa; as ferramentas carregadas ficam em `ferramentas_carregadas`.
