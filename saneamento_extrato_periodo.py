"""Saneamento de um período de extrato corrompido pela extração por IA.

Contexto: enquanto `_TX_LINE` não aceitava saldo negativo (corrigido em
2026-08-21), extratos de conta no vermelho falhavam na camada determinística e
caíam na camada de IA. Na SINCOPEÇAS, fev–mai/2026, a IA além de trocar valor
por saldo em 34 lançamentos, **perdeu ~144 lançamentos**: o banco tem 258 onde
os extratos têm 402, e as somas do período não reconciliam com nada.

Corrigir linha a linha não resolve — o período precisa ser reconstruído a
partir dos PDFs originais, que agora parseiam e reconciliam no centavo.

Por que não dá para simplesmente reimportar por cima: o `hash_dedup` de PDF
deriva de `fitid = MD5(data + historico + valor + idx)`, e a correção do parser
muda os três. A reimportação não deduplicaria contra o que já existe — inseriria
as 402 ao lado das 258.

Fases:
    1. Backup dos registros afetados em JSON (sempre, antes de qualquer escrita)
    2. Soft-delete dos registros contábeis das transações do período
    3. Soft-delete das transações do período
    4. Reimportação dos PDFs pelo serviço da aplicação (aplica a barreira de valor)
    5. Conferência: soma do banco × soma do extrato, por competência

O padrão é DRY-RUN. Nada é escrito sem `--executar`.

Uso:
    python saneamento_extrato_periodo.py                # simula
    python saneamento_extrato_periodo.py --executar     # aplica
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from uuid import UUID

from prod_db import conectar_producao, obter_url, resumo_destino

# ── Escopo do saneamento ────────────────────────────────────────────────────
EMPRESA_LIKE = "%SINCOPE%"
DATA_DE = "2026-02-01"
DATA_ATE = "2026-06-01"   # exclusivo

_BASE = (
    r"J:\SINCOPEÇAS - SINDICATO DO COMERCIO VAREJISTA, ATACADISTA"
    r"\CONTABIL\EXTRATOS\2026"
)
EXTRATOS = [
    ("2026-02", rf"{_BASE}\02.2026\EXTRATOS\EXTRATO conta corrente.pdf"),
    ("2026-03", rf"{_BASE}\03.2026\EXTRATO\EXTRATO conta corrente.pdf"),
    ("2026-04", rf"{_BASE}\04.2026\EXTRATO conta corrente.pdf"),
    ("2026-05", rf"{_BASE}\05.2026\EXTRATO conta corrente.pdf"),
]


def _preparar_saida() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass


def _parsear_extratos() -> dict[str, list]:
    """Parseia os PDFs com o parser corrigido, antes de tocar no banco.

    Se algum arquivo falhar, o saneamento é abortado com o banco intacto —
    apagar o período sem ter o substituto em mãos seria perda de dado.
    """
    from src.domain.extrato.pdf_parser import (
        _PDFBudget,
        _extrair_linhas_pdfplumber,
        _parse_linhas_multipagina,
    )

    parsed: dict[str, list] = {}
    for mes, caminho in EXTRATOS:
        p = Path(caminho)
        if not p.exists():
            raise SystemExit(f"❌ Extrato não encontrado: {caminho}")
        budget = _PDFBudget(
            deadline=time.monotonic() + 300, max_pages=60, ai_calls_restantes=0
        )
        linhas, _ = _extrair_linhas_pdfplumber(p.read_bytes(), budget)
        tx = _parse_linhas_multipagina(linhas, 2026)
        if not tx:
            raise SystemExit(f"❌ {mes}: parser não extraiu nenhuma transação.")
        parsed[mes] = tx
        print(f"   {mes}: {len(tx):3d} transações, soma {sum(t.valor for t in tx):>12}")
    return parsed


def _escopo(cur) -> tuple[UUID, UUID, list[tuple]]:
    """Resolve empresa/agência e lista as transações do período."""
    cur.execute(
        """
        SELECT t.id, t.empresa_id, t.agencia_id, t.data, t.historico, t.valor,
               t.dc, t.status, t.hash_dedup
        FROM transacoes t
        JOIN empresas e ON e.id = t.empresa_id
        WHERE t.deleted_at IS NULL
          AND e.razao_social ILIKE %s
          AND t.data >= %s AND t.data < %s
        ORDER BY t.data
        """,
        (EMPRESA_LIKE, DATA_DE, DATA_ATE),
    )
    linhas = cur.fetchall()
    if not linhas:
        raise SystemExit("Nenhuma transação no escopo — nada a sanear.")

    empresas = {r[1] for r in linhas}
    agencias = {r[2] for r in linhas}
    if len(empresas) != 1 or len(agencias) != 1:
        raise SystemExit(
            f"❌ Escopo ambíguo: {len(empresas)} empresa(s), {len(agencias)} agência(s). "
            "O script assume uma conta só — revise EMPRESA_LIKE."
        )
    return empresas.pop(), agencias.pop(), linhas


def _backup(cur, transacao_ids: list[UUID]) -> Path:
    cur.execute(
        """
        SELECT id, empresa_id, agencia_id, data, historico, valor, dc, status, hash_dedup
        FROM transacoes WHERE id = ANY(%s::uuid[])
        """,
        (transacao_ids,),
    )
    transacoes = cur.fetchall()
    cur.execute(
        """
        SELECT id, empresa_id, transacao_id, lancamento_id, conta_id, descricao,
               historico, historico_extrato, dc, tipo_regra, valor, data_lancamento
        FROM registros_contabeis
        WHERE transacao_id = ANY(%s::uuid[]) AND deleted_at IS NULL
        """,
        (transacao_ids,),
    )
    registros = cur.fetchall()

    destino = (
        Path(tempfile.gettempdir())
        / f"backup_saneamento_{datetime.now():%Y%m%d_%H%M%S}.json"
    )
    destino.write_text(
        json.dumps(
            {
                "gerado_em": datetime.now(timezone.utc).isoformat(),
                "escopo": {"empresa_like": EMPRESA_LIKE, "de": DATA_DE, "ate": DATA_ATE},
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


async def _reimportar(empresa_id: UUID, agencia_id: UUID, parsed: dict[str, list]) -> None:
    """Reimporta pelo serviço da aplicação — aplica a barreira de valor e o dedup."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from src.db.session import _normalizar_url
    from src.domain.extrato.service import ExtratoService

    engine = create_async_engine(_normalizar_url(obter_url()), pool_pre_ping=True)
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    try:
        for mes, transacoes in parsed.items():
            async with factory() as db:
                svc = ExtratoService(db=db, empresa_id=empresa_id)
                res = await svc.importar_transacoes_raw(transacoes, agencia_id)
                await db.commit()
            print(
                f"   {mes}: importadas={res.importadas} duplicadas={res.duplicadas} "
                f"rejeitadas={getattr(res, 'rejeitadas', 0)}"
            )
    finally:
        await engine.dispose()


def _conferir(cur, parsed: dict[str, list]) -> bool:
    cur.execute(
        """
        SELECT to_char(t.data,'YYYY-MM'), COUNT(*),
               COALESCE(SUM(CASE WHEN t.dc='D' THEN -t.valor ELSE t.valor END),0)
        FROM transacoes t JOIN empresas e ON e.id = t.empresa_id
        WHERE t.deleted_at IS NULL AND e.razao_social ILIKE %s
          AND t.data >= %s AND t.data < %s
        GROUP BY 1 ORDER BY 1
        """,
        (EMPRESA_LIKE, DATA_DE, DATA_ATE),
    )
    banco = {m: (c, s) for m, c, s in cur.fetchall()}

    print(f"\n   {'mês':<9}{'banco':>20}{'extrato':>20}   confere")
    print("   " + "-" * 58)
    tudo_ok = True
    for mes, transacoes in parsed.items():
        qb, sb = banco.get(mes, (0, Decimal(0)))
        qp, sp = len(transacoes), sum(t.valor for t in transacoes)
        ok = qb == qp and Decimal(sb) == sp
        tudo_ok &= ok
        print(
            f"   {mes:<9}{f'{qb} / {sb}':>20}{f'{qp} / {sp}':>20}   "
            f"{'OK' if ok else 'DIVERGE'}"
        )
    return tudo_ok


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--executar",
        action="store_true",
        help="aplica as alterações (sem esta flag apenas simula)",
    )
    _preparar_saida()
    args = ap.parse_args()

    modo = "EXECUÇÃO" if args.executar else "DRY-RUN (nada será escrito)"
    print(f"Destino: {resumo_destino()}")
    print(f"Modo   : {modo}")
    print(f"Escopo : {EMPRESA_LIKE}  {DATA_DE} → {DATA_ATE}\n")

    print("1) Parseando os extratos originais (antes de tocar no banco)")
    parsed = _parsear_extratos()

    conn = conectar_producao()
    conn.set_session(readonly=not args.executar)
    cur = conn.cursor()

    empresa_id, agencia_id, linhas = _escopo(cur)
    ids = [r[0] for r in linhas]
    cur.execute(
        "SELECT COUNT(*) FROM registros_contabeis "
        "WHERE transacao_id = ANY(%s::uuid[]) AND deleted_at IS NULL",
        (ids,),
    )
    n_registros = cur.fetchone()[0]

    print(f"\n2) Escopo no banco: {len(ids)} transações, {n_registros} registros contábeis")
    print(f"   empresa={empresa_id}  agência={agencia_id}")
    total_novo = sum(len(v) for v in parsed.values())
    print(f"   serão substituídas por {total_novo} transações vindas dos extratos")

    if not args.executar:
        print("\n" + "=" * 72)
        print("DRY-RUN — nada foi alterado. Para aplicar:")
        print("   python saneamento_extrato_periodo.py --executar")
        print("=" * 72)
        cur.close()
        conn.close()
        return 0

    print("\n3) Backup")
    _backup(cur, ids)

    print("\n4) Soft-delete")
    agora = datetime.now(timezone.utc)
    cur.execute(
        "UPDATE registros_contabeis SET deleted_at = %s "
        "WHERE transacao_id = ANY(%s::uuid[]) AND deleted_at IS NULL",
        (agora, ids),
    )
    print(f"   registros contábeis: {cur.rowcount}")
    cur.execute(
        "UPDATE transacoes SET deleted_at = %s WHERE id = ANY(%s::uuid[]) AND deleted_at IS NULL",
        (agora, ids),
    )
    print(f"   transações        : {cur.rowcount}")
    conn.commit()

    print("\n5) Reimportação")
    asyncio.run(_reimportar(empresa_id, agencia_id, parsed))

    print("\n6) Conferência")
    ok = _conferir(cur, parsed)
    cur.close()
    conn.close()

    print("\n" + "=" * 72)
    print("✅ Saneamento concluído e conferido." if ok
          else "⚠ Saneamento aplicado, mas a conferência DIVERGE — revisar antes de classificar.")
    print("=" * 72)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
