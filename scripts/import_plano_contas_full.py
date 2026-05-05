#!/usr/bin/env python
"""
Importa o Plano de Contas COMPLETO do MrContador para cada empresa.

O script anterior (import_mrcont.py) so criou PlanoConta para contas
referenciadas nas regras CSV. Este script completa o plano de contas
importando TODAS as contas do arquivo JSON extraido do MrContador.

Entrada: C:/Users/nathan.carvalho/Downloads/mrcont_plano_contas.json
  (gerado pelo script de extracao via browser)

Execucao:
    cd C:/Users/nathan.carvalho/Documents/contabil-core
    .venv/Scripts/python scripts/import_plano_contas_full.py
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from src.db.models import Empresa, PlanoConta

DATABASE_URL = "postgresql+asyncpg://contabil:contabil@localhost:5435/contabil_dev"
JSON_PATH    = Path("C:/Users/nathan.carvalho/Downloads/mrcont_plano_contas.json")

# ─── helpers ────────────────────────────────────────────────────────────────

def _normalize(name: str) -> str:
    name = name.upper()
    name = re.sub(r"[^A-Z0-9 ]", " ", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name


def _guess_tipo(classificacao: str) -> str:
    try:
        d = str(int(classificacao.split(".")[0]))[0]
        return {"1": "ativo", "2": "passivo", "3": "patrimonio_liquido",
                "4": "receita", "5": "despesa", "6": "custo"}.get(d, "despesa")
    except Exception:
        return "despesa"


# ─── main ────────────────────────────────────────────────────────────────────

async def importar_empresa(
    session: AsyncSession,
    empresa: Empresa,
    mrc_contas: list[dict],
) -> dict:
    """Cria PlanoConta para todas as contas MrContador que ainda nao existem."""

    # Detecta classificacoes duplicadas no MrContador (dados com colisao)
    class_counter: Counter = Counter(c["classificacao"] for c in mrc_contas)
    dup_classificacoes: set[str] = {k for k, v in class_counter.items() if v > 1}

    # Carrega codigos ja existentes no banco para esta empresa
    res = await session.execute(
        select(PlanoConta.codigo).where(
            PlanoConta.empresa_id == empresa.id,
            PlanoConta.deleted_at.is_(None),
        )
    )
    existing_codigos: set[str] = {row[0] for row in res.all()}

    criadas  = 0
    puladas  = 0  # ja existia
    colisoes = 0  # classificacao duplicada no MrContador

    # Rastreia classificacoes ja inseridas nesta execucao (evita duplicata dentro do loop)
    inserted_classificacoes: set[str] = set(existing_codigos)

    for c in mrc_contas:
        classificacao = c["classificacao"]
        descricao     = c["descricao"] or f"Conta {c['conta']}"
        tipo_sa       = c["tipo"]   # S ou A
        tipo          = _guess_tipo(classificacao)
        conta_num     = str(c["conta"])

        # Classificacao duplicada no MrContador: usa codigo numerico como fallback
        if classificacao in dup_classificacoes:
            codigo_usar = conta_num
            colisoes += 1
        else:
            codigo_usar = classificacao

        # Ja existe no banco? Pula
        if codigo_usar in inserted_classificacoes:
            puladas += 1
            continue

        pc = PlanoConta(
            empresa_id = empresa.id,
            codigo     = codigo_usar,
            descricao  = descricao,
            tipo       = tipo,
            tipo_sa    = tipo_sa,
        )
        session.add(pc)
        inserted_classificacoes.add(codigo_usar)
        criadas += 1

    await session.flush()
    return {"criadas": criadas, "puladas": puladas, "colisoes": colisoes}


async def main() -> None:
    print("=" * 65)
    print("  Importacao Plano de Contas Completo - MrContador -> Contabil Core")
    print("=" * 65)

    with open(JSON_PATH, encoding="utf-8") as f:
        mrc_data: dict = json.load(f)

    # Indice normalizado do MrContador
    mrc_by_norm: dict[str, tuple[str, dict]] = {}
    for nome, info in mrc_data.items():
        mrc_by_norm[_normalize(nome)] = (nome, info)

    engine = create_async_engine(DATABASE_URL, echo=False)
    AsyncSessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with AsyncSessionLocal() as session:
        res = await session.execute(
            select(Empresa).where(Empresa.deleted_at.is_(None))
        )
        empresas = res.scalars().all()

    print(f"\nEmpresas no banco : {len(empresas)}")
    print(f"Empresas no JSON  : {len(mrc_data)}\n")

    totais = {"matched": 0, "sem_match": 0, "criadas": 0, "puladas": 0, "colisoes": 0}
    sem_match: list[str] = []

    for empresa in sorted(empresas, key=lambda e: e.razao_social):
        norm = _normalize(empresa.razao_social)
        match = mrc_by_norm.get(norm)

        # Fuzzy fallback
        if not match:
            best_score, best_entry = 0.0, None
            words_db = set(norm.split())
            for mrc_norm, entry in mrc_by_norm.items():
                words_mrc = set(mrc_norm.split())
                inter = words_db & words_mrc
                union = words_db | words_mrc
                score = len(inter) / len(union) if union else 0
                if score > best_score:
                    best_score, best_entry = score, entry
            if best_score >= 0.75:
                match = best_entry

        if not match:
            sem_match.append(empresa.razao_social)
            totais["sem_match"] += 1
            continue

        mrc_nome, mrc_info = match
        mrc_contas = mrc_info.get("contas", [])
        totais["matched"] += 1

        async with AsyncSessionLocal() as session:
            async with session.begin():
                r = await importar_empresa(session, empresa, mrc_contas)

        totais["criadas"]  += r["criadas"]
        totais["puladas"]  += r["puladas"]
        totais["colisoes"] += r["colisoes"]

        print(f"  {empresa.razao_social[:48]:<48} | "
              f"mrc={len(mrc_contas):>4} | "
              f"novas={r['criadas']:>4} puladas={r['puladas']:>4}")

    await engine.dispose()

    print()
    print("=" * 65)
    print("  RESULTADO FINAL")
    print("=" * 65)
    print(f"  Empresas com match      : {totais['matched']}")
    print(f"  Empresas sem match      : {totais['sem_match']}")
    print(f"  Contas criadas (novas)  : {totais['criadas']}")
    print(f"  Contas ja existiam      : {totais['puladas']}")
    print(f"  Colisoes de classif.    : {totais['colisoes']} (codigo numerico mantido)")
    if sem_match:
        print()
        print("  Sem correspondencia:")
        for n in sem_match:
            print(f"    - {n}")
    print("=" * 65)


if __name__ == "__main__":
    asyncio.run(main())
