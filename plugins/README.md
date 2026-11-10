# Plugins públicos do Duo

Esta pasta é o local de publicação dos JSONs de plugins do Duo, junto do template do
servidor. O servidor não precisa estar ligado para o Duo verificar atualizações: a
consulta busca apenas o arquivo público no GitHub.

## Plugins disponíveis

Formato **1**, com extensão mínima **3.71.141** (o Gemini web exige a **3.71.145**).
ChatGPT web está na **1.1.1**; Gemini web, na **1.0.0**; os demais, na **1.0.2**. As
definições foram validadas pelos schemas e pelo verificador de compatibilidade do Duo
nessa versão do código. Essa validação não executa os serviços autenticados dos
provedores.

A **1.1.0** do ChatGPT web envia a mensagem pela rede, de dentro da aba logada (sessão,
verificação e anexos no mesmo protocolo da página), sem depender do editor nem da aba
visível, e lê a resposta consultando a conversa. Também lista os modelos do seletor da
conta. Se o envio pela rede for recusado antes de o ChatGPT aceitar a mensagem, o Duo
usa a tela como reserva.

A **1.0.0** do Gemini web envia pela tela da aba logada: escolhe o modelo e o nível de
raciocínio no menu do próprio Gemini, cola os anexos no editor e lê a resposta na conversa
quando ela fica pronta. Oferece 3.8 Flash (padrão), 3.1 Pro, 4 Argon e 3.5 Flash-Lite,
com raciocínio Baixo, Médio (padrão) ou Alto. Pela tela, com o modelo escolhido, a
resposta vem em segundos; pela rede, só o modelo padrão da conta respondia rápido. Quando
o Gemini devolve a recusa pronta ("Sou um modelo de linguagem...") no lugar da resposta, o
Duo manda "continue" na mesma conversa (até duas vezes) e entrega a resposta seguinte. O
envio pela tela depende de recursos da extensão **3.71.145**: em versões anteriores, o
Duo explica a exigência e não instala o plugin.

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
