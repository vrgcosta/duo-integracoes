# Plugins públicos do Duo

Esta pasta é o local de publicação dos JSONs de plugins do Duo, junto do template do
servidor. O servidor não precisa estar ligado para o Duo verificar atualizações: a
consulta busca apenas o arquivo público no GitHub.

## Plugins disponíveis

Formato **1**. ChatGPT web está na **1.3.0**, Claude web na **1.3.0** e Gemini web na
**1.5.0**, os três com extensão mínima **3.71.146**; os de jurisprudência estão na **1.0.2**,
com extensão mínima **3.71.141**. As definições foram validadas pelos schemas e pelo
verificador de compatibilidade do Duo nessa versão do código. Essa validação não executa os
serviços autenticados dos provedores.

A extensão mínima **3.71.146** é a versão prevista para a entrega que lê a seção `erros`;
confira o número quando essa versão da extensão for publicada e ajuste os três JSONs se
ela sair com outro número.

ChatGPT web **1.1.3**, Claude web **1.1.1** e Gemini web **1.1.0** declaram, na seção
`erros`, como reconhecer as falhas de cada provedor. Com isso, o Duo diz o que aconteceu e
o que fazer: sessão expirada (abrir o site e entrar de novo), limite de uso com a hora de
liberação e se o limite é do modelo ou da conta (trocar de modelo só aparece quando resolve),
bloqueio da conta ou do acesso (sem insistir) e verificação do Cloudflare. No ChatGPT web,
os avisos da página (`pagina.avisos`) passaram para essa seção, como regras de diálogo. As
versões da extensão sem esse recurso não instalam essas versões.

ChatGPT web **1.1.4**, Claude web **1.1.2** e Gemini web **1.1.1** dizem de onde vem cada
regra de bloqueio (`origem`: a recusa do envio, o erro no stream ou o aviso da página), para
a frase nunca casar com o texto de uma resposta do modelo. O aviso de limite do ChatGPT em
português também reconhece "atingiu o seu limite".

No ChatGPT web, "unusual activity" aparece em dois lugares, com papéis diferentes, e os dois
ficam: `pagina.rede.envioDireto.recusa` decide o caminho do envio (na recusa 403 com essa
frase, a rede fica pausada por 60 minutos e o turno vai pela tela) e a regra `bloqueio` de
`erros` dá o tipo do erro, que só aparece se a tela também falhar. Não há aviso em dobro: a
pausa não mostra nada ao usuário.

A **1.1.0** do Claude web declara quantas sessões o plugin roda ao mesmo tempo: 4 por
padrão e até 6, valor que o usuário ajusta em **Sessões ao mesmo tempo**, nas
configurações do plugin. Declara também o prazo que o claude.ai dá ao resultado de uma
ferramenta (4 minutos): perto dele, os subagentes devolvem o que ficou pronto, em vez de o
resultado inteiro se perder. As versões da extensão sem esses recursos ignoram os dois
campos, e o plugin funciona como na 1.0.2.

A **1.2.0** do Claude web reconstrói as ferramentas ao reabrir uma conversa
(`conversas.ler.ferramentas`): cada chamada (`tool_use`) e o resultado dela (`tool_result`),
gravados pelo claude.ai na resposta do assistente, voltam como ferramentas usadas, na ordem em
que aconteceram, em vez de a releitura mostrar só o texto.

A **1.1.0** do ChatGPT web envia a mensagem pela rede, de dentro da aba logada (sessão,
verificação e anexos no mesmo protocolo da página), sem depender do editor nem da aba
visível, e lê a resposta consultando a conversa. Também lista os modelos do seletor da
conta. Se o envio pela rede for recusado antes de o ChatGPT aceitar a mensagem, o Duo
usa a tela como reserva.

A **1.1.2** do ChatGPT web declara quantas sessões o plugin roda ao mesmo tempo: 2 por
padrão e até 5, valor que o usuário ajusta em **Sessões ao mesmo tempo**, nas configurações
do plugin. Cada turno em andamento usa uma aba do ChatGPT aberta pelo Duo, e o limite
acompanha as 5 abas que o Duo abre por site. As versões da extensão sem esse recurso
ignoram o campo.

A **1.2.0** do Gemini web lê a lista de modelos da própria conta (`formulario.catalogo`, pela chamada RPC de catálogo da página): o nome no seletor, o id e a família de cada modelo vêm de lá, e um modelo renomeado pelo Google aparece com o nome novo sem mudar o plugin. A lista `modelos` fica só como reserva para quando o catálogo não responde. `catalogo.sobrepor` corrige o nome que o catálogo traz errado para um id (hoje o 4 Argon, que o catálogo chama de 4 Carbon), `porFamilia` leva os campos próprios do modo do Argon e `idNoMenu` diz o id do item no menu para o caminho da tela.

A **1.3.0** do Gemini web mostra o raciocínio do modelo enquanto a resposta chega
(`stream.quadros.raciocinio`): o resumo do raciocínio que o Gemini manda junto da resposta
aparece no bloco de raciocínio do Duo (conferido em 2026-11-12 pelo caminho da rede, com o
4 Hydrogen).

A **1.4.0** do Gemini web traz o histórico do Gemini para o Duo: lista as conversas (as
fixadas primeiro), busca pelo texto delas (inclusive pelo número do processo no título),
renomeia e exclui, lista os Gems e as conversas de cada um, tudo pelas chamadas RPC da
própria página (`conversas.*.rpc`; a data de cada conversa vem em segundos,
`atualizadoEmUnidade`). Ao reabrir uma conversa, o raciocínio gravado de cada resposta volta
junto (`ler.turnos.pensamento`). A conta Google logada na aba passa a identificar os vínculos
processo→conversa (`identidade` com `formulario.contexto`, lido da própria página), e trocar
de conta não mistura as conversas. Conferido em 2026-11-12 na aba logada. Ainda não: uma
conversa nova enviada com um Gem escolhido nasce fora do Gem (o envio não leva o Gem).

A **1.5.0** do Gemini web cria a conversa nova dentro do Gem escolhido: o envio pela rede leva
o id do Gem na posição do pedido em que a própria página o manda (`{projetoId}` no molde do
`StreamGenerate`; conferido em 2026-11-12 enviando pela página dentro de um Gem e depois pela
rede, com a conversa listada entre as do Gem). Pela tela, que é só a reserva, a conversa nova
ainda nasce fora do Gem. As listas de conversas (geral, do Gem e a busca) seguem as páginas
seguintes da chamada RPC (`rpc.paginas`: o marcador da próxima página vem na resposta e volta
na chamada), até 5 páginas de 100 conversas na lista e 3 na busca.

A **1.3.0** do Claude web lê a cota que o claude.ai informa ao fim de cada resposta
(`stream.limite`, evento `message_limit`): vale a janela mais usada (5 horas ou 7 dias), em
porcentagem, e o estado dela vira a severidade do Duo (dentro do limite, perto, esgotado). Os
trabalhos do Duo Forge só param quando o estado é de alerta ou quando a janela passa do teto
(90%); dentro do limite, seguem.

A **1.3.0** do ChatGPT web identifica a conta logada (`contexto` com a leitura da conta,
`/backend-api/me`), e trocar de conta não mistura os vínculos processo→conversa. Os vínculos
gravados antes, sem conta, passam para a primeira conta identificada. O ChatGPT não informa a
cota de mensagens nem o uso de tokens no stream (conferido em 2026-11-12), então o plugin não
declara `stream.limite` nem `stream.usage`.

A **1.2.0** do ChatGPT web traz o raciocínio de volta ao reabrir uma conversa
(`conversas.ler.pensamento`): o ChatGPT grava o raciocínio numa mensagem própria, antes da
resposta, e o Duo junta essa mensagem ao balão da resposta (`fundir`), como aparece ao vivo.

A **1.1.0** do Gemini web tem dois caminhos, e o próprio plugin escolhe qual vai primeiro
(`formulario.principal`). O outro fica de reserva para quando o primeiro falha antes de a
mensagem sair. Oferece 4 Hydrogen (padrão; o antigo 3.8 Flash, renomeado pelo Google em 2026-11-12 com o mesmo id), 3.1 Pro, 4 Argon e 3.5 Flash-Lite, com raciocínio
Baixo, Médio (padrão) ou Alto.

- **Rede** (principal): manda o pedido pela API interna da página (`StreamGenerate`), sem
  depender da aba visível. **Todos os modelos vão pela rede com o modelo e o nível
  escolhidos**, inclusive o 4 Hydrogen (conferido em 2026-11-12: 4 Hydrogen, ex-3.8 Flash, e 4 Argon
  responderam pela rede com o modelo pedido). O 4 Argon leva os campos próprios do modo dele
  no pedido. O Duo confere na resposta qual modelo a serviu: se o Gemini responder com outro,
  o turno falha dizendo qual respondeu, em vez de mostrar a resposta errada. Nada vai no
  "modelo padrão da conta" no lugar do escolhido.
- **Tela** (reserva): escolhe o modelo e o nível no menu do próprio Gemini, cola os anexos
  no editor e lê a resposta na conversa quando ela fica pronta.

Nos dois caminhos, quando o Gemini devolve a resposta pronta de falha ("Sou um modelo de
linguagem..."), o Duo manda "continue" na mesma conversa (até duas vezes); se ainda assim
não vier resposta, o turno termina com o aviso de falha. O limite de uso do Gemini é da
conta (o 3.5 Flash-Lite não conta nele): quando ele acaba, o Duo consulta a página de uso
do Gemini para mostrar a hora de liberação. Se a resposta não começar em 60 s, o turno é
dado como travado.

O pedido pela rede leva o tier da conta fixo em 2 (Google AI Pro), no cabeçalho
`x-goog-ext-525001261-jspb`. Esse valor foi validado só numa conta AI Pro; numa conta
gratuita, Plus ou Ultra, se a rede falhar, troque `"principal": "rede"` por
`"principal": "tela"` no JSON e importe de novo, e avise para conferirmos o valor.

Os arquivos estão na branch `main` do repositório público `vrgcosta/duo-integracoes`.
Copie o link **Raw** para o campo **Link do plugin** da importação, ou baixe
o JSON para importar por arquivo:

| Plugin | Definição | Raw para importar e atualizar |
|---|---|---|
| Claude web | [claude-web.blueprint.json](claude-web.blueprint.json) | [Importar Claude](https://raw.githubusercontent.com/vrgcosta/duo-integracoes/main/plugins/claude-web.blueprint.json) |
| ChatGPT web | [chatgpt-web.blueprint.json](chatgpt-web.blueprint.json) | [Importar ChatGPT](https://raw.githubusercontent.com/vrgcosta/duo-integracoes/main/plugins/chatgpt-web.blueprint.json) |
| Gemini web | [gemini-web.blueprint.json](gemini-web.blueprint.json) | [Importar Gemini](https://raw.githubusercontent.com/vrgcosta/duo-integracoes/main/plugins/gemini-web.blueprint.json) |
| Jurisprudência TJBA | [buscar-juris-tjba.blueprint.json](buscar-juris-tjba.blueprint.json) | [Importar TJBA](https://raw.githubusercontent.com/vrgcosta/duo-integracoes/main/plugins/buscar-juris-tjba.blueprint.json) |
| Jurisprudência STJ/STF | [buscar-juris-stj-stf.blueprint.json](buscar-juris-stj-stf.blueprint.json) | [Importar STJ/STF](https://raw.githubusercontent.com/vrgcosta/duo-integracoes/main/plugins/buscar-juris-stj-stf.blueprint.json) |

Os IDs foram preservados para reconhecer as instalações existentes. Quem já tem um
desses plugins pode colar o Raw correspondente no campo **Link de atualização** do plugin
e depois usar **Atualizar**.
As ferramentas de jurisprudência mantêm as chaves de configuração `JURIS_VRGC_URL`,
`JURIS_VRGC_API_KEY`, `FLOW_VRGC_URL` e `FLOW_VRGC_API_KEY`: os valores continuam locais.
Os providers web usam a sessão do usuário nos respectivos serviços.

### Migrar uma instalação da publicação privada de revisão

A versão 1.0.1 preserva os IDs, receitas de execução e chaves de configuração da
publicação 1.0.0; muda a origem de atualização para este repositório público. Quem
instalou da branch de revisão do repositório anterior deve colar o Raw correspondente
desta tabela no campo **Link de atualização** e depois usar **Atualizar**. O Duo mantém a origem
aprovada localmente e não troca essa origem silenciosamente. Os nomes dos arquivos
não levam datas para que os endereços sejam mantidos entre publicações.

## Instalação e atualização

Cada plugin publicado deve ter um arquivo próprio nesta pasta. No GitHub, abra o JSON
e copie o link do arquivo ou o botão **Raw**. No Duo, abra **Configurações → Plugins →
Importar plugin** e cole o endereço em **Link do plugin**. Depois disso, o Duo verifica
novas versões ao abrir o gerenciador e oferece **Verificar** e **Atualizar** no detalhe
do plugin.

Para um arquivo `plugins/meu-plugin.json` na branch `main`, os endereços são:

- Arquivo: `https://github.com/vrgcosta/duo-integracoes/blob/main/plugins/meu-plugin.json`
- Raw: `https://raw.githubusercontent.com/vrgcosta/duo-integracoes/main/plugins/meu-plugin.json`

Esses endereços são exemplos: `meu-plugin.json` ainda não foi publicado. Repositório e
arquivo precisam estar públicos. Se usar outra branch, ajuste o endereço. Para branches
com barra no nome, use o endereço fornecido pelo botão Raw.

Plugins já instalados por arquivo podem receber o endereço no campo **Link de
atualização**, no detalhe do plugin. O Duo confere o id e o tipo ao sair do campo; não substitui a definição instalada. A atualização
continua dependendo do aceite do usuário.

## Metadados de publicação

Acrescente estes campos à definição completa existente (o trecho abaixo não é um plugin
completo):

```json
{
  "versao": "1.0.0",
  "versaoFormato": 1,
  "compatibilidade": {
    "extensaoMinima": "3.71.141"
  },
  "atualizacao": {
    "url": "https://raw.githubusercontent.com/vrgcosta/duo-integracoes/main/plugins/meu-plugin.json"
  }
}
```

A versão mínima acima é ilustrativa: informe a primeira versão publicada da extensão
em que os recursos desse plugin foram testados. Para publicar:

1. Mantenha o `id` e o tipo do plugin; eles identificam a instalação.
2. Incremente `versao` (três números, por exemplo `1.0.1`; sem prefixo `v`).
3. Se usar um recurso novo da extensão, eleve `compatibilidade.extensaoMinima`.
4. Só eleve `versaoFormato` quando o contrato do interpretador mudar de forma incompatível.
5. Publique primeiro a extensão que oferece o suporte, depois o JSON que o exige.

`compatibilidade.extensaoMaxima` é opcional e inclusiva. Serve para um limite conhecido;
não precisa acompanhar a versão mais recente da extensão. Se a publicação for
incompatível com a extensão instalada, o Duo explica a exigência e bloqueia a instalação.
Ele mantém a definição instalada e permite reverter a última troca.

O mecanismo de importação por URL/atualização depende da entrega correspondente no
PJe✚R. Nas versões anteriores, a importação por arquivo continua funcionando quando o
contrato do plugin for compatível.

## Blueprint do servidor local

**Não publique `servidor/dados/duo-provedor-local.blueprint.json`.** Esse arquivo é gerado com a
chave de acesso de cada instalação e deve continuar em `dados/`, fora do Git.

O molde `servidor/blueprint/modelo.json` também não é um plugin pronto para importação por URL:
contém marcadores de porta, chave e destino local que o servidor precisa preencher.
Por enquanto, para o servidor local, siga a instalação pelo arquivo gerado descrita no
[README do servidor](../servidor/README.md). Uma publicação atualizável do servidor local precisa primeiro separar
a definição pública das credenciais e configurações locais, preservando também a regra
de destino local/nuvem. Não substitua o arquivo gerado por um JSON público sem essa etapa.

Outros plugins que já sejam definições públicas completas podem ser publicados nesta
pasta. Não inclua tokens, cookies, chaves pessoais ou dados de processos nos arquivos.
