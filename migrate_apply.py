"""Aplica migrate_prod.sql no banco de produção em lotes."""
import sys, time
import psycopg2

PROD_URL = "postgres://postgres:b2ad156f04d4203f02f3@vps.41tech.cloud:3308/contabil_db"
SQL_FILE = "migrate_prod.sql"
BATCH_SIZE = 500   # inserts por transação


def parse_url(url):
    # postgres://user:pass@host:port/db
    url = url.replace("postgres://", "")
    userpass, rest = url.split("@", 1)
    user, password = userpass.split(":", 1)
    hostport, dbname = rest.split("/", 1)
    if ":" in hostport:
        host, port = hostport.split(":", 1)
    else:
        host, port = hostport, "5432"
    return dict(host=host, port=int(port), dbname=dbname, user=user, password=password)


def main():
    print(f"Conectando em produção...")
    params = parse_url(PROD_URL)
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
