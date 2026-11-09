"""Catálogo compacto de ferramentas com meta-ferramenta.

Modelos locais se perdem com dezenas de definições completas. O Duo manda as favoritas e
resumos de uma linha das demais; o modelo vê só as favoritas e uma meta-ferramenta
(`carregar_ferramentas`) que traz as outras sob demanda, valendo para o resto da conversa.
"""

from __future__ import annotations

from . import leitura

META = "carregar_ferramentas"

_ABERTURA_META = (
    "Carrega ferramentas adicionais do DUO para esta conversa. "
    "Chame antes de usar qualquer uma destas:\n"
)


def _primeira_frase(descricao: str) -> str:
    return (descricao or "").split(". ", 1)[0].strip()


class Catalogo:
    def __init__(self, defs: list[dict], compacto: dict | None,
                 ativas: list[str], carregadas: list[str]) -> None:
        # `ativas` restringe sempre, com ou sem compacto: é escolha do usuário.
        self._defs = [d for d in defs if d["name"] in ativas] if ativas else list(defs)
        self._compacto = compacto
        self._carregadas = list(carregadas)

    @property
    def carregadas(self) -> list[str]:
        return list(self._carregadas)

    def _adiadas(self) -> list[dict]:
        if not self._compacto:
            return []
        favoritas = set(self._compacto.get("favoritas") or [])
        return [d for d in self._defs if d["name"] not in favoritas]

    def _meta(self, adiadas: list[dict]) -> dict:
        resumos = (self._compacto or {}).get("resumos") or {}
        linhas = [f"• {d['name']} — {resumos.get(d['name']) or _primeira_frase(d.get('description', ''))}"
                  for d in adiadas]
        return {
            "name": META,
            "description": _ABERTURA_META + "\n".join(linhas),
            "input_schema": {
                "type": "object",
                "properties": {"nomes": {"type": "array", "items": {"type": "string"},
                                         "description": "Nomes das ferramentas a carregar"}},
                "required": ["nomes"],
            },
        }

    def visiveis(self, com_leitura: bool = False) -> list[dict]:
        """As definições a mostrar ao modelo.

        `com_leitura` acrescenta `ler_trecho_resultado`, só quando há marca dela na janela: fora
        disso ela seria um peso constante, numa janela que costuma ser pequena.
        """
        return self._normais() + ([leitura.DEFINICAO] if com_leitura else [])

    def _normais(self) -> list[dict]:
        adiadas = self._adiadas()
        if not adiadas:
            return list(self._defs)
        nomes_adiados = {d["name"] for d in adiadas}
        # Mantém a ordem original das defs: favoritas e carregadas intercaladas como vieram.
        mostradas = [d for d in self._defs
                     if d["name"] not in nomes_adiados or d["name"] in self._carregadas]
        # A meta fica sempre, listando todas as adiadas: o prompt do modelo não muda de
        # forma a confundi-lo quando ele já carregou algumas.
        return mostradas + [self._meta(adiadas)]

    def adiada(self, nome: str) -> bool:
        """Existe e o Duo a oferece, mas o modelo só viu o resumo dela."""
        return nome not in self._carregadas and any(d["name"] == nome for d in self._adiadas())

    def carregar(self, nome: str) -> None:
        if nome not in self._carregadas:
            self._carregadas.append(nome)

    def definicao(self, nome: str) -> dict | None:
        """A definição de uma ferramenta que o Duo ofereceu, visível ou não para o modelo."""
        return next((d for d in self._defs if d["name"] == nome), None)

    def eh_meta(self, nome: str) -> bool:
        return nome == META

    def eh_leitura(self, nome: str) -> bool:
        # Vale mesmo sem estar na lista: o modelo pode repetir a chamada que já viu no histórico, e
        # ela só lê o que já é da conversa. Por isso também não depende das `ativas`.
        return nome == leitura.NOME

    def conhece(self, nome: str) -> bool:
        return nome != META and any(d["name"] == nome for d in self.visiveis())

    def responder_meta(self, argumentos: dict) -> str:
        # Modelos pequenos erram o formato. Aceita um nome solto e ignora o que não for texto:
        # um TypeError aqui derrubaria o turno inteiro por causa de um argumento torto.
        pedidos = argumentos.get("nomes") if isinstance(argumentos, dict) else None
        if isinstance(pedidos, str):
            pedidos = [pedidos]
        elif not isinstance(pedidos, list):
            pedidos = []
        pedidos = [nome for nome in pedidos if isinstance(nome, str)]
        validos = {d["name"] for d in self._adiadas()}
        novos, disponiveis, invalidos = [], [], []
        for nome in pedidos:
            if nome in validos:
                if nome not in self._carregadas:
                    self._carregadas.append(nome)
                if nome not in novos:
                    novos.append(nome)
            elif self.conhece(nome) or self.eh_meta(nome) or self.eh_leitura(nome):
                # Chamá-la de desconhecida fazia o modelo desistir de um caminho certo.
                if nome not in disponiveis:
                    disponiveis.append(nome)
            elif nome not in invalidos:
                invalidos.append(nome)
        partes = []
        if novos:
            partes.append(f"Ferramentas carregadas: {', '.join(novos)}. Já pode chamá-las.")
        if disponiveis:
            partes.append(f"Já disponíveis: {', '.join(disponiveis)}. Não precisam ser carregadas: pode chamá-las.")
        if not partes:
            partes.append("Nenhuma ferramenta carregada.")
        if invalidos:
            partes.append(f"Desconhecidas: {', '.join(invalidos)}.")
        return " ".join(partes)
