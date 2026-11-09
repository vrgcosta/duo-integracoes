"""Armazenamento local: conversas em árvore, busca, arquivos e projetos.

Conversas, mensagens e anexos vivem num único arquivo SQLite (`dados/duo.sqlite3`); os
projetos continuam sendo pastas com um `instrucoes.md`, porque é o usuário quem os edita, no
bloco de notas. As mensagens formam uma árvore (cada nó aponta para o pai), o que permite
editar e regenerar sem perder ramos.

Por que SQLite e não um JSON por conversa: a busca precisa de índice (FTS5), a exclusão de uma
conversa precisa levar os anexos junto numa operação só, e a gravação passa a ser transacional
(uma queda no meio não deixa meia mensagem). O arquivo continua sendo fácil de inspecionar
(DB Browser for SQLite) e de copiar com o servidor parado.
"""

import json
import logging
import os
import re
import sqlite3
import threading
import unicodedata
import uuid as _uuid
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from typing import Iterator

log = logging.getLogger(__name__)

# Pai "virtual" da primeira mensagem de toda conversa.
RAIZ = "00000000-0000-4000-8000-000000000000"

# Os ids chegam da rede; validar antes de tocar o banco é o que mantém lixo (e tentativa de
# injeção) longe do SQL, que de todo modo é sempre parametrizado. Projetos viram nome de pasta,
# então a validação ali também impede path traversal ("../config", "a/b"). Sempre com
# `fullmatch`: com `match` e `$`, um id terminado em "\n" passaria.
_RE_CONVERSA = re.compile(r"[0-9a-f-]{36}")
_RE_ARQUIVO = re.compile(r"[0-9a-f]{32}")
_RE_PROJETO = re.compile(r"[a-z0-9-]+")

_CAMPOS_RESUMO = ("uuid", "name", "model", "project_uuid", "created_at", "updated_at")
_COLUNAS_RESUMO = ", ".join(f"c.{campo}" for campo in _CAMPOS_RESUMO)

# Campos de conversa que `atualizar_conversa` aceita. O nome do campo vira coluna no SQL, então
# só entra o que está nesta lista: nunca texto vindo de fora.
_CAMPOS_EDITAVEIS = ("name", "model", "project_uuid", "pensamento", "ferramentas_carregadas", "fator_tokens",
                     "ultimo_uso", "ultimo_envio")

_CAMPOS_DO_RESUMO = ("ate_no", "texto", "modelo", "tokens_antes", "tokens_depois", "created_at")

NOME_BANCO = "duo.sqlite3"

# Versão do banco, guardada no `PRAGMA user_version` (0 num banco que nunca o gravou). A 1 marca o
# banco cujo índice de busca já passou pela limpeza única de `_preparar_fts`: os bancos anteriores
# excluíam conversas deixando os termos nos segmentos do FTS5. A 2 acrescenta o que a janela de
# contexto guarda na conversa (`_COLUNAS_V2`) e a tabela `resumos`, que o `_ESQUEMA` cria em todo banco.
_VERSAO_FTS_LIMPO = 1
_VERSAO_BANCO = 2

# Quem já tem a tabela `conversas` recebe estas colunas por ALTER TABLE; o `_ESQUEMA` as traz nos bancos novos.
_COLUNAS_V2 = {
    # Quantas vezes o motor conta mais que a estimativa, e a última entrada que ele informou (ver `janela.fator_real`).
    "fator_tokens": "REAL NOT NULL DEFAULT 1.0",
    "ultimo_uso": "INTEGER",
    # O que o usuário tirou da janela, e o `system` e as ferramentas do último envio: o medidor conta como o
    # próximo envio, mas o Duo só manda isso ao enviar. Pode citar o processo; sai junto com a conversa.
    "excluidos_janela": "TEXT NOT NULL DEFAULT '[]'",
    "ultimo_envio": "TEXT",
}

# A opção secure-delete do FTS5 só existe a partir do SQLite 3.42. Sem ela, um DELETE no índice só
# acrescenta um marcador de "excluído" e os termos originais continuam nos segmentos, legíveis por
# quem abrir o arquivo; por isso, nos SQLite mais antigos, `excluir` reorganiza o índice inteiro.
_FTS_SECURE_DELETE = sqlite3.sqlite_version_info >= (3, 42, 0)

_ESQUEMA = """
CREATE TABLE IF NOT EXISTS conversas (
    uuid TEXT PRIMARY KEY,
    name TEXT,
    model TEXT,
    project_uuid TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    pensamento INTEGER,
    ferramentas_carregadas TEXT NOT NULL DEFAULT '[]',
    fator_tokens REAL NOT NULL DEFAULT 1.0,
    ultimo_uso INTEGER,
    excluidos_janela TEXT NOT NULL DEFAULT '[]',
    ultimo_envio TEXT
);
CREATE INDEX IF NOT EXISTS conversas_por_atualizacao ON conversas(updated_at);
CREATE INDEX IF NOT EXISTS conversas_por_projeto ON conversas(project_uuid);

CREATE TABLE IF NOT EXISTS mensagens (
    uuid TEXT PRIMARY KEY,
    conversa_uuid TEXT NOT NULL REFERENCES conversas(uuid) ON DELETE CASCADE,
    ordem INTEGER NOT NULL,
    parent_message_uuid TEXT,
    sender TEXT,
    created_at TEXT,
    text TEXT,
    content TEXT NOT NULL DEFAULT '[]',
    attachments TEXT NOT NULL DEFAULT '[]',
    files TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS mensagens_por_conversa ON mensagens(conversa_uuid, ordem);

CREATE TABLE IF NOT EXISTS resumos (
    conversa_uuid TEXT PRIMARY KEY REFERENCES conversas(uuid) ON DELETE CASCADE,
    ate_no TEXT NOT NULL,
    texto TEXT NOT NULL,
    modelo TEXT,
    tokens_antes INTEGER NOT NULL,
    tokens_depois INTEGER NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS arquivos (
    file_uuid TEXT PRIMARY KEY,
    nome TEXT,
    mime TEXT,
    dados BLOB NOT NULL,
    created_at TEXT NOT NULL
);
"""

# O índice de busca. `chave` é 'nome' para o título da conversa ou o uuid da mensagem, para que
# regravar uma mensagem troque só a linha dela.
_INDICE_FTS = """
CREATE VIRTUAL TABLE IF NOT EXISTS busca_fts USING fts5(
    conversa_uuid UNINDEXED, chave UNINDEXED, texto,
    tokenize = "unicode61 remove_diacritics 2"
)
"""
# Sem FTS5: o mesmo formato, com o texto já normalizado (sem acento, sem caixa) para o LIKE.
_INDICE_LIKE = """
CREATE TABLE IF NOT EXISTS busca_like (conversa_uuid TEXT NOT NULL, chave TEXT NOT NULL, texto TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS busca_like_por_conversa ON busca_like(conversa_uuid, chave)
"""


class ErroMigracao(OSError):
    """Um arquivo do formato antigo (JSON) que não deu para importar; a mensagem diz qual.

    É um `OSError` como as demais falhas de disco do armazenamento: quem não distingue os casos
    continua tratando todos do mesmo jeito.
    """


def _agora() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _normalizar(texto: str) -> str:
    # Sem acento e sem caixa: "acao" precisa achar "AÇÃO".
    decomposto = unicodedata.normalize("NFD", texto)
    return "".join(c for c in decomposto if unicodedata.category(c) != "Mn").casefold()


def _json(valor: object) -> str:
    return json.dumps(valor, ensure_ascii=False)


def _tem_fts5() -> bool:
    """O SQLite embutido neste Python tem FTS5? Alguns builds (distribuições enxutas) não têm."""
    try:
        with closing(sqlite3.connect(":memory:")) as con:
            con.execute("CREATE VIRTUAL TABLE t USING fts5(x)")
        return True
    except sqlite3.Error:
        return False


def _texto_dos_blocos(content: object) -> str:
    """Só os blocos de texto entram na busca: chamadas e resultados de ferramentas são ruído."""
    if not isinstance(content, list):
        return ""
    partes = [b.get("text") or "" for b in content if isinstance(b, dict) and b.get("type") == "text"]
    return "\n".join(p for p in partes if isinstance(p, str) and p)


def _escapar_like(texto: str) -> str:
    # `%` e `_` digitados pelo usuário valem como texto, não como curinga.
    return texto.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _ids_de_arquivos(files: object) -> list[str]:
    """Os ids de anexo de um nó. Outros itens são ignorados, como no histórico."""
    if not isinstance(files, list):
        return []
    return [f for f in files if isinstance(f, str) and _RE_ARQUIVO.fullmatch(f)]


class Armazenamento:
    def __init__(self, pasta: str):
        self._pasta = pasta
        self._projetos = os.path.join(pasta, "projetos")
        os.makedirs(self._projetos, exist_ok=True)
        # Uma conexão só, compartilhada entre as threads do servidor, e uma trava que a serializa:
        # o servidor é local e de poucos usuários, e ler-modificar-gravar de duas threads na
        # mesma conversa perderia mensagens. As leituras também passam pela trava, porque um
        # objeto de conexão não aceita uso simultâneo.
        self._trava = threading.Lock()
        self._con: sqlite3.Connection | None = sqlite3.connect(
            os.path.join(pasta, NOME_BANCO), check_same_thread=False,
            # Autocommit: as transações são abertas à mão (`_transacao`), com BEGIN IMMEDIATE, para
            # que o conflito de gravação apareça logo no início e não no meio da operação.
            isolation_level=None)
        try:
            self._abrir()
        except BaseException:
            # Falha na abertura (ex.: migração) não pode deixar o arquivo preso: no Windows, a
            # pasta não poderia ser apagada.
            self.fechar()
            raise

    # --- conexão -----------------------------------------------------------

    def _abrir(self) -> None:
        con = self._con
        # WAL: leitores não bloqueiam o escritor (e vice-versa) e a queda no meio de uma
        # gravação não corrompe o arquivo. `busy_timeout` cobre outro processo (o DB Browser,
        # um segundo servidor) segurando o banco por instantes.
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA busy_timeout=5000")
        con.execute("PRAGMA foreign_keys=ON")
        # Zera as páginas e os trechos de página que um DELETE libera. Sem isso, o SQLite só marca o
        # espaço como livre e o texto "excluído" continua legível no arquivo até ser reaproveitado.
        # Não basta sozinho: o índice FTS5 não apaga termos (ver `_preparar_fts`) e o -wal guarda
        # cópias das páginas (ver `_esvaziar_wal`).
        con.execute("PRAGMA secure_delete=ON")
        con.executescript(_ESQUEMA)
        # Detectado uma vez, na abertura: o caminho de busca não muda no meio da execução.
        self._fts = _tem_fts5() and self._preparar_fts()
        self._tabela_busca = "busca_fts" if self._fts else "busca_like"
        if not self._fts:
            con.executescript(_INDICE_LIKE)
        self._migrar_esquema()
        self._reindexar_se_preciso()
        self._migrar_json()

    def _migrar_esquema(self) -> None:
        """Leva o banco à versão atual. Fica fora de `_preparar_fts`: um Python sem FTS5 também migra.

        Olha as colunas que já existem, em vez de confiar só na versão: um banco que passou por um
        Python sem FTS5 e voltou não pode falhar por acrescentar a mesma coluna duas vezes.
        """
        with self._transacao() as con:
            if con.execute("PRAGMA user_version").fetchone()[0] >= _VERSAO_BANCO:
                return
            existentes = {linha[1] for linha in con.execute("PRAGMA table_info(conversas)")}
            for coluna, definicao in _COLUNAS_V2.items():
                if coluna not in existentes:
                    con.execute(f"ALTER TABLE conversas ADD COLUMN {coluna} {definicao}")
            con.execute(f"PRAGMA user_version = {_VERSAO_BANCO}")

    def _preparar_fts(self) -> bool:
        """Deixa o índice FTS5 pronto para excluir sem resíduo. False se este SQLite não o lê.

        Um índice que passou por uma exclusão com secure-delete fica num formato que o FTS5 anterior
        ao 3.42 não lê ("invalid fts5 file format"). Num Python assim, a busca cai no LIKE em vez de
        o servidor não abrir: o índice é só um derivado das conversas, e o caminho LIKE se reconstrói
        sozinho a partir delas.
        """
        con = self._con
        con.execute(_INDICE_FTS)
        try:
            con.execute("SELECT 1 FROM busca_fts LIMIT 1").fetchall()
        except sqlite3.Error as e:
            if "fts5" not in str(e).lower():
                raise
            log.warning("O índice de busca (FTS5) de %s não abre com o SQLite %s deste Python (%s). "
                        "A busca usa o caminho por trecho, mais lento; um Python com SQLite 3.42 ou "
                        "mais novo volta ao FTS5.", NOME_BANCO, sqlite3.sqlite_version, e)
            return False
        with self._transacao() as con:
            if con.execute("SELECT 1 FROM sqlite_master WHERE name = 'busca_like'").fetchone():
                # O banco passou por um Python sem FTS5. Naquele período, o índice FTS5 (que ele não
                # abre) ficou parado: não tem as conversas novas e ainda guarda o texto das que foram
                # excluídas. Refazer do zero é o único jeito de ele voltar a bater com o histórico;
                # com `secure_delete`, as páginas das duas tabelas apagadas saem zeradas.
                con.execute("DROP TABLE busca_fts")
                con.execute("DROP TABLE busca_like")
                con.execute(_INDICE_FTS)
            if _FTS_SECURE_DELETE:
                # Opção persistente (fica gravada no índice): DELETE e UPDATE passam a tirar os
                # termos dos segmentos, em vez de só marcá-los como excluídos.
                con.execute("INSERT INTO busca_fts(busca_fts, rank) VALUES('secure-delete', 1)")
            if con.execute("PRAGMA user_version").fetchone()[0] < _VERSAO_FTS_LIMPO:
                # Uma vez por banco: os criados antes desta versão podem ter termos de conversas já
                # excluídas nos segmentos. O `optimize` funde todos os segmentos num só, aplicando os
                # marcadores, e as páginas antigas saem zeradas pelo `secure_delete`.
                con.execute("INSERT INTO busca_fts(busca_fts) VALUES('optimize')")
                con.execute(f"PRAGMA user_version = {_VERSAO_FTS_LIMPO}")
        return True

    def fechar(self) -> None:
        """Fecha o banco. Idempotente. Sem isso o arquivo (e o -wal) ficam presos no Windows."""
        with self._trava:
            con, self._con = self._con, None
            if con is not None:
                con.close()

    @contextmanager
    def _leitura(self) -> Iterator[sqlite3.Connection]:
        with self._trava:
            if self._con is None:
                raise OSError("O armazenamento já foi fechado.")
            try:
                yield self._con
            except sqlite3.Error as e:
                raise OSError(f"Erro no banco de dados: {e}") from e

    @contextmanager
    def _transacao(self) -> Iterator[sqlite3.Connection]:
        """Uma transação de escrita: ou tudo entra, ou nada.

        Erro do SQLite vira `OSError`: quem chama (o turno) já trata falha de disco assim, e
        "banco travado" ou "disco cheio" são exatamente isso para o usuário.
        """
        with self._leitura() as con:
            con.execute("BEGIN IMMEDIATE")
            try:
                yield con
                # O COMMIT também pode falhar (disco cheio, E/S, chave estrangeira adiada). Dentro
                # do `try`, a falha cai no ROLLBACK abaixo: sem isso a transação ficaria aberta,
                # as leituras veriam linhas não gravadas e as escritas seguintes falhariam.
                con.execute("COMMIT")
            except BaseException:
                if con.in_transaction:
                    try:
                        con.execute("ROLLBACK")
                    except sqlite3.Error:
                        pass
                raise

    # --- índice de busca ---------------------------------------------------

    def _indexar(self, con: sqlite3.Connection, cid: str, chave: str, texto: str) -> None:
        if not texto:
            return
        if not self._fts:
            texto = _normalizar(texto)
        con.execute(f"INSERT INTO {self._tabela_busca}(conversa_uuid, chave, texto) VALUES (?, ?, ?)",
                    (cid, chave, texto))

    def _desindexar(self, con: sqlite3.Connection, cid: str, chave: str | None = None) -> None:
        if chave is None:
            con.execute(f"DELETE FROM {self._tabela_busca} WHERE conversa_uuid = ?", (cid,))
        else:
            con.execute(f"DELETE FROM {self._tabela_busca} WHERE conversa_uuid = ? AND chave = ?",
                        (cid, chave))

    def _reindexar_se_preciso(self) -> None:
        """Reconstrói o índice quando a tabela em uso está vazia mas há conversas.

        Cobre o banco criado num Python com FTS5 e aberto noutro sem ele (ou o contrário): cada
        caminho tem a sua tabela, e a que não existia ainda precisa ser preenchida.
        """
        with self._transacao() as con:
            if con.execute(f"SELECT 1 FROM {self._tabela_busca} LIMIT 1").fetchone():
                return
            for cid, nome in con.execute("SELECT uuid, name FROM conversas").fetchall():
                self._indexar(con, cid, "nome", nome or "")
            for cid, uid, content in con.execute(
                    "SELECT conversa_uuid, uuid, content FROM mensagens").fetchall():
                self._indexar(con, cid, uid, _texto_dos_blocos(json.loads(content)))

    # --- conversas ---------------------------------------------------------

    @staticmethod
    def _valida_conversa(cid: object) -> bool:
        return isinstance(cid, str) and _RE_CONVERSA.fullmatch(cid) is not None

    @staticmethod
    def _no_da_linha(linha: tuple) -> dict:
        uid, pai, remetente, criado, texto, content, attachments, files = linha
        return {"uuid": uid, "parent_message_uuid": pai, "sender": remetente, "created_at": criado,
                "text": texto, "content": json.loads(content),
                "attachments": json.loads(attachments), "files": json.loads(files)}

    def _montar(self, con: sqlite3.Connection, linha: tuple) -> dict:
        uid, nome, modelo, projeto, criada, atualizada, pensamento, ferramentas = linha
        mensagens = con.execute(
            "SELECT uuid, parent_message_uuid, sender, created_at, text, content, attachments, files "
            "FROM mensagens WHERE conversa_uuid = ? ORDER BY ordem", (uid,)).fetchall()
        return {
            "uuid": uid, "name": nome, "model": modelo, "project_uuid": projeto,
            "created_at": criada, "updated_at": atualizada,
            "pensamento": None if pensamento is None else bool(pensamento),
            "ferramentas_carregadas": json.loads(ferramentas),
            "chat_messages": [self._no_da_linha(m) for m in mensagens],
        }

    def criar_conversa(self, nome: str, modelo: str, projeto: str | None) -> dict:
        agora = _agora()
        conversa = {
            "uuid": str(_uuid.uuid4()),
            "name": nome,
            "model": modelo,
            "project_uuid": projeto,
            "created_at": agora,
            "updated_at": agora,
            "pensamento": None,
            "ferramentas_carregadas": [],
            "chat_messages": [],
        }
        with self._transacao() as con:
            con.execute(
                "INSERT INTO conversas(uuid, name, model, project_uuid, created_at, updated_at, "
                "pensamento, ferramentas_carregadas) VALUES (?, ?, ?, ?, ?, ?, NULL, '[]')",
                (conversa["uuid"], nome, modelo, projeto, agora, agora))
            self._indexar(con, conversa["uuid"], "nome", nome or "")
        return conversa

    def ler_conversa(self, cid: str) -> dict | None:
        if not self._valida_conversa(cid):
            return None
        with self._leitura() as con:
            linha = con.execute(
                "SELECT uuid, name, model, project_uuid, created_at, updated_at, pensamento, "
                "ferramentas_carregadas FROM conversas WHERE uuid = ?", (cid,)).fetchone()
            return None if linha is None else self._montar(con, linha)

    def estado_janela(self, cid: str) -> dict | None:
        """O que a janela de contexto guarda da conversa; None se ela não existe.

        Fica fora de `ler_conversa`, que devolve a conversa no formato que a interface lê. E é uma
        consulta barata: o turno relê a cada rodada, para valer o que o usuário mudou no meio dele.
        """
        if not self._valida_conversa(cid):
            return None
        with self._leitura() as con:
            linha = con.execute("SELECT fator_tokens, ultimo_uso, excluidos_janela, ultimo_envio FROM conversas "
                                "WHERE uuid = ?", (cid,)).fetchone()
            if linha is None:
                return None
            resumo = con.execute(f"SELECT {', '.join(_CAMPOS_DO_RESUMO)} FROM resumos WHERE conversa_uuid = ?",
                                 (cid,)).fetchone()
        return {"fator_tokens": linha[0], "ultimo_uso": linha[1], "excluidos": json.loads(linha[2]),
                "ultimo_envio": json.loads(linha[3]) if linha[3] else None,
                "resumo": dict(zip(_CAMPOS_DO_RESUMO, resumo)) if resumo else None}

    def definir_excluidos(self, cid: str, excluidos: list[str]) -> bool:
        """Grava o que o usuário tirou da janela. Não mexe em `updated_at`: não é atividade na conversa."""
        if not self._valida_conversa(cid):
            return False
        with self._transacao() as con:
            return bool(con.execute("UPDATE conversas SET excluidos_janela = ? WHERE uuid = ?",
                                    (_json(excluidos), cid)).rowcount)

    def gravar_resumo(self, cid: str, resumo: dict) -> bool:
        """Grava o resumo da compactação, no lugar do anterior. False se a conversa não existe mais.

        Fica numa tabela à parte, e a árvore não muda. Não mexe em `updated_at`: não é atividade na
        conversa. A tabela não entra na busca, que continua achando o que foi dito de verdade.
        """
        if not self._valida_conversa(cid):
            return False
        with self._transacao() as con:
            if con.execute("SELECT 1 FROM conversas WHERE uuid = ?", (cid,)).fetchone() is None:
                return False
            con.execute(f"INSERT OR REPLACE INTO resumos(conversa_uuid, {', '.join(_CAMPOS_DO_RESUMO)}) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (cid, resumo["ate_no"], resumo["texto"], resumo["modelo"], resumo["tokens_antes"],
                         resumo["tokens_depois"], resumo.get("created_at") or _agora()))
        return True

    def apagar_resumo(self, cid: str) -> bool:
        """Volta a usar os turnos originais. False se não havia resumo."""
        if not self._valida_conversa(cid):
            return False
        with self._transacao() as con:
            return bool(con.execute("DELETE FROM resumos WHERE conversa_uuid = ?", (cid,)).rowcount)

    @staticmethod
    def _resumos(linhas: list[tuple]) -> list[dict]:
        return [dict(zip(_CAMPOS_RESUMO, linha)) for linha in linhas]

    def listar_conversas(self, projeto: str | None = None) -> list[dict]:
        # ISO 8601 UTC ordena como texto; o rowid desempata as criadas no mesmo segundo.
        sql = f"SELECT {_COLUNAS_RESUMO} FROM conversas c"
        parametros: tuple = ()
        if projeto is not None:
            sql += " WHERE c.project_uuid = ?"
            parametros = (projeto,)
        with self._leitura() as con:
            return self._resumos(con.execute(
                sql + " ORDER BY c.updated_at DESC, c.rowid DESC", parametros).fetchall())

    def buscar(self, termo: str) -> list[dict]:
        base = f"SELECT {_COLUNAS_RESUMO} FROM conversas c"
        ordem = " ORDER BY c.updated_at DESC, c.rowid DESC"
        if self._fts:
            # Cada palavra vira um prefixo entre aspas: "cobran" acha "cobrança", e as aspas
            # impedem que o que o usuário digitou (OR, NEAR, *, ") seja lido como operador.
            # Normaliza como no LIKE: texto colado de PDF vem em NFD, e a marca combinante
            # (categoria Mn) não é `\w`: sem isto ela partiria a palavra ao meio.
            palavras = re.findall(r"\w+", _normalizar(termo))
            consulta = " ".join(f'"{p}"*' for p in palavras)
            if palavras:
                sql = (base + " WHERE c.uuid IN (SELECT conversa_uuid FROM busca_fts "
                       "WHERE busca_fts MATCH ?)" + ordem)
                parametros: tuple = (consulta,)
            elif termo.strip():
                return []  # só pontuação: não há o que procurar
            else:
                sql, parametros = base + ordem, ()
        else:
            alvo = _normalizar(termo)
            sql = (base + " WHERE c.uuid IN (SELECT conversa_uuid FROM busca_like "
                   "WHERE texto LIKE ? ESCAPE '\\')" + ordem)
            parametros = (f"%{_escapar_like(alvo)}%",)
        with self._leitura() as con:
            return self._resumos(con.execute(sql, parametros).fetchall())

    def atualizar_conversa(self, cid: str, **campos) -> bool:
        desconhecidos = set(campos) - set(_CAMPOS_EDITAVEIS)
        if desconhecidos:
            raise ValueError(f"Campo de conversa não editável: {', '.join(sorted(desconhecidos))}")
        if not self._valida_conversa(cid):
            return False
        valores = dict(campos)
        if "pensamento" in valores and valores["pensamento"] is not None:
            valores["pensamento"] = int(bool(valores["pensamento"]))
        for campo in ("ferramentas_carregadas", "ultimo_envio"):
            if campo in valores:
                valores[campo] = _json(valores[campo])
        with self._transacao() as con:
            # Os nomes de coluna vêm de `_CAMPOS_EDITAVEIS` (checado acima); os valores, por parâmetro.
            atribuicoes = "".join(f"{campo} = ?, " for campo in valores)
            achou = con.execute(f"UPDATE conversas SET {atribuicoes}updated_at = ? WHERE uuid = ?",
                                (*valores.values(), _agora(), cid)).rowcount
            if not achou:
                return False
            if "name" in valores:
                self._desindexar(con, cid, "nome")
                self._indexar(con, cid, "nome", valores["name"] or "")
        return True

    def excluir(self, cid: str) -> bool:
        if not self._valida_conversa(cid):
            return False
        with self._transacao() as con:
            if con.execute("SELECT 1 FROM conversas WHERE uuid = ?", (cid,)).fetchone() is None:
                return False
            # Os anexos pertencem à conversa que os citou: sem isto, um processo sigiloso
            # "excluído" continuaria guardado no banco.
            for (files,) in con.execute(
                    "SELECT files FROM mensagens WHERE conversa_uuid = ?", (cid,)).fetchall():
                for fid in _ids_de_arquivos(json.loads(files)):
                    con.execute("DELETE FROM arquivos WHERE file_uuid = ?", (fid,))
            self._desindexar(con, cid)
            con.execute("DELETE FROM conversas WHERE uuid = ?", (cid,))  # mensagens saem em cascata
            if self._fts and not _FTS_SECURE_DELETE:
                # Sem secure-delete (SQLite < 3.42), o DELETE acima só marcou os termos como
                # excluídos. O `optimize` reescreve o índice num segmento só, já sem eles. Custa
                # tempo proporcional ao índice, mas excluir é raro e o sigilo não espera.
                con.execute("INSERT INTO busca_fts(busca_fts) VALUES('optimize')")
        self._esvaziar_wal()
        return True

    def _esvaziar_wal(self) -> None:
        """Copia o -wal para o banco e o deixa com zero bytes. Melhor esforço: só avisa se falhar.

        No modo WAL, cada gravação vai primeiro para `duo.sqlite3-wal`, com a página inteira. Mesmo
        depois de copiadas para o banco (checkpoint), as páginas antigas continuam no -wal, que é
        reaproveitado do começo, e não apagado: ali sobrevivem o texto de uma conversa excluída e as
        versões anteriores das mensagens. O TRUNCATE leva tudo para o banco, onde o `secure_delete`
        já zerou o que saiu, e corta o -wal a zero bytes.

        Não completa se outro programa (o DB Browser, por exemplo) estiver no meio de uma leitura;
        a exclusão já valeu, e o -wal fica para o próximo checkpoint (outra exclusão ou o
        encerramento do servidor, quando ele for o último a usar o banco).
        """
        try:
            with self._leitura() as con:
                ocupado = con.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0]
        except OSError as e:
            ocupado, motivo = 1, str(e)
        else:
            motivo = "outro programa está lendo o banco"
        if ocupado:
            log.warning("Conversa excluída, mas %s-wal não pôde ser esvaziado agora (%s): o texto "
                        "dela continua nesse arquivo até o próximo checkpoint (outra exclusão, ou o "
                        "servidor encerrado com o banco fechado nos outros programas).",
                        NOME_BANCO, motivo)

    def gravar_mensagem(self, cid: str, no: dict) -> None:
        if not self._valida_conversa(cid):
            raise KeyError(f"Conversa inexistente: {cid}")
        content = no.get("content") or []
        with self._transacao() as con:
            if con.execute("SELECT 1 FROM conversas WHERE uuid = ?", (cid,)).fetchone() is None:
                raise KeyError(f"Conversa inexistente: {cid}")
            ordem = con.execute("SELECT COALESCE(MAX(ordem), -1) + 1 FROM mensagens "
                                "WHERE conversa_uuid = ?", (cid,)).fetchone()[0]
            # Upsert por uuid: o nó do assistente é regravado a cada rodada de ferramentas, e
            # regravar não pode mudar o lugar dele na conversa (por isso `ordem` não está no UPDATE).
            con.execute(
                "INSERT INTO mensagens(uuid, conversa_uuid, ordem, parent_message_uuid, sender, "
                "created_at, text, content, attachments, files) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(uuid) DO UPDATE SET parent_message_uuid = excluded.parent_message_uuid, "
                "sender = excluded.sender, created_at = excluded.created_at, text = excluded.text, "
                "content = excluded.content, attachments = excluded.attachments, files = excluded.files "
                "WHERE mensagens.conversa_uuid = excluded.conversa_uuid",
                (no["uuid"], cid, ordem, no.get("parent_message_uuid"), no.get("sender"),
                 no.get("created_at"), no.get("text") or "", _json(content),
                 _json(no.get("attachments") or []), _json(no.get("files") or [])))
            self._desindexar(con, cid, no["uuid"])
            self._indexar(con, cid, no["uuid"], _texto_dos_blocos(content))
            con.execute("UPDATE conversas SET updated_at = ? WHERE uuid = ?", (_agora(), cid))

    # --- arquivos ----------------------------------------------------------

    def salvar_arquivo(self, nome: str, mime: str, dados: bytes) -> str:
        fid = _uuid.uuid4().hex
        # O nome original é só um dado numa coluna; nunca vira caminho, então um nome
        # malicioso não tem como sair da pasta.
        with self._transacao() as con:
            con.execute("INSERT INTO arquivos(file_uuid, nome, mime, dados, created_at) "
                        "VALUES (?, ?, ?, ?, ?)", (fid, nome, mime, dados, _agora()))
        return fid

    def obter_arquivo(self, fid: str) -> tuple[str, str, bytes] | None:
        if not isinstance(fid, str) or not _RE_ARQUIVO.fullmatch(fid):
            return None
        with self._leitura() as con:
            linha = con.execute("SELECT nome, mime, dados FROM arquivos WHERE file_uuid = ?",
                                (fid,)).fetchone()
        return None if linha is None else (linha[0], linha[1], bytes(linha[2]))

    # --- projetos ----------------------------------------------------------

    def _ler_instrucoes(self, pid: str) -> str | None:
        if not isinstance(pid, str) or not _RE_PROJETO.fullmatch(pid):
            return None
        caminho = os.path.join(self._projetos, pid, "instrucoes.md")
        try:
            with open(caminho, encoding="utf-8") as f:
                return f.read()
        except (OSError, UnicodeDecodeError):
            return None

    def instrucoes_projeto(self, pid: str) -> str:
        return self._ler_instrucoes(pid) or ""

    def listar_projetos(self) -> list[dict]:
        projetos = []
        for pid in sorted(os.listdir(self._projetos)):
            texto = self._ler_instrucoes(pid)
            if texto is None:
                continue
            # O nome vem do primeiro título "# ..."; sem título, o slug basta.
            nome = pid
            for linha in texto.splitlines():
                if linha.startswith("# "):
                    nome = linha[2:].strip() or pid
                    break
            projetos.append({"uuid": pid, "name": nome})
        return projetos

    # --- migração do formato antigo (um JSON por conversa) -----------------

    def _migrar_json(self) -> None:
        """Importa `conversas/*.json` e `arquivos/*` do formato antigo, uma única vez.

        Só roda com o banco vazio: assim é idempotente, e uma pasta antiga devolvida pelo
        usuário depois nunca duplica nem sobrescreve o que o banco já tem. As pastas antigas são
        renomeadas (nunca apagadas) só depois de o banco confirmar a gravação; se algo falhar no
        meio, nada é gravado nem renomeado e a próxima abertura tenta de novo.
        """
        pasta_conversas = os.path.join(self._pasta, "conversas")
        pasta_arquivos = os.path.join(self._pasta, "arquivos")
        if not os.path.isdir(pasta_conversas):
            return
        nomes = sorted(n for n in os.listdir(pasta_conversas) if n.endswith(".json"))
        if not nomes:
            return
        with self._transacao() as con:
            if con.execute("SELECT 1 FROM conversas LIMIT 1").fetchone():
                return
            importadas = ignoradas = 0
            for nome in nomes:
                cid = nome[:-5]
                try:
                    with open(os.path.join(pasta_conversas, nome), encoding="utf-8") as f:
                        dados = json.load(f)
                except (OSError, ValueError):  # JSON corrompido ou ilegível
                    dados = None
                if (not self._valida_conversa(cid) or not isinstance(dados, dict)
                        or not isinstance(dados.get("chat_messages", []), list)):
                    ignoradas += 1
                    continue
                try:
                    self._importar_conversa(con, cid, dados)
                except (sqlite3.Error, TypeError, ValueError, AttributeError) as e:
                    # Um campo de tipo inesperado (um nome que é objeto, uma data que é lista) não
                    # entra no banco. A transação inteira é desfeita, como em qualquer falha daqui;
                    # a diferença é dizer qual arquivo corrigir, em vez de um erro genérico do SQLite.
                    raise ErroMigracao(f"{os.path.join(pasta_conversas, nome)}: {e}") from e
                importadas += 1
            arquivos = self._importar_arquivos(con, pasta_arquivos)
        sufixo = datetime.now().strftime("%Y%m%d")
        guardadas = [destino for destino in (self._guardar_pasta_antiga(pasta, sufixo)
                                             for pasta in (pasta_conversas, pasta_arquivos)) if destino]
        log.info("Histórico migrado do JSON para %s: %d conversa(s), %d arquivo(s), %d ignorada(s).",
                 NOME_BANCO, importadas, arquivos, ignoradas)
        if guardadas:
            # As pastas antigas são cópias integrais em arquivos comuns: excluir uma conversa no Duo
            # não chega a elas, e é o usuário quem precisa apagá-las.
            log.info("O formato antigo ficou em %s: são cópias do histórico sem criptografia. Confira "
                     "o histórico no Duo e apague-as.", " e ".join(guardadas))

    def _importar_conversa(self, con: sqlite3.Connection, cid: str, dados: dict) -> None:
        agora = _agora()
        pensamento = dados.get("pensamento")
        con.execute(
            "INSERT INTO conversas(uuid, name, model, project_uuid, created_at, updated_at, "
            "pensamento, ferramentas_carregadas) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (cid, dados.get("name"), dados.get("model"), dados.get("project_uuid"),
             dados.get("created_at") or agora, dados.get("updated_at") or agora,
             None if pensamento is None else int(bool(pensamento)),
             _json(dados.get("ferramentas_carregadas") or [])))
        self._indexar(con, cid, "nome", dados.get("name") or "")
        for ordem, no in enumerate(dados.get("chat_messages", [])):
            if not isinstance(no, dict) or not isinstance(no.get("uuid"), str):
                continue
            content = no.get("content") or []
            # OR IGNORE: um uuid de nó repetido (em outra conversa) não derruba a migração inteira.
            cursor = con.execute(
                "INSERT OR IGNORE INTO mensagens(uuid, conversa_uuid, ordem, parent_message_uuid, "
                "sender, created_at, text, content, attachments, files) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (no["uuid"], cid, ordem, no.get("parent_message_uuid"), no.get("sender"),
                 no.get("created_at"), no.get("text") or "", _json(content),
                 _json(no.get("attachments") or []), _json(no.get("files") or [])))
            if cursor.rowcount:
                self._indexar(con, cid, no["uuid"], _texto_dos_blocos(content))

    @staticmethod
    def _importar_arquivos(con: sqlite3.Connection, pasta: str) -> int:
        if not os.path.isdir(pasta):
            return 0
        total = 0
        for nome in os.listdir(pasta):
            if not _RE_ARQUIVO.fullmatch(nome):
                continue  # os `.json` de metadados são lidos junto com o arquivo deles
            caminho = os.path.join(pasta, nome)
            try:
                with open(caminho + ".json", encoding="utf-8") as f:
                    meta = json.load(f)
                with open(caminho, "rb") as f:
                    dados = f.read()
                criado = datetime.fromtimestamp(os.path.getmtime(caminho), timezone.utc)
            except (OSError, ValueError):
                continue
            if not isinstance(meta, dict):
                continue  # metadado que não é objeto: tratado como o ilegível, logo acima
            try:
                con.execute("INSERT OR IGNORE INTO arquivos(file_uuid, nome, mime, dados, created_at) "
                            "VALUES (?, ?, ?, ?, ?)",
                            (nome, meta.get("nome"), meta.get("mime"), dados,
                             criado.strftime("%Y-%m-%dT%H:%M:%SZ")))
            except sqlite3.Error as e:  # "nome" ou "mime" de tipo inesperado
                raise ErroMigracao(f"{caminho}.json: {e}") from e
            total += 1
        return total

    @staticmethod
    def _guardar_pasta_antiga(pasta: str, sufixo: str) -> str | None:
        """Renomeia a pasta antiga para `<pasta>.migrado-AAAAMMDD` e devolve o novo caminho."""
        if not os.path.isdir(pasta):
            return None
        destino = f"{pasta}.migrado-{sufixo}"
        extra = 1
        while os.path.exists(destino):  # migração repetida no mesmo dia
            extra += 1
            destino = f"{pasta}.migrado-{sufixo}-{extra}"
        try:
            os.rename(pasta, destino)
        except OSError as e:
            # Os dados já estão no banco; a pasta antiga só fica no caminho, e o banco não vazio
            # impede uma segunda migração.
            log.warning("Não foi possível renomear %s (%s); renomeie à mão.", pasta, e)
            return None
        return destino
