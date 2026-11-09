"""Servidor local que liga o Duo (extensão PJe+R) a um LLM rodando na própria máquina.

Uso:
    py servidor.py --simulado        respostas simuladas, sem LLM (testar o Duo)
    py servidor.py                   usa o backend do config (padrão: ollama)
    py servidor.py --backend openai --url http://127.0.0.1:1234/v1 --modelo meu-modelo
    py servidor.py --verificar       confere o ambiente (modelo, capacidades, contexto) e sai
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
import urllib.request

from duo_provedor import VERSAO
from duo_provedor.armazenamento import NOME_BANCO, ErroMigracao
from duo_provedor.backends import criar_backend
from duo_provedor.backends.base import ErroBackend
from duo_provedor.blueprint import (NOME_ARQUIVO, destino_local, eh_loopback, gerar_blueprint,
                                    ler_blueprint, mensagem_nuvem_bloqueada, nuvem_bloqueada)
from duo_provedor.config import (CABECALHO_CHAVE, PORTA_PADRAO, URL_PADRAO, ErroConfig, carregar_config,
                                 salvar_config)
from duo_provedor.hardware import PERFIS, detectar
from duo_provedor.http import criar_servidor
from duo_provedor.pasta_dados import mensagem_pasta, pasta_que_sai_da_maquina
from duo_provedor.servico import Servico
from duo_provedor.verificar import verificar

PASTA = os.path.dirname(os.path.abspath(__file__))
PASTA_DADOS = os.path.join(PASTA, "dados")
ARQUIVO_SISTEMA = os.path.join(PASTA, "prompts", "sistema.md")
# Reserva para quando prompts/sistema.md for apagado ou ilegível: o servidor sobe mesmo assim.
SISTEMA_MINIMO = "Você é o assistente do DUO (PJe), rodando localmente. Responda em português."


def _ler_sistema() -> str:
    try:
        with open(ARQUIVO_SISTEMA, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return SISTEMA_MINIMO


def _aplicar_argumentos(cfg: dict, args: argparse.Namespace) -> bool:
    """Grava no config só o que foi pedido explicitamente. Devolve True se algo mudou."""
    original = json.loads(json.dumps(cfg))
    if args.backend and args.backend != cfg["backend"]:
        cfg["backend"] = args.backend
        # Trocar de backend sem informar a URL deixaria a do motor anterior apontando para o lugar errado.
        if not args.url:
            cfg["url"] = URL_PADRAO[args.backend]
    if args.url:
        cfg["url"] = args.url
    if args.perfil:
        cfg["perfil"] = args.perfil
        cfg.update(PERFIS[args.perfil])
    # Depois do perfil: --modelo explícito vale mais que o modelo sugerido pelo perfil.
    if args.modelo:
        cfg["modelo_padrao"] = args.modelo
    if args.porta:
        cfg["porta"] = args.porta
    return cfg != original


def _este_servidor(cfg: dict) -> bool:
    """A porta ocupada é de uma instância deste servidor? Testa com a chave, que só nós temos."""
    pedido = urllib.request.Request(f"http://127.0.0.1:{cfg['porta']}/api/contexto",
                                    headers={CABECALHO_CHAVE: cfg["chave"]})
    try:
        with urllib.request.urlopen(pedido, timeout=3) as r:
            return json.load(r).get("conta") == "local"
    except (OSError, ValueError, AttributeError):
        return False


def _aviso_modelo_padrao(cfg: dict, backend) -> str | None:
    """Avisa já no banner que o modelo padrão é de nuvem e vai ser recusado nos envios."""
    try:
        info = backend.info_modelo(cfg["modelo_padrao"])
    except ErroBackend:
        return None  # motor fora do ar: o 1º envio e o --verificar explicam
    if nuvem_bloqueada(cfg, info):
        return " ATENÇÃO: " + mensagem_nuvem_bloqueada(cfg["modelo_padrao"])
    return None


def _aviso_reimportar(anterior: dict | None, novo: dict) -> list[str]:
    """O Duo guarda uma cópia do blueprint importado; se o gerado mudou, a cópia ficou velha."""
    if anterior is None or anterior == novo:
        return []
    antes, agora = anterior.get("destinoLocal"), novo["destinoLocal"]
    if isinstance(antes, bool) and antes != agora:
        # O caso grave: o DUO continua achando que o destino é local enquanto os autos saem.
        de, para = ("local", "externo") if antes else ("externo", "local")
        return ["!" * 72,
                f" REIMPORTE o blueprint no DUO: o destino mudou de {de} para {para}.",
                f" Até lá, o DUO usa a cópia antiga, que ainda declara o destino como {de}.",
                "!" * 72]
    return [" O blueprint mudou (porta, chave ou molde): REIMPORTE-o no DUO."]


def _aviso_pasta() -> tuple[str, str] | None:
    """O aviso de pasta sincronizada ou de rede para `dados/`, ou None num disco local comum."""
    # realpath: uma junção ou um atalho de pasta dentro de C:\ pode apontar para o OneDrive.
    onde = pasta_que_sai_da_maquina(os.path.realpath(PASTA_DADOS))
    return None if onde is None else mensagem_pasta(PASTA_DADOS, onde)


def _verificar(cfg: dict, backend) -> int:
    linhas = verificar(cfg, backend, detectar())
    for linha in linhas:
        print(f"[{linha.nivel}] {linha.texto}")
    aviso = _aviso_pasta()
    if aviso:
        risco, acao = aviso
        print(f"[aviso] {risco[0].upper()}{risco[1:]} {acao}")
    return 1 if any(l.nivel == "erro" for l in linhas) else 0


def _mensagem_historico(erro: Exception) -> str:
    """O que dizer quando o histórico não abre: qual arquivo, e o que fazer, sem traceback."""
    if isinstance(erro, ErroMigracao):
        return (f"Não foi possível importar o histórico antigo: {erro}\n"
                "Nada foi gravado nem renomeado. Corrija esse arquivo, ou tire-o da pasta "
                f"{os.path.join(PASTA_DADOS, 'conversas')}, e inicie de novo.")
    banco = os.path.join(PASTA_DADOS, NOME_BANCO)
    return (f"Não foi possível abrir o histórico em {banco}: {erro}\n"
            "Confira se a pasta pode ser gravada e se há espaço no disco. Se o arquivo estiver "
            f"corrompido, renomeie-o (por exemplo, para {NOME_BANCO}.corrompido), junto com "
            f"{NOME_BANCO}-wal e {NOME_BANCO}-shm se existirem, e inicie de novo: o servidor cria um "
            "histórico vazio. O arquivo renomeado guarda o texto das conversas: tente recuperá-lo no "
            "DB Browser for SQLite e apague-o depois.")


def _argumentos(argv: list[str] | None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="Servidor local entre o Duo (PJe+R) e um LLM local",
        epilog="--backend, --url, --modelo, --porta e --perfil ficam gravados em dados/config.json "
               "quando o servidor sobe; com --verificar valem só para o diagnóstico.")
    ap.add_argument("--simulado", action="store_true", help="respostas simuladas (não usa LLM)")
    ap.add_argument("--backend", choices=("ollama", "openai"), help="motor de LLM")
    ap.add_argument("--url", help="URL do motor de LLM")
    ap.add_argument("--modelo", help="modelo padrão")
    ap.add_argument("--porta", type=int, help=f"porta local (padrão {PORTA_PADRAO})")
    ap.add_argument("--verificar", action="store_true",
                    help="confere o ambiente (backend, modelo, capacidades, contexto) e sai")
    ap.add_argument("--perfil", choices=tuple(PERFIS), help="perfil de hardware (cpu ou gpu)")
    return ap.parse_args(argv)


def _configurar_log() -> None:
    # Uma linha por envio e uma por fim de turno (ver turno.py), com data e hora: dá para conferir o
    # que chegou do DUO sem abrir dados/. O texto das mensagens nunca vai para o log (sigilo).
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%Y-%m-%d %H:%M:%S")


def _banner(cfg: dict, backend, simulado: bool, caminho: str, anterior: dict | None, novo: dict) -> None:
    print("=" * 72)
    print(f" Servidor local do Duo {VERSAO}  -  http://127.0.0.1:{cfg['porta']}")
    if simulado:
        print(" Backend: simulado (sem LLM)")
    else:
        print(f" Backend: {cfg['backend']} em {cfg['url']}  -  modelo: {cfg['modelo_padrao']}")
    print(f" Blueprint para importar no Duo: {caminho}")
    if destino_local(cfg, simulado):
        print(" Destino: local")
    elif not eh_loopback(cfg["url"]):
        print(f" ATENÇÃO: o backend em {cfg['url']} não é local — o conteúdo dos autos sai desta máquina.")
    else:
        print(" ATENÇÃO: permitir_modelos_nuvem está ligado — com um modelo de nuvem, o conteúdo dos "
              "autos sai desta máquina.")
    aviso = None if simulado else _aviso_modelo_padrao(cfg, backend)
    if aviso:
        print(aviso)
    pasta = _aviso_pasta()
    if pasta:
        # Com o mesmo destaque do "REIMPORTE": o modelo pode ser local e, ainda assim, o histórico
        # inteiro estar indo para a nuvem.
        risco, acao = pasta
        for linha in ("!" * 72, f" ATENÇÃO: {risco}", f" {acao}", "!" * 72):
            print(linha)
    for linha in _aviso_reimportar(anterior, novo):
        print(linha)
    print(" Ctrl+C encerra.")
    print("=" * 72, flush=True)


def main(argv: list[str] | None = None) -> int:
    args = _argumentos(argv)
    _configurar_log()
    try:
        cfg = carregar_config(PASTA_DADOS)
    except ErroConfig as erro:
        print(erro, file=sys.stderr)
        return 1
    # As opções valem já para esta execução, mas só são gravadas depois do bind: se outra instância
    # estiver na porta, gravar config e blueprint faria o disco mentir sobre o servidor que roda.
    mudou = _aplicar_argumentos(cfg, args)
    try:
        backend = criar_backend(cfg, args.simulado)
    except ErroBackend as erro:
        print(f"Não foi possível iniciar o backend: {erro}", file=sys.stderr)
        return 1

    if args.verificar:
        return _verificar(cfg, backend)

    # Primeiro o bind, depois o banco. Abrir o histórico pode migrar o formato antigo (e renomear
    # dados/conversas); se outra instância já estiver rodando nesta pasta, isso mexeria nos dados
    # dela antes de descobrirmos que a porta está ocupada.
    try:
        servidor = criar_servidor(None, cfg["porta"])
    except OSError:
        if _este_servidor(cfg):
            print(f"O servidor já está rodando em http://127.0.0.1:{cfg['porta']}. Nada a fazer.")
            if mudou or args.simulado:
                print("As opções da linha de comando NÃO foram aplicadas (nem gravadas): encerre o "
                      "servidor que está rodando (Ctrl+C na janela dele) e inicie de novo com elas.")
            return 0
        print(f"A porta {cfg['porta']} está ocupada por outro programa. Rode com --porta N para usar "
              "outra e reimporte no Duo o blueprint gerado.", file=sys.stderr)
        return 1
    try:
        servico = Servico(cfg, PASTA_DADOS, backend, _ler_sistema())
    except (OSError, sqlite3.Error) as erro:
        # Banco corrompido, JSON antigo com campo estranho, pasta sem permissão: a mensagem diz o
        # arquivo e o que fazer. Um traceback só assustaria quem não programa.
        servidor.server_close()
        print(_mensagem_historico(erro), file=sys.stderr)
        return 1
    servidor.definir_servico(servico)

    try:
        if mudou:
            salvar_config(PASTA_DADOS, cfg)
        caminho = os.path.join(PASTA_DADOS, NOME_ARQUIVO)
        anterior = ler_blueprint(caminho)
        novo = gerar_blueprint(cfg, caminho, args.simulado)
        _banner(cfg, backend, args.simulado, caminho, anterior, novo)
        servidor.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        servidor.server_close()
        servico.fechar()
    return 0


if __name__ == "__main__":
    # O console do Windows costuma ser cp1252; o banner tem travessão e acentos.
    for fluxo in (sys.stdout, sys.stderr):
        if hasattr(fluxo, "reconfigure"):
            fluxo.reconfigure(encoding="utf-8")
    raise SystemExit(main())
