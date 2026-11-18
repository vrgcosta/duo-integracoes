"""Confere os plugins publicados contra o contrato do motor do Duo (contrato/contrato-motor.json).

O contrato vem do PJe+R (apps/extension/scripts/exportar-contrato-plugins.ts) e diz, campo a campo,
a quem ele se aplica (`escopo`: comum ou um mecanismo, como pagina, servidor, emulacao, nativo,
formulario) e se precisa estar no plugin (`nivel`: obrigatorio, recomendado, opcional). O teste:

- descobre os mecanismos de cada plugin pelo proprio JSON (regras em `mecanismos` do contrato);
- falha com campo que o motor nao conhece (erro de digitacao ou campo que saiu do motor), com campo
  interno do motor e com campo de mecanismo que o plugin nao usa (copia errada de outro plugin);
- falha com campo obrigatorio ausente e com campo recomendado ausente sem motivo em
  contrato/alinhamento.json: quando o motor ganha um campo comum recomendado, ou um plugin passa a
  usar um, os outros plugins do mesmo tipo param aqui ate adotar o campo ou registrar por que nao
  se aplica;
- confere os metadados de publicacao: id unico, link de atualizacao apontando para o proprio
  arquivo, versao com tres numeros, versao nova quando o JSON mudou em relacao a origin/main, e a
  versao atual citada no README;
- mantem a pasta em ordem: na raiz de plugins/ so os plugins (*.blueprint.json) e o README; o resto
  em subpastas (contrato/, testes/). Os plugins nao saem da raiz: o link de atualizacao de cada
  instalacao aponta para plugins/<nome>.blueprint.json;
- aplica as regras de tela do Duo aos textos que o plugin leva a tela (anexos.limiteLeitura): tamanho
  em tokens, nunca em caracteres;
- passa a limpeza de texto dos plugins do Copilot (`stream.limpezaTexto`) por uma copia do limpador do
  motor, com o texto inteiro e em pedacos: a ancora de citacao sai, o resto do texto fica.

Rodar na raiz do repositorio:  py -m unittest discover -s plugins/testes -v
"""
import json
import re
import subprocess
import unittest
from pathlib import Path

PLUGINS = Path(__file__).resolve().parents[1]
RAIZ = PLUGINS.parent
CONTRATO = json.loads((PLUGINS / "contrato" / "contrato-motor.json").read_text(encoding="utf-8"))
ALINHAMENTO = json.loads((PLUGINS / "contrato" / "alinhamento.json").read_text(encoding="utf-8"))
ARQUIVOS = sorted(PLUGINS.glob("*.blueprint.json"))
# Na raiz de plugins/, alem dos plugins, so isto.
RAIZ_PERMITIDOS = {"README.md"}
# A regra da tela do Duo: tamanho em tokens, nunca em caracteres (a mesma do teste sem-caracteres-na-tela do motor).
RE_CARACTERES = re.compile(r"\bcaracteres\b|\bcar\.|\bchars\b", re.IGNORECASE)
NIVEIS_EXIGIDOS = ("obrigatorio", "recomendado")


def _ler(caminho: Path) -> dict:
    return json.loads(caminho.read_text(encoding="utf-8"))


def _valor(obj: object, caminho: str):
    """O valor no caminho pontuado, ou None se algum trecho falta."""
    atual = obj
    for parte in caminho.split("."):
        if not isinstance(atual, dict) or parte not in atual:
            return None
        atual = atual[parte]
    return atual


def _casa(plugin: dict, regra: dict) -> bool:
    valor = _valor(plugin, regra["campo"])
    if "igual" in regra:
        return valor == regra["igual"]
    if "diferente" in regra:
        return valor != regra["diferente"]
    if "existe" in regra:
        return (valor is not None) == regra["existe"]
    raise ValueError(f"regra de mecanismo sem condicao: {regra}")


def tipo_do_plugin(plugin: dict) -> str:
    for tipo, regra in CONTRATO["detectarTipo"].items():
        if _casa(plugin, regra):
            return tipo
    return "provedor"


def mecanismos(plugin: dict, tipo: str) -> set:
    regras = CONTRATO["mecanismos"].get(tipo, {})
    return {"comum"} | {nome for nome, regra in regras.items() if _casa(plugin, regra)}


def conferir(plugin: dict, nome_arquivo: str) -> list:
    """Os problemas do plugin contra o contrato, um por linha."""
    tipo = tipo_do_plugin(plugin)
    campos = CONTRATO["tipos"][tipo]["campos"]
    ativos = mecanismos(plugin, tipo)
    excecoes = ALINHAMENTO.get("excecoes", {}).get(nome_arquivo, {})
    filhos = {}
    for caminho in campos:
        pai, _, nome = caminho.rpartition(".")
        filhos.setdefault(pai, []).append(nome)
    problemas = []
    vistos = set()

    def exigir(no: dict, caminho: str) -> None:
        for nome in filhos.get(caminho, []):
            cam = f"{caminho}.{nome}" if caminho else nome
            campo = campos[cam]
            if campo["nivel"] not in NIVEIS_EXIGIDOS or campo["escopo"] not in ativos or nome in no:
                continue
            if campo["nivel"] == "recomendado" and cam in excecoes:
                vistos.add(cam)
                continue
            problemas.append(
                f"{cam}: {campo['nivel']} para {campo['escopo']} e ausente"
                + ("" if campo["nivel"] == "obrigatorio" else " (adote, ou registre o motivo em alinhamento.json)")
            )

    def andar(valor: object, caminho: str) -> None:
        if isinstance(valor, list):
            for item in valor:
                andar(item, caminho)
            return
        if not isinstance(valor, dict):
            return
        exigir(valor, caminho)
        for chave, filho in valor.items():
            cam = f"{caminho}.{chave}" if caminho else chave
            campo = campos.get(cam)
            if campo is None:
                problemas.append(f"{cam}: campo que o motor nao conhece (erro de digitacao, ou saiu do motor?)")
                continue
            if campo["escopo"] == "interno":
                problemas.append(f"{cam}: campo interno do motor, nao vai no plugin")
                continue
            if campo["escopo"] not in ativos:
                problemas.append(
                    f"{cam}: campo de {campo['escopo']}, mas o plugin nao usa esse mecanismo "
                    f"(usa: {', '.join(sorted(ativos - {'comum'})) or 'nenhum'})"
                )
                continue
            if not campo.get("livre"):
                andar(filho, cam)

    andar(plugin, "")
    for cam, motivo in excecoes.items():
        if not str(motivo).strip():
            problemas.append(f"alinhamento.json: {cam} sem motivo")
        elif cam not in vistos:
            problemas.append(f"alinhamento.json: excecao de {cam} nao se aplica mais (o campo existe ou nao e exigido)")
    return problemas


def _na_main(arquivo: Path):
    """O JSON publicado em origin/main, ou None (arquivo novo, ou git indisponivel)."""
    rel = arquivo.relative_to(RAIZ).as_posix()
    try:
        saida = subprocess.run(
            ["git", "show", f"origin/main:{rel}"], cwd=RAIZ, capture_output=True, check=True
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        return None
    return json.loads(saida.decode("utf-8"))


class TestContrato(unittest.TestCase):
    def test_contrato_e_alinhamento_carregam(self) -> None:
        self.assertEqual(CONTRATO["formato"], 1)
        self.assertTrue(ARQUIVOS, "nenhum plugin em plugins/")
        nomes = {a.name for a in ARQUIVOS}
        for arquivo in ALINHAMENTO.get("excecoes", {}):
            self.assertIn(arquivo, nomes, f"alinhamento.json cita plugin inexistente: {arquivo}")

    def test_plugins_conferem_com_o_contrato(self) -> None:
        for arquivo in ARQUIVOS:
            with self.subTest(plugin=arquivo.name):
                problemas = conferir(_ler(arquivo), arquivo.name)
                self.assertEqual(problemas, [], "\n" + "\n".join(problemas))


class TestPublicacao(unittest.TestCase):
    def test_ids_unicos(self) -> None:
        ids = [_ler(a)["id"] for a in ARQUIVOS]
        self.assertEqual(len(ids), len(set(ids)), f"id repetido: {ids}")

    def test_metadados(self) -> None:
        for arquivo in ARQUIVOS:
            with self.subTest(plugin=arquivo.name):
                plugin = _ler(arquivo)
                self.assertRegex(plugin.get("versao", ""), r"^\d+\.\d+\.\d+$")
                url = _valor(plugin, "atualizacao.url") or ""
                self.assertTrue(
                    url.endswith(f"/plugins/{arquivo.name}"),
                    f"atualizacao.url deve apontar para o proprio arquivo: {url}",
                )

    def test_versao_sobe_quando_o_json_muda(self) -> None:
        for arquivo in ARQUIVOS:
            with self.subTest(plugin=arquivo.name):
                publicado = _na_main(arquivo)
                if publicado is None:
                    continue
                atual = _ler(arquivo)
                if atual != publicado:
                    self.assertNotEqual(
                        atual.get("versao"),
                        publicado.get("versao"),
                        "o JSON mudou em relacao a origin/main sem subir a versao",
                    )

    def test_raiz_so_tem_plugins(self) -> None:
        # Os plugins ficam na raiz (o link de atualizacao de cada instalacao aponta para ela); o resto, em subpastas.
        sobrando = sorted(
            p.name
            for p in PLUGINS.iterdir()
            if p.is_file() and not p.name.endswith(".blueprint.json") and p.name not in RAIZ_PERMITIDOS
        )
        self.assertEqual(sobrando, [], "na raiz de plugins/ so os plugins e o README; mova o resto para uma subpasta")

    def test_textos_de_tela_em_tokens(self) -> None:
        # As linhas da secao "Leitura de anexo" vao a tela do Duo como estao: tamanho em tokens, nunca em caracteres.
        # O dado `caracteres` pode ficar no JSON (dimensiona os volumes); o texto mostrado, nao.
        for arquivo in ARQUIVOS:
            with self.subTest(plugin=arquivo.name):
                limite = _valor(_ler(arquivo), "anexos.limiteLeitura") or {}
                textos = [limite.get("orientacao"), limite.get("fonte")]
                for linha in limite.get("linhas") or []:
                    textos += [linha.get(c) for c in ("rotulo", "destaque", "texto", "detalhe")]
                    for exemplo in linha.get("exemplos") or []:
                        textos += [exemplo.get("rotulo"), exemplo.get("valor")]
                achados = [t for t in textos if isinstance(t, str) and RE_CARACTERES.search(t)]
                self.assertEqual(achados, [], "a tela fala em tokens, nao em caracteres")

    def test_readme_cita_a_versao_atual(self) -> None:
        readme = (PLUGINS / "README.md").read_text(encoding="utf-8")
        for arquivo in ARQUIVOS:
            with self.subTest(plugin=arquivo.name):
                versao = _ler(arquivo)["versao"]
                self.assertTrue(f"**{versao}**" in readme, f"README nao cita a versao {versao}")


class TestConferencia(unittest.TestCase):
    """O proprio verificador: cada regra pega o que deve pegar."""

    def setUp(self) -> None:
        self.base = _ler(PLUGINS / "chatgpt-web.blueprint.json")
        self.nome = "chatgpt-web.blueprint.json"

    def _problemas(self, plugin: dict) -> str:
        return "\n".join(conferir(plugin, self.nome))

    def test_campo_desconhecido(self) -> None:
        self.base["sesoes"] = {"padrao": 2}
        self.assertIn("sesoes: campo que o motor nao conhece", self._problemas(self.base))

    def test_campo_de_outro_mecanismo(self) -> None:
        self.base["ferramentas"]["enviarResultado"] = {"caminho": "/x", "corpo": {}}
        self.assertIn("ferramentas.enviarResultado: campo de nativo", self._problemas(self.base))

    def test_campo_interno(self) -> None:
        self.base["pagina"]["rede"]["quadros"] = {}
        self.assertIn("pagina.rede.quadros: campo interno", self._problemas(self.base))

    def test_obrigatorio_ausente(self) -> None:
        del self.base["stream"]["framing"]
        self.assertIn("stream.framing: obrigatorio", self._problemas(self.base))

    def test_recomendado_ausente(self) -> None:
        self.base.pop("sessoes")
        self.assertIn("sessoes: recomendado para comum e ausente", self._problemas(self.base))

    def test_molde_livre_nao_e_conferido(self) -> None:
        self.base["enviar"]["corpo"] = {"qualquer": {"coisa": 1}}
        self.assertNotIn("enviar.corpo", self._problemas(self.base))


class TestRegraLimiteFerramentas(unittest.TestCase):
    """A regra falha_pronta do Gemini web para o falso 'limite de chamadas de ferramentas'."""

    @classmethod
    def setUpClass(cls) -> None:
        import re
        import unicodedata

        plugin = _ler(PLUGINS / "gemini-web.blueprint.json")
        regras = [
            r for r in plugin["erros"]["regras"]
            if r.get("tipo") == "falha_pronta" and "ferramenta" in r.get("texto", "")
        ]
        assert len(regras) == 1, "regra do limite de ferramentas ausente"
        cls.regra = regras[0]
        cls.rx = re.compile(cls.regra["texto"])
        cls.norm = staticmethod(
            lambda s: "".join(
                c for c in unicodedata.normalize("NFD", s.lower()) if not unicodedata.combining(c)
            )
        )

    def test_parametros(self) -> None:
        self.assertEqual(self.regra["maxCaracteres"], 2000)
        self.assertEqual(self.regra["inicioCaracteres"], 400)
        self.assertEqual(self.regra["tentativas"], 2)
        self.assertEqual(self.regra["origem"], "resposta")

    def test_resposta_longa_que_abre_com_o_falso_limite(self) -> None:
        # Visto em 2026-11-18: a frase abriu uma analise parcial de uns 3 mil caracteres, e o teto de 2000 a deixou
        # passar como resposta final. Com inicioCaracteres, o motor olha so o comeco (a extensao velha ignora o campo
        # e segue com o maxCaracteres).
        resposta = (
            "Não foi possível ler o texto integral das peças do processo porque o limite de chamadas de ferramentas "
            "se esgotou após a listagem inicial dos documentos, expedientes e movimentos. "
            + "Com base nos metadados, segue o levantamento parcial. " * 60
        )
        self.assertGreater(len(resposta), self.regra["maxCaracteres"])
        self.assertRegex(self.norm(resposta[: self.regra["inicioCaracteres"]]), self.rx)

    def test_casa_as_frases_reais(self) -> None:
        for frase in (
            "Não foi possível extrair o conteúdo decisório da sentença, pois o limite de execução de ferramentas foi atingido antes da leitura do texto do documento 12345678.",
            "Não foi possível acessar os documentos do processo, pois o limite de chamadas de ferramentas disponíveis para este subagente foi atingido antes da consulta aos autos",
            "O limite de chamadas de ferramentas atingiu o teto.",
            "Atingi o limite de ferramenta nesta conversa.",
            "Tool call limit reached.",
            "Contudo, não foi possível verificar o teor do que ela decidiu, pois a ferramenta anexar_processo_pdf entregou apenas o Volume 1 de 7 (páginas 1 a 100 de um total de 643 páginas), não havendo mais chamadas de ferramentas disponíveis nesta etapa para carregar o volume final ou extrair o texto isolado do referido ID.",
            "Não há mais chamadas de ferramenta nesta etapa.",
            "O processo integral em PDF possui 643 páginas divididas em 7 volumes, e as chamadas de ferramentas disponíveis permitiram apenas a obtenção e leitura do Volume 1 (páginas 1 a 100).",
            "As chamadas de ferramentas se esgotaram antes do volume final.",
            "Não foi possível ler os documentos de IDs 12345678 e 87654321 porque o limite de chamadas de ferramentas disponível nesta execução é zero (0 remaining), impedindo a invocação de obter_texto_documento.",
            "Não foi possível executar obter_texto_documento nos IDs 12345678 e 87654321 porque o sistema interno de ferramentas permanece bloqueado com 0 remaining chamadas disponíveis neste ambiente, impedindo a emissão de novos blocos duo-ferramenta.",
        ):
            with self.subTest(frase=frase):
                self.assertRegex(self.norm(frase), self.rx)

    def test_nao_casa_texto_juridico(self) -> None:
        for frase in (
            "O prazo tem como data limite 30/11.",
            "Jornada com limite de 20 horas semanais.",
            "Há um limite de 100 itens por chamada.",
            "O autor extrapolou o limite da razoabilidade. Ferramentas de trabalho foram apreendidas.",
            "Não há mais chamadas telefônicas registradas; as ferramentas foram devolvidas.",
            "Restam 10 parcelas, e o sistema de cobrança foi bloqueado pelo juízo.",
            "O sistema de ferramentas de busca da Receita indicou bens.",
        ):
            with self.subTest(frase=frase):
                self.assertNotRegex(self.norm(frase), self.rx)

    def test_nao_casa_pedido_de_ferramentas(self) -> None:
        # Resposta real de 2026-11-17: tres pedidos de ferramenta com o argumento "limite". A regra casava
        # 'limite": 30}}' com o rotulo 'duo-ferramenta' da cerca seguinte, e o Duo nao rodava as ferramentas.
        cerca = "`" * 3
        blocos = [
            ("listar_documentos_processo", 100),
            ("listar_expedientes_processo", 30),
            ("listar_movimentos_processo_pje", 100),
        ]
        texto = "\n\n".join(
            f'{cerca}duo-ferramenta\n{{"nome": "{nome}", "argumentos": '
            f'{{"numero_processo": "0001234-56.2026.8.99.0000", "limite": {limite}}}}}\n{cerca}'
            for nome, limite in blocos
        )
        self.assertNotRegex(self.norm(texto), self.rx)


class LimpadorTexto:
    """Copia do LimpadorTexto do motor (engine/limpeza-texto.ts): remove de `de` ate `ate`, inclusive, com o
    texto chegando em pedacos. Segura o buffer a partir de um `de` sem `ate` e a cauda que ainda pode virar um
    `de`; marcador que nunca fecha sai cru no `finalizar` (fail-open, como no motor)."""

    def __init__(self, regras: list) -> None:
        self.regras = regras
        self.buf = ""
        self.maior_de = max((len(r["de"]) for r in regras), default=0)

    def _proxima_abertura(self):
        achado = None
        for regra in self.regras:
            i = self.buf.find(regra["de"])
            if i != -1 and (achado is None or i < achado[0]):
                achado = (i, regra)
        return achado

    def _cauda_suspeita(self) -> int:
        for n in range(min(self.maior_de - 1, len(self.buf)), 0, -1):
            cauda = self.buf[-n:]
            if any(r["de"].startswith(cauda) for r in self.regras):
                return n
        return 0

    def alimentar(self, trecho: str) -> str:
        if not self.regras:
            return trecho
        self.buf += trecho
        saida = ""
        while True:
            achado = self._proxima_abertura()
            if achado is None:
                break
            i, regra = achado
            fim = self.buf.find(regra["ate"], i + len(regra["de"]))
            saida += self.buf[:i]
            if fim == -1:
                self.buf = self.buf[i:]
                return saida
            self.buf = self.buf[fim + len(regra["ate"]):]
        cauda = self._cauda_suspeita()
        saida += self.buf[: len(self.buf) - cauda]
        self.buf = self.buf[len(self.buf) - cauda:]
        return saida

    def finalizar(self) -> str:
        resto, self.buf = self.buf, ""
        return resto


class TestLimpezaCitacaoCopilot(unittest.TestCase):
    """A ancora de citacao do Copilot sai do texto: `<cite>turn1search1</cite>` pelo Chathub (o socket do Duo)
    e o formato da OpenAI com caracteres de uso privado pelo StreamHub (o socket novo da pagina)."""

    PLUGINS_COPILOT = ("copilot-mcloud-web.blueprint.json", "copilot-m365-web.blueprint.json")
    INICIO, SEPARADOR, FIM = chr(0xE200), chr(0xE202), chr(0xE201)

    @classmethod
    def setUpClass(cls) -> None:
        cls.regras = {nome: _ler(PLUGINS / nome)["stream"]["limpezaTexto"] for nome in cls.PLUGINS_COPILOT}

    def _limpar(self, regras: list, texto: str, passo: int = 0) -> str:
        limpador = LimpadorTexto(regras)
        pedacos = [texto] if not passo else [texto[i : i + passo] for i in range(0, len(texto), passo)]
        return "".join(limpador.alimentar(p) for p in pedacos) + limpador.finalizar()

    def _conferir(self, texto: str, esperado: str) -> None:
        for nome, regras in self.regras.items():
            for passo in (0, 1, 2, 3, 7):
                with self.subTest(plugin=nome, passo=passo):
                    self.assertEqual(self._limpar(regras, texto, passo), esperado)

    def test_os_dois_plugins_tem_as_mesmas_regras(self) -> None:
        mcloud, m365 = (self.regras[n] for n in self.PLUGINS_COPILOT)
        self.assertEqual(mcloud, m365)

    def test_cite_do_chathub_sai_com_o_espaco_antes(self) -> None:
        texto = (
            "- Pedido: tutela recursal para o fornecimento do medicamento. <cite>turn1search1</cite>\n"
            "- Contrarrazoes: pede o desprovimento.<cite>turn1search1</cite><cite>turn1search3</cite>\n"
            "Conclusao sem fonte."
        )
        esperado = (
            "- Pedido: tutela recursal para o fornecimento do medicamento.\n"
            "- Contrarrazoes: pede o desprovimento.\n"
            "Conclusao sem fonte."
        )
        self._conferir(texto, esperado)

    def test_formato_da_openai_do_streamhub(self) -> None:
        ancora = f"{self.INICIO}cite{self.SEPARADOR}turn1search1{self.SEPARADOR}turn1search6{self.FIM}"
        self._conferir(f"Promulgada em 1988. {ancora}\n- Segundo ponto.", "Promulgada em 1988. \n- Segundo ponto.")

    def test_texto_sem_ancora_nao_muda(self) -> None:
        texto = "O juiz determinou que a parte cite a fonte; a <citacao> do art. 178 e o termo turn1search1 ficam."
        self._conferir(texto, texto)

    def test_cite_que_nao_fecha_sai_cru(self) -> None:
        # Fail-open do motor: melhor mostrar a marca do que engolir o resto da resposta.
        self._conferir("Texto final. <cite>turn1search1", "Texto final. <cite>turn1search1")


if __name__ == "__main__":
    unittest.main()
