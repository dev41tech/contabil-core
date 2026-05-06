"""Reseta arquivos CONCLUIDO com 0 fornecedores para permitir reprocessamento."""
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

# Lista arquivos com problema
cur.execute("""
    SELECT id, nome_arquivo, status, total_fornecedores
    FROM cp_arquivo
    WHERE total_fornecedores = 0
""")
rows = cur.fetchall()
print(f"Arquivos com 0 fornecedores: {len(rows)}")
for r in rows:
    print(f"  id={r[0]} status={r[2]} arquivo={r[1]}")

if rows:
    # Reseta para ERRO para que o upload permita reprocessar
    cur.execute("""
        UPDATE cp_arquivo
        SET status = 'ERRO',
            mensagem_erro = 'Reprocessar: OpenAI nao estava configurada na primeira execucao'
        WHERE total_fornecedores = 0
    """)
    conn.commit()
    print(f"\n{cur.rowcount} arquivo(s) resetado(s) para ERRO — pode reenviar o PDF.")
else:
    print("Nenhum arquivo para resetar.")

cur.close()
conn.close()
