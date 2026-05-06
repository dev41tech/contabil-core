"""Verifica estado do ConcilPro no banco de produção."""
import psycopg2

PROD_URL = "postgres://postgres:b2ad156f04d4203f02f3@vps.41tech.cloud:3308/contabil_db"

def parse_url(url):
    url = url.replace("postgres://", "")
    userpass, rest = url.split("@", 1)
    user, password = userpass.split(":", 1)
    hostport, dbname = rest.split("/", 1)
    host, port = (hostport.split(":", 1) if ":" in hostport else (hostport, "5432"))
    return dict(host=host, port=int(port), dbname=dbname, user=user, password=password)

conn = psycopg2.connect(**parse_url(PROD_URL), connect_timeout=15)
cur = conn.cursor()

print("=== TODOS os cp_arquivo ===")
cur.execute("""
    SELECT id, nome_arquivo, status, total_fornecedores, mensagem_erro, created_at
    FROM cp_arquivo ORDER BY id
""")
for r in cur.fetchall():
    print(f"  id={r[0]} | status={r[2]} | fornecedores={r[3]}")
    print(f"    arquivo : {r[1]}")
    print(f"    criado  : {r[5]}")
    if r[4]:
        print(f"    erro    : {r[4][:120]}")

print()
cur.execute("SELECT COUNT(*) FROM cp_fornecedor")
print(f"cp_fornecedor total : {cur.fetchone()[0]}")
cur.execute("SELECT COUNT(*) FROM cp_lancamento")
print(f"cp_lancamento total : {cur.fetchone()[0]}")

# Verifica se a chave openai está visível via env do processo (indiretamente)
print()
print("=== Variáveis de ambiente do banco (postgres_version) ===")
cur.execute("SELECT version()")
print(f"  {cur.fetchone()[0][:60]}")

cur.close()
conn.close()
