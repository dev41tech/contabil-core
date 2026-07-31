"""Verifica estado do ConcilPro no banco de produção."""
from prod_db import conectar_producao, resumo_destino

print(f"Conectando em {resumo_destino()}\n")
conn = conectar_producao()
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
