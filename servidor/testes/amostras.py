"""Textos sintéticos usados na calibragem da estimativa de tokens. Nomes e fatos são inventados."""

from __future__ import annotations

# Amostras medidas no Ollama com o qwen3.5:4b (`prompt_eval_count`): em 30 mil caracteres, 6757, 25075 e 14261 tokens.
PROSA = ("A parte autora ajuizou a presente demanda alegando que a operadora negou a cobertura do "
         "procedimento indicado pelo medico assistente, razao pela qual requereu a concessao de tutela "
         "de urgencia, com fundamento na probabilidade do direito e no perigo de dano. ")
LISTA_IDS = "ID 123456789 | 14/10/2026 | Peticao | 0001234-56.2026.8.99.0000 | fls. 123-145\n"
JSON_DUO = '{"id":"101","tipo":"Decisao","data":"18/03/2026","paginas":12,"assinado":true},\n'


def amostra(base: str, tamanho: int = 30000) -> str:
    return (base * (tamanho // len(base) + 1))[:tamanho]


_PESSOAS = [
    "Antônio Conceição de Araújo", "Inês Magalhães Guimarães", "Bárbara Simões Brandão", "Sérgio Galvão Falcão",
    "Cecília Assunção Leitão", "Mônica Gonçalves Negrão", "Lúcia Câmara Patrício", "Fábio Jerônimo de Sá",
    "Tânia Conceição Lustosa", "Márcio Antônio Peçanha", "Vânia Célia Sampaio", "João Emílio Bião",
    "Núbia Iná Açucena", "Cláudio Inácio Românico", "Eugênia Lúcio Malcher", "Délio Aurélio Tarcísio",
]
_ORGAOS = [
    "Tribunal de Justiça", "Vara da Fazenda Pública de São João das Águas", "Terceira Câmara Cível",
    "Juizado Especial de Itaúna do Sertão", "Ministério Público Estadual", "Defensoria Pública",
    "Procuradoria-Geral do Município de Conceição da Chapada", "Secretaria Municipal de Saúde",
    "Instituto de Previdência dos Servidores", "Conselho Tutelar de Santa Luzia do Paraguaçu",
    "Cartório de Registro de Imóveis de Jequié Velho", "Superintendência de Trânsito",
    "Coordenação de Execução Orçamentária", "Corregedoria das Comarcas do Interior",
]
_TITULOS = [
    "DECISÃO INTERLOCUTÓRIA", "CERTIDÃO DE PUBLICAÇÃO", "RELATÓRIO", "FUNDAMENTAÇÃO JURÍDICA", "DISPOSITIVO",
    "ACÓRDÃO", "PETIÇÃO INICIAL", "CONTESTAÇÃO", "RÉPLICA", "PROCURAÇÃO", "SENTENÇA", "EMBARGOS DE DECLARAÇÃO",
    "AGRAVO DE INSTRUMENTO", "MANIFESTAÇÃO MINISTERIAL", "EXPEDIÇÃO DE ALVARÁ", "INTIMAÇÃO ELETRÔNICA",
]
_FRASES = [
    "Trata-se de ação de indenização por danos morais e materiais ajuizada por {p} em face de {o}.",
    "A petição inicial narra que a demandante sofreu lesão à honra, à imagem e à dignidade após a negativação indevida.",
    "O Excelentíssimo Senhor Doutor {p}, em despacho saneador, determinou a produção de prova pericial contábil.",
    "Citado, o réu apresentou contestação, arguindo preliminar de ilegitimidade passiva e prescrição trienal.",
    "A audiência de conciliação restou infrutífera, razão pela qual os autos vieram conclusos para deliberação.",
    "Não há nulidades a declarar, tampouco questões prejudiciais pendentes de apreciação por este Juízo.",
    "O {o} manifestou-se pela procedência parcial do pedido, sem prejuízo da reparação cabível à vítima.",
    "Compulsando os autos, verifica-se que a notificação extrajudicial foi recebida por {p} antes do vencimento.",
    "A jurisprudência é pacífica quanto à responsabilidade objetiva do fornecedor pela falha na prestação do serviço.",
    "Consoante lição da doutrina, a boa-fé objetiva impõe deveres anexos de lealdade, informação e cooperação.",
    "Ademais, o laudo técnico não evidenciou nexo de causalidade entre a conduta omissiva e o prejuízo alegado.",
    "Quanto à gratuidade de justiça, a declaração de hipossuficiência goza de presunção relativa de veracidade.",
    "O exequente requereu a penhora de numerário por meio eletrônico, além da inclusão do nome no cadastro de inadimplentes.",
    "A União e o Município de São Tomé das Laranjeiras não integram a lide, inexistindo litisconsórcio necessário.",
    "É inaplicável a súmula invocada, porquanto os precedentes que a originaram versam sobre situação fática diversa.",
    "Foi deferida a tutela de urgência, ante a probabilidade do direito e o perigo de dano irreparável à saúde.",
    "A operadora de saúde não comprovou a exclusão contratual, ônus que lhe competia por força da inversão probatória.",
    "Sobreveio a informação de que {p} faleceu no curso da demanda, impondo-se a habilitação dos sucessores.",
    "Às fls. indicadas, consta certidão do oficial de justiça atestando a impossibilidade de localização do executado.",
    "A sentença merece reforma apenas no tocante aos honorários sucumbenciais, fixados em patamar desproporcional.",
    "Vistos, relatados e discutidos estes autos, acordam os Desembargadores integrantes da {o}, por unanimidade.",
    "A Fazenda Pública foi intimada para impugnar os cálculos, permanecendo silente até o término do prazo.",
    "Oficie-se à {o}, solicitando informações sobre o benefício previdenciário concedido a {p}.",
    "Não obstante as alegações recursais, o conjunto probatório revela má-fé e conduta temerária do apelante.",
    "A testemunha {p} afirmou, sob compromisso, que presenciou a assinatura do contrato de compra e venda.",
    "Decorrido o prazo sem manifestação, certifique-se o trânsito em julgado e arquivem-se com as cautelas de praxe.",
    "O benefício assistencial pressupõe a comprovação da miserabilidade e da deficiência de longo prazo.",
    "Há evidente excesso de execução, pois a atualização monetária incidiu em duplicidade sobre a mesma parcela.",
    "A cláusula penal é abusiva e deve ser reduzida equitativamente, à luz da função social do contrato.",
    "Por conseguinte, julgo extinto o processo sem resolução do mérito, por ausência de interesse processual.",
    "A autora é pessoa idosa, razão pela qual se determina a tramitação prioritária e a intervenção ministerial.",
    "O réu não se desincumbiu de demonstrar a regularidade da cobrança, tampouco a origem da dívida impugnada.",
    "Remetam-se os autos à contadoria judicial para apuração do saldo devedor, com urgência e sob sigilo.",
    "Trata-se de agravo interposto por {p} contra a decisão que indeferiu a produção de prova testemunhal.",
    "A reintegração de posse exige demonstração do esbulho, da data em que ocorreu e da continuação da posse.",
    "Em homenagem ao contraditório, intime-se a parte contrária para, querendo, responder no prazo legal.",
    "Condeno o vencido ao pagamento das custas e da verba honorária, observada a suspensão da exigibilidade.",
]


def texto_juridico_acentuado(tamanho: int = 30000) -> str:
    """Peças processuais fictícias: acentos, maiúsculas, nomes próprios e vocabulário variado.

    Sem sorteio: os índices andam em passos diferentes sobre listas de tamanhos diferentes, para o texto
    não repetir a mesma sequência e sair sempre igual, já que o valor real foi medido sobre ele.
    """
    partes: list[str] = []
    atual = i = 0
    while atual < tamanho:
        if i % 7 == 0:
            parte = f"\n\n{_TITULOS[i // 7 % len(_TITULOS)]}\n\n"
        else:
            parte = _FRASES[i * 5 % len(_FRASES)].format(p=_PESSOAS[i * 3 % len(_PESSOAS)],
                                                         o=_ORGAOS[i * 7 % len(_ORGAOS)]) + " "
        partes.append(parte)
        atual += len(parte)
        i += 1
    return "".join(partes)[:tamanho]
