#!/usr/bin/env python
"""
Atualiza PlanoConta no banco com os dados reais do MrContador.

Arquivo de entrada: C:/Users/nathan.carvalho/Downloads/mrcont_plano_contas.json
  Estrutura: {
    "NOME EMPRESA": {
      "parceiroId": 33,
      "cnpj": "...",
      "contas": [
        {"conta": 552, "classificacao": "1.1.1.02.0004", "tipo": "A", "descricao": "BANCO - ITAU", "grau": 5},
        ...
      ]
    }
  }

O campo `conta` (conConta no MrContador) e o mesmo numero que aparece
na coluna `conta` dos CSVs das regras, e que foi salvo como `codigo`
no PlanoConta do nosso banco.

Execucao:
    cd C:/Users/nathan.carvalho/Documents/contabil-core
    .venv/Scripts/python scripts/update_plano_contas.py
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from src.db.models import Empresa, PlanoConta

DATABASE_URL = "postgresql+asyncpg://contabil:contabil@localhost:5435/contabil_dev"
JSON_PATH = Path("C:/Users/nathan.carvalho/Downloads/mrcont_plano_contas.json")

# ─── helpers ────────────────────────────────────────────────────────────────

def _normalize(name: str) -> str:
    """Remove pontuacao, espacos extras e caixa para comparacao."""
    name = name.upper()
    name = re.sub(r"[^A-Z0-9 ]", " ", name)  # remove &, ., - etc
    name = re.sub(r"\s+", " ", name).strip()
    return name


def _guess_tipo(classificacao: str, tipo_sa: str) -> str:
    """Heuristica pelo primeiro digito da classificacao."""
    try:
        d = str(int(classificacao.split(".")[0]))[0]
        mapa = {
            "1": "ativo",
            "2": "passivo",
            "3": "patrimonio_liquido",
            "4": "receita",
            "5": "despesa",
            "6": "custo",
        }
        return mapa.get(d, "despesa")
    except Exception:
        return "despesa"


# ─── main ────────────────────────────────────────────────────────────────────

async def main() -> None:
    print("=" * 60)
    print("  Atualizacao Plano de Contas - MrContador -> Contabil Core")
    print("=" * 60)

    with open(JSON_PATH, encoding="utf-8") as f:
        mrc_data: dict = json.load(f)

    # Normaliza nomes do MrContador para lookup rapido
    mrc_by_norm: dict[str, dict] = {}
    for nome, info in mrc_data.items():
        mrc_by_norm[_normalize(nome)] = {"nome": nome, **info}

    engine = create_async_engine(DATABASE_URL, echo=False)
    AsyncSessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with AsyncSessionLocal() as session:
        res = await session.execute(
            select(Empresa).where(Empresa.deleted_at.is_(None))
        )
        empresas = res.scalars().all()

    print(f"\nEmpresas no banco: {len(empresas)}")

    totais = {
        "empresas_matched": 0,
        "empresas_sem_match": 0,
        "contas_atualizadas": 0,
        "contas_nao_encontradas": 0,
    }
    sem_match: list[str] = []

    for empresa in empresas:
        norm = _normalize(empresa.razao_social)
        mrc_info = mrc_by_norm.get(norm)

        if not mrc_info:
            # Tentativa de match parcial (remove palavras curtas)
            for mrc_norm, info in mrc_by_norm.items():
                words_db  = set(norm.split())
                words_mrc = set(mrc_norm.split())
                # Jaccard similarity
                inter = words_db & words_mrc
                union = words_db | words_mrc
                if len(union) > 0 and len(inter) / len(union) >= 0.75:
                    mrc_info = info
                    break

        if not mrc_info:
            sem_match.append(empresa.razao_social)
            totais["empresas_sem_match"] += 1
            continue

        totais["empresas_matched"] += 1

        # Constroi lookup: conConta -> dados  (lookup por codigo numerico)
        # E tambem: classificacao -> dados    (para registros ja atualizados)
        conta_lookup: dict[str, dict] = {}
        class_lookup: dict[str, dict] = {}
        for c in mrc_info.get("contas", []):
            conta_lookup[str(c["conta"])] = c
            # So adiciona ao class_lookup se nao houver colisao (pega primeiro)
            if c["classificacao"] not in class_lookup:
                class_lookup[c["classificacao"]] = c

        # Detecta classificacoes duplicadas para esta empresa (dados ruins no MrContador)
        # Para duplicatas, o codigo ficara como conConta numerico (evita colisao)
        from collections import Counter
        class_counter: Counter = Counter()
        for c in mrc_info.get("contas", []):
            class_counter[c["classificacao"]] += 1
        dup_classificacoes: set[str] = {k for k, v in class_counter.items() if v > 1}

        # Busca PlanoContas desta empresa
        async with AsyncSessionLocal() as session:
            res = await session.execute(
                select(PlanoConta).where(
                    PlanoConta.empresa_id == empresa.id,
                    PlanoConta.deleted_at.is_(None),
                )
            )
            planos = res.scalars().all()

            atualizados = 0
            nao_encontrados = 0

            for pc in planos:
                # Tenta pelo codigo numerico (conConta) primeiro
                mrc_conta = conta_lookup.get(str(pc.codigo))
                # Se nao achou, tenta pela classificacao (registro ja foi atualizado)
                if mrc_conta is None:
                    mrc_conta = class_lookup.get(str(pc.codigo))
                if mrc_conta is None:
                    nao_encontrados += 1
                    continue

                classificacao = mrc_conta["classificacao"]
                descricao     = mrc_conta["descricao"]
                tipo_sa       = mrc_conta["tipo"]  # S ou A
                tipo          = _guess_tipo(classificacao, tipo_sa)

                # Se a classificacao e duplicada, mantem o codigo numerico original
                # para evitar colisao de unique constraint, mas atualiza descricao
                if classificacao in dup_classificacoes:
                    pc.descricao = descricao
                    pc.tipo      = tipo
                    pc.tipo_sa   = tipo_sa
                else:
                    pc.codigo    = classificacao
                    pc.descricao = descricao
                    pc.tipo      = tipo
                    pc.tipo_sa   = tipo_sa
                atualizados += 1

            await session.commit()

        totais["contas_atualizadas"]     += atualizados
        totais["contas_nao_encontradas"] += nao_encontrados

        match_nome = mrc_info["nome"]
        print(f"  OK  {empresa.razao_social[:45]:<45} | "
              f"match: {match_nome[:35]:<35} | "
              f"atualizadas={atualizados} nao_enc={nao_encontrados}")

    await engine.dispose()

    print()
    print("=" * 60)
    print("  RESULTADO FINAL")
    print("=" * 60)
    print(f"  Empresas com match    : {totais['empresas_matched']}")
    print(f"  Empresas sem match    : {totais['empresas_sem_match']}")
    print(f"  Contas atualizadas    : {totais['contas_atualizadas']}")
    print(f"  Contas nao encontradas: {totais['contas_nao_encontradas']}")

    if sem_match:
        print()
        print("  Empresas sem correspondencia no MrContador:")
        for nome in sem_match:
            print(f"    - {nome}")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
