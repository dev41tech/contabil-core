"""Aplica migrate_prod.sql no banco de produção em lotes."""
import sys, time
import psycopg2

from prod_db import obter_url, parse_url, resumo_destino

SQL_FILE = "migrate_prod.sql"
BATCH_SIZE = 500   # inserts por transação


def main():
    print(f"Conectando em {resumo_destino()}...")
    params = parse_url(obter_url())
    conn = psycopg2.connect(**params, connect_timeout=15)
    conn.autocommit = False
    cur = conn.cursor()
    print("Conectado OK.")

    print(f"Lendo {SQL_FILE}...")
    with open(SQL_FILE, encoding="utf-8") as f:
        conteudo = f.read()

    # Separa os INSERTs (ignora BEGIN/COMMIT — controlamos manualmente)
    linhas = [
        l.strip() for l in conteudo.splitlines()
        if l.strip()
        and not l.strip().startswith("--")
        and l.strip() not in ("BEGIN;", "COMMIT;")
    ]

    total = len(linhas)
    print(f"Total de statements: {total}")

    inseridos = 0
    erros = 0
    inicio = time.time()

    # Processa em lotes
    for i in range(0, total, BATCH_SIZE):
        lote = linhas[i:i + BATCH_SIZE]
        try:
            for sql in lote:
                if sql:
                    cur.execute(sql)
            conn.commit()
            inseridos += len(lote)
        except Exception as e:
            conn.rollback()
            erros += len(lote)
            print(f"  ERRO no lote {i}-{i+len(lote)}: {e}")

        if (i // BATCH_SIZE) % 10 == 0:
            elapsed = time.time() - inicio
            pct = inseridos / total * 100
            print(f"  {inseridos}/{total} ({pct:.0f}%) — {elapsed:.0f}s")

    elapsed = time.time() - inicio
    print(f"\nConcluído em {elapsed:.0f}s")
    print(f"  Inseridos : {inseridos}")
    print(f"  Erros     : {erros}")

    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
