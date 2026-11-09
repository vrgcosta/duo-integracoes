import json
import os
import shutil
import sqlite3
import tempfile
import threading
import time
import unicodedata
import unittest
import uuid
from contextlib import closing, nullcontext
from unittest import mock

from duo_provedor import armazenamento
from duo_provedor.armazenamento import RAIZ, Armazenamento, _normalizar, _tem_fts5


def _no(uuid: str, texto: str = "olá", pai: str = RAIZ, sender: str = "human") -> dict:
    return {
        "uuid": uuid,
        "parent_message_uuid": pai,
        "sender": sender,
        "created_at": "2026-01-01T00:00:00Z",
        "text": texto,
        "content": [{"type": "text", "text": texto}],
        "attachments": [],
        "files": [],
    }


def _tabelas(arm: Armazenamento) -> set[str]:
    with arm._leitura() as con:
        return {linha[0] for linha in con.execute("SELECT name FROM sqlite_master")}


# Palavras que só a conversa "sigilosa" dos testes de exclusão contém. Cada uma começa por uma letra
# que nenhuma outra palavra dos testes usa: o FTS5 grava os termos de uma página com o prefixo em
# comum comprimido, e uma palavra que dividisse o começo com outra poderia sumir da varredura dos
# bytes sem ter saído do arquivo (o teste passaria sem provar nada).
_SIGILOSAS = ("Zebrafantasma", "Xilofonequântico", "Wumpusraríssimo", "Yetiresumido", "Quokkaenviado")
_ANEXO_SIGILOSO = b"QWERTYANEXOSIGILOSO" * 20


def _formas(palavra: str) -> set[str]:
    # O texto como foi digitado (tabelas de mensagens, conteúdo do FTS) e a forma que o índice guarda
    # (sem caixa e sem acento: termos do FTS5 e coluna da busca por LIKE).
    return {palavra, palavra.casefold(), _normalizar(palavra)}


def _vestigios(pasta: str) -> list[tuple[str, str]]:
    """O que sobrou da conversa sigilosa nos bytes crus de cada arquivo da pasta (banco, -wal, -shm)."""
    achados = []
    for nome in sorted(os.listdir(pasta)):
        caminho = os.path.join(pasta, nome)
        if not os.path.isfile(caminho):
            continue
        with open(caminho, "rb") as f:
            dados = f.read()
        for palavra in _SIGILOSAS:
            for forma in sorted(_formas(palavra)):
                if forma.encode("utf-8") in dados:
                    achados.append((nome, forma))
        if _ANEXO_SIGILOSO[:19] in dados:
            achados.append((nome, "anexo"))
    return achados


def _sem_fts5():
    return mock.patch.object(armazenamento, "_tem_fts5", return_value=False)


def _sem_secure_delete_do_fts():
    # Como num SQLite anterior ao 3.42, que não tem a opção secure-delete do FTS5.
    return mock.patch.object(armazenamento, "_FTS_SECURE_DELETE", False)


class TestArmazenamento(unittest.TestCase):
    _ESTADO_INICIAL = {"fator_tokens": 1.0, "ultimo_uso": None, "excluidos": [], "ultimo_envio": None, "resumo": None}

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.arm = self._abrir(self._tmp.name)

    def _abrir(self, pasta: str) -> Armazenamento:
        arm = Armazenamento(pasta)
        # Os cleanups rodam do último para o primeiro: a conexão fecha antes de a pasta ser
        # apagada (no Windows, um banco aberto trava a pasta).
        self.addCleanup(arm.fechar)
        return arm

    def _pasta_temporaria(self) -> str:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return tmp.name

    def test_criar_e_ler(self):
        c = self.arm.criar_conversa("Teste", "m1", None)
        self.assertEqual(
            set(c),
            {"uuid", "name", "model", "project_uuid", "created_at", "updated_at",
             "pensamento", "ferramentas_carregadas", "chat_messages"},
        )
        self.assertIsNone(c["pensamento"])
        self.assertEqual(c["ferramentas_carregadas"], [])
        self.assertTrue(c["created_at"].endswith("Z"))
        self.assertEqual(self.arm.ler_conversa(c["uuid"]), c)

    def test_gravar_mensagem_faz_upsert(self):
        c = self.arm.criar_conversa("T", "m", None)
        u = "11111111-1111-4111-8111-111111111111"
        self.arm.gravar_mensagem(c["uuid"], _no(u, "antigo"))
        self.arm.gravar_mensagem(c["uuid"], _no(u, "novo"))
        msgs = self.arm.ler_conversa(c["uuid"])["chat_messages"]
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0]["content"][0]["text"], "novo")

    def test_ordem_das_mensagens_e_preservada_no_upsert(self):
        c = self.arm.criar_conversa("T", "m", None)
        u1, u2 = str(uuid.uuid4()), str(uuid.uuid4())
        self.arm.gravar_mensagem(c["uuid"], _no(u1, "primeira"))
        self.arm.gravar_mensagem(c["uuid"], _no(u2, "segunda", pai=u1, sender="assistant"))
        self.arm.gravar_mensagem(c["uuid"], _no(u1, "primeira, editada"))
        msgs = self.arm.ler_conversa(c["uuid"])["chat_messages"]
        self.assertEqual([m["uuid"] for m in msgs], [u1, u2])
        self.assertEqual(msgs[0]["text"], "primeira, editada")
        self.assertEqual(msgs[1], _no(u2, "segunda", pai=u1, sender="assistant"))

    def test_gravar_em_conversa_inexistente(self):
        with self.assertRaises(KeyError):
            self.arm.gravar_mensagem(str(uuid.uuid4()), _no(str(uuid.uuid4())))

    def test_listar_ordena_por_atualizacao(self):
        a = self.arm.criar_conversa("A", "m", None)
        time.sleep(1.1)  # as datas têm resolução de segundos
        b = self.arm.criar_conversa("B", "m", None)
        self.assertEqual([x["uuid"] for x in self.arm.listar_conversas()], [b["uuid"], a["uuid"]])
        time.sleep(1.1)  # as datas têm resolução de segundos
        self.arm.gravar_mensagem(a["uuid"], _no("22222222-2222-4222-8222-222222222222"))
        self.assertEqual([x["uuid"] for x in self.arm.listar_conversas()], [a["uuid"], b["uuid"]])
        self.assertEqual(
            set(self.arm.listar_conversas()[0]),
            {"uuid", "name", "model", "project_uuid", "created_at", "updated_at"},
        )

    def test_listar_por_projeto(self):
        a = self.arm.criar_conversa("A", "m", "civel")
        self.arm.criar_conversa("B", "m", None)
        self.assertEqual([x["uuid"] for x in self.arm.listar_conversas("civel")], [a["uuid"]])

    def test_pensamento_e_ferramentas_ida_e_volta(self):
        c = self.arm.criar_conversa("A", "m", None)
        for valor in (True, False, None):
            self.arm.atualizar_conversa(c["uuid"], pensamento=valor)
            self.assertIs(self.arm.ler_conversa(c["uuid"])["pensamento"], valor)
        self.arm.atualizar_conversa(c["uuid"], ferramentas_carregadas=["listar_documentos"], model="m2")
        lida = self.arm.ler_conversa(c["uuid"])
        self.assertEqual((lida["ferramentas_carregadas"], lida["model"]), (["listar_documentos"], "m2"))

    def test_atualizar_conversa(self):
        c = self.arm.criar_conversa("A", "m", None)
        self.assertTrue(self.arm.atualizar_conversa(c["uuid"], name="Novo", pensamento=True))
        lida = self.arm.ler_conversa(c["uuid"])
        self.assertEqual((lida["name"], lida["pensamento"]), ("Novo", True))
        self.assertFalse(self.arm.atualizar_conversa("../x", name="y"))
        self.assertFalse(self.arm.atualizar_conversa(str(uuid.uuid4()), name="y"))

    def test_atualizar_recusa_campo_desconhecido(self):
        # O nome do campo vira coluna no SQL; só os conhecidos passam.
        c = self.arm.criar_conversa("A", "m", None)
        with self.assertRaises(ValueError):
            self.arm.atualizar_conversa(c["uuid"], **{"name=''; DROP TABLE conversas; --": 1})

    def test_excluir(self):
        c = self.arm.criar_conversa("A", "m", None)
        self.assertTrue(self.arm.excluir(c["uuid"]))
        self.assertIsNone(self.arm.ler_conversa(c["uuid"]))
        self.assertFalse(self.arm.excluir(c["uuid"]))

    def test_excluir_apaga_anexos_da_conversa(self):
        c = self.arm.criar_conversa("A", "m", None)
        outra = self.arm.criar_conversa("B", "m", None)
        f1 = self.arm.salvar_arquivo("a.pdf", "application/pdf", b"1")
        f2 = self.arm.salvar_arquivo("b.pdf", "application/pdf", b"2")
        f3 = self.arm.salvar_arquivo("c.pdf", "application/pdf", b"3")
        solto = self.arm.salvar_arquivo("solto.pdf", "application/pdf", b"4")
        m1 = _no(str(uuid.uuid4()), "com anexo")
        m1["files"] = [f1, f2, {"nao": "e id"}, "../x"]  # o que não é id é ignorado
        self.arm.gravar_mensagem(c["uuid"], m1)
        m2 = _no(str(uuid.uuid4()), "da outra")
        m2["files"] = [f3]
        self.arm.gravar_mensagem(outra["uuid"], m2)
        self.assertTrue(self.arm.excluir(c["uuid"]))
        self.assertIsNone(self.arm.obter_arquivo(f1))
        self.assertIsNone(self.arm.obter_arquivo(f2))
        # O que não era da conversa excluída fica.
        self.assertIsNotNone(self.arm.obter_arquivo(f3))
        self.assertIsNotNone(self.arm.obter_arquivo(solto))
        with self.arm._leitura() as con:
            restantes = con.execute("SELECT COUNT(*) FROM mensagens WHERE conversa_uuid=?",
                                    (c["uuid"],)).fetchone()[0]
        self.assertEqual(restantes, 0)  # ON DELETE CASCADE

    def test_excluir_tira_a_conversa_da_busca(self):
        c = self.arm.criar_conversa("Sentença", "m", None)
        self.arm.gravar_mensagem(c["uuid"], _no(str(uuid.uuid4()), "dosimetria da pena"))
        self.assertEqual(len(self.arm.buscar("dosimetria")), 1)
        self.arm.excluir(c["uuid"])
        self.assertEqual(self.arm.buscar("dosimetria"), [])
        self.assertEqual(self.arm.buscar("sentenca"), [])

    # --- exclusão sem resíduo nos arquivos ---

    def _conversa_sigilosa(self, arm: Armazenamento) -> str:
        """Cria, ao lado de conversas comuns, uma com título, mensagem e anexo que só ela tem."""
        for i in range(5):
            outra = arm.criar_conversa(f"Outra {i}", "m", None)
            arm.gravar_mensagem(outra["uuid"], _no(str(uuid.uuid4()), f"texto comum da conversa {i}"))
        c = arm.criar_conversa(f"Processo {_SIGILOSAS[0]}", "m", None)
        fid = arm.salvar_arquivo("sigilo.txt", "text/plain", _ANEXO_SIGILOSO)
        m = _no(str(uuid.uuid4()), f"O réu {_SIGILOSAS[1]} confessou o {_SIGILOSAS[2]}.")
        m["files"] = [fid]
        arm.gravar_mensagem(c["uuid"], m)
        # O turno regrava o nó do assistente a cada rodada: as versões antigas também são texto da conversa.
        m["content"].append({"type": "text", "text": "Mais um parágrafo."})
        arm.gravar_mensagem(c["uuid"], m)
        # O medidor guarda o `system` do último envio, que pode citar o processo.
        arm.atualizar_conversa(c["uuid"], ultimo_envio={"system": f"Processo do réu {_SIGILOSAS[4]}"})
        # E o resumo da compactação, que também é texto dos autos. O primeiro é trocado: o texto antigo também sai.
        for texto in (f"Rascunho sobre {_SIGILOSAS[3]}", f"O réu {_SIGILOSAS[3]} confessou."):
            self.assertTrue(arm.gravar_resumo(c["uuid"], {"ate_no": m["uuid"], "texto": texto, "modelo": "m",
                                                          "tokens_antes": 100, "tokens_depois": 10}))
        return c["uuid"]

    def test_excluir_nao_deixa_o_texto_nos_arquivos_do_banco(self):
        # O efeito, e não a configuração: depois de excluir, nenhum byte do banco, do -wal ou do -shm
        # tem o título, a mensagem ou o anexo; com o banco aberto (o -wal existe) e depois de fechado.
        caminhos = []
        if _tem_fts5():
            if sqlite3.sqlite_version_info >= (3, 42, 0):
                caminhos.append(("FTS5 com secure-delete", None))
            caminhos.append(("FTS5 com optimize (SQLite < 3.42)", _sem_secure_delete_do_fts))
        caminhos.append(("LIKE (Python sem FTS5)", _sem_fts5))
        for nome, condicao in caminhos:
            with self.subTest(caminho=nome), (condicao() if condicao else nullcontext()):
                pasta = self._pasta_temporaria()
                arm = self._abrir(pasta)
                cid = self._conversa_sigilosa(arm)
                # Prova de que a varredura enxerga o texto: antes de excluir, tudo está em algum arquivo.
                antes = {forma for _, forma in _vestigios(pasta)}
                for palavra in _SIGILOSAS:
                    self.assertTrue(_formas(palavra) & antes, palavra)
                self.assertIn("anexo", antes)

                self.assertTrue(arm.excluir(cid))
                self.assertEqual(_vestigios(pasta), [], "com o banco aberto")
                arm.fechar()
                self.assertEqual(_vestigios(pasta), [], "depois de fechar")
                # O resto do histórico continua inteiro e buscável.
                arm = self._abrir(pasta)
                self.assertEqual(len(arm.buscar("comum")), 5)
                self.assertEqual(len(arm.listar_conversas()), 5)

    @unittest.skipUnless(_tem_fts5() and sqlite3.sqlite_version_info >= (3, 42, 0),
                         "precisa de FTS5 com secure-delete (SQLite 3.42 ou mais novo)")
    def test_secure_delete_do_fts_fica_ligado(self):
        with self.arm._leitura() as con:
            self.assertEqual(con.execute("SELECT v FROM busca_fts_config WHERE k = 'secure-delete'").fetchone(),
                             (1,))

    def test_wal_preso_por_outro_leitor_avisa_e_a_exclusao_vale(self):
        c = self.arm.criar_conversa("A", "m", None)
        with self.arm._leitura() as con:
            con.execute("PRAGMA busy_timeout=0")  # sem esperar os 5 s do servidor
        with closing(sqlite3.connect(os.path.join(self._tmp.name, "duo.sqlite3"), isolation_level=None)) as outro:
            # Como o DB Browser com o banco aberto: uma leitura presa a uma foto antiga do banco
            # impede o -wal de ser esvaziado.
            outro.execute("BEGIN")
            outro.execute("SELECT COUNT(*) FROM conversas").fetchone()
            with self.assertLogs("duo_provedor.armazenamento", "WARNING") as registro:
                self.assertTrue(self.arm.excluir(c["uuid"]))
            outro.execute("COMMIT")
        self.assertIsNone(self.arm.ler_conversa(c["uuid"]))
        self.assertIn("duo.sqlite3-wal", "\n".join(registro.output))

    @unittest.skipUnless(_tem_fts5(), "este Python não foi compilado com FTS5")
    def test_banco_antigo_tem_o_indice_limpo_uma_vez_na_abertura(self):
        # Antes da correção, excluir só marcava os termos como excluídos nos segmentos do FTS5. Um
        # banco dessa época passa por um `optimize` na primeira abertura, e só nela.
        pasta = self._pasta_temporaria()
        arm = self._abrir(pasta)
        cid = self._conversa_sigilosa(arm)
        with arm._transacao() as con:
            # O que a versão anterior fazia ao excluir: sem secure-delete no FTS5 e sem optimize.
            con.execute("INSERT INTO busca_fts(busca_fts, rank) VALUES('secure-delete', 0)")
            con.execute("DELETE FROM busca_fts WHERE conversa_uuid = ?", (cid,))
            con.execute("DELETE FROM conversas WHERE uuid = ?", (cid,))
            con.execute("DELETE FROM arquivos")
            con.execute("PRAGMA user_version = 0")
        arm.fechar()
        self.assertIn("xilofonequantico", {forma for _, forma in _vestigios(pasta)})  # o resíduo existe

        self._abrir(pasta).fechar()
        self.assertEqual(_vestigios(pasta), [])
        arm = self._abrir(pasta)
        with arm._leitura() as con:
            self.assertEqual(con.execute("PRAGMA user_version").fetchone()[0], armazenamento._VERSAO_BANCO)
        self.assertEqual(len(arm.buscar("comum")), 5)

    @unittest.skipUnless(_tem_fts5(), "este Python não foi compilado com FTS5")
    def test_volta_ao_fts5_refaz_o_indice_sem_o_que_foi_excluido_no_like(self):
        # Excluída num Python sem FTS5, a conversa sai da busca por LIKE, mas o índice FTS5, que aquele
        # Python não abre, ainda guarda o texto dela; e as conversas novas não estão nele. Na volta
        # ao FTS5, o índice é refeito do zero.
        pasta = self._pasta_temporaria()
        arm = self._abrir(pasta)
        cid = self._conversa_sigilosa(arm)
        arm.fechar()
        with _sem_fts5():
            arm = self._abrir(pasta)
        nova = arm.criar_conversa("Criada sem FTS5", "m", None)
        self.assertTrue(arm.excluir(cid))
        arm.fechar()
        self.assertTrue(_vestigios(pasta))  # ficou no índice FTS5, que o caminho LIKE não toca

        arm = self._abrir(pasta)
        self.assertEqual([x["uuid"] for x in arm.buscar("criada")], [nova["uuid"]])
        self.assertEqual(arm.buscar("xilofonequantico"), [])
        self.assertNotIn("busca_like", _tabelas(arm))
        arm.fechar()
        self.assertEqual(_vestigios(pasta), [])

    @unittest.skipUnless(_tem_fts5(), "este Python não foi compilado com FTS5")
    def test_indice_fts5_ilegivel_cai_no_like_em_vez_de_travar(self):
        # Depois de uma exclusão com secure-delete (SQLite 3.42+), o índice fica num formato que o
        # FTS5 mais antigo não lê ("invalid fts5 file format"). Simulado com uma versão de formato
        # que nenhum SQLite conhece.
        pasta = self._pasta_temporaria()
        arm = self._abrir(pasta)
        c = arm.criar_conversa("Ação de cobrança", "m", None)
        with arm._transacao() as con:
            con.execute("UPDATE busca_fts_config SET v = 99 WHERE k = 'version'")
        arm.fechar()
        with self.assertLogs("duo_provedor.armazenamento", "WARNING") as registro:
            arm = self._abrir(pasta)
        self.assertIn("FTS5", "\n".join(registro.output))
        self.assertIn("busca_like", _tabelas(arm))
        self.assertEqual([x["uuid"] for x in arm.buscar("acao")], [c["uuid"]])
        self.assertTrue(arm.excluir(c["uuid"]))
        self.assertEqual(arm.buscar("acao"), [])

    def test_ids_invalidos_nao_tocam_disco(self):
        self.assertIsNone(self.arm.ler_conversa("../config"))
        self.assertIsNone(self.arm.ler_conversa("a/b"))
        self.assertFalse(self.arm.excluir("../x"))
        self.assertIsNone(self.arm.obter_arquivo("../../x"))
        self.assertEqual(self.arm.instrucoes_projeto("../x"), "")

    def test_id_com_quebra_de_linha_no_fim_nao_toca_o_banco(self):
        # Com `match` e `$`, o "\n" final passaria. A validação vem antes de qualquer SQL.
        c = self.arm.criar_conversa("A", "m", None)
        fid = self.arm.salvar_arquivo("a.txt", "text/plain", b"x")
        tocou = AssertionError("tocou o banco ou o disco com um id inválido")
        with mock.patch.object(Armazenamento, "_leitura", side_effect=tocou), \
                mock.patch.object(Armazenamento, "_transacao", side_effect=tocou), \
                mock.patch("duo_provedor.armazenamento.open", side_effect=tocou, create=True), \
                mock.patch("os.path.isfile", side_effect=tocou):
            self.assertIsNone(self.arm.ler_conversa(c["uuid"] + "\n"))
            self.assertFalse(self.arm.excluir(c["uuid"] + "\n"))
            self.assertFalse(self.arm.atualizar_conversa(c["uuid"] + "\n", name="x"))
            self.assertIsNone(self.arm.obter_arquivo(fid + "\n"))
            self.assertEqual(self.arm.instrucoes_projeto("civel\n"), "")
            with self.assertRaises(KeyError):
                self.arm.gravar_mensagem(c["uuid"] + "\n", _no(str(uuid.uuid4())))

    def test_sql_injetado_no_id_e_no_termo_e_inofensivo(self):
        c = self.arm.criar_conversa("Alvo", "m", None)
        self.assertIsNone(self.arm.ler_conversa("' OR 1=1 --"))
        self.assertEqual(self.arm.buscar("x' OR '1'='1"), [])
        self.assertEqual(self.arm.buscar('"; DROP TABLE conversas; --'), [])
        self.assertIsNotNone(self.arm.ler_conversa(c["uuid"]))

    def test_banco_em_wal_com_chaves_estrangeiras(self):
        self.assertTrue(os.path.isfile(os.path.join(self._tmp.name, "duo.sqlite3")))
        with self.arm._leitura() as con:
            self.assertEqual(con.execute("PRAGMA journal_mode").fetchone()[0], "wal")
            self.assertEqual(con.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            self.assertEqual(con.execute("PRAGMA busy_timeout").fetchone()[0], 5000)
            self.assertEqual(con.execute("PRAGMA secure_delete").fetchone()[0], 1)

    def test_commit_que_falha_desfaz_e_a_proxima_escrita_funciona(self):
        c = self.arm.criar_conversa("A", "m", None)
        with self.assertRaises(OSError):
            with self.arm._transacao() as con:
                # Chave estrangeira adiada: só o COMMIT descobre que a linha é órfã.
                con.execute("PRAGMA defer_foreign_keys=ON")
                con.execute("INSERT INTO mensagens(uuid, conversa_uuid, ordem) VALUES (?, ?, 0)",
                            (str(uuid.uuid4()), str(uuid.uuid4())))
        with self.arm._leitura() as con:
            self.assertFalse(con.in_transaction)
            self.assertEqual(con.execute("SELECT COUNT(*) FROM mensagens").fetchone()[0], 0)
        self.arm.gravar_mensagem(c["uuid"], _no(str(uuid.uuid4()), "depois da falha"))
        self.assertEqual(len(self.arm.ler_conversa(c["uuid"])["chat_messages"]), 1)

    def test_leitura_e_escrita_concorrentes(self):
        # Uma conexão só, compartilhada entre threads: a trava é o que impede o uso simultâneo.
        c = self.arm.criar_conversa("Busca", "m", None)
        erros, lidas, parar = [], [], threading.Event()

        def ler():
            while not parar.is_set():
                try:
                    lidas.append(self.arm.ler_conversa(c["uuid"]) is not None)
                    self.arm.listar_conversas()
                    self.arm.buscar("busca")
                except Exception as e:  # noqa: BLE001 - o teste quer ver qualquer falha
                    erros.append(e)

        def escrever_outra():
            for _ in range(20):
                try:
                    outra = self.arm.criar_conversa("Outra", "m", None)
                    self.arm.excluir(outra["uuid"])
                except Exception as e:  # noqa: BLE001
                    erros.append(e)

        leitores = [threading.Thread(target=ler) for _ in range(3)]
        escritor = threading.Thread(target=escrever_outra)
        for t in leitores + [escritor]:
            t.start()
        try:
            for _ in range(40):
                self.arm.gravar_mensagem(c["uuid"], _no(str(uuid.uuid4())))
        finally:
            parar.set()
            for t in leitores:
                t.join()
            escritor.join()
        self.assertEqual(erros, [])
        self.assertTrue(lidas and all(lidas))
        self.assertEqual(len(self.arm.ler_conversa(c["uuid"])["chat_messages"]), 40)

    def test_arquivo_ida_e_volta(self):
        fid = self.arm.salvar_arquivo("petição.pdf", "application/pdf", b"%PDF-1")
        self.assertRegex(fid, r"^[0-9a-f]{32}$")
        self.assertEqual(self.arm.obter_arquivo(fid), ("petição.pdf", "application/pdf", b"%PDF-1"))
        self.assertIsNone(self.arm.obter_arquivo("0" * 32))

    def test_projetos(self):
        pasta = os.path.join(self._tmp.name, "projetos", "civel")
        os.makedirs(pasta)
        with open(os.path.join(pasta, "instrucoes.md"), "w", encoding="utf-8") as f:
            f.write("# Cível\nUse linguagem formal.")
        sem_titulo = os.path.join(self._tmp.name, "projetos", "penal")
        os.makedirs(sem_titulo)
        with open(os.path.join(sem_titulo, "instrucoes.md"), "w", encoding="utf-8") as f:
            f.write("Sem título.")
        self.assertEqual(
            self.arm.listar_projetos(),
            [{"uuid": "civel", "name": "Cível"}, {"uuid": "penal", "name": "penal"}],
        )
        self.assertEqual(self.arm.instrucoes_projeto("civel"), "# Cível\nUse linguagem formal.")

    # --- busca ---

    def test_buscar_ignora_acento_e_caixa(self):
        # Vale nos dois caminhos (FTS5 e LIKE); o teste seguinte força o segundo.
        a = self.arm.criar_conversa("Ação de cobrança", "m", None)
        b = self.arm.criar_conversa("Outra", "m", None)
        self.arm.criar_conversa("Nada", "m", None)
        self.arm.gravar_mensagem(b["uuid"], _no("33333333-3333-4333-8333-333333333333", "Trata da AÇÃO"))
        achados = {x["uuid"] for x in self.arm.buscar("acao")}
        self.assertEqual(achados, {a["uuid"], b["uuid"]})

    @unittest.skipUnless(_tem_fts5(), "este Python não foi compilado com FTS5")
    def test_busca_fts_sem_acento(self):
        a = self.arm.criar_conversa("Ação de cobrança", "m", None)
        b = self.arm.criar_conversa("Outra", "m", None)
        self.arm.gravar_mensagem(b["uuid"], _no(str(uuid.uuid4()), "Cálculo da CORREÇÃO monetária"))
        self.assertIn("busca_fts", _tabelas(self.arm))  # o caminho é o do FTS, não o do LIKE
        self.assertNotIn("busca_like", _tabelas(self.arm))
        self.assertEqual({x["uuid"] for x in self.arm.buscar("ACAO")}, {a["uuid"]})
        self.assertEqual({x["uuid"] for x in self.arm.buscar("correcao monetaria")}, {b["uuid"]})
        # Prefixo: o que se digita pela metade ainda acha.
        self.assertEqual({x["uuid"] for x in self.arm.buscar("cobran")}, {a["uuid"]})
        # Texto colado de PDF vem decomposto (NFD): "a" + acento combinante.
        nfd = unicodedata.normalize("NFD", "AÇÃO cobrança")
        self.assertNotEqual(nfd, "AÇÃO cobrança")
        self.assertEqual({x["uuid"] for x in self.arm.buscar(nfd)}, {a["uuid"]})
        # Pontuação sozinha não vira consulta inválida.
        self.assertEqual(self.arm.buscar("..."), [])

    @unittest.skipUnless(_tem_fts5(), "este Python não foi compilado com FTS5")
    def test_busca_acompanha_renomeacao(self):
        c = self.arm.criar_conversa("Nome velho", "m", None)
        self.arm.atualizar_conversa(c["uuid"], name="Réplica nova")
        self.assertEqual(self.arm.buscar("velho"), [])
        self.assertEqual([x["uuid"] for x in self.arm.buscar("replica")], [c["uuid"]])

    @unittest.skipUnless(_tem_fts5(), "este Python não foi compilado com FTS5")
    def test_busca_acompanha_regravacao_da_mensagem(self):
        c = self.arm.criar_conversa("T", "m", None)
        u = str(uuid.uuid4())
        self.arm.gravar_mensagem(c["uuid"], _no(u, "texto antigo"))
        self.arm.gravar_mensagem(c["uuid"], _no(u, "texto atual"))
        self.assertEqual(self.arm.buscar("antigo"), [])
        self.assertEqual(len(self.arm.buscar("atual")), 1)

    def test_busca_sem_fts5_cai_no_like(self):
        with mock.patch("duo_provedor.armazenamento._tem_fts5", return_value=False):
            arm = self._abrir(self._pasta_temporaria())
        a = arm.criar_conversa("Ação de cobrança", "m", None)
        b = arm.criar_conversa("Outra", "m", None)
        arm.criar_conversa("100% real_", "m", None)
        arm.gravar_mensagem(b["uuid"], _no(str(uuid.uuid4()), "Trata da AÇÃO"))
        self.assertIn("busca_like", _tabelas(arm))
        self.assertNotIn("busca_fts", _tabelas(arm))
        self.assertEqual({x["uuid"] for x in arm.buscar("acao")}, {a["uuid"], b["uuid"]})
        self.assertEqual({x["uuid"] for x in arm.buscar("COBRANCA")}, {a["uuid"]})
        # No LIKE, % e _ do termo valem como texto, não como curinga.
        self.assertEqual(len(arm.buscar("100%")), 1)
        self.assertEqual(arm.buscar("ou_ra"), [])  # sem escape, "_" casaria com o "t" de "Outra"
        self.assertEqual(len(arm.buscar("")), 3)

    def test_reabrir_sem_fts5_reconstroi_o_indice_de_busca(self):
        # Um banco criado com FTS5 e aberto num Python sem ele continua buscável.
        pasta = self._pasta_temporaria()
        arm = Armazenamento(pasta)
        c = arm.criar_conversa("Ação de cobrança", "m", None)
        arm.fechar()
        with mock.patch("duo_provedor.armazenamento._tem_fts5", return_value=False):
            arm2 = self._abrir(pasta)
        self.assertEqual([x["uuid"] for x in arm2.buscar("acao")], [c["uuid"]])

    def test_busca_vazia_devolve_todas(self):
        a = self.arm.criar_conversa("A", "m", None)
        self.assertEqual([x["uuid"] for x in self.arm.buscar("")], [a["uuid"]])

    # --- migração do JSON antigo ---

    def _montar_json_antigo(self, pasta: str) -> tuple[dict, str]:
        os.makedirs(os.path.join(pasta, "conversas"))
        os.makedirs(os.path.join(pasta, "arquivos"))
        fid = uuid.uuid4().hex
        with open(os.path.join(pasta, "arquivos", fid), "wb") as f:
            f.write(b"%PDF-antigo")
        with open(os.path.join(pasta, "arquivos", fid + ".json"), "w", encoding="utf-8") as f:
            json.dump({"nome": "petição.pdf", "mime": "application/pdf"}, f)
        m1, m2 = str(uuid.uuid4()), str(uuid.uuid4())
        conversa = {
            "uuid": str(uuid.uuid4()), "name": "Ação antiga", "model": "m1", "project_uuid": "civel",
            "created_at": "2025-01-01T10:00:00Z", "updated_at": "2025-02-02T10:00:00Z",
            "pensamento": True, "ferramentas_carregadas": ["listar_documentos"],
            "chat_messages": [_no(m1, "Pergunta sobre a AÇÃO"), _no(m2, "Resposta", m1, "assistant")],
        }
        conversa["chat_messages"][0]["files"] = [fid]
        with open(os.path.join(pasta, "conversas", conversa["uuid"] + ".json"), "w", encoding="utf-8") as f:
            json.dump(conversa, f, ensure_ascii=False)
        # Um JSON corrompido não pode impedir a migração das demais.
        with open(os.path.join(pasta, "conversas", "lixo.json"), "w", encoding="utf-8") as f:
            f.write("{ corrompido")
        return conversa, fid

    def test_migracao_de_json(self):
        pasta = self._pasta_temporaria()
        conversa, fid = self._montar_json_antigo(pasta)
        arm = self._abrir(pasta)
        self.assertEqual(arm.ler_conversa(conversa["uuid"]), conversa)
        self.assertEqual(arm.obter_arquivo(fid), ("petição.pdf", "application/pdf", b"%PDF-antigo"))
        self.assertEqual([x["uuid"] for x in arm.buscar("acao")], [conversa["uuid"]])
        self.assertEqual([x["uuid"] for x in arm.listar_conversas("civel")], [conversa["uuid"]])
        # As pastas antigas são renomeadas, não apagadas.
        nomes = os.listdir(pasta)
        self.assertNotIn("conversas", nomes)
        self.assertNotIn("arquivos", nomes)
        migrada = [n for n in nomes if n.startswith("conversas.migrado-")]
        self.assertEqual(len(migrada), 1, nomes)
        self.assertRegex(migrada[0], r"^conversas\.migrado-\d{8}$")
        self.assertTrue(any(n.startswith("arquivos.migrado-") for n in nomes), nomes)
        self.assertTrue(os.path.isfile(os.path.join(pasta, migrada[0], conversa["uuid"] + ".json")))

    def test_migracao_idempotente(self):
        pasta = self._pasta_temporaria()
        conversa, _ = self._montar_json_antigo(pasta)
        self._abrir(pasta).fechar()
        # O usuário devolve a pasta antiga com o servidor parado: o banco já tem dados e vence,
        # sem duplicar nem mexer de novo na pasta.
        migrada = next(n for n in os.listdir(pasta) if n.startswith("conversas.migrado-"))
        os.rename(os.path.join(pasta, migrada), os.path.join(pasta, "conversas"))
        arm = self._abrir(pasta)
        self.assertEqual(len(arm.listar_conversas()), 1)
        self.assertIn("conversas", os.listdir(pasta))
        arm.fechar()
        # E reabrir sem pastas antigas também não muda nada.
        os.rename(os.path.join(pasta, "conversas"), os.path.join(pasta, "guardada"))
        arm2 = self._abrir(pasta)
        self.assertEqual(arm2.ler_conversa(conversa["uuid"]), conversa)
        self.assertEqual(len(arm2.listar_conversas()), 1)

    def test_migracao_falha_no_meio_nao_deixa_meia_migracao(self):
        pasta = self._pasta_temporaria()
        self._montar_json_antigo(pasta)
        with mock.patch("duo_provedor.armazenamento.Armazenamento._importar_conversa",
                        side_effect=RuntimeError("quebrou")):
            with self.assertRaises(RuntimeError):
                Armazenamento(pasta)
        # Nada foi renomeado; a próxima abertura tenta de novo e completa.
        self.assertIn("conversas", os.listdir(pasta))
        arm = self._abrir(pasta)
        self.assertEqual(len(arm.listar_conversas()), 1)

    def test_migracao_avisa_que_as_pastas_antigas_sao_copias_para_apagar(self):
        pasta = self._pasta_temporaria()
        self._montar_json_antigo(pasta)
        with self.assertLogs("duo_provedor.armazenamento", "INFO") as registro:
            self._abrir(pasta)
        texto = "\n".join(registro.output)
        self.assertIn("conversas.migrado-", texto)
        self.assertIn("arquivos.migrado-", texto)
        self.assertIn("apague-as", texto)

    def test_migracao_com_campo_de_tipo_errado_nomeia_o_arquivo_e_nao_grava_nada(self):
        pasta = self._pasta_temporaria()
        conversa, _ = self._montar_json_antigo(pasta)
        ruim = str(uuid.uuid4())
        with open(os.path.join(pasta, "conversas", ruim + ".json"), "w", encoding="utf-8") as f:
            json.dump({"name": ["não", "é", "texto"], "chat_messages": []}, f)
        with self.assertRaises(armazenamento.ErroMigracao) as erro:
            Armazenamento(pasta)
        self.assertIn(ruim + ".json", str(erro.exception))
        self.assertIsInstance(erro.exception, OSError)  # quem trata OSError continua tratando
        self.assertIn("conversas", os.listdir(pasta))  # nada renomeado
        os.remove(os.path.join(pasta, "conversas", ruim + ".json"))
        # Sem o arquivo ruim, a próxima abertura migra o resto: nada tinha sido gravado pela metade.
        self.assertEqual(self._abrir(pasta).ler_conversa(conversa["uuid"]), conversa)

    def test_metadado_de_anexo_que_nao_e_objeto_e_ignorado(self):
        pasta = self._pasta_temporaria()
        conversa, fid = self._montar_json_antigo(pasta)
        with open(os.path.join(pasta, "arquivos", fid + ".json"), "w", encoding="utf-8") as f:
            json.dump(["lista", "em vez de objeto"], f)
        arm = self._abrir(pasta)
        self.assertEqual(arm.ler_conversa(conversa["uuid"]), conversa)
        self.assertIsNone(arm.obter_arquivo(fid))

    def test_sem_json_antigo_nao_cria_pastas_antigas(self):
        self.assertNotIn("conversas", os.listdir(self._tmp.name))
        self.assertNotIn("arquivos", os.listdir(self._tmp.name))

    # --- estado da janela de contexto ---

    # O esquema como era na versão 1, antes de a janela guardar qualquer coisa na conversa.
    _ESQUEMA_V1 = """
    CREATE TABLE conversas (uuid TEXT PRIMARY KEY, name TEXT, model TEXT, project_uuid TEXT, created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL, pensamento INTEGER, ferramentas_carregadas TEXT NOT NULL DEFAULT '[]');
    INSERT INTO conversas(uuid, name, model, created_at, updated_at)
        VALUES ('11111111-2222-4333-8444-555555555555', 'Antiga', 'm', '2025-01-01T00:00:00Z', '2025-01-01T00:00:00Z');
    PRAGMA user_version = 1;
    """

    def _banco_v1(self) -> tuple[str, str]:
        pasta = self._pasta_temporaria()
        with closing(sqlite3.connect(os.path.join(pasta, "duo.sqlite3"))) as con:
            con.executescript(self._ESQUEMA_V1)
        return pasta, "11111111-2222-4333-8444-555555555555"

    def test_banco_existente_recebe_as_colunas_da_janela_sem_perder_nada(self):
        caminhos = [("FTS5", nullcontext())] if _tem_fts5() else []
        for nome, condicao in caminhos + [("LIKE (Python sem FTS5)", _sem_fts5())]:
            with self.subTest(caminho=nome), condicao:
                pasta, cid = self._banco_v1()
                arm = self._abrir(pasta)
                self.assertEqual(arm.ler_conversa(cid)["name"], "Antiga")
                self.assertEqual(arm.estado_janela(cid), self._ESTADO_INICIAL)
                with arm._leitura() as con:
                    self.assertEqual(con.execute("PRAGMA user_version").fetchone()[0], armazenamento._VERSAO_BANCO)
                # Abrir de novo não pode tentar acrescentar a mesma coluna.
                arm.atualizar_conversa(cid, fator_tokens=1.5, ultimo_uso=9000)
                arm.fechar()
                self.assertEqual(self._abrir(pasta).estado_janela(cid),
                                 {**self._ESTADO_INICIAL, "fator_tokens": 1.5, "ultimo_uso": 9000})

    def test_migracao_interrompida_recomeca_sem_duplicar_coluna(self):
        # Um banco com parte das colunas e a versão antiga: passa por `_migrar_esquema` sem erro.
        pasta, cid = self._banco_v1()
        with closing(sqlite3.connect(os.path.join(pasta, "duo.sqlite3"))) as con:
            con.execute("ALTER TABLE conversas ADD COLUMN ultimo_uso INTEGER")
        self.assertEqual(self._abrir(pasta).estado_janela(cid), self._ESTADO_INICIAL)

    def test_excluidos_gravam_sem_mexer_na_ordem_da_lista(self):
        c = self.arm.criar_conversa("A", "m", None)
        with self.arm._transacao() as con:
            con.execute("UPDATE conversas SET updated_at = '2020-01-01T00:00:00Z'")
        self.assertTrue(self.arm.definir_excluidos(c["uuid"], ["a", "b"]))
        self.assertEqual(self.arm.estado_janela(c["uuid"])["excluidos"], ["a", "b"])
        self.assertEqual(self.arm.ler_conversa(c["uuid"])["updated_at"], "2020-01-01T00:00:00Z")
        self.assertFalse(self.arm.definir_excluidos("11111111-2222-4333-8444-555555555555", ["a"]))

    def test_resumo_grava_troca_apaga_e_nao_entra_na_busca(self):
        c = self.arm.criar_conversa("A", "m", None)
        cid = c["uuid"]
        with self.arm._transacao() as con:
            con.execute("UPDATE conversas SET updated_at = '2020-01-01T00:00:00Z'")
        resumo = {"ate_no": "n1", "texto": "resumo jacarandá", "modelo": "m", "tokens_antes": 100, "tokens_depois": 10}
        self.assertFalse(self.arm.apagar_resumo(cid))
        self.assertTrue(self.arm.gravar_resumo(cid, resumo))
        gravado = self.arm.estado_janela(cid)["resumo"]
        self.assertEqual({k: gravado[k] for k in resumo}, resumo)
        self.assertTrue(gravado["created_at"])
        self.assertTrue(self.arm.gravar_resumo(cid, {**resumo, "texto": "outro"}))
        self.assertEqual(self.arm.estado_janela(cid)["resumo"]["texto"], "outro")
        self.assertEqual(self.arm.buscar("jacaranda"), [])
        # Compactar não é atividade na conversa: não muda a ordem da lista.
        self.assertEqual(self.arm.ler_conversa(cid)["updated_at"], "2020-01-01T00:00:00Z")
        self.assertTrue(self.arm.apagar_resumo(cid))
        self.assertIsNone(self.arm.estado_janela(cid)["resumo"])
        self.assertFalse(self.arm.gravar_resumo("11111111-2222-4333-8444-555555555555", resumo))

    def test_resumo_sai_junto_com_a_conversa(self):
        c = self.arm.criar_conversa("A", "m", None)
        self.arm.gravar_resumo(c["uuid"], {"ate_no": "n", "texto": "t", "modelo": "m", "tokens_antes": 2,
                                           "tokens_depois": 1})
        self.assertTrue(self.arm.excluir(c["uuid"]))
        with self.arm._leitura() as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM resumos").fetchone(), (0,))

    def test_estado_da_janela_de_conversa_inexistente_e_none(self):
        self.assertIsNone(self.arm.estado_janela("11111111-2222-4333-8444-555555555555"))
        self.assertIsNone(self.arm.estado_janela("../../x"))

    def test_estado_da_janela_nao_aparece_na_conversa_lida_pela_interface(self):
        c = self.arm.criar_conversa("A", "m", None)
        self.arm.atualizar_conversa(c["uuid"], fator_tokens=1.5, ultimo_uso=9000)
        lida = self.arm.ler_conversa(c["uuid"])
        self.assertNotIn("fator_tokens", lida)
        self.assertNotIn("ultimo_uso", lida)

    def test_fechar_e_idempotente_e_libera_a_pasta(self):
        pasta = tempfile.mkdtemp()
        arm = Armazenamento(pasta)
        arm.criar_conversa("A", "m", None)
        arm.fechar()
        arm.fechar()
        shutil.rmtree(pasta)  # no Windows, falharia com o banco aberto
        self.assertFalse(os.path.exists(pasta))


if __name__ == "__main__":
    unittest.main()
