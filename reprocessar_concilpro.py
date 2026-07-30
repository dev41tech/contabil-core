"""
Reprocessa arquivos do ConcilPro já gravados, usando o parser corrigido.

Motivo: até o fix do Formato 6 (baselines sobrepostas), a extração perdia cerca
de metade dos lançamentos de cada razão e o `_recuperar_lancamentos_ocultos`
fabricava entradas genéricas no lugar. Débito faltando infla o "a pagar", então
os saldos em aberto gravados estão superestimados.

Uso:
    python reprocessar_concilpro.py                      # dry-run de todos
    python reprocessar_concilpro.py --ids 1,5            # dry-run de alguns
    python reprocessar_concilpro.py --ids 5 --apply      # GRAVA em produção

O dry-run é o padrão: parseia os PDFs, compara com o que está no banco e não
escreve nada. `--apply` apaga fornecedores/lançamentos dos arquivos alvo e
regrava. Antes de qualquer escrita é gerado um dump JSON para rollback.
"""
import argparse
import json
import os
import sys
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import psycopg2

from prod_db import conectar_producao, obter_url, parse_url, resumo_destino

# id do cp_arquivo → PDF de origem local
# ids 2 e 3 são o mesmo arquivo enviado duas vezes
PDFS = {
    1: Path.home() / "Downloads" / "Razão cassol.pdf",
    2: Path.home() / "Downloads" / "Razão JS.pdf",
    3: Path.home() / "Downloads" / "Razão JS.pdf",
    4: Path.home() / "Downloads" / "Razão 2025.pdf",
    5: Path.home() / "Downloads" / "Razão.pdf",
}

DUMP_DIR = Path(__file__).parent / "backup_concilpro"


def apontar_orm_para_producao():
    """
    Faz o SQLAlchemy usar o banco de PRODUÇÃO.

    `_processar_arquivo_background` grava via SyncSessionLocal, que resolve o
    destino por `get_settings().database_url` — e o `.env` local aponta para o
    banco de desenvolvimento. Sem isto, `--apply` apagaria dados de produção e
    regravaria no banco errado. Precisa rodar ANTES de qualquer import de `src`,
    porque get_settings() é cacheado.
    """
    if "src.core.config" in sys.modules:
        sys.exit("❌ src.core.config já importado — o destino do ORM não pode mais ser trocado.")

    url = obter_url()
    os.environ["DATABASE_URL"] = url

    from src.core.config import get_settings

    alvo = parse_url(url)
    efetivo = str(get_settings().database_url)
    esperado = f"{alvo['host']}:{alvo['port']}"

    if esperado not in efetivo or alvo["dbname"] not in efetivo:
        sys.exit(
            "❌ o ORM não está apontando para produção — abortando antes de apagar nada.\n"
            f"   esperado conter : {esperado}/{alvo['dbname']}\n"
            f"   efetivo         : {efetivo.replace(alvo['password'], '***')}"
        )

    print(f"🔒 ORM apontado para produção: {resumo_destino()}\n")


def _serializar(valor):
    if isinstance(valor, Decimal):
        return str(valor)
    if isinstance(valor, (datetime, date)):
        return valor.isoformat()
    return valor


def estado_atual(cur, arquivo_id):
    """Lê fornecedores + lançamentos de um arquivo (para dump e comparação)."""
    cur.execute("""
        SELECT id, codigo_conta, nome_fornecedor, total_debito, total_credito,
               saldo_final, valor_a_pagar, status_pagamento, divergencia_calculo
        FROM cp_fornecedor WHERE arquivo_origem_id = %s ORDER BY codigo_conta
    """, (arquivo_id,))
    cols = [d[0] for d in cur.description]
    fornecedores = [dict(zip(cols, r)) for r in cur.fetchall()]

    ids = [f["id"] for f in fornecedores]
    lancamentos = []
    if ids:
        cur.execute("""
            SELECT * FROM cp_lancamento WHERE fornecedor_id = ANY(%s) ORDER BY id
        """, (ids,))
        cols = [d[0] for d in cur.description]
        lancamentos = [dict(zip(cols, r)) for r in cur.fetchall()]

    return fornecedores, lancamentos


def gerar_dump(cur, arquivo_ids):
    """Dump JSON do estado atual — rollback e comparação antes/depois."""
    DUMP_DIR.mkdir(exist_ok=True)
    marca = datetime.now().strftime("%Y%m%d-%H%M%S")
    destino = DUMP_DIR / f"dump-{marca}.json"

    payload = {}
    for arquivo_id in arquivo_ids:
        fornecedores, lancamentos = estado_atual(cur, arquivo_id)
        payload[str(arquivo_id)] = {
            "fornecedores": [{k: _serializar(v) for k, v in f.items()} for f in fornecedores],
            "lancamentos": [{k: _serializar(v) for k, v in l.items()} for l in lancamentos],
        }

    destino.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    total_f = sum(len(v["fornecedores"]) for v in payload.values())
    total_l = sum(len(v["lancamentos"]) for v in payload.values())
    print(f"💾 Dump salvo: {destino}")
    print(f"   {total_f} fornecedores | {total_l} lançamentos preservados\n")
    return destino


def parsear(arquivo_id):
    """Roda o parser corrigido sobre o PDF local do arquivo."""
    from src.domain.concilpro.parser import parsear_arquivo_razao
    from src.domain.concilpro.consolidador import consolidar_todos_fornecedores

    caminho = PDFS[arquivo_id]
    if not caminho.exists():
        raise FileNotFoundError(f"PDF não encontrado: {caminho}")

    dados = parsear_arquivo_razao(caminho.read_bytes())
    return consolidar_todos_fornecedores(dados)


def comparar(cur, arquivo_id, dados):
    """Imprime o diff entre o banco e o resultado do parser corrigido."""
    fornecedores_db, lancamentos_db = estado_atual(cur, arquivo_id)
    por_conta = {f["codigo_conta"]: f for f in fornecedores_db}

    sinteticos_db = sum(1 for l in lancamentos_db if "RECUPERADO" in (l["historico"] or ""))
    novos = dados["fornecedores"]
    lanc_novos = sum(len(f.get("lancamentos", [])) for f in novos)
    sinteticos_novos = sum(
        1 for f in novos for l in f.get("lancamentos", []) if l.get("sintetico")
    )

    print(f"  fornecedores : {len(fornecedores_db):4d} → {len(novos):4d}")
    print(f"  lançamentos  : {len(lancamentos_db):4d} → {lanc_novos:4d}")
    print(f"  fabricados   : {sinteticos_db:4d} → {sinteticos_novos:4d}")

    a_pagar_antes = sum(Decimal(str(f["valor_a_pagar"] or 0)) for f in fornecedores_db)
    a_pagar_depois = Decimal("0")
    mudancas = []

    for forn in novos:
        codigo = str(forn.get("codigo_conta") or "")[:10]
        deb = sum(Decimal(str(l.get("valor_debito", 0))) for l in forn.get("lancamentos", []))
        cred = sum(Decimal(str(l.get("valor_credito", 0))) for l in forn.get("lancamentos", []))
        saldo_ant = Decimal(str(forn.get("saldo_anterior", 0)))
        novo_a_pagar = cred - deb
        a_pagar_depois += novo_a_pagar

        antigo = por_conta.get(codigo)
        if antigo is None:
            mudancas.append((codigo, forn.get("nome_fornecedor", "")[:34], None, novo_a_pagar))
            continue
        velho_a_pagar = Decimal(str(antigo["valor_a_pagar"] or 0))
        if abs(velho_a_pagar - novo_a_pagar) > Decimal("0.01"):
            mudancas.append((codigo, forn.get("nome_fornecedor", "")[:34],
                             velho_a_pagar, novo_a_pagar))

    print(f"  a pagar      : R$ {a_pagar_antes:>14,.2f} → R$ {a_pagar_depois:>14,.2f}")

    if mudancas:
        print(f"\n  {len(mudancas)} fornecedor(es) com 'a pagar' alterado:")
        for codigo, nome, velho, novo in mudancas[:25]:
            antes = f"R$ {velho:>12,.2f}" if velho is not None else "     (novo)  "
            print(f"    {codigo:<8} {nome:<34} {antes} → R$ {novo:>12,.2f}")
        if len(mudancas) > 25:
            print(f"    … e {len(mudancas) - 25} outros")
    print()


def aplicar(conn, cur, arquivo_id, conteudo):
    """Apaga os dados do arquivo e regrava com o parser corrigido."""
    from src.api.v1.concilpro import _processar_arquivo_background

    cur.execute("""
        DELETE FROM cp_lancamento WHERE fornecedor_id IN (
            SELECT id FROM cp_fornecedor WHERE arquivo_origem_id = %s
        )
    """, (arquivo_id,))
    lanc_apagados = cur.rowcount
    cur.execute("DELETE FROM cp_fornecedor WHERE arquivo_origem_id = %s", (arquivo_id,))
    forn_apagados = cur.rowcount
    cur.execute(
        "UPDATE cp_arquivo SET status = 'PROCESSANDO', mensagem_erro = NULL WHERE id = %s",
        (arquivo_id,),
    )
    conn.commit()
    print(f"  🗑️  apagados: {forn_apagados} fornecedores, {lanc_apagados} lançamentos")

    print(f"  ⚙️  reprocessando…")
    _processar_arquivo_background(arquivo_id, conteudo)
    print(f"  ✅ arquivo {arquivo_id} regravado")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ids", help="ids de cp_arquivo separados por vírgula (padrão: todos)")
    ap.add_argument("--apply", action="store_true",
                    help="GRAVA em produção (sem esta flag, apenas dry-run)")
    args = ap.parse_args()

    if args.ids:
        alvos = [int(x) for x in args.ids.split(",")]
        desconhecidos = [i for i in alvos if i not in PDFS]
        if desconhecidos:
            sys.exit(f"❌ sem PDF mapeado para o(s) id(s): {desconhecidos}")
    else:
        alvos = sorted(PDFS)

    faltando = [str(PDFS[i]) for i in alvos if not PDFS[i].exists()]
    if faltando:
        sys.exit("❌ PDFs não encontrados:\n  " + "\n  ".join(sorted(set(faltando))))

    modo = "APLICAR (grava em produção)" if args.apply else "DRY-RUN (nada é gravado)"
    print(f"\n{'=' * 74}\nReprocessamento ConcilPro — {modo}")
    print(f"arquivos alvo: {alvos}\n{'=' * 74}\n")

    if args.apply:
        apontar_orm_para_producao()

    conn = conectar_producao()
    cur = conn.cursor()

    try:
        gerar_dump(cur, alvos)

        for arquivo_id in alvos:
            cur.execute("SELECT nome_arquivo FROM cp_arquivo WHERE id = %s", (arquivo_id,))
            row = cur.fetchone()
            if not row:
                print(f"⚠️  arquivo {arquivo_id} não existe no banco — pulando\n")
                continue

            print(f"── arquivo {arquivo_id}: {row[0]} ──")
            print(f"   PDF: {PDFS[arquivo_id].name}")

            if args.apply:
                # Não parseia aqui só para comparar: o reprocessamento abaixo já
                # parseia, e uma segunda passada dobraria o custo de IA além de
                # exibir um diff que não corresponde ao que foi gravado.
                antes, _ = estado_atual(cur, arquivo_id)
                a_pagar_antes = sum(Decimal(str(f["valor_a_pagar"] or 0)) for f in antes)
                aplicar(conn, cur, arquivo_id, PDFS[arquivo_id].read_bytes())

                depois, lanc_depois = estado_atual(cur, arquivo_id)
                a_pagar_depois = sum(Decimal(str(f["valor_a_pagar"] or 0)) for f in depois)
                sinteticos = sum(1 for l in lanc_depois if "RECUPERADO" in (l["historico"] or ""))
                divergentes = sum(1 for f in depois if f["divergencia_calculo"])
                print(f"  fornecedores : {len(antes):4d} → {len(depois):4d}")
                print(f"  lançamentos  : {len(lanc_depois):4d} (sintéticos: {sinteticos})")
                print(f"  divergências : {divergentes}")
                print(f"  a pagar      : R$ {a_pagar_antes:>14,.2f} → R$ {a_pagar_depois:>14,.2f}")
                print()
            else:
                comparar(cur, arquivo_id, parsear(arquivo_id))

        if not args.apply:
            print("=" * 74)
            print("DRY-RUN — nada foi gravado. Revise o diff acima e rode com --apply.")
            print("=" * 74)
    finally:
        cur.close()
        conn.close()


if __name__ == "__main__":
    main()
