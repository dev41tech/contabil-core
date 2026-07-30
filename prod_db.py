"""
Conexão com o banco de PRODUÇÃO para os scripts de manutenção da raiz.

A URL vem de `PROD_DATABASE_URL` no `.env` — nunca hardcoded. Até 2026-07-30 a
senha de produção estava escrita em quatro scripts deste diretório e chegou ao
histórico do git; se este módulo for contornado, o problema volta.

Uso:
    from prod_db import conectar_producao, resumo_destino

    print(resumo_destino())
    conn = conectar_producao()
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Dict

import psycopg2

_ENV_VAR = "PROD_DATABASE_URL"


def _carregar_env() -> None:
    """Lê o .env da raiz sem depender de python-dotenv."""
    caminho = Path(__file__).parent / ".env"
    if not caminho.exists():
        return
    for linha in caminho.read_text(encoding="utf-8").splitlines():
        linha = linha.strip()
        if not linha or linha.startswith("#") or "=" not in linha:
            continue
        chave, _, valor = linha.partition("=")
        chave = chave.strip()
        if chave and chave not in os.environ:
            os.environ[chave] = valor.strip().strip('"').strip("'")


def obter_url() -> str:
    """Devolve a URL de produção, ou encerra com instruções se não estiver configurada."""
    _carregar_env()
    url = os.environ.get(_ENV_VAR, "").strip()
    if not url:
        sys.exit(
            f"❌ {_ENV_VAR} não configurada.\n"
            f"   Adicione ao .env da raiz do projeto:\n"
            f"   {_ENV_VAR}=postgres://USUARIO:SENHA@HOST:PORTA/BANCO"
        )
    return url


def parse_url(url: str) -> Dict:
    """Quebra postgres://user:pass@host:port/db nos parâmetros do psycopg2."""
    url = url.replace("postgresql://", "").replace("postgres://", "")
    userpass, rest = url.split("@", 1)
    user, password = userpass.split(":", 1)
    hostport, dbname = rest.split("/", 1)
    host, port = (hostport.split(":", 1) if ":" in hostport else (hostport, "5432"))
    return dict(host=host, port=int(port), dbname=dbname, user=user, password=password.split("?")[0])


def conectar_producao(timeout: int = 15):
    """Abre conexão psycopg2 com o banco de produção."""
    return psycopg2.connect(**parse_url(obter_url()), connect_timeout=timeout)


def resumo_destino() -> str:
    """Descreve o destino sem revelar a senha — para log e confirmação visual."""
    p = parse_url(obter_url())
    return f"{p['host']}:{p['port']}/{p['dbname']} (usuário {p['user']})"
