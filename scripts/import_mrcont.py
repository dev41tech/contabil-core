#!/usr/bin/env python
"""
Importa empresas + regras do MrContador (output/*/final.csv) para o Contabil Core.

Execução:
    cd C:/Users/nathan.carvalho/Documents/contabil-core
    .venv/Scripts/python scripts/import_mrcont.py

O que faz:
  1. Lê cada pasta em MrContador/output/ como uma Empresa
  2. Cria AgenciaBancaria para cada agência única encontrada nos CSVs
  3. Cria PlanoConta para cada código de conta encontrado
  4. Cria Regra para cada linha do CSV
  5. Idempotente: re-execuções ignoram o que já existe (sem duplicatas)
"""

from __future__ import annotations

import asyncio
import csv
import os
import sys
from pathlib import Path

# Garante que src/ está no path
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from src.db.models import AgenciaBancaria, Empresa, PlanoConta, Regra, Tenant

# ─────────────────────────────────────────────── configurações

DATABASE_URL = "postgresql+asyncpg://contabil:contabil@localhost:5435/contabil_dev"
OUTPUT_DIR   = Path("C:/Users/nathan.carvalho/Documents/MrContador/output")
REGIME_PADRAO = "lucro_presumido"

# ─────────────────────────────────────────────── helpers

def _razao_social(folder: str) -> str:
    """'AJAF_LOGISTICA_LTDA' → 'AJAF LOGISTICA LTDA'"""
    return folder.replace("_", " ").strip()


def _cnpj_placeholder(idx: int) -> str:
    """Gera CNPJ placeholder único. O usuário deve atualizar depois."""
    n = idx + 1
    return f"{n:02d}.{(n * 3) % 1000:03d}.{(n * 7) % 1000:03d}/0001-{n % 100:02d}"


def _parse_agencia(raw: str) -> tuple[str, str, str, str | None] | None:
    """
    Identifica banco_sigla, agencia, numero, digito a partir de strings como:
      'Itau 7285 12287 0'          -> ('Itau', '7285', '12287', '0')
      'BBC DIGITAL 0001 1858612 5' -> ('BBC DIGITAL', '0001', '1858612', '5')
      'Banco C6 1 364409371'       -> ('Banco C6', '1', '364409371', None)
      'BB 2456 72426 2'            -> ('BB', '2456', '72426', '2')

    Heurística: banco_sigla = tokens até o primeiro token puramente numérico.
    Os tokens numéricos restantes são: agencia, numero[, digito].
    """
    parts = raw.strip().split()
    if not parts:
        return None

    # Separa tokens de texto (banco) dos numéricos (agencia/numero/digito)
    split_idx = next(
        (i for i, p in enumerate(parts) if p.isdigit() or (p.startswith("0") and p[1:].isdigit())),
        None,
    )
    if split_idx is None or split_idx == 0:
        # Nenhum token numérico encontrado — tenta fallback com últimos 3
        if len(parts) >= 4:
            split_idx = len(parts) - 3
        else:
            return None

    banco_sigla  = " ".join(parts[:split_idx])
    numericos    = parts[split_idx:]

    if len(numericos) < 2:
        return None  # Precisa de pelo menos agencia + numero

    agencia = numericos[0]
    numero  = numericos[1]
    digito  = numericos[2] if len(numericos) >= 3 else None

    # Segurança: trunca digito ao tamanho máximo da coluna (varchar 5)
    if digito and len(digito) > 5:
        digito = digito[:5]

    if not banco_sigla:
        banco_sigla = "Banco"

    return banco_sigla, agencia, numero, digito


def _guess_tipo_conta(codigo: str, dc_predominante: str) -> str:
    """Heurística pelo primeiro dígito do código."""
    try:
        d = str(int(codigo.strip()))[0]
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
        return "receita" if dc_predominante == "C" else "despesa"


# ─────────────────────────────────────────────── importação principal

async def importar(session: AsyncSession, tenant: Tenant, idx: int, folder: str) -> dict:
    csv_path = OUTPUT_DIR / folder / "final.csv"
    if not csv_path.exists():
        return {"ok": False, "motivo": "sem final.csv"}

    with open(csv_path, encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))

    if not rows:
        return {"ok": False, "motivo": "CSV vazio"}

    razao = _razao_social(folder)
    cnpj  = _cnpj_placeholder(idx)

    # ── 1. Empresa ────────────────────────────────────────────────────────────
    res = await session.execute(
        select(Empresa).where(
            Empresa.tenant_id == tenant.id,
            Empresa.razao_social == razao,
            Empresa.deleted_at.is_(None),
        )
    )
    empresa = res.scalar_one_or_none()
    empresa_criada = False
    if not empresa:
        empresa = Empresa(
            tenant_id=tenant.id,
            razao_social=razao,
            cnpj=cnpj,
            regime_tributario=REGIME_PADRAO,
            ativa=True,
        )
        session.add(empresa)
        await session.flush()
        empresa_criada = True

    # ── 2. Agências ───────────────────────────────────────────────────────────
    agencia_cache: dict[str, AgenciaBancaria | None] = {}

    for row in rows:
        raw = row["agencia_bancaria"].strip()
        if raw in agencia_cache or not raw:
            continue

        parsed = _parse_agencia(raw)
        if parsed is None:
            agencia_cache[raw] = None
            continue

        banco_sigla, agencia_num, numero, digito = parsed

        res = await session.execute(
            select(AgenciaBancaria).where(
                AgenciaBancaria.empresa_id == empresa.id,
                AgenciaBancaria.banco_sigla == banco_sigla,
                AgenciaBancaria.agencia == agencia_num,
                AgenciaBancaria.numero == numero,
                AgenciaBancaria.deleted_at.is_(None),
            )
        )
        ag = res.scalar_one_or_none()
        if not ag:
            ag = AgenciaBancaria(
                empresa_id=empresa.id,
                banco_sigla=banco_sigla,
                agencia=agencia_num,
                numero=numero,
                digito=digito,
                ativa=True,
            )
            session.add(ag)
            await session.flush()

        agencia_cache[raw] = ag

    # ── 3. Plano de contas ────────────────────────────────────────────────────
    conta_cache: dict[str, PlanoConta | None] = {}

    # Calcula DC predominante por código para estimar tipo
    dc_por_conta: dict[str, list[str]] = {}
    for row in rows:
        c = row["conta"].strip()
        if c:
            dc_por_conta.setdefault(c, []).append(row["dc"].strip().upper())

    for codigo in dc_por_conta:
        if codigo in conta_cache:
            continue

        res = await session.execute(
            select(PlanoConta).where(
                PlanoConta.empresa_id == empresa.id,
                PlanoConta.codigo == codigo,
                PlanoConta.deleted_at.is_(None),
            )
        )
        pc = res.scalar_one_or_none()
        if not pc:
            dcs = dc_por_conta[codigo]
            dc_pred = "C" if dcs.count("C") > dcs.count("D") else "D"
            tipo = _guess_tipo_conta(codigo, dc_pred)
            pc = PlanoConta(
                empresa_id=empresa.id,
                codigo=codigo,
                descricao=f"Conta {codigo}",
                tipo=tipo,
                tipo_sa="A",
            )
            session.add(pc)
            await session.flush()

        conta_cache[codigo] = pc

    # ── 4. Regras ─────────────────────────────────────────────────────────────
    regras_criadas = 0
    regras_dup     = 0
    regras_erro    = 0

    # Controle in-memory de duplicatas (empresa_id + agencia_id + historico)
    seen: set[tuple] = set()

    # Carrega chaves já existentes no DB para esta empresa (evita round-trips por linha)
    res_exist = await session.execute(
        select(Regra.agencia_id, Regra.historico).where(
            Regra.empresa_id == empresa.id,
            Regra.deleted_at.is_(None),
        )
    )
    for ag_id, hist in res_exist.all():
        seen.add((ag_id, hist))

    for row in rows:
        raw_ag   = row["agencia_bancaria"].strip()
        codigo   = row["conta"].strip()
        descricao_csv = row["descricao"].strip()
        historico_csv = row.get("historico", "").strip()
        dc       = row["dc"].strip().upper()
        manter   = row.get("historico_extrato", "false").strip().lower() == "true"

        ag_obj = agencia_cache.get(raw_ag)
        pc_obj = conta_cache.get(codigo)

        if ag_obj is None or pc_obj is None:
            regras_erro += 1
            continue

        if not dc or dc not in ("D", "C"):
            dc = "D"

        # historico = padrão de busca = descricao_csv
        historico_padrao = descricao_csv
        if not historico_padrao:
            regras_erro += 1
            continue

        key = (ag_obj.id, historico_padrao)
        if key in seen:
            regras_dup += 1
            continue
        seen.add(key)

        regra = Regra(
            empresa_id=empresa.id,
            conta_id=pc_obj.id,
            agencia_id=ag_obj.id,
            descricao=historico_csv or descricao_csv,
            historico=historico_padrao,
            dc=dc,
            tipo="automatica",
            manter_historico=manter,
            ativa=True,
        )
        session.add(regra)
        regras_criadas += 1

    return {
        "ok": True,
        "empresa_criada": empresa_criada,
        "agencias": len([v for v in agencia_cache.values() if v]),
        "contas": len(conta_cache),
        "regras_criadas": regras_criadas,
        "regras_dup": regras_dup,
        "regras_erro": regras_erro,
    }


# ─────────────────────────────────────────────── entrypoint

async def main() -> None:
    print("=" * 60)
    print("  Importacao MrContador -> Contabil Core")
    print("=" * 60)

    engine = create_async_engine(DATABASE_URL, echo=False)
    AsyncSessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    # Busca tenant existente
    async with AsyncSessionLocal() as session:
        res = await session.execute(
            select(Tenant).where(Tenant.deleted_at.is_(None)).limit(1)
        )
        tenant = res.scalar_one_or_none()

    if not tenant:
        print("\nERRO: Nenhum tenant encontrado no banco.")
        print("Execute o setup inicial da aplicação antes de importar.")
        await engine.dispose()
        return

    print(f"\nTenant: {tenant.nome}  ({tenant.id})\n")

    # Lista pastas com CSV
    dirs = sorted([
        d for d in os.listdir(OUTPUT_DIR)
        if (OUTPUT_DIR / d).is_dir() and d != "errors" and (OUTPUT_DIR / d / "final.csv").exists()
    ])
    print(f"Empresas encontradas: {len(dirs)}\n")

    totais = {
        "empresas_criadas": 0,
        "agencias": 0,
        "contas": 0,
        "regras_criadas": 0,
        "regras_dup": 0,
        "regras_erro": 0,
        "falhas": 0,
    }

    for idx, folder in enumerate(dirs):
        print(f"[{idx+1:03d}/{len(dirs)}] {_razao_social(folder)}", end=" ... ", flush=True)
        try:
            async with AsyncSessionLocal() as session:
                async with session.begin():
                    r = await importar(session, tenant, idx, folder)

            if not r["ok"]:
                print(f"PULADO ({r['motivo']})")
                totais["falhas"] += 1
                continue

            totais["empresas_criadas"] += r["empresa_criada"]
            totais["agencias"]         += r["agencias"]
            totais["contas"]           += r["contas"]
            totais["regras_criadas"]   += r["regras_criadas"]
            totais["regras_dup"]       += r["regras_dup"]
            totais["regras_erro"]      += r["regras_erro"]

            status = "NOVA" if r["empresa_criada"] else "existente"
            print(f"OK [{status}] | ag={r['agencias']} contas={r['contas']} "
                  f"regras={r['regras_criadas']} dup={r['regras_dup']}")

        except Exception as exc:
            print(f"ERRO: {exc}")
            totais["falhas"] += 1

    await engine.dispose()

    print()
    print("=" * 60)
    print("  RESULTADO FINAL")
    print("=" * 60)
    print(f"  Empresas criadas   : {totais['empresas_criadas']}")
    print(f"  Agências criadas   : {totais['agencias']}")
    print(f"  Contas criadas     : {totais['contas']}")
    print(f"  Regras criadas     : {totais['regras_criadas']}")
    print(f"  Regras duplicadas  : {totais['regras_dup']}")
    print(f"  Regras com erro    : {totais['regras_erro']}")
    print(f"  Pastas com falha   : {totais['falhas']}")
    print("=" * 60)
    print()
    print("ATENÇÃO: Os CNPJs foram gerados como placeholders.")
    print("Acesse Configurações > Empresas para atualizar os CNPJs reais.")
    print()
    print("Os códigos de conta foram criados com tipo inferido pelo código.")
    print("Revise em Configurações > Plano de Contas se necessário.")


if __name__ == "__main__":
    asyncio.run(main())
