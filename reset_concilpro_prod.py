"""Reseta arquivos CONCLUIDO com 0 fornecedores para permitir reprocessamento."""
from prod_db import conectar_producao, resumo_destino

print(f"Conectando em {resumo_destino()}\n")
conn = conectar_producao()
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
