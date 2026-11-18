# Plugins públicos do Duo

Esta pasta é o local de publicação dos JSONs de plugins do Duo, junto do template do
servidor. O servidor não precisa estar ligado para o Duo verificar atualizações: a
consulta busca apenas o arquivo público no GitHub.

## Plugins disponíveis

Formato **1**. ChatGPT web está na **1.5.0**, Claude web na **1.3.2** e Gemini web na
**1.6.3**, os três com extensão mínima **3.71.146**; os de jurisprudência estão na **1.0.4** (STJ/STF) e na **1.0.2** (TJBA),
com extensão mínima **3.71.141**. As definições foram validadas pelos schemas e pelo
verificador de compatibilidade do Duo nessa versão do código. Essa validação não executa os
serviços autenticados dos provedores.

ChatGPT web **1.5.0** faz o modelo ler aos poucos. `ferramentas.maxChamadasPorRodada: 4`: as chamadas além de quatro
numa mesma resposta não rodam, e o modelo as pede de novo depois de analisar as outras (ajustável na tela do plugin, em
"Ferramentas por vez"). `ferramentas.limiteInline` cai de 140 mil para 80 mil caracteres: com peças reais, o ChatGPT
recusou pela rede mensagens de cerca de 139 mil caracteres (em 14/11 e em 18/11/2026, num subagente que pediu dez peças
de uma vez); os 140 mil vinham de uma medição com texto de preenchimento, que gasta menos tokens. Pela tela do site,
texto de lei de 230 mil caracteres passou (18/11/2026), mas o Duo envia pela rede. O que passa do limite não vai mais
para um anexo: fica para a próxima mensagem, e o modelo o recebe ao pedir de novo, sem nova consulta (entrega em lotes
da extensão). Extensões sem esse recurso ignoram `maxChamadasPorRodada` e seguem mandando o excedente num anexo; a
extensão mínima não muda.

Gemini web **1.6.3**: a regra do falso limite de ferramentas olha só o começo da resposta (`inicioCaracteres: 400`),
qualquer que seja o tamanho dela. Antes, a frase que abria uma análise parcial de uns 3 mil caracteres passava do teto
de 2000 (`maxCaracteres`) e chegava ao Duo como resposta final (visto em 18/11/2026, numa conta gratuita). A regra
também reconhece "0 remaining" e "sistema de ferramentas ... bloqueado". Extensões sem esse recurso ignoram o campo e
seguem com o `maxCaracteres`.

ChatGPT web **1.3.3** deixa de declarar `pagina.rede.envioDireto.modelos.reserva`: o motor não manda mais o
modelo genérico "auto" (que o provedor resolvia por conta própria, trocando o modelo e tirando o raciocínio sem
aviso). O modelo sai sempre do catálogo da conta, pelo nome; sem modelo que sirva, o envio pela rede não sai e diz por quê.

Gemini web **1.6.2** tira da fonte da medição de leitura o número do processo usado nela: a informação do
plugin é genérica.

Gemini web **1.6.1** corrige a regra do falso limite de ferramentas: ela casava o argumento `"limite"` de um pedido
de ferramenta com o rótulo `duo-ferramenta` da cerca seguinte, e o Duo tratava o pedido como o falso aviso de limite
(não rodava as ferramentas e reenviava "Não há limite..."). Os trechos livres da regra não atravessam mais aspas,
chaves nem crases (texto de JSON e de cerca).

ChatGPT web **1.4.0** e Gemini web **1.6.0** declaram quanto o provedor lê de um anexo de uma vez (`anexos.limiteLeitura`,
com `medidoEm` e `fonte`; é só informação, nada bloqueia). O Gemini web: PDF de cerca de 100 a 130 páginas (o Duo usa 100 como
teto) e arquivo de texto de cerca de 108 mil tokens (380 mil caracteres no dado anexos.limiteLeitura.caracteres; a tela mostra em tokens), medidos em 14/11/2026 com os autos de um processo do PJe; acima
disso o Duo divide o processo em volumes. `anexos.leituraParcial.padroes` traz a frase com que o Gemini avisa, no texto da
resposta, que parou de ler ("105 a 300 não foram carregadas"). O ChatGPT web leu 349 páginas inteiras (cerca de 20 minutos,
com OCR das digitalizadas), sem teto de páginas; o limite de entrada por modelo vem do catálogo da conta
(`pagina.rede.envioDireto.modelos.campoLimiteEntrada: "max_tokens"`), lido na hora de enviar. Extensões antigas ignoram
esses campos (não mostram nem usam o limite); a extensão mínima não muda.

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

Os prazos do turno pela aba ficam em `erros.espera`. Além de `primeiroDadoSeg` (do aceite ao primeiro
pedaço da resposta, padrão 60 s), o motor lê `aceiteSeg` (de quando a pergunta sai até o provedor
aceitá-la, padrão 20 s; estourado, o Duo confere a conversa antes de reenviar) e `respostaMaxMin` (do
aceite ao fim da resposta, padrão 30 min, de 1 a 120). Com `respostaMaxMin` declarado, ele vale no lugar
do `esperaMaxMin` de `formulario.tela`, `formulario.envio` e `envioSocket`, que ficam obsoletos. Sem
ele, vale o maior `esperaMaxMin` declarado: o Copilot e o Gemini web, que declaram 15, seguem com 15
minutos. Extensões antigas ignoram os campos novos, e nenhum plugin precisou mudar.

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
do plugin. Os turnos simultâneos dividem uma única aba do ChatGPT aberta pelo Duo (enviados
pela rede, cada um na sua conversa); quem limita quantos rodam juntos é esse número. As
versões da extensão sem esse recurso ignoram o campo.

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

A **1.3.2** do ChatGPT web e a **1.5.2** do Gemini web sobem o limite dos resultados de ferramenta no corpo da
mensagem (`ferramentas.limiteInline`) de 24 mil para 140 mil e 400 mil caracteres. Medido em 2026-11-14, texto numa
mensagem pela rede: o ChatGPT web lê inteiro até cerca de 160 mil e o servidor recusa 200 mil (413
`message_length_exceeds_limit`); o Gemini web lê inteiro ao menos 750 mil. Com 24 mil, o que passava ia para o
arquivo anexado, que o site não lê inteiro, e o modelo pedia de novo as peças que ficaram lá.

A **1.5.1** do Gemini web diz quais recusas da chamada RPC significam "conversa que não existe"
(`rpc.envelope.naoEncontrado`, com o código lido em `codigoErro`): a leitura de uma conversa
que não existe para a conta logada responde com o código 5 (NOT_FOUND), e o Duo passa a tratá-la como o
404 dos outros plugins, limpando o vínculo do processo com a conversa que não abre mais.
Conferido em 2026-11-12 na aba logada: id de conversa inexistente dá 5 (erro 1167, o mesmo da conversa apagada); id malformado
dá 3, que segue como falha comum.

A **1.0.0** do Microsoft 365 Copilot web (`copilot-m365-web.blueprint.json`) envia pela rede, de
dentro da aba logada do m365.cloud.microsoft/chat, sem o editor da página: para cada turno a aba
abre uma conexão WebSocket própria com o chat do Copilot (`pagina.rede.envioSocket`), manda o
pedido montado pelos moldes do plugin e repassa o que chega, decodificado por
`stream.registros`. A credencial é a que a própria página manda nos pedidos dela (audiência
`https://substrate.office.com/sydney`), guardada só em memória na aba; se ela ainda não apareceu
ou venceu, a aba recarrega uma vez e retoma o envio. O modelo vai no pedido (`tone`): a lista vem
da conta, pelo mesmo pedido que a página usa para montar o menu (`GetAgentList`), então uma conta
que tem Claude além do GPT mostra os dois. Os turnos simultâneos dividem a aba (2 por padrão, até
5). Ao reabrir uma conversa, o Duo a lê da página (`/chat/conversation/{id}`). Conferido em
2026-11-13 na aba logada de uma conta Copilot Chat (Básico): conversa nova e continuação na mesma
conversa, e três turnos ao mesmo tempo com modelos diferentes (Automático, Resposta rápida e
GPT-5.6 Sol), cada um com a própria resposta. Ainda não: lista, busca, renomear e excluir conversas
(ver `contrato/alinhamento.json`) e anexos.

A **1.0.0** do Microsoft 365 Copilot web em copilot.cloud.microsoft (`copilot-mcloud-web.blueprint.json`)
é o mesmo plugin para a conta que a Microsoft levou para o endereço novo: em 2026-11-17, numa conta
Copilot Chat (Básico), `m365.cloud.microsoft` (a raiz e o `/chat/`) passou a redirecionar para
`copilot.cloud.microsoft/chat` (`redirfrom=mcmToCcm`). Com o plugin do m365, a aba que o Duo abre termina
fora da origem do `baseURL`: não recebe os scripts do Duo, o envio para em "a aba do provedor não carregou"
e cada tentativa deixa outra aba fixada. Mudam o endereço (`baseURL`, `site`, `icone`) e a identificação
do cliente no pedido: `clientPlatform` `ccmcopilot-web` e `clientEntrypoint` `ccmcopilot-officeweb`, que a
página nova monta a partir do `referrer` dela (`ccmcopilot`; no m365 era `mcmcopilot`). Conferido na
origem nova, sem enviar mensagem: catálogo de modelos (`GetAgentList`), credencial
`https://substrate.office.com/sydney`, hub `m365Copilot/Chathub` e `sydneyWebScenario`. Ainda não: o envio
de ponta a ponta pelo Duo. Tem `id` próprio e instala ao lado do plugin do m365.

A **1.0.1** do Copilot web em copilot.cloud.microsoft sobe o limite dos resultados de ferramenta no corpo da
mensagem (`ferramentas.limiteInline`) de 24 mil para 120 mil caracteres. O envio por WebSocket ainda não leva
anexos, e com 24 mil o que passava ia para o arquivo anexado: a rodada falhava com "anexos não declarados no
envio por WebSocket" (visto em 2026-11-17 com sete leituras de documento, cerca de 20 mil tokens). O teto
vem do editor da própria página, que aceita até 128 mil caracteres (`charCountLimit`, lido no código da
página em 2026-11-17); 120 mil deixa margem para o envelope dos resultados. Não foi medido no servidor.

A **1.0.2** do Copilot web em copilot.cloud.microsoft sobe o `limiteInline` para 600 mil caracteres, agora
medido no servidor (2026-11-17, conta Copilot Chat Básico, pelo mesmo WebSocket do plugin, com resultados de
ferramenta fictícios em blocos `duo-resultado`): o limite de 128 mil é só do editor da página. Até 802 mil
caracteres o Copilot guarda a mensagem inteira e o modelo lê tudo (Resposta rápida e Automático contaram os
blocos e citaram o último parágrafo do documento do meio e do último). Com 1,6 milhão ele guarda a mensagem,
mas falha ao responder (`InternalError`, "Desculpe, não consegui responder a isso"), nos dois modelos: acima
do teto não há recusa por tamanho, e sim essa falha. 600 mil deixa margem para texto real, que tem acentos e
gasta mais tokens por caractere que o texto do teste. O registro final do stream também traz a cota restante
de cada modelo (`item.throttling.metering`), ainda não lida pelo plugin.

A **1.0.3** do Copilot web em copilot.cloud.microsoft passa a se chamar só **Copilot** no Duo (`rotulo`). O
`id` não muda: a instalação, os vínculos processo→conversa e as configurações continuam os mesmos.

A **1.0.4** do Copilot declara quanto ele lê de uma vez (`anexos.limiteLeitura`): 600 mil caracteres, o
mesmo teto do `limiteInline`, medido em 2026-11-17 (ver a 1.0.2). Com ele, o Duo divide o processo inteiro em
volumes que cabem numa mensagem, em vez de mandar tudo num resultado só, que acima de cerca de 800 mil
caracteres faz o Copilot falhar ao responder. A tela do plugin mostra a medição. Até a 1.1.0, o plugin ainda
não enviava arquivo (o envio por WebSocket do Duo não subia arquivos; o Copilot lê PDF), e o Duo usava o texto
do processo ou de cada peça.
As extensões sem esse campo o ignoram, e o processo vai num arquivo só, como antes.

A **1.1.0** do Copilot envia PDF (`pagina.rede.envioSocket.anexos`). Para cada arquivo, a aba do Copilot abre
uma sessão de upload no OneDrive da conta (Microsoft Graph, `createUploadSession` na pasta especial do
Copilot), com a credencial que a própria página manda ao Graph; os bytes vão sem credencial, em partes de
10 MiB, para o endereço que a sessão devolve; o Duo lê o item (`sharepointIds`) e a mensagem do turno leva a
anotação `LocalFile` dele, com o `id` `SPO_<base64 de siteId,webId,listId>_<id do item>`, a URL e o nome. O
arquivo fica na pasta “Arquivos de Chat do Microsoft Copilot” do OneDrive, como quando se anexa pelo site.
Conferido em 2026-11-17 com PDFs fictícios: o modelo leu o código escrito dentro de um PDF subido e anotado
sem a interface do site (e não leu quando a anotação levava só a URL). O texto continua indo dentro da
mensagem, a leitura medida inteira; o quanto o Copilot lê de um PDF não foi medido. As extensões sem esse
recurso ignoram `anexos` e seguem mandando só texto: o pedido do turno não muda quando não há arquivo.

A **1.1.1** do Copilot passa a mostrar na tela os tamanhos medidos em tokens, como o resto do Duo (cerca de
229 mil tokens lidos inteiros, 171 mil de teto), em vez de caracteres. O teto que divide o processo em volumes
não muda (`caracteres: 600000`, que o Duo converte).

A **1.1.2** do Copilot e a **1.0.1** do Microsoft 365 Copilot web tiram da resposta a âncora de citação do
Copilot (`stream.limpezaTexto`). Quando o modelo usa uma fonte (o arquivo enviado ou uma busca na web), ele
marca o trecho com o identificador dela, como `turn1search1` ("turno 1, resultado de busca 1"), e a página do
Copilot troca a marca por uma nota numerada com link. Pelo WebSocket que o Duo usa (`Chathub`), a âncora chega
como HTML, `<cite>turn1search1</cite>`, fica guardada assim na conversa e aparecia no Duo, em itálico, no fim de
cada parágrafo. A conversa feita pela própria página (o socket novo dela, `StreamHub`) traz o formato da OpenAI,
com caracteres de uso privado (`U+E200` `cite` `U+E202` `turn1search1` `U+E202` `turn1search6` `U+E201`), que a
regra antiga já tirava e continua tirando. A regra com o espaço na frente (` <cite>`) leva junto o espaço que
sobraria antes da âncora. Conferido em 2026-11-18 numa conta Copilot Chat (Básico): uma conversa aberta pelo
Duo com o PDF anexado e uma pergunta de teste com busca na web feita na página. A leitura da conversa ao
reabrir passa pela mesma limpeza, então as conversas antigas também aparecem sem a âncora.

A **1.3.0** do Claude web lê a cota que o claude.ai informa ao fim de cada resposta
(`stream.limite`, evento `message_limit`): vale a janela mais usada (5 horas ou 7 dias), em
porcentagem, e o estado dela vira a severidade do Duo (dentro do limite, perto, esgotado). Os
trabalhos do Duo Forge só param quando o estado é de alerta ou quando a janela passa do teto
(90%); dentro do limite, seguem.

A **1.3.1** do ChatGPT web declara o formato do quadro do WebSocket por onde o ChatGPT às
vezes continua a resposta (`pagina.rede.quadroWs`: o caminho do trecho do stream e os caminhos
do id da conversa) e o campo da conversa no stream do envio (`campoConversaStream`). Antes, esse
formato era fixo na extensão; agora o motor só segue o WebSocket com ele declarado, e cada
trecho vai ao turno da conversa dele, mesmo com vários turnos na mesma aba. A extensão que ainda
não lê esses campos os ignora; com a extensão nova e o plugin 1.3.0, a resposta continua chegando
pelo envio e pela leitura da conversa, só sem a continuação pelo WebSocket.

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
| Microsoft 365 Copilot web | [copilot-m365-web.blueprint.json](copilot-m365-web.blueprint.json) | [Importar Copilot](https://raw.githubusercontent.com/vrgcosta/duo-integracoes/main/plugins/copilot-m365-web.blueprint.json) |
| Copilot (copilot.cloud.microsoft) | [copilot-mcloud-web.blueprint.json](copilot-mcloud-web.blueprint.json) | [Importar Copilot (copilot.cloud)](https://raw.githubusercontent.com/vrgcosta/duo-integracoes/main/plugins/copilot-mcloud-web.blueprint.json) |
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

## Estrutura da pasta

- `plugins/`: só os plugins (`*.blueprint.json`) e este README. Os plugins ficam aqui porque o link de
  atualização de cada instalação aponta para `plugins/<nome>.blueprint.json`: mudar um de lugar quebra a
  atualização de quem já o instalou.
- `plugins/contrato/`: o contrato do motor (`contrato-motor.json`, gerado pelo PJe✚R) e o alinhamento de
  cada plugin com ele (`alinhamento.json`).
- `plugins/testes/`: o teste dos plugins.

O teste falha se aparecer na raiz de `plugins/` outro arquivo que não seja plugin ou este README.

## Teste dos plugins

Antes de publicar uma mudança em qualquer plugin, rode na raiz do repositório:

```bash
py -m unittest discover -s plugins/testes -v
```

O teste confere cada JSON contra `contrato/contrato-motor.json`, o contrato do motor do Duo, gerado no
PJe✚R por `apps/extension/scripts/exportar-contrato-plugins.ts` (não edite à mão). O contrato diz,
campo a campo, a quem ele se aplica (`escopo`: `comum` a todos, ou um mecanismo como `pagina`,
`servidor`, `emulacao`, `nativo`, `formulario`, `quadros`, `sse`) e se precisa estar lá
(`nivel`: `obrigatorio`, `recomendado`, `opcional`). O teste falha:

- com campo que o motor não conhece (erro de digitação, ou campo que saiu do motor);
- com campo de um mecanismo que o plugin não usa (cópia errada de outro plugin);
- com campo obrigatório ausente;
- com campo recomendado ausente sem motivo em `contrato/alinhamento.json`. Quando o motor ganha um campo
  recomendado, ou um plugin passa a usar um, os outros param aqui até adotar o campo ou registrar
  por que ele não se aplica. Motivo com **PENDENTE** é lacuna conhecida, a resolver;
- com o JSON mudado em relação à `origin/main` sem subir a `versao`, com o link de atualização
  apontando para outro arquivo, ou com a versão atual ausente deste README.

Quando o motor mudar o formato, gere o contrato de novo no PJe✚R e publique-o aqui junto.

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

`site` é opcional: o endereço `https` da página do serviço (por exemplo
`"site": "https://flow.vrgc.com.br"`). O detalhe do plugin mostra o link para a pessoa abrir
o serviço, e o ícone usa o favicon desse endereço quando o plugin não declara `icone`.
Extensões antigas ignoram o campo.

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
