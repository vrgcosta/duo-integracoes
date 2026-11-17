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
  em tokens, nunca em caracteres.

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
        self.assertEqual(self.regra["tentativas"], 2)
        self.assertEqual(self.regra["origem"], "resposta")

    def test_casa_as_frases_reais(self) -> None:
        for frase in (
            "Não foi possível extrair o conteúdo decisório da sentença, pois o limite de execução de ferramentas foi atingido antes da leitura do texto do documento 96922725.",
            "Não foi possível acessar os documentos do processo, pois o limite de chamadas de ferramentas disponíveis para este subagente foi atingido antes da consulta aos autos",
            "O limite de chamadas de ferramentas atingiu o teto.",
            "Atingi o limite de ferramenta nesta conversa.",
            "Tool call limit reached.",
            "Contudo, não foi possível verificar o teor do que ela decidiu, pois a ferramenta anexar_processo_pdf entregou apenas o Volume 1 de 7 (páginas 1 a 100 de um total de 643 páginas), não havendo mais chamadas de ferramentas disponíveis nesta etapa para carregar o volume final ou extrair o texto isolado do referido ID.",
            "Não há mais chamadas de ferramenta nesta etapa.",
            "O processo integral em PDF possui 643 páginas divididas em 7 volumes, e as chamadas de ferramentas disponíveis permitiram apenas a obtenção e leitura do Volume 1 (páginas 1 a 100).",
            "As chamadas de ferramentas se esgotaram antes do volume final.",
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
            f'{{"numero_processo": "8049312-80.2026.8.05.0000", "limite": {limite}}}}}\n{cerca}'
            for nome, limite in blocos
        )
        self.assertNotRegex(self.norm(texto), self.rx)


if __name__ == "__main__":
    unittest.main()
