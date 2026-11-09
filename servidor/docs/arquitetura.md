# Arquitetura

## O turno

Uma resposta do modelo é um *turno*. Tudo acontece numa única resposta SSE aberta pelo Duo:

```
Duo                       servidor                         backend / modelo
 |  POST /completion  --->  |                                    |
 |                          | grava a mensagem humana            |
 |                          | monta o histórico (ramo da folha)  |
 |                          | ajusta à janela de contexto        |
 |                          |  ---- contar_tokens(pedido) ----->  |  (se não couber, corta de novo ou recusa)
 |                          |  ---- conversar(pedido) --------->  |
 |  <--- SSE: texto/pensamento  <--- eventos (Texto, Raciocinio) -|
 |                          |  <--- ChamadaFerramenta ----------- |
 |  <--- SSE: tool_use      |                                    |
 |                          | PAUSA (ping a cada 15 s)           |
 |  executa a ferramenta    |                                    |
 |  POST /tool_result ---->  | entrega o resultado ao turno       |
 |                          |  ---- conversar(pedido) --------->  |  (retoma na mesma resposta SSE)
 |  <--- SSE: texto final   |                                    |
 |  <--- message_stop       | grava o nó do assistente           |
```

O laço repete até o modelo responder sem pedir ferramentas ou até `max_rodadas`. Se o Duo fechar a
conexão, o turno é cancelado (inclusive durante a carga do modelo) e a conversa fica livre.

## Módulos

| Módulo | Responsabilidade |
|---|---|
| `servidor.py` | Linha de comando, banner, `--verificar`, log; grava config e blueprint só depois do bind. |
| `duo_provedor/config.py` | `dados/config.json`, chave de acesso, padrões por perfil. |
| `pasta_dados.py` | Avisa quando `dados/` fica em pasta sincronizada ou de rede, que leva o histórico para fora da máquina. |
| `hardware.py` | Detecta RAM/VRAM e escolhe o perfil `cpu` ou `gpu`. |
| `blueprint.py` | Preenche `blueprint/modelo.json` e calcula `destinoLocal`. |
| `http.py` | Servidor HTTP local, roteamento, chave e bloqueio de origem web. |
| `servico.py` | Uma função por rota do blueprint. |
| `armazenamento.py` | Conversas em árvore, anexos e busca num banco SQLite; projetos em pastas. |
| `historico.py` | Do ramo da árvore ao formato neutro de mensagens. |
| `janela.py` | Ordem de corte, nota ao modelo e avisos visíveis de perda. |
| `envio.py` | O que vai ao modelo numa rodada: a conta única do turno e do medidor. |
| `medidor.py` | `GET /janela`: o que o próximo envio usaria, com o estado de cada turno e resultado. |
| `compactacao.py` | O resumo dos turnos antigos pelo modelo, em segundo plano, com teto e cancelamento. |
| `leitura.py` | `ler_trecho_resultado`: lê do nó gravado a parte de um resultado que não coube. |
| `turno.py` | O laço do turno, a pausa e o cancelamento. |
| `sse.py` | Quadros SSE no estilo claude.ai, ping, detecção de cliente ausente. |
| `ferramentas.py`, `catalogo.py` | Modo nativo/cercas; catálogo compacto e meta-ferramenta. |
| `emulacao.py` | Ferramentas por blocos de texto para modelos sem tool-calling. |
| `verificar.py` | Diagnóstico `--verificar`. |
| `backends/` | `base` (contrato), `ollama`, `openai`, `simulado`, `transporte`. |

## Decisões

**API nativa do Ollama, e não a `/v1`.** Só a nativa aceita `num_ctx` por requisição. Sem isso, o
Ollama usa o contexto padrão dele e trunca o prompt em silêncio, o pior defeito possível para quem
lê autos. Também só ela tem `think` explícito. (A `/v1` continua disponível pelo backend `openai`.)
Medido contra o Ollama 0.30.10: `/api/chat` entrega o raciocínio em `message.thinking`; os
`tool_calls` trazem `id` e `function.index`; e o Ollama só escreve os cabeçalhos HTTP com o primeiro
pedaço da resposta. Por isso o cancelamento durante a carga do modelo vigia o socket antes de ler a
resposta (`backends/transporte.py`). Na `/v1` o raciocínio vem em `delta.reasoning`.

**O servidor é o dono do histórico.** O Duo manda só a folha (`parent_message_uuid`) e o prompt; o
servidor remonta o ramo. Assim regenerar e editar mensagens funcionam como ramos, a janela de contexto
é decidida num só lugar, e o contexto do modelo não depende do que o navegador lembra.

**Meta-ferramenta.** Modelos pequenos se perdem com 21 definições. Com o catálogo compacto, ele vê as
favoritas e `carregar_ferramentas`, que o servidor resolve sozinho (marcada `interno`, nunca aparece
no SSE nem na leitura da conversa). O que foi carregado vale para o resto da conversa.

**Preâmbulo fixo.** Quando o Duo não manda `system`, o primeiro pedido do usuário traz o preâmbulo da
tarefa. A janela fixa o texto desse pedido, porque perder a instrução inicial estraga o resto da
conversa. Só o texto: a resposta e os anexos daquele turno seguem a ordem de corte como os demais.

**Corte por idade.** Enquanto o pedido não couber, e cada passo só até onde precisar:

1. Percorre os turnos anteriores, do mais antigo ao mais novo. Em cada um, limpa os resultados de
   ferramenta, do mais antigo ao mais novo; o texto do assistente fica, porque é o resumo do que já se
   descobriu, e o par chamada/resultado continua válido, porque os motores nativos rejeitam chamada
   sem resposta. Se ainda não couber, tira o texto do turno: o primeiro, quando fixado, fica reduzido ao
   texto do pedido; os demais saem inteiros. Só então passa ao turno seguinte.
2. No turno atual, limpa os resultados mais velhos. O pedido humano e os resultados da rodada mais
   recente nunca saem: sem eles, o modelo repetiria a chamada.
3. Por último, trunca os resultados da rodada mais recente, com um teto comum: o que é pequeno fica
   inteiro e os grandes dividem o espaço. Fica um mínimo de cada um; o que acontece se nem isso couber está em
   "Só o motor prova que não cabe".

O resumo da compactação, que seria o mais velho, é a exceção: entre os turnos anteriores, sai por último. Quem compactou quer
manter aquilo, e é justamente na janela apertada que se compacta. Se sair, o `GET /janela` diz, e a resposta avisa.

A garantia é: **com o mesmo fator de tokens, e sem o sistema e o catálogo encolherem, o que saiu não
volta, e nada sai antes de tudo o que é mais velho que ele.** Vale porque o corte não guarda estado, a
conversa só cresce e o peso reservado no corte é sempre o mesmo: a ordem de corte de uma conversa é o
começo da ordem da mesma conversa mais tarde, inclusive quando a rodada que estava protegida entra na
fila, depois de tudo. A exceção é benigna: um resultado truncado pode voltar inteiro quando sai um mais
velho que lhe tomava o espaço, e isso nunca tira nada mais novo; e o resumo que saiu pode voltar, quando o último turno
passa a sair antes dele, o que também não tira nada. Quando o fator desce, o que saiu por causa dele
pode voltar; a outra metade continua valendo, porque nada sai antes do que é mais velho. O teste de propriedade
exercita a garantia pelo fluxo da rodada (`turno.montar_janela`), com a definição da leitura incluída.

Três versões anteriores erraram aqui, e o motivo fica registrado. A primeira protegia o primeiro turno
inteiro e removia os do meio: numa sessão real, o turno 1 não achou o processo originário, o turno 2
achou, e no turno 3 o turno 2 saiu, o 1 voltou inteiro e o modelo respondeu "não localizei" e "não há
falta de contexto". A segunda limpava os resultados de todos os turnos anteriores antes de tirar qualquer
texto: uma rodada grande e protegida do turno 2 fazia o turno 1 sair, no turno 3 ela deixava de ser
protegida, era limpa primeiro, o pedido cabia e o turno 1 voltava no lugar dos resultados mais novos. A
terceira contava a definição de `ler_trecho_resultado` só quando um ajuste sem ela deixava marca: no envio
em que o único turno com marca saía, os ~180 tokens dela sumiam da conta, e um resultado que tinha saído
voltava.

**Resultado só é truncado quando não cabe.** Antes, o `historico` cortava todo resultado num limite fixo,
com a janela sobrando ou não, e documentos de 12.525 e 13.356 caracteres perdiam o meio à toa. O corte
agora é só da janela, que enxerga o espaço de verdade, e `limite_resultado` vale 0 (sem teto) por padrão.
Nas cercas os resultados vão juntos numa mensagem `usuario`, mas ficam também separados em `itens`, para a
janela limpar ou truncar um e refazer o texto com `emulacao.mensagem_resultados`.

**Os lembretes valem só para o turno em que nasceram.** O servidor fala com o modelo no meio de um turno: o teto de rodadas,
a rodada vazia, a correção de uma chamada inválida, a falta de espaço. No turno seguinte, "não chame mais ferramentas" só o
atrapalharia, então `historico.ramo` os tira do que está gravado, inclusive em conversas antigas. O nó só com lembrete não é
gravado: sem nada que o usuário veja, não há resposta, e o pedido fica sem resposta, que é o que manda o Duo seguir pelo pai dele.

**A rodada final é sem ferramentas de verdade.** Ao bater em `max_rodadas`, o modelo ainda responde com o que obteve, e nessa
rodada as definições não vão ao motor em nenhum dos modos: nem o campo `tools`, nem o catálogo em texto das cercas. Por
isso também não contam. Reservar o peso delas truncava resultados, numa janela de 8192 com 21 ferramentas, por causa de
5 mil tokens que não seriam enviados. A nota ao modelo diz para contar ao usuário o que ficou de fora, em vez de mandar ler, e
as marcas dessa rodada também não oferecem a leitura, a mensagem de resultados das cercas não pede novos blocos, o cabeçalho das
leituras não manda continuar e os lembretes de antes saem: quem obedecesse teria a chamada descartada e ficaria sem resposta.
Vazia, ela ganha mais uma chance, com um lembrete.

**O turno não fica em círculos.** Medido no `qwen3.5:4b`, com as definições reais do Duo e 12 tentativas por caso: sem chamada
vazia no histórico, 12 trazem o argumento; com uma, 0, mesmo com o raciocínio dizendo que ele é preciso. Um lembrete ou um erro mais
claro, com a vazia ainda à vista, davam de 0 a 2; o mesmo lembrete, sem ela, dava 11. Daí as três travas em `turno.py`:

- `classificar` confere os obrigatórios com `parametros_faltando`: faltou, a chamada vira lembrete, sem `tool_use` no nó. A
  ferramenta adiada é carregada mesmo assim, porque o modelo acabou de mostrar que a quer.
- `_tirar_repetidas` barra a mesma chamada (nome e argumentos, em qualquer ordem) logo depois de si mesma e além de
  `EXECUCOES_DA_MESMA_CHAMADA`. Repetir depois de outra ferramenta é legítimo: etiquetar e listar de novo. A leitura interna tem
  a conta dela.
- `LIMITE_SEM_PROGRESSO` rodadas seguidas sem resultado (só recusas, ou só `{"erro": ...}` do Duo) levam à rodada final.

Conversa gravada antes disso pode ter a chamada vazia: `para_turnos` não a mostra ao modelo quando ela voltou com erro. A árvore não muda.

**O turno sabe se respondeu.** O que decide é a última rodada, e não o turno: "vou consultar" antes de uma ferramenta não é
resposta. Sem ela, o aviso sai antes dos outros, e diz o que foi feito e o que tentar. O aviso de que o modelo respondeu com o
que tinha, depois da falta de espaço, só sai se ele respondeu.

**Toda marca diz como ler.** A marca antiga dizia "peça trechos específicos", mas as ferramentas do Duo
não têm parâmetro de faixa: era um beco sem saída, e o modelo concluía sobre o documento sem o meio dele.
Agora o nó guarda o resultado inteiro e `ler_trecho_resultado(id, inicio)` lê de lá. O servidor a responde
como a meta-ferramenta (marcada `interno`, nunca aparece no SSE nem na leitura da conversa, sem pedido de
autorização). Ela só entra no catálogo quando há marca na janela, para não pesar em toda conversa, mas o
peso dela é reservado assim que algo precisa sair, haja marca ou não, como o da nota: se a reserva
dependesse da marca, o peso iria e viria de um envio para outro. O trecho é do tamanho que cabe
com todos os outros resultados limpos e o texto da conversa mantido; assim, reler não empurra para fora o
que o modelo já escreveu. A conta é a do modo em que ele vai ao modelo: nas cercas, o trecho é medido como
sai, escapado dentro do JSON, e a moldura entra no piso, porque sem isso quase toda leitura voltava truncada
e gastava as rodadas relendo. O resultado de uma leitura segue a mesma ordem de corte, e a marca dele leva ao
original. Para não haver círculo, ler de novo um trecho inteiro à vista só recebe o aviso de que já foi lido
e de onde continuar, que é o fim da leitura que o modelo viu; se ela foi truncada, a resposta diz de onde ler a
parte omitida; e um trecho que saiu da janela pode ser relido uma vez. O teto de tudo é `max_rodadas`.

**O modelo é avisado do corte.** Os avisos na tela só o usuário lê, e quem responde é o modelo: sem
saber do corte, ele trata a falta como ausência. Quando algo sai, o texto do sistema ganha uma nota
com as contagens, que manda ler em vez de negar e não concluir sobre documento com parte omitida. A nota é
reservada no orçamento assim que algo precisa sair, para o próprio aviso não estourar a janela. Ela e o
corte mudam o começo do prompt, então a rodada em que o corte avança relê o prompt em vez de aproveitar o
cache de prefixo do Ollama; é o preço de o modelo saber.

**O usuário sabe o que ficou sem ler.** O turno anota, a cada rodada, que faixas de cada resultado o
modelo teve à vista, somando as leituras. No fim, se sobrou parte de um resultado truncado neste turno,
avisa quantos tokens ficaram sem ler. O aviso é do fim porque, durante o turno, ele ainda pode ler o resto.
Os avisos de limpeza nomeiam a ferramenta e os tokens, e não citam chamadas internas, que o usuário não vê.

**Tudo em tokens.** O que o usuário e o modelo leem sobre tamanho está em tokens, e o log também; só `inicio` é
em caracteres, porque é a posição exata de onde continuar. A estimativa olha o conteúdo porque, medido com o
`qwen3.5:4b`, uma frase corrida repetida rende 4,4 caracteres por token, peças fictícias com vocabulário variado
4,1, linhas de IDs e datas 1,2 e JSON 2,1, e são listas e JSON o forte do que o Duo manda: a conta antiga, de 3
por token, ficava 2,5 vezes abaixo do real nas listas. Dígito vale 1,1 token, grupo de até 4 símbolos seguidos 1,2,
e letras e espaços 1 a cada 4,25 caracteres. Abaixo do real, o motor corta o começo em silêncio; acima demais, sai
da janela o que cabia. Em 30 mil caracteres:

| Amostra | Medido | Estimado |
|---|---|---|
| Frase corrida repetida, sem acento | 6757 | 7395 (+9,4%) |
| Linhas de IDs e datas | 25075 | 25943 (+3,5%) |
| JSON | 14261 | 14478 (+1,5%) |
| Peças fictícias, com acentos, maiúsculas e nomes | 7379 | 7685 (+4,1%) |
| As mesmas, sem acento | 7653 | 7685 (+0,4%) |
| As mesmas, em minúsculas | 7303 | 7685 (+5,2%) |
| As mesmas, sem acento e em minúsculas | 7567 | 7685 (+1,6%) |

O acento não encarece: o texto com ele tem menos tokens que o mesmo sem ele, porque a forma sem acento é mais
rara. Por isso não há peso por letra acentuada, e a calibragem está nas letras.

**Estimativa corrigida pelo que o motor mede.** Mesmo calibrada, a estimativa não conta o molde do motor. A razão entre o
número do motor e a estimativa vira um fator, entre 1 e 2, que fica gravado na conversa (`fator_tokens`, `ultimo_uso`):
recomeçar em 1 a cada turno fazia a 1ª rodada passar da janela e era a causa normal de o que tinha saído voltar e sair de
novo. Vale para o modelo que mediu: trocar de modelo volta a 1. Abaixo de 1 não vai, porque a estimativa errar um pouco para
mais é o lado seguro se a contagem falhar depois.

Com a contagem prévia, o fator é o da medição do pedido que foi, **para os dois lados**, também dentro do turno, e só desce
quando ela fica mais de 5% abaixo, para não oscilar a cada rodada. Só crescer era um defeito: ele multiplica também o sistema e
as ferramentas, e um documento de OCR denso deixava o fator em 2, numa janela de 16384 com 21 ferramentas, com cada pedido levando
só o sistema e a mensagem atual e o medidor mostrando quase o dobro do real.

Se a contagem passa do orçamento, o ajuste é refeito com a proporção medida, que vale só para aquela rodada. Essa proporção é a de um
pedido com o documento denso dentro, e costuma ser demais para o que sobra sem ele; guardá-la até o fim do turno fazia os
resultados normais chegarem truncados com metade da janela livre, sob um aviso de "para caber na janela" que era falso. Por isso, se
a medição mostra que sobrou espaço por causa do fator, há uma contagem extra por rodada para levar mais: com o fator medido, ou com o
ponto médio entre o que passou e o que coube. Se não couber, vai o pedido que já tinha cabido.

**O preço.** Uma rodada comum conta uma vez, e o envio aproveita o prefill: custa um token. O pior caso de uma rodada são 3 contagens e
4 prefills (a que passou, a que coube, a extra e o envio, que só aproveita a última); só o que passa de `num_ctx` sai de graça,
porque o motor recusa antes do prefill. Abaixo de `FATOR_MAXIMO`, a medição fica gravada, e o pior caso não se repete enquanto a
proporção não mudar. Acima, ela não cabe no fator gravado, e ele se repetiria em toda rodada enquanto o documento ficasse na janela:
por isso, depois da 1ª vez no turno, vale o fator do pedido que foi, até o fim do turno, com um prefill de contagem por rodada. Isso
troca contagem por espaço, e o defeito descrito acima volta em parte, limitado a esse turno e a esse caso. Se o pedido cortado deixar mais
da metade do orçamento livre, o documento já saiu, e o fator desce até o meio do caminho para a medida, de uma rodada para outra: sem a
contagem extra, descer de uma vez o traria de volta. Se a contagem passar do orçamento, cabendo na janela, o pedido vai como está, com o
aviso de que sobra pouco para a resposta, e o fator sobe para a rodada seguinte. Acima da janela, conta de novo, porque não paga prefill e
esse número é a prova para recusar. O turno seguinte recomeça do fator gravado, e pode pagar o pior caso uma vez. Rodada com imagem não mede
nem segura o fator, porque a proporção dela não é a do texto: nela, o pior caso pode se repetir a cada rodada que passar do orçamento.

Sem contagem, a entrada reportada na rodada mede a seguinte, e o fator só cresce, porque o `prompt_eval_count` do Ollama
pode excluir o prefixo em cache e um número baixo não prova que a conversa pesa menos. Rodada com imagem não mede, porque o
motor as conta e a janela não as pesa.

**Contagem exata antes de processar.** O Ollama corta calado ao estourar `num_ctx`: 60 mil caracteres numa só
mensagem, numa janela de 4096, viram `prompt_eval_count` 4095, e o modelo perde o começo. Em várias mensagens é pior: ele
tira as mais antigas e conta o que sobrou, então 6586 tokens em `num_ctx` 4096 deram 3304, um número que parece caber.
A estimativa corrigida reduz o risco, mas não o elimina. Por isso, antes de cada rodada, `Backend.contar_tokens`
pede o total ao motor. O Ollama não tem rota de contagem: é o próprio `/api/chat`, com o pedido idêntico (mensagens,
ferramentas, `think`, `num_ctx`), `num_predict: 1` e `truncate: false`. Cabendo, o prefill fica para o envio em seguida
aproveitar (no mesmo pedido, 3,9 s na 1ª contagem, com a carga do modelo, e 0,3 s na 2ª), e o custo a mais é um token;
com `num_predict` 0, medimos 3 a 4 s a mais sem motivo aparente. Passando, ele responde 400 em 0,3 s, sem prefill, com o
total no corpo (`type: exceed_context_size_error`, `n_prompt_tokens`). O envio real também vai com `truncate: false`, então
o motor não corta calado em nenhum caminho: se a contagem faltou e ele recusar o envio, é como uma contagem acima da
janela. Medido no 0.30.10, com a mesma semente, a resposta normal sai igual com e sem o campo, em pedaços, texto e números; e o
estouro, que sem ele respondia 200 com 4095, volta como 400 com os 8832 tokens reais, em 0,3 s. Acima do orçamento, o fator sobe e o ajuste
é refeito com a proporção medida, em no máximo 3 contagens; depois da última, vale o último fator, sem contar de novo.
Falha ou demora (o limite é `espera_primeiro_token`) caem para a estimativa, e não se tenta de novo no mesmo turno,
porque cada espera se somaria à da próxima rodada. Enquanto conta, o SSE segue com ping, e fechar o Duo cancela. O
`GET /janela` não conta: ele é frequente, e fica com a estimativa e o fator gravado.

**Só o motor prova que não cabe.** Deixar de enviar exige prova de que o motor cortaria. Uma versão anterior recusava
tudo o que passasse do espaço útil, inclusive pela estimativa, que costuma errar para mais (uma frase corrida repetida,
com o `qwen3.5:4b` e `num_ctx` 32768: 1032 estimados para 901 contados): recusava pedidos que caberiam,
no backend `openai` o tamanho configurado nem chega ao servidor, e um turno morria no meio com folga em `num_ctx`. Agora:

- com o número do motor, até o espaço útil, vai; entre ele e a janela, vai com aviso de que sobra pouco para a resposta;
- com o número do motor acima da janela, na contagem ou na recusa do envio (ou em `num_ctx - 1`, onde um Ollama antigo
  satura), tira as ferramentas e vai
  para a rodada final, a mesma do teto, com aviso. O turno não morre por falta de espaço: o trabalho das rodadas anteriores
  vira resposta. Vale também na 1ª rodada, porque ali as definições podem ser o que não coube;
- só o pedido que nem sem ferramentas cabe é recusado, e o erro diz o maior item (a mensagem, um anexo pelo nome, um
  resultado, as instruções ou o 1º pedido fixado), quanto ocupa e quanto cabe. Se o motor recusar sem dizer o total, o erro
  diz só isso e o maior item pela estimativa, sem inventar número, e a medição fica como estava. O pedido fica no histórico, sem nó do
  assistente, e o Duo manda o seguinte pelo pai dele;
- sem o número do motor, nunca recusa, e avisa que a medida é estimada. O resto do aviso vem de `Backend.pode_cortar_calado`: o
  Ollama, que vai com `truncate: false`, recusa o que não couber; o backend `openai` pode cortar o começo. Um Ollama antigo, que
  ignore o campo, não dá como saber de antemão: só se vê pelo uso em `num_ctx - 1` ou pela contagem muito abaixo da estimativa,
  e os dois têm aviso próprio, abaixo. Avisa a cada turno em
  que a contagem falha, ou em que a última montagem vai sem conferir, inclusive com a estimativa cabendo. O motor que nunca
  conta avisa no 1º envio da conversa, e de novo só se a estimativa passar do espaço útil: o aviso fica no texto da resposta, volta
  ao modelo a cada turno e, repetido em todas, ninguém mais o lê;
- um Ollama que ignore `truncate: false` tira as mensagens antigas e conta o que sobrou (840 para 2346 reais em `num_ctx`
  2048), sem chegar a saturar. Só a estimativa denuncia isso: se ela, sem fator, passa da janela e o número vem abaixo de 70% dela
  (medida, ela erra uns 17% para mais), a medida não vale como exata, não mexe no fator e o usuário é avisado. O
  `--verificar` mostra a versão do motor, e avisa se é anterior à medida, sem afirmar em qual versão o campo surgiu.

**O medidor é a conta do envio, antes da conferência.** Em plugin, quem tem o histórico é o servidor, e o medidor do Duo só mostra o que
vem dele. `GET /janela` passa pelas mesmas funções do turno (`envio.preparar` e `envio.montar`), com um próximo
pedido vazio no fim do ramo: é o que faz o último turno contar como anterior, como vai contar de fato, e o teste
compara a soma com o pedido que chega ao backend. O Duo só manda o `system` e as ferramentas ao enviar, então o
último envio fica gravado na conversa (`ultimo_envio`) e sai com ela na exclusão. Antes do primeiro envio, o medidor
não os conhece, e a resposta traz `provisorio: true`. A leitura não chama o modelo: o total é a estimativa corrigida pelo fator gravado, e `fonte` diz se
há medição real por trás.

**O que o usuário tira não volta sozinho.** A lista fica na conversa (`excluidos_janela`), e sair da janela é só
o que o modelo vê: `historico.sem_excluidos` trabalha nos nós, antes de tudo, e a árvore gravada não muda. O turno
sai inteiro. O resultado vira uma marca, sem instrução de leitura, porque os motores nativos rejeitam chamada sem
resposta. Tirar um item também tira o que uma `ler_trecho_resultado` trouxe dele antes, e essa ferramenta passa a recusar
o pedido: senão o caminho de volta seria o próprio modelo. A lista é relida a cada rodada, para valer no meio de um turno, e o
pedido em curso não sai. O painel manda a lista montada com o que vê em `itens`, então o `PUT` soma a ela o que já estava
tirado e não aparece: sem isso, um item escondido por um resumo ou por outro ramo voltaria sozinho no primeiro `PUT`.

**O motor nunca corta calado o que é resumido.** A compactação usa o mesmo `truncate: false`: se o motor recusar uma fatia,
na contagem ou no envio, ela é partida ao meio, em vez de virar um resumo sem o começo do trecho.

**A compactação roda no servidor.** Um resumo em processador leva minutos, e o Chrome pode encerrar o service worker
do Duo quando um `fetch` passa de 30 s sem resposta. Por isso o `POST` só inicia, e quem acompanha é o `GET /janela`,
a cada poucos segundos: não há requisição longa, ela sobrevive a recarregar a aba e continua cancelável depois.
Enquanto roda, a conversa fica travada para novos turnos, que gravariam nós no trecho que está sendo resumido, e para
novas exclusões, porque o item tirado já pode ter ido ao modelo. O plano é lido sob a mesma trava do registro e da
gravação das exclusões, para um `PUT` não caber entre um e outro. Uma thread roda o motor e outra espera olhando o
cancelamento e o teto: um motor parado não prende a conversa em "rodando". Ao gravar, ela confere de novo sob a trava
de quem cancela, e a resposta que chega depois é descartada. O `DELETE` cancela e desfaz com o mesmo verbo, e o clique em
"cancelar" pode chegar depois do fim: o que distingue é se alguém já leu a janela e viu o desfecho. Sem ter visto, é um
cancelamento atrasado, e o resumo fica, seja o novo, seja o anterior a uma tentativa que falhou.

**O resumo não apaga o original.** Ele fica na tabela `resumos`, e `historico.com_resumo` o usa no lugar dos turnos só
no que vai ao modelo, numa mensagem que diz que foi gerado pelo modelo e manda conferir nos autos. Como a troca é por nó
do ramo, quem segue por outro ramo não o recebe. Enquanto ele valer, o modelo só conhece o trecho por ele: sem marca na
janela, `ler_trecho_resultado` não é oferecida, e o id dos resultados não aparece no resumo. Ler o original é desfazer.
A tabela não entra na busca, que segue achando o que foi dito de verdade, e sai com a conversa, sem resíduo. A
entrada grande demais é fatiada por turnos, e só um turno que sozinho não cabe é partido; a cada passada, a contagem do
motor confere o tamanho, porque um resumo feito sem o começo do trecho seria pior do que nenhum. Um modelo que não encurta o texto vira
erro, em vez de gastar o teto, e a compactação é só por pedido do usuário: o corte automático continua sendo a rede.

**SQLite, e não um JSON por conversa.** O histórico é um arquivo só (`dados/duo.sqlite3`, módulo
`sqlite3` da biblioteca padrão, então a regra de zero dependências continua valendo). Três ganhos
que o JSON não dava: a **busca** passa a ter índice (FTS5, sem acento e sem caixa; sem FTS5 no
Python, cai num `LIKE` sobre texto normalizado); a **exclusão** leva mensagens, índice e anexos numa
transação só, em vez de deixar anexos órfãos, e zera nos arquivos o que tirou (`PRAGMA
secure_delete`, a opção `secure-delete` do FTS5 ou um `optimize` no SQLite anterior ao 3.42, e o
`-wal` esvaziado logo depois); e a **gravação** é atômica, sem o `os.replace` de
arquivo temporário que no Windows falhava com antivírus ou editor segurando o arquivo. Uma conexão
só, protegida por trava (o servidor é local e de poucos usuários), em modo WAL. As mensagens guardam
`content`, `attachments` e `files` como JSON no texto da coluna, porque o formato do nó é o do
claude.ai e muda com ele; o que o servidor consulta (`ordem`, pai, remetente) é coluna de verdade.
Os projetos ficam fora do banco, como pastas com `instrucoes.md`: quem os edita é o usuário, no
bloco de notas. Quem vem do formato antigo é migrado na primeira abertura, sem apagar nada.

**Sem dependências.** Só biblioteca padrão: quem instala não precisa de `pip`, e o código inteiro cabe
numa leitura.
