"""Zera os uploads de extrato bancário para recomeçar a importação.

Os extratos foram importados enquanto o parser tinha o bug do saldo devedor
(corrigido em 2026-08-21) e enquanto a data era gravada como instante em UTC
(corrigido em 2026-08-24). O que está no banco não representa mais o que os
parsers produzem hoje, e reimportar por cima não resolve: o `hash_dedup` de PDF
deriva de `fitid = MD5(data + historico + valor + idx)`, e as correções mudaram
os três — as linhas novas entrariam ao lado das antigas em vez de deduplicar.

O que este script apaga, por empresa:

    transacoes                 (soft delete)
    registros_contabeis        (soft delete) — as partidas geradas pelo NEO a
                               partir dessas transações; sem isso o razão fica
                               apontando para transação apagada

O que ele NÃO toca: plano de contas, regras, contrapartes, notas, comprovantes,
cartões e ConcilPro. Notas e comprovantes voltam a ficar sem transação associada
(`transacao_id = NULL`), disponíveis para a próxima importação.

SOMENTE SOFT DELETE. Nada é removido de fato: `deleted_at` é preenchido, e há
backup JSON completo antes de qualquer escrita. O padrão é DRY-RUN.

Uso:
    .venv\\Scripts\\python.exe limpar_extratos.py                  # simula
    .venv\\Scripts\\python.exe limpar_extratos.py --executar       # aplica
    .venv\\Scripts\\python.exe limpar_extratos.py --empresa TRECHO # limita a uma empresa
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from prod_db import conectar_producao, resumo_destino


def _preparar_saida() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass


def _inventario(cur, filtro_empresa: str | None) -> list[tuple]:
    """Quantas transações e partidas cada empresa tem, para o operador conferir."""
    cur.execute(
        """
        SELECT e.razao_social,
               COUNT(t.id) AS transacoes,
               COUNT(*) FILTER (WHERE t.status <> 'pendente') AS classificadas,
               MIN(t.data) AS de,
               MAX(t.data) AS ate
        FROM transacoes t
        JOIN empresas e ON e.id = t.empresa_id
        WHERE t.deleted_at IS NULL
          AND (%s IS NULL OR e.razao_social ILIKE %s)
        GROUP BY e.razao_social
        ORDER BY e.razao_social
        """,
        (filtro_empresa, f"%{filtro_empresa}%" if filtro_empresa else None),
    )
    return cur.fetchall()


def _ids_no_escopo(cur, filtro_empresa: str | None) -> list:
    cur.execute(
        """
        SELECT t.id
        FROM transacoes t
        JOIN empresas e ON e.id = t.empresa_id
        WHERE t.deleted_at IS NULL
          AND (%s IS NULL OR e.razao_social ILIKE %s)
        """,
        (filtro_empresa, f"%{filtro_empresa}%" if filtro_empresa else None),
    )
    return [r[0] for r in cur.fetchall()]


def _backup(cur, ids: list) -> Path:
    cur.execute(
        """
        SELECT id, empresa_id, agencia_id, data, historico, valor, saldo_apos,
               dc, status, hash_dedup
        FROM transacoes WHERE id = ANY(%s::uuid[])
        """,
        (ids,),
    )
    transacoes = cur.fetchall()
    cur.execute(
        """
        SELECT id, empresa_id, transacao_id, lancamento_id, conta_id, descricao,
               historico, historico_extrato, dc, tipo_regra, valor, data_lancamento
        FROM registros_contabeis
        WHERE transacao_id = ANY(%s::uuid[]) AND deleted_at IS NULL
        """,
        (ids,),
    )
    registros = cur.fetchall()

    destino = (
        Path(tempfile.gettempdir())
        / f"backup_limpeza_extratos_{datetime.now():%Y%m%d_%H%M%S}.json"
    )
    destino.write_text(
        json.dumps(
            {
                "gerado_em": datetime.now(timezone.utc).isoformat(),
                "transacoes": transacoes,
                "registros_contabeis": registros,
            },
            default=str,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"   backup: {destino}")
    print(f"   {len(transacoes)} transações e {len(registros)} registros contábeis salvos")
    return destino


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--executar", action="store_true", help="aplica (sem isso, só simula)")
    ap.add_argument("--empresa", help="limita a empresas cuja razão social contenha o trecho")
    _preparar_saida()
    args = ap.parse_args()

    modo = "EXECUÇÃO" if args.executar else "DRY-RUN (nada será escrito)"
    print(f"Destino: {resumo_destino()}")
    print(f"Modo   : {modo}")
    print(f"Escopo : {args.empresa or 'TODAS as empresas'}\n")

    conn = conectar_producao()
    conn.set_session(readonly=not args.executar)
    cur = conn.cursor()

    inventario = _inventario(cur, args.empresa)
    if not inventario:
        print("Nenhuma transação de extrato encontrada — nada a limpar.")
        cur.close()
        conn.close()
        return 0

    print(f"{'empresa':<44}{'transações':>12}{'classificadas':>15}  período")
    print("-" * 92)
    total = classificadas_total = 0
    for razao, qtd, classificadas, de, ate in inventario:
        periodo = f"{de} → {ate}" if de else ""
        print(f"{(razao or '')[:42]:<44}{qtd:>12}{classificadas:>15}  {periodo}")
        total += qtd
        classificadas_total += classificadas
    print("-" * 92)
    print(f"{'TOTAL':<44}{total:>12}{classificadas_total:>15}")

    ids = _ids_no_escopo(cur, args.empresa)
    cur.execute(
        "SELECT COUNT(*) FROM registros_contabeis "
        "WHERE transacao_id = ANY(%s::uuid[]) AND deleted_at IS NULL",
        (ids,),
    )
    n_registros = cur.fetchone()[0]
    print(f"\nPartidas contábeis geradas a partir dessas transações: {n_registros}")

    if not args.executar:
        print("\n" + "=" * 72)
        print("DRY-RUN — nada foi alterado. Para aplicar:")
        print("   .venv\\Scripts\\python.exe limpar_extratos.py --executar")
        print("=" * 72)
        cur.close()
        conn.close()
        return 0

    print("\n1) Backup")
    _backup(cur, ids)

    print("\n2) Soft delete")
    agora = datetime.now(timezone.utc)
    cur.execute(
        "UPDATE registros_contabeis SET deleted_at = %s "
        "WHERE transacao_id = ANY(%s::uuid[]) AND deleted_at IS NULL",
        (agora, ids),
    )
    print(f"   registros contábeis: {cur.rowcount}")
    # Desassocia notas e comprovantes: eles sobrevivem à limpeza e precisam ficar
    # livres para a próxima importação reencontrá-los.
    cur.execute(
        "UPDATE notas_fiscais SET transacao_id = NULL "
        "WHERE transacao_id = ANY(%s::uuid[])",
        (ids,),
    )
    print(f"   notas desassociadas : {cur.rowcount}")
    cur.execute(
        "UPDATE comprovantes SET transacao_id = NULL "
        "WHERE transacao_id = ANY(%s::uuid[])",
        (ids,),
    )
    print(f"   comprovantes desass.: {cur.rowcount}")
    cur.execute(
        "UPDATE transacoes SET deleted_at = %s WHERE id = ANY(%s::uuid[]) AND deleted_at IS NULL",
        (agora, ids),
    )
    print(f"   transações          : {cur.rowcount}")
    conn.commit()

    restantes = _inventario(cur, args.empresa)
    cur.close()
    conn.close()

    print("\n3) Conferência")
    if restantes:
        print("   ⚠ ainda restam transações ativas no escopo:")
        for razao, qtd, *_ in restantes:
            print(f"     {razao}: {qtd}")
        return 1
    print("   nenhuma transação ativa no escopo — extrato zerado")
    print("\n" + "=" * 72)
    print("✅ Limpeza concluída. Pode reimportar os extratos.")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    sys.exit(main())
