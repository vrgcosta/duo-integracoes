# Servidor local do Duo

Os comandos deste documento são executados dentro de `servidor/`. A pasta irmã
[`plugins/`](../plugins/README.md) guarda as publicações de plugins; os arquivos gerados
com chave em `dados/` pertencem somente à instalação local.

Um servidor pequeno, em Python puro, que liga o assistente **Duo** da extensão PJe✚R a um modelo de
linguagem rodando **no seu próprio computador**. Sem nuvem, sem conta, sem `pip install`.

## O que é

O Duo conversa com um "provedor" por um *blueprint*: um arquivo JSON que descreve as rotas, o formato
das mensagens e as opções do provedor. Este repositório tem duas peças:

- o **servidor** (`servidor.py`), que escuta só em `127.0.0.1`, guarda as conversas em `dados/` e fala
  com o modelo (Ollama por padrão; também LM Studio, llama.cpp e vLLM);
- o **blueprint** que ele gera (`dados/duo-provedor-local.blueprint.json`), que você importa no Duo.

## Por que existe

É a vitrine do **modo plugin** do Duo, pensada para três públicos:

- **Quem desenvolve na equipe:** um provedor completo e legível, com testes, para ver como o contrato
  do blueprint funciona de ponta a ponta (`docs/blueprint.md` é o contrato; `docs/arquitetura.md`
  explica o turno).
- **Quem trabalha com processos e se preocupa com sigilo:** com o modelo local, o conteúdo dos autos
  não sai da sua máquina. Veja "Privacidade".
- **Parceiros que querem plugar o próprio modelo:** `docs/plugar-seu-backend.md` mostra como escrever
  um backend novo em cerca de 40 linhas.

## Requisitos

- Python 3.11 ou mais novo (nada além da biblioteca padrão).
- [Ollama](https://ollama.com) (ou outro servidor de modelos; ver "Outros backends").
- Memória: com GPU, 8 GB de VRAM ou mais; sem GPU, 16 GB de RAM são recomendados.
- A extensão PJe✚R com o Duo.

## Trilha sem GPU (só processador)

1. Instale o Ollama (https://ollama.com/download) e deixe-o rodando.
2. Baixe o modelo do perfil `cpu`: `ollama pull qwen3.5:4b` (cerca de 3,3 GB).
3. Inicie o servidor: `iniciar.cmd` (Windows) ou `./iniciar.sh` (Linux/macOS).
4. No Duo, abra Configurações → Plugins e importe `dados/duo-provedor-local.blueprint.json`.
5. Escolha **Plugin (LLM local)** como provedor.

No processador a primeira palavra demora (o modelo lê todo o contexto antes de responder). O perfil
`cpu` já usa contexto menor (16384 tokens) e espera até 600 s pelo primeiro token.

## Trilha com GPU

1. Instale o Ollama (https://ollama.com/download) e deixe-o rodando.
2. Baixe o modelo do perfil `gpu`: `ollama pull qwen3.5:9b` (cerca de 6,6 GB).
3. Inicie o servidor: `iniciar.cmd` (Windows) ou `./iniciar.sh` (Linux/macOS).
4. No Duo, abra Configurações → Plugins e importe `dados/duo-provedor-local.blueprint.json`.
5. Escolha **Plugin (LLM local)** como provedor.

O perfil é escolhido na primeira execução pelo hardware detectado (NVIDIA com 8 GB de VRAM ou mais, ou
Mac com chip Apple e 16 GB ou mais). Para forçar: `iniciar.cmd --perfil gpu`.

## Conferir antes de abrir o Duo (as duas trilhas)

Rode `iniciar.cmd --verificar` (Windows) ou `./iniciar.sh --verificar` (Linux/macOS). Ele mostra o
perfil, confere se o backend responde, se o modelo está instalado (ou se é de nuvem e está bloqueado)
e quais capacidades ele tem (ferramentas, raciocínio, contexto). Mostra a versão do Ollama e avisa se ela é anterior à
medida (0.30.10): nela, o motor recusa o pedido acima da janela em vez de cortá-lo calado. Com o modelo já carregado (depois de
uma conversa), avisa também se uma parte dele ficou no processador, o que deixa as respostas mais
lentas, e sugere um modelo menor. Sai com código 1 se houver erro.

As opções de linha de comando (`--backend`, `--url`, `--modelo`, `--porta`, `--perfil`) ficam
gravadas em `dados/config.json` quando o servidor sobe, e valem para as próximas execuções. Com
`--verificar` elas valem só para o diagnóstico e não são gravadas; se o servidor já estiver rodando,
também não são aplicadas (encerre-o e inicie de novo).

Enquanto roda, o console mostra uma linha por envio (modelo, modo de ferramentas, opções do Duo,
tamanho do sistema, catálogo e número de ferramentas) e uma por resposta (rodadas, tokens, avisos).
O texto das conversas, dos anexos e dos resultados de ferramentas nunca vai para essas linhas.

## Modelos testados

> **Teste de capacidade, não avaliação jurídica.** A bateria confere se o modelo consegue operar o
> Duo: chamar a ferramenta certa com JSON válido, responder em português, obedecer ao botão de
> pensamento e caber na GPU. Ela não julga se o resumo ou a resposta estão certos. Os textos enviados
> ao modelo são sintéticos, escritos no próprio script.

Avaliação de **2026-10-23** com `avaliar_modelos.py`: Ollama 0.30.10, Windows 11, AMD Ryzen 9 7900,
64 GB de RAM, NVIDIA RTX 4070 com 12 GB, contexto de **16384** e de **32768** tokens. Os resultados
(passou ou não) foram os mesmos nos dois contextos; só a memória muda.

| Modelo | Download | Ferramentas nativas | Cercas | Botão de pensamento | Visão¹ | Resposta em pt-BR | Memória (16k / 32k)² | Velocidade³ |
|---|---|---|---|---|---|---|---|---|
| `qwen3.5:4b` | 3,3 GB | sim | sim | sim | sim | sim | 4,5 / 4,8 GB, toda na GPU | ~105 tokens/s |
| `qwen3.5:9b` | 6,6 GB | sim | sim | sim | sim | sim | 7,2 / 7,6 GB, toda na GPU | ~68 tokens/s |
| `gemma4:12b` | 8,0 GB | sim | sim | sim | sim | sim | 10,1 / 9,7 GB, toda na GPU | ~88 tokens/s |
| `qwen3:4b` | 2,5 GB | sim | sim | só em pedidos curtos⁴ | não | **não**⁴ | 5,3 / 8,2 GB, toda na GPU | ~120 tokens/s |
| `deepseek-r1:8b` | 5,2 GB | **não**⁵ | sim | **não** (raciocina mesmo desligado) | não | sim | 7,5 / 10,2 GB, toda na GPU | ~74 tokens/s |
| `gpt-oss:20b` | 13,8 GB | sim | **não**⁶ | **não** (raciocina mesmo desligado) | não | sim | 14,1 GB, ~70% na GPU | ~55 tokens/s |
| `gemma4:e4b` | 6,6 GB | — | — | — | — | — | não carrega⁷ | — |

1. Visão: o que o modelo declara; a bateria não testa imagens.
2. Memória: a maior entre o que o Ollama informa (`/api/ps`), o aumento de VRAM medido pelo
   `nvidia-smi` e o próprio download. O `/api/ps` do Ollama 0.30.10 informa 1,1 a 1,4 GB para o
   `gemma4:12b`, que ocupa perto de 10 GB na placa.
3. Velocidade: geração de um resumo curto **nesta GPU**, com o pensamento desligado. **Nada foi
   medido no processador**: não há número de velocidade para o perfil `cpu`, e no processador a
   geração é bem mais lenta que qualquer valor da tabela. O "pensamento desligado" não vale para o
   `deepseek-r1:8b` e o `gpt-oss:20b`, que raciocinam mesmo assim: neles, a velocidade é a da geração
   inteira, raciocínio incluído. O primeiro token chegou em até 1 s em todos; nesses dois, o primeiro
   texto visível demorou de 1,6 a 12,7 s, por causa do raciocínio.
4. A etiqueta `qwen3:4b` aponta hoje para o Qwen3-4B-Thinking-2507, que raciocina sempre. Com o
   pensamento desligado, o raciocínio (em inglês) aparece no texto da resposta: num pedido de resumo,
   foram 5 a 8 mil caracteres em vez de 3 frases. Por isso saiu do perfil `cpu`.
5. Não chamou `listar_documentos`: respondeu em texto, pedindo dados ao usuário ou citando uma
   ferramenta que não existe. No segundo passo, o Ollama respondeu erro 500 ao interpretar a saída do
   modelo (`does not match the expected peg-native format`). Nas cercas, ele acertou os dois passos.
6. Nas cercas ele faz uma chamada nativa a uma função chamada `duo-ferramenta`, que não existe, em vez
   de escrever o bloco de texto. No modo `auto` isso não importa, porque ele usa as ferramentas
   nativas, que funcionaram.
7. No Ollama 0.30.10 deste Windows, o processo do modelo encerra ao carregar (`0xc0000409`,
   `Gemma4Assistant requires ctx_other to be set`), com qualquer contexto, inclusive o padrão.

**Recomendação por hardware** (são os padrões dos perfis):

- **Sem GPU (16 GB de RAM):** `qwen3.5:4b`, perfil `cpu`.
- **GPU com 8 GB:** `qwen3.5:9b`, perfil `gpu`, **no limite**. Ele pede de 7,2 a 7,6 GB (contexto
  de 16k a 32k), e uma placa de 8 GiB tem 8,6 GB. Com a área de trabalho e o navegador ocupando o
  que ocupavam nesta máquina (de 1,3 a 1,8 GB), até uns 11% do modelo iriam para o processador; se a
  placa já estiver com 2,3 GB ocupados, de 12 a 17%. São estimativas, não medidas: não houve placa
  de 8 GB nesta avaliação. A parte no processador deixa a resposta mais lenta, mas não quebra nada,
  e o `--verificar` avisa quando isso acontece. A saída é o `qwen3.5:4b`
  (`iniciar.cmd --modelo qwen3.5:4b`), que cabe inteiro.
- **GPU com 12 GB ou mais:** `qwen3.5:9b`, perfil `gpu`, inteiro na placa. O `gemma4:12b` também
  passou em tudo e é maior, mas ocupa perto de 10 GB e, neste Ollama, a medida de memória dele vem
  errada e o irmão `gemma4:e4b` nem carrega; fica como alternativa (`--modelo gemma4:12b`).

O resumo "Recomendado para" que o script imprime usa uma regra mais conservadora (a memória medida tem
de caber na VRAM menos 1,5 GB, o meio do que a área de trabalho e o navegador ocupavam nesta máquina,
de 1,3 a 1,8 GB) e por isso indica o `qwen3.5:4b` para 8 GB e o `gemma4:12b` para 12 GB. O padrão do
perfil `gpu`, que vale de 8 GB para cima, ficou no meio: o melhor modelo que passou em tudo e cabe,
no limite, nos 8 GB nominais.

**Quem já usava o servidor:** o modelo padrão fica gravado em `dados/config.json` e não muda sozinho.
Para adotar o novo, inicie uma vez com `iniciar.cmd --perfil cpu` (ou `--perfil gpu`), ou escolha o
modelo com `--modelo`.

### Como avaliar no seu computador

Com o Ollama rodando e os modelos já baixados (`ollama pull ...`):

```
py avaliar_modelos.py --modelos qwen3.5:4b,qwen3.5:9b --contexto 16384 --saida dados/avaliacao.md
```

O script carrega um modelo por vez, roda as baterias e o descarrega no fim; cada modelo leva de
alguns segundos a poucos minutos. O relatório sai no console e, com `--saida`, num arquivo Markdown em
`dados/`, com uma linha por modelo, o resumo "Recomendado para" e os detalhes de cada bateria.
`--prazo` limita cada bateria (padrão 180 s). Com `--backend openai --url ...` ele avalia outro
servidor compatível, sem as medidas de memória, que só o Ollama local dá.

## Opções no DUO

Na tela do plugin, o Duo mostra quatro campos. O valor vai em cada envio; 0 ou -1 significam "usar o
padrão do servidor" (`dados/config.json`).

| Opção | O que faz |
|---|---|
| Janela de contexto (tokens) | Quanto da conversa o modelo enxerga. Mais contexto usa mais memória e deixa o primeiro token mais lento. |
| Temperatura | Criatividade. Valores baixos deixam a resposta mais estável. |
| Modo de ferramentas | `auto` usa o tool-calling do modelo quando existe e, senão, o emula por blocos de texto ("cercas"). |
| Máximo de rodadas de ferramentas | Quantas idas e vindas de ferramentas por resposta. |

O botão de pensamento do Duo liga o raciocínio do modelo, quando ele tem.

A lista de modelos do Duo só traz os que conversam: modelos de embedding instalados no Ollama (como
`qwen3-embedding`) ficam de fora.

### Quando a conversa não cabe na janela

A conta é em tokens, e os avisos também. Os resultados de ferramenta vão inteiros ao modelo enquanto
couberem. Quando não cabem, o servidor corta por idade, do mais antigo para o mais novo, cada passo só
até onde precisar:

1. De cada turno anterior, começando pelo mais antigo: primeiro os resultados de ferramenta e, se não
   bastar, o texto do turno. Só então o corte passa ao turno seguinte. O texto sai inteiro, menos o do
   primeiro pedido quando o Duo não manda instruções: ali saem a resposta e os anexos, e o pedido fica.
2. No turno atual, os resultados mais velhos. O seu pedido e os resultados da rodada mais recente não
   saem.
3. Por último, os resultados da rodada mais recente são truncados no meio. O que é pequeno fica inteiro
   e os grandes dividem o espaço. É o único caso em que um resultado é truncado.

**Quando nem assim cabe.** O servidor só deixa de enviar com prova de que o motor cortaria, e prova só há com o número
do próprio motor, acima da janela:

| Situação | O que acontece |
|---|---|
| Contagem do motor até o espaço útil | Vai ao modelo, sem aviso. |
| Contagem entre o espaço útil e a janela | Vai ao modelo, com o aviso de que sobra pouco para a resposta. |
| Contagem acima da janela | Sem as ferramentas, cabendo: o modelo responde com o que já obteve, com aviso. Nem assim: erro que diz o maior item, quanto ocupa e quanto cabe, por exemplo `O anexo "peticao.pdf" ocupa ~14135 tokens e só cabem ~5943`. |
| A contagem falhou, demorou, ou o motor contou bem menos que a estimativa | Vai ao modelo, sempre com aviso de que a medida é estimada. O Ollama recusa o que não couber, e vale a linha da contagem acima da janela. O backend `openai` pode cortar o começo sem avisar, e o aviso diz isso. Nunca é recusado sem a prova do motor. |
| O motor nunca conta (backend `openai`) | Vai ao modelo. Avisa no 1º envio da conversa que os números são estimados, e de novo sempre que a estimativa passar do espaço útil. Avisar em toda resposta só ensinaria a não ler o aviso. |

Como a conversa só cresce e o espaço reservado para a nota e para a ferramenta de leitura é sempre o
mesmo, nada sai antes do que é mais velho, e em geral o que saiu não volta. Há ressalvas. Um resultado
truncado pode voltar inteiro quando sai um mais velho que lhe tomava o espaço. E o que saiu pode voltar
quando a correção pela contagem desce, a janela muda, o modelo ou o modo de ferramentas muda, ou o prompt de
sistema e o catálogo do Duo encolhem de um envio para outro.

**O turno termina com uma resposta.** Modelo pequeno às vezes raciocina, anuncia o que vai fazer e para
sem fazer: essa rodada ganha mais uma chance, com um lembrete que o usuário não vê. Se ele chamar uma
ferramenta que só conhecia pelo resumo do catálogo, ela é carregada e, se a chamada trouxer os parâmetros obrigatórios,
executada. Ao bater em `max_rodadas`, ainda há uma última rodada, sem ferramentas, para responder com o que já foi obtido;
vazia, ela ganha mais uma chance. Se mesmo assim o modelo parar sem escrever a resposta, um "vou consultar" de antes não passa
por ela: o aviso diz quantas consultas ele fez e que basta pedir de novo, com os resultados ainda na conversa.

**O turno não fica em círculos.** Três travas, antes de o Duo ou o PJe serem consultados:

- Chamada sem parâmetro obrigatório não é executada nem entra no histórico; o modelo recebe um lembrete com o que faltou. Tirar
  do histórico é o que resolve: com uma chamada vazia à vista, o `qwen3.5:4b` a imitava em 12 de 12 tentativas, mesmo
  raciocinando que precisava do argumento, e nem um erro mais claro o tirava disso.
- A mesma chamada, com os mesmos argumentos, não roda de novo logo em seguida, nem mais de duas vezes no turno.
- Depois de três rodadas seguidas sem nenhum resultado, o turno vai para a rodada final, e o usuário é avisado.

**O modelo pode ler o que foi omitido.** No lugar do que saiu fica uma marca que diz como ler, por
exemplo `[… 3200 de 9800 tokens omitidos; leia com ler_trecho_resultado(id="…", inicio=8400) …]`. Essa
ferramenta é respondida pelo próprio servidor, sem pedir autorização e sem aparecer no Duo, e só é
oferecida ao modelo quando há marca na janela. Ela devolve o trecho que cabe, o total em tokens e de onde
continuar. Reler o mesmo trecho na mesma resposta só traz o texto de novo se ele tiver saído da janela, e no
máximo duas vezes; nos outros casos, a resposta diz que ele já foi lido e de onde continuar. O teto continua
sendo o máximo de rodadas.

**O que você vê:** a resposta termina com um aviso do que saiu da janela, com o nome da ferramenta e os
tokens. Se a parte omitida de um resultado do turno ficou sem ler, aparece "Atenção: o modelo respondeu
sem ler N de M tokens de obter_texto_documento". O modelo recebe uma nota com as mesmas contagens, que o
manda ler em vez de afirmar que algo não existe ou concluir sobre um documento pela metade.

**Como os tokens são contados:** por uma estimativa do tipo de conteúdo, porque dígitos e símbolos rendem
bem menos caracteres por token que um texto corrido. Medido com o `qwen3.5:4b`, 30 mil caracteres dão de 6757 a
7653 tokens em texto corrido, com ou sem acento, 25075 em linhas de IDs e datas e 14261 em JSON; a estimativa
fica um pouco acima em todos. Depois de cada rodada, o servidor compara a estimativa com os tokens que o motor
contou e corrige as rodadas seguintes. A correção fica gravada na conversa e vale para as próximas respostas,
enquanto o modelo for o mesmo. Com o Ollama, o servidor também pede a contagem exata ao motor antes de cada rodada, e a
correção acompanha o que ela mede, para os dois lados: um documento denso que já saiu não encarece o resto da conversa. Ela é feita
gerando um único token. No caso comum, é uma contagem por rodada, e o custo a mais é esse token, porque o trabalho de ler o pedido é
aproveitado no envio em seguida. Se a contagem passa do espaço útil, o pedido é cortado e contado de novo: no pior caso, 3 contagens
na rodada e 4 leituras completas do pedido, porque o envio só aproveita a da última. Num modelo lento e numa janela grande, isso
se sente em cada rodada em que acontece. Um documento mais denso que o dobro da estimativa faria isso em toda rodada; por isso, depois
da 1ª vez no turno, o servidor fica com o ajuste que funcionou e paga só uma contagem por rodada, à custa de levar menos do que caberia. No
backend `openai` e no simulado não há como contar, e vale a estimativa. Se a contagem falhar ou demorar, idem.

**Medidor e painel do Duo.** O blueprint declara `janelaContexto`, e o Duo passa a mostrar a janela usada e o
limite, com os números do servidor. Eles saem da mesma conta do próximo envio, marcados como corrigidos pela medição do motor
(`real`) ou estimados. O medidor não chama o modelo, então é a conta de antes de o motor conferir: com um documento denso no
histórico, ele o mostra dentro e o total fica acima do que vai de fato, até o envio o tirar. O que foi medido no último envio
aparece à parte, como número exato. No painel dá para tirar da janela um turno ou um resultado de ferramenta, e pôr de volta: o
que você tirou não volta sozinho, nem com a janela sobrando, e o modelo não consegue lê-lo com
`ler_trecho_resultado`. O original continua na conversa. O contrato está em [docs/blueprint.md](docs/blueprint.md).
**Quem já importou o blueprint** precisa reimportá-lo para o Duo enxergar essas rotas.

**Compactar a conversa.** No painel, "Compactar conversa" troca os turnos anteriores ao último por um resumo feito pelo
próprio modelo, com o qual você abre espaço na janela sem perder o assunto. O modelo é instruído a preservar número do
processo, IDs de documentos, datas, nomes e pedidos, a marcar as dúvidas e a não acrescentar nada, mas é um resumo: a
mensagem que ele passa a ver no lugar dos turnos diz que foi gerada pelo modelo e manda conferir nos autos antes de
citar. Você lê o texto no painel. O original continua na conversa e "Desfazer resumo" volta aos turnos; enquanto o
resumo valer, o modelo só conhece aquele trecho por ele, e precisa consultar de novo o que não estiver ali. Entre os turnos anteriores, o resumo é o último a sair da janela; se sair, a resposta
avisa e o painel mostra. Roda no servidor, em segundo plano: em processador leva minutos, com teto de 30, dá para
recarregar a aba e dá para cancelar; enquanto roda, a conversa não aceita mensagens. Se falhar, nada muda. Modelo de nuvem
bloqueado também não compacta, porque o resumo leva os autos a ele como um turno leva.

**`limite_resultado`** (em `dados/config.json`, em tokens): com `0`, o padrão, não há teto, e só a janela
decide. Um valor positivo limita cada resultado mesmo com a janela sobrando; o modelo ainda pode ler o
resto. **Quem já usava o servidor** tem `12000` gravado, que agora vale como teto de 12000 tokens por
resultado; para tirar o teto, troque por `0` e reinicie o servidor.

## Onde ficam os dados

Tudo vive em `dados/`, que não é versionado:

- `dados/duo.sqlite3`: o histórico (conversas, mensagens em árvore, anexos, o índice de busca e os resumos das
  compactações). Com
  o servidor rodando aparecem também `duo.sqlite3-wal` e `duo.sqlite3-shm`; somem ao encerrar com
  Ctrl+C (fechando a janela no X, ficam até a próxima abertura, que os incorpora ao banco). Os
  resultados de ferramenta ficam aqui inteiros, mesmo quando não couberam na janela: o corte só muda o
  que vai ao modelo, e é daqui que `ler_trecho_resultado` lê. As leituras também ficam gravadas, como
  parte da conversa, e saem com ela.
- `dados/projetos/<nome>/instrucoes.md`: as instruções de cada projeto. Continuam sendo arquivos de
  texto, para você editar no bloco de notas.
- `dados/config.json` e `dados/duo-provedor-local.blueprint.json`: a configuração e o blueprint (este
  contém a chave de acesso).

**Processo sigiloso:** o banco não é criptografado; use disco criptografado (BitLocker ou FileVault)
para `dados/`. Detalhes em [Privacidade](#privacidade).

**Fora do OneDrive e de pastas de rede:** se a pasta do servidor estiver dentro do OneDrive, do
Dropbox ou do Google Drive, o aplicativo deles copia `dados/` (com o texto dos autos) para a nuvem
sem perguntar nada. No Windows, a Área de Trabalho e os Documentos costumam estar dentro do OneDrive.
Numa pasta de rede, o histórico passa pela rede e fica no servidor de arquivos, e o SQLite nem
funciona direito ali (o modo WAL exige disco local). Deixe o servidor num disco desta máquina, por
exemplo em `C:\duo-integracoes\servidor`. O banner avisa quando a pasta está no OneDrive ou na rede; os
outros serviços de sincronização ele não reconhece.

**Inspecionar o histórico:** abra `dados/duo.sqlite3` no [DB Browser for SQLite](https://sqlitebrowser.org/).
Prefira abrir só para leitura ("Abrir somente leitura"), com ou sem o servidor rodando. Para copiar o
histórico, pare o servidor e copie o arquivo `duo.sqlite3`.

**Busca:** sem acento e sem diferença entre maiúsculas e minúsculas; a palavra digitada pela metade
também acha ("cobran" acha "cobrança"). Isso usa o FTS5 do SQLite. Num Python sem FTS5 o servidor
cai sozinho numa busca por trecho (mais lenta, mesma regra de acentos e caixa).

**Quem atualiza de uma versão anterior:** na primeira abertura, se existirem `dados/conversas/*.json`
e o banco estiver vazio, o servidor importa as conversas e os anexos (`dados/arquivos`) e renomeia as
pastas para `conversas.migrado-AAAAMMDD` e `arquivos.migrado-AAAAMMDD`. Nada é apagado
automaticamente. Depois de conferir o histórico no Duo, **apague essas duas pastas**: são cópias das
conversas e dos anexos em arquivos comuns, sem criptografia, e excluir uma conversa no Duo não as
alcança.

## Privacidade

- **O que fica local:** o servidor só aceita conexões de `127.0.0.1`, exige uma chave no cabeçalho e
  recusa requisições vindas de páginas web. As conversas ficam em `dados/duo.sqlite3`, na sua máquina. Com o
  Ollama local, o texto dos autos vai do navegador ao servidor e ao modelo sem sair do computador.
- **Modelos de nuvem:** o Ollama também oferece modelos hospedados (nomes terminados em `-cloud` ou
  `:cloud`). Por padrão o servidor os esconde da lista **e recusa** qualquer envio para eles, mesmo
  que o Duo peça um pelo id ou que ele seja o modelo padrão. Ligar `permitir_modelos_nuvem` no
  `config.json` os libera, e o conteúdo passa a sair da máquina; o `--verificar` e o banner avisam.
- **Backend `openai`:** a API compatível com a OpenAI não informa onde o modelo roda. O servidor
  reconhece a convenção do Ollama `/v1` (sufixo `-cloud` ou `:cloud`) e trata esses modelos como de
  nuvem. Em outros servidores compatíveis (LM Studio, llama.cpp, vLLM, um proxy) ele **não tem como
  saber** se o modelo repassa o texto a outro lugar: isso fica por conta da configuração do próprio
  servidor.
- **Backend remoto:** apontar `--url` para outro computador (ou um serviço) também envia o conteúdo
  para fora. O servidor avisa na inicialização.
- **Reimporte quando o destino muda:** o Duo guarda a cópia do blueprint que você importou, e é ela
  que declara se o destino é local. Ao ligar `permitir_modelos_nuvem` ou apontar `--url` para fora
  (ou ao desfazer isso), o servidor regenera o blueprint e mostra no banner **"REIMPORTE o blueprint
  no DUO"**. Até você reimportar, o Duo continua com a declaração antiga.
- **Excluir uma conversa apaga também os anexos dela:** as mensagens, o índice de busca, o resumo da
  compactação, o último `system` guardado e os arquivos enviados por upload e citados nas mensagens saem do
  banco juntos. Um upload que nenhuma
  mensagem chegou a citar (por exemplo, um anexo enviado e descartado antes do envio) não pertence a
  nenhuma conversa e **não** é apagado por esta exclusão; só some se você apagar o banco.
- **O que a exclusão garante:** o servidor sobrescreve com zeros, nos próprios arquivos de `dados/`,
  o que excluiu: as linhas da conversa, das mensagens, do resumo e dos anexos; os termos dela no índice de busca
  (com a opção `secure-delete` do FTS5, no SQLite 3.42 ou mais novo; nos anteriores, o índice é
  reescrito a cada exclusão); e as cópias que o `duo.sqlite3-wal` guardava, porque ele é esvaziado
  logo depois da exclusão. Quem abrir esses arquivos depois, mesmo lendo os bytes, não acha o texto
  excluído.
- **O que fica por conta do disco:** o servidor controla o conteúdo dos arquivos, não o disco. Um SSD
  grava cada alteração num bloco novo e só limpa o antigo mais tarde, e pontos de restauração ou
  backups do sistema podem guardar versões anteriores do banco. Se outro programa estiver lendo o
  banco na hora da exclusão (o DB Browser aberto, por exemplo), o `-wal` não pode ser esvaziado na
  hora: o console avisa, e o texto fica nele até a próxima exclusão ou até o servidor encerrar com
  Ctrl+C; se a janela for fechada no X nesse meio-tempo, até a próxima abertura.
- **Processo sigiloso: use disco criptografado.** O banco guarda o texto das conversas e os anexos
  sem criptografia. Para processos em segredo de justiça, deixe `dados/` num disco criptografado
  (BitLocker no Windows, FileVault no macOS): ele cobre os restos do item anterior e também o
  computador perdido ou roubado.
- **Pasta sincronizada ou de rede:** com o servidor dentro do OneDrive, do Dropbox, do Google Drive ou
  numa pasta de rede, o histórico inteiro (texto dos autos e anexos) sai da máquina sem aviso, mesmo
  com o modelo local. O banner da inicialização avisa no caso do OneDrive e das pastas de rede; veja
  [Onde ficam os dados](#onde-ficam-os-dados).
- **Não compartilhe o blueprint gerado:** ele contém a chave de acesso. Quem tiver o arquivo e acesso à
  porta local pode usar o seu servidor. Por isso `dados/` não é versionado. Para trocar a chave, apague
  `chave` em `dados/config.json` e reimporte o blueprint.

## DUO com e sem os ajustes do motor

Três ajustes do motor do Duo, opt-in por blueprint, melhoram o uso com modelos locais. O blueprint
deste repositório já os declara; um Duo sem eles continua funcionando.

| Ajuste | Com | Sem |
|---|---|---|
| `destinoLocal` | O aviso de sigilo reconhece que o destino é local (URL de loopback e modelos de nuvem ocultos) e não fala em "serviço externo". | Aviso genérico de sigilo, como para qualquer provedor externo. |
| `{sistema}` | O sistema do Duo (perfil do usuário, índice da base de conhecimento, documentos com IDs) chega ao servidor, que o junta ao `prompts/sistema.md`. | Só o `prompts/sistema.md` vale; o modelo não recebe o contexto do Duo. |
| `ferramentas.campoCatalogo` | Catálogo compacto: o modelo vê as favoritas e uma meta-ferramenta (`carregar_ferramentas`) que traz as demais sob demanda. | O catálogo completo, 21 ferramentas, vai em todo turno, o que pesa em modelo pequeno. |

## Outros backends

Qualquer servidor com API compatível com a da OpenAI serve:

```
py servidor.py --backend openai --url http://127.0.0.1:1234/v1 --modelo nome-do-modelo
```

| Servidor | URL usual | Contexto (configurado no servidor) |
|---|---|---|
| LM Studio | `http://127.0.0.1:1234/v1` | Controle de contexto ao carregar o modelo |
| llama.cpp (`llama-server`) | `http://127.0.0.1:8080/v1` | `-c N` |
| vLLM | `http://127.0.0.1:8000/v1` | `--max-model-len N` |
| Ollama `/v1` | `http://127.0.0.1:11434/v1` | variável `OLLAMA_CONTEXT_LENGTH` |

**O contexto é configurado no próprio servidor.** A API compatível com a OpenAI não tem campo para o
tamanho da janela, então a opção "Janela de contexto" do Duo só ajusta o corte feito por este servidor;
se o motor carregar menos, ele trunca o prompt por conta própria. Já com o backend `ollama` (API
nativa) o servidor envia `num_ctx` em cada requisição, e **não** é preciso mexer em
`OLLAMA_CONTEXT_LENGTH`.

Para serviços que exigem chave de API, preencha `chave_backend` em `dados/config.json`.

## Problemas comuns

- **"A porta 8766 está ocupada":** outro programa a usa. Rode com `--porta 8800` e reimporte o
  blueprint (a porta faz parte dele). Se for este mesmo servidor, ele avisa que já está rodando.
- **"O modelo ... não está instalado":** rode `ollama pull <modelo>` (o `--verificar` diz qual) ou use
  `--modelo` com um que você já tenha.
- **Muito lento no processador:** use um modelo menor (por exemplo `qwen3:1.7b`), reduza a janela de
  contexto e desligue o pensamento. O primeiro token demora por causa da leitura do contexto.
- **O modelo "esquece" o começo da conversa:** o contexto está pequeno. O servidor corta do mais antigo
  para o mais novo (veja [Quando a conversa não cabe na janela](#quando-a-conversa-não-cabe-na-janela)) e
  avisa na tela quando isso acontece; aumente a janela se a memória permitir. No backend `openai`,
  confira a configuração do próprio servidor.
- **"Atenção: o modelo respondeu sem ler N de M tokens":** o documento não coube e o modelo não leu o
  resto. Peça que leia a parte omitida antes de responder, ou aumente a janela. Com um
  `limite_resultado` positivo em `dados/config.json`, troque por `0`.
- **"Não consegui falar com o Ollama":** o Ollama não está rodando. Abra o aplicativo ou rode
  `ollama serve`.
- **Lacuna conhecida:** o Duo envia o fuso horário (`timezone`) e o servidor o aceita, mas ainda não o
  repassa ao modelo. Datas relativas ("hoje") dependem do que o Duo escrever no sistema.

## Testes

Na raiz do repositório:

```
py -m unittest discover -s testes -t . -v
```

Os testes usam servidores falsos (`testes/falso.py`) e um cliente que imita o Duo
(`testes/cliente_duo.py`); não precisam de Ollama nem de rede.

## Créditos

O servidor HTTP, o fluxo SSE e o laço de rodadas, as medidas de segurança (chave, bloqueio de origem web), a
leitura da configuração e a geração do blueprint, a emulação de ferramentas por cercas e o backend simulado foram
adaptados do servidor da **DUO ChatGPT Ponte** (terceiros), versões 0.1.0 e 0.2.0. O `blueprint/modelo.json` partiu
do `modelo-blueprint.json` da Ponte 0.2.0, e guarda dele a estrutura e campos como `baseURL`, `sessao`,
`modelosEndpoint`, `contexto` e `anexos`. O restante (os backends reais, o armazenamento, o histórico em árvore, a
janela de contexto, a contagem, a compactação e o catálogo) foi escrito para este repositório.
