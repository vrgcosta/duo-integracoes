"""Serviço: estado compartilhado do servidor e os handlers das rotas.

Cada handler recebe `(h, consulta, *grupos)` — `h` é o Manipulador da requisição — e devolve o
objeto JSON da resposta, ou None quando já respondeu por conta própria (SSE).
"""

from __future__ import annotations

import threading

from . import VERSAO, compactacao, historico, medidor
from .armazenamento import Armazenamento
from .backends.base import Backend, ErroBackend
from .http import ErroHTTP
from .sse import EmissorSSE
from .turno import Turno

_ID = r"([0-9a-f-]{36})"
_MSG_COMPACTANDO = "Há uma compactação em andamento nesta conversa. Espere ela terminar ou cancele-a."

# A ordem importa: "/busca" precisa vir antes de "/{id}" para não ser lida como id.
ROTAS: list[tuple[str, str, str]] = [
    ("GET", r"/api/contexto", "contexto"),
    ("GET", r"/api/modelos", "modelos"),
    ("GET", r"/api/conversas", "listar"),
    ("POST", r"/api/conversas", "criar"),
    ("GET", r"/api/conversas/busca", "buscar"),
    ("GET", rf"/api/conversas/{_ID}", "ler"),
    ("PUT", rf"/api/conversas/{_ID}", "renomear"),
    ("DELETE", rf"/api/conversas/{_ID}", "excluir"),
    ("PUT", rf"/api/conversas/{_ID}/pensamento", "pensamento"),
    ("GET", rf"/api/conversas/{_ID}/janela", "janela"),
    ("PUT", rf"/api/conversas/{_ID}/janela", "janela_excluir"),
    ("POST", rf"/api/conversas/{_ID}/compactar", "compactar"),
    ("DELETE", rf"/api/conversas/{_ID}/compactar", "descompactar"),
    ("POST", rf"/api/conversas/{_ID}/completion", "completion"),
    ("POST", rf"/api/conversas/{_ID}/tool_result", "tool_result"),
    ("GET", r"/api/projetos", "projetos"),
    ("GET", r"/api/projetos/([a-z0-9-]+)/conversas", "projeto_conversas"),
    ("POST", r"/api/upload", "upload"),
]


class Servico:
    ROTAS = ROTAS

    def __init__(self, cfg: dict, pasta_dados: str, backend: Backend, sistema_padrao: str) -> None:
        self.cfg = cfg
        self.armazenamento = Armazenamento(pasta_dados)
        self.backend = backend
        self.sistema_padrao = sistema_padrao
        # Um turno por conversa em andamento, e as compactações (rodando, ou a que falhou por último). A trava
        # protege os dois juntos: um turno e uma compactação na mesma conversa não podem começar ao mesmo tempo.
        self.turnos: dict[str, Turno] = {}
        self.compactacoes: dict[str, compactacao.Compactacao] = {}
        self.lock_turnos = threading.Lock()

    def fechar(self) -> None:
        """Para as compactações e libera o banco do histórico; chamado quando o servidor encerra."""
        with self.lock_turnos:
            abertas = list(self.compactacoes.values())
            self.compactacoes.clear()
        for aberta in abertas:
            aberta.cancelar()
        for aberta in abertas:
            aberta.aguardar(2)
        self.armazenamento.fechar()

    # -- contexto e modelos ------------------------------------------------

    def contexto(self, h, consulta):
        return {"conta": "local", "backend": self.backend.nome, "versao": VERSAO}

    def modelos(self, h, consulta):
        itens = [{"id": "padrao", "label": f"Padrão do servidor ({self.cfg['modelo_padrao']})"}]
        try:
            descobertos = self.backend.listar_modelos()
        except ErroBackend:
            # Motor fora do ar: o seletor ainda mostra o padrão, e o erro aparece ao conversar.
            descobertos = []
        for m in descobertos:
            if m.nuvem and not self.cfg.get("permitir_modelos_nuvem"):
                continue
            itens.append({"id": m.id, "label": m.rotulo})
        return {"modelos": itens}

    # -- conversas ---------------------------------------------------------

    def listar(self, h, consulta):
        return self.armazenamento.listar_conversas()

    def criar(self, h, consulta):
        corpo = h.ler_json()
        nome, modelo, projeto = corpo.get("name") or "", corpo.get("model") or "padrao", corpo.get("project_uuid")
        if not isinstance(nome, str) or not isinstance(modelo, str):
            raise ErroHTTP("name e model devem ser texto", 400)
        if projeto is not None and not isinstance(projeto, str):
            raise ErroHTTP("project_uuid deve ser texto", 400)
        return {"uuid": self.armazenamento.criar_conversa(nome, modelo, projeto or None)["uuid"]}

    def buscar(self, h, consulta):
        return self.armazenamento.buscar((consulta.get("termo") or [""])[0])

    def ler(self, h, consulta, cid):
        conversa = self.armazenamento.ler_conversa(cid)
        if conversa is None:
            raise ErroHTTP("conversa inexistente", 404)
        return historico.conversa_visivel(conversa)

    def renomear(self, h, consulta, cid):
        nome = h.ler_json().get("name")
        if not isinstance(nome, str):
            raise ErroHTTP("name deve ser texto", 400)
        if not self.armazenamento.atualizar_conversa(cid, name=nome):
            raise ErroHTTP("conversa inexistente", 404)
        return {"ok": True}

    def excluir(self, h, consulta, cid):
        with self.lock_turnos:
            aberta = self.compactacoes.pop(cid, None)
        if aberta is not None:
            aberta.cancelar()
        if not self.armazenamento.excluir(cid):
            raise ErroHTTP("conversa inexistente", 404)
        return {"ok": True}

    def pensamento(self, h, consulta, cid):
        ativo = h.ler_json().get("ativo")
        if not isinstance(ativo, bool):
            raise ErroHTTP("ativo deve ser verdadeiro ou falso", 400)
        if not self.armazenamento.atualizar_conversa(cid, pensamento=ativo):
            raise ErroHTTP("conversa inexistente", 404)
        return {"ok": True}

    def janela(self, h, consulta, cid):
        conversa = self.armazenamento.ler_conversa(cid)
        if conversa is None:
            raise ErroHTTP("conversa inexistente", 404)
        return medidor.medir(self, conversa, (consulta.get("modelo") or [None])[0],
                             (consulta.get("contexto") or [None])[0])

    def janela_excluir(self, h, consulta, cid):
        excluidos = h.ler_json().get("excluidos")
        if not isinstance(excluidos, list) or any(not isinstance(item, str) for item in excluidos):
            raise ErroHTTP("excluidos deve ser uma lista de ids", 400)
        # A gravação fica sob a trava, junto com a checagem: é o que impede um item tirado agora de ir a um resumo
        # que está começando (ver `compactar`).
        with self.lock_turnos:
            # O item tirado agora já pode ter ido ao modelo, e voltaria no resumo. Melhor dizer logo que descartar
            # no fim uma compactação de vários minutos.
            if self._compactando(cid):
                raise ErroHTTP(_MSG_COMPACTANDO, 409)
            conversa, estado = self.armazenamento.ler_conversa(cid), self.armazenamento.estado_janela(cid)
            if conversa is None or estado is None:
                raise ErroHTTP("conversa inexistente", 404)
            # O painel manda a lista montada com o que vê. O que está tirado e ele não vê (por trás de um resumo, ou em
            # outro ramo) não pode voltar por não estar nela; e religar, ele só religa o que vê.
            visiveis = medidor.ids_visiveis(conversa, estado["resumo"])
            escondidos = [item for item in estado["excluidos"] if item not in visiveis]
            self.armazenamento.definir_excluidos(cid, list(dict.fromkeys(excluidos + escondidos)))
        return {"ok": True}

    def _compactando(self, cid: str) -> bool:
        """Só com a trava na mão."""
        aberta = self.compactacoes.get(cid)
        return aberta is not None and aberta.rodando

    def compactacao_publica(self, cid: str) -> dict | None:
        """O andamento, para a leitura da janela; quem lê fica sabendo do desfecho (ver `descompactar`)."""
        with self.lock_turnos:
            aberta = self.compactacoes.get(cid)
            if aberta is None:
                return None
            if not aberta.rodando:
                aberta.visto = True
            if aberta.estado == "pronta":
                del self.compactacoes[cid]
                return None
            return aberta.publico()

    def _exigir_livre_para_compactar(self, cid: str) -> None:
        """Só com a trava na mão."""
        if cid in self.turnos:
            raise ErroHTTP("Há um envio em andamento nesta conversa: espere a resposta terminar para compactar.", 409)
        if self._compactando(cid):
            raise ErroHTTP("Já há uma compactação em andamento nesta conversa.", 409)

    def compactar(self, h, consulta, cid):
        """Só inicia, e responde na hora: quem acompanha é o `GET /janela`."""
        corpo = h.ler_json()
        with self.lock_turnos:
            self._exigir_livre_para_compactar(cid)
        conversa = self.armazenamento.ler_conversa(cid)
        if conversa is None:
            raise ErroHTTP("conversa inexistente", 404)
        modelo = compactacao.escolher_modelo(self, conversa, corpo)
        with self.lock_turnos:
            # De novo: o motor pode ter demorado, e um envio pode ter começado nesse meio-tempo. O plano sai aqui
            # dentro: lido antes do registro, um PUT no intervalo seria aceito e o item tirado iria ao resumo.
            self._exigir_livre_para_compactar(cid)
            conversa = self.armazenamento.ler_conversa(cid)
            if conversa is None:
                raise ErroHTTP("conversa inexistente", 404)
            nova = compactacao.Compactacao(self, cid, compactacao.planejar(self, conversa, corpo, modelo))
            self.compactacoes[cid] = nova
        nova.iniciar()
        h.responder_json(202, {"estado": "rodando"})
        return None

    def descompactar(self, h, consulta, cid):
        """Cancela ou desfaz, com o mesmo verbo: o que distingue é o que quem chamou já viu.

        Se a compactação terminou e ninguém leu a janela depois, este é um cancelamento que chegou tarde. Apagar o
        resumo aí jogaria fora o que acabou de ficar pronto ou, se ela falhou, o anterior, que ainda valia.
        """
        if self.armazenamento.estado_janela(cid) is None:
            raise ErroHTTP("conversa inexistente", 404)
        with self.lock_turnos:
            aberta = self.compactacoes.pop(cid, None)
            if aberta is not None and aberta.rodando:
                aberta.cancelar()
                return {"ok": True, "cancelada": True}
            if aberta is not None and not aberta.visto:
                return {"ok": True, "cancelada": False, "terminou_antes": True}
        self.armazenamento.apagar_resumo(cid)
        return {"ok": True}

    def completion(self, h, consulta, cid):
        corpo = h.ler_json()
        if self.armazenamento.ler_conversa(cid) is None:
            raise ErroHTTP("conversa inexistente", 404)
        _validar_envio(corpo)
        turno = Turno(self, cid, corpo, EmissorSSE(_escritor(h), h.cliente_conectado))
        with self.lock_turnos:
            # Dois turnos na mesma conversa gravariam nós concorrentes no mesmo ramo.
            if cid in self.turnos:
                raise ErroHTTP("Já há um envio em andamento nesta conversa.", 409)
            # Um turno gravaria nós no trecho que está sendo resumido.
            if self._compactando(cid):
                raise ErroHTTP(_MSG_COMPACTANDO, 409)
            # O erro de uma compactação anterior deixa de valer: a conversa seguiu.
            self.compactacoes.pop(cid, None)
            self.turnos[cid] = turno
        try:
            h.iniciar_sse()
            turno.executar()
        finally:
            with self.lock_turnos:
                if self.turnos.get(cid) is turno:
                    del self.turnos[cid]
        return None

    def tool_result(self, h, consulta, cid):
        corpo = h.ler_json()
        with self.lock_turnos:
            turno = self.turnos.get(cid)
        if turno is None:
            raise ErroHTTP("Não há envio em andamento aguardando resultados nesta conversa.", 409)
        if not isinstance(corpo.get("tool_use_id"), str):
            raise ErroHTTP("tool_use_id deve ser texto", 400)
        if not turno.entregar_resultado(corpo):
            raise ErroHTTP("Esta conversa não aguarda resultado com esse tool_use_id.", 409)
        return {"ok": True}

    # -- projetos e anexos -------------------------------------------------

    def projetos(self, h, consulta):
        return self.armazenamento.listar_projetos()

    def projeto_conversas(self, h, consulta, pid):
        return self.armazenamento.listar_conversas(pid)

    def upload(self, h, consulta):
        nome, mime, dados = h.ler_multipart("file")
        return {"file_uuid": self.armazenamento.salvar_arquivo(nome, mime, dados)}


def _validar_envio(corpo: dict) -> None:
    """Recusa com 400, antes de abrir o SSE, o corpo que o turno não saberia ler."""
    for campo in ("prompt", "model", "parent_message_uuid", "system"):
        if corpo.get(campo) is not None and not isinstance(corpo[campo], str):
            raise ErroHTTP(f"{campo} deve ser texto", 400)
    for campo in ("tools", "files", "attachments"):
        if corpo.get(campo) is not None and not isinstance(corpo[campo], list):
            raise ErroHTTP(f"{campo} deve ser uma lista", 400)
    if any(not isinstance(t, dict) or not isinstance(t.get("name"), str) for t in corpo.get("tools") or []):
        raise ErroHTTP("cada item de tools deve ser um objeto com name", 400)
    if any(not isinstance(a, dict) for a in corpo.get("attachments") or []):
        raise ErroHTTP("cada item de attachments deve ser um objeto", 400)
    if corpo.get("catalogo") is not None and not isinstance(corpo["catalogo"], dict):
        raise ErroHTTP("catalogo deve ser um objeto", 400)


def _escritor(h):
    def escrever(dados: bytes) -> None:
        h.wfile.write(dados)
        h.wfile.flush()
    return escrever
