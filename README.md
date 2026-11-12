# Duo: servidor de exemplo e plugins

Este repositório reúne duas entregas independentes para o assistente Duo da extensão
PJe✚R:

- **[`servidor/`](servidor/README.md)**: template de servidor local em Python, backends,
  documentação, testes e molde do blueprint gerado para cada instalação.
- **[`plugins/`](plugins/README.md)**: pasta de publicação das definições públicas de
  plugins, com versão, requisitos da extensão e endereço para atualização pelo Duo.

```text
servidor/
  servidor.py
  iniciar.cmd
  iniciar.sh
  duo_provedor/
  blueprint/
  docs/
  testes/
plugins/
  README.md
```

## Usar o servidor local

Entre em `servidor/` antes de executar os comandos do tutorial:

```powershell
cd servidor
.\iniciar.cmd
```

No Linux/macOS, dentro dessa mesma pasta, execute `./iniciar.sh`. Os scripts também
podem ser chamados pelo caminho `servidor/iniciar.cmd` ou `servidor/iniciar.sh`, pois
ajustam o diretório de trabalho automaticamente.

Veja requisitos, perfis de CPU/GPU, modelos, configuração e instalação do blueprint no
[README do servidor](servidor/README.md).

Os dados e a chave de acesso são gerados em `servidor/dados/`, que continua fora do Git.
O arquivo gerado com chave é próprio de cada instalação; não deve ir para `plugins/`.

## Publicar plugins

Cada definição pública completa terá seu JSON em `plugins/`. O Duo pode importar o
link do arquivo no GitHub, verificar novas versões e oferecer **Atualizar**, nas versões
da extensão que incluem esse recurso. Os requisitos da extensão ficam declarados em
cada plugin. Consulte o [guia de publicação](plugins/README.md).

Há cinco plugins públicos na branch `main`: Claude web, ChatGPT web, Gemini web, busca de
jurisprudência TJBA e busca de jurisprudência STJ/STF. Os links de importação e as versões
estão no [catálogo de plugins](plugins/README.md#plugins-disponíveis).
O molde do servidor local continua separado em `servidor/blueprint/modelo.json`, pois
exige preenchimento da porta, chave e regra de destino local antes de ser instalado.

## Testes do servidor

```powershell
cd servidor
py -m unittest discover -s testes -t . -v
```

Os testes usam backends simulados e não precisam de um modelo instalado. Os testes da
extensão e do seu mecanismo de atualização ficam no repositório do PJe✚R.
