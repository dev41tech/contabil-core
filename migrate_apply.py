"""Aplica migrate_prod.sql no banco de produção de forma atômica."""

from __future__ import annotations

import sys
import time

import psycopg2

from prod_db import obter_url, parse_url, resumo_destino

SQL_FILE = "migrate_prod.sql"
BATCH_SIZE = 500  # usado apenas para progresso; existe uma única transação


def carregar_statements() -> list[str]:
    print(f"Lendo {SQL_FILE}...")
    with open(SQL_FILE, encoding="utf-8") as arquivo:
        conteudo = arquivo.read()

    # O arquivo gerado contém um INSERT por linha. BEGIN/COMMIT são ignorados,
    # pois esta aplicação controla uma única transação para o arquivo inteiro.
    return [
        linha.strip()
        for linha in conteudo.splitlines()
        if linha.strip()
        and not linha.strip().startswith("--")
        and linha.strip() not in ("BEGIN;", "COMMIT;")
    ]


def main() -> int:
    print(f"Conectando em {resumo_destino()}...")
    params = parse_url(obter_url())
    statements = carregar_statements()
    total = len(statements)
    print(f"Total de statements: {total}")

    conn = psycopg2.connect(**params, connect_timeout=15)
    conn.autocommit = False
    cur = conn.cursor()
    print("Conectado OK.")

    inicio = time.time()
    executados = 0
    indice_atual = 0

    try:
        for indice_atual, sql in enumerate(statements, start=1):
            cur.execute(sql)
            executados = indice_atual

            if executados % BATCH_SIZE == 0 or executados == total:
                elapsed = time.time() - inicio
                pct = executados / total * 100 if total else 100
                print(f"  {executados}/{total} ({pct:.0f}%) — {elapsed:.0f}s")

        conn.commit()
    except Exception as exc:
        conn.rollback()
        elapsed = time.time() - inicio
        print(f"\nFALHA no statement {indice_atual}/{total}: {exc}", file=sys.stderr)
        print("Migração abortada; a transação inteira foi revertida.", file=sys.stderr)
        print(f"  Executados antes da falha: {executados}", file=sys.stderr)
        print("  Alterações persistidas   : 0", file=sys.stderr)
        print(f"  Tempo até a falha        : {elapsed:.0f}s", file=sys.stderr)
        return 1
    finally:
        cur.close()
        conn.close()

    elapsed = time.time() - inicio
    print(f"\nConcluído com sucesso em {elapsed:.0f}s")
    print(f"  Inseridos : {executados}")
    print("  Erros     : 0")
    return 0


if __name__ == "__main__":
    sys.exit(main())
