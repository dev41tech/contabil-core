#!/usr/bin/env python
"""Importa o plano de contas completo do MrContador para cada empresa.

Por segurança, a execução padrão é dry-run. Para persistir as inclusões é
necessário informar as duas flags ``--aplicar --confirmar-escrita``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from src.db.models import Empresa, PlanoConta

DATABASE_URL = "postgresql+asyncpg://contabil:contabil@localhost:5435/contabil_dev"
JSON_PATH = Path("C:/Users/nathan.carvalho/Downloads/mrcont_plano_contas.json")
FUZZY_THRESHOLD = 0.75
FUZZY_MIN_MARGIN = 0.10


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="explicita o modo padrão: apenas mostra o que seria incluído",
    )
    parser.add_argument(
        "--aplicar",
        action="store_true",
        help="habilita inclusões; exige também --confirmar-escrita",
    )
    parser.add_argument(
        "--confirmar-escrita",
        action="store_true",
        help="segunda confirmação explícita para gravar no banco",
    )
    args = parser.parse_args()
    if args.dry_run and (args.aplicar or args.confirmar_escrita):
        parser.error("--dry-run não pode ser combinado com flags de escrita")
    if args.aplicar != args.confirmar_escrita:
        parser.error("para gravar, informe juntas: --aplicar --confirmar-escrita")
    return args


def _normalize(name: str) -> str:
    name = name.upper()
    name = re.sub(r"[^A-Z0-9 ]", " ", name)
    return re.sub(r"\s+", " ", name).strip()


def _normalize_cnpj(value: object) -> str | None:
    digits = re.sub(r"\D", "", str(value or ""))
    return digits if len(digits) == 14 else None


def _jaccard(left: str, right: str) -> float:
    left_words = set(left.split())
    right_words = set(right.split())
    union = left_words | right_words
    return len(left_words & right_words) / len(union) if union else 0.0


def _indexar_mrcontador(data: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            **info,
            "nome": nome,
            "nome_normalizado": _normalize(nome),
            "cnpj_fornecido": bool(str(info.get("cnpj") or "").strip()),
            "cnpj_normalizado": _normalize_cnpj(info.get("cnpj")),
        }
        for nome, info in data.items()
    ]


def _casar_empresa(
    razao_social: str,
    cnpj: object,
    candidatos: list[dict[str, Any]],
) -> tuple[dict[str, Any] | None, str]:
    """Casa por CNPJ ou, sem CNPJ disponível no candidato, por nome inequívoco."""
    cnpj_normalizado = _normalize_cnpj(cnpj)
    cnpj_fornecido = bool(str(cnpj or "").strip())
    elegiveis = candidatos

    if cnpj_fornecido:
        if cnpj_normalizado is None:
            return None, "CNPJ da empresa inválido; fallback fuzzy bloqueado"
        por_cnpj = [c for c in candidatos if c["cnpj_normalizado"] == cnpj_normalizado]
        if len(por_cnpj) == 1:
            return por_cnpj[0], "CNPJ"
        if len(por_cnpj) > 1:
            return None, f"CNPJ duplicado no JSON ({len(por_cnpj)} candidatos)"

        elegiveis = [c for c in candidatos if not c["cnpj_fornecido"]]
        if not elegiveis:
            return None, "CNPJ sem correspondência; fallback fuzzy bloqueado"

    nome_normalizado = _normalize(razao_social)
    por_nome = [c for c in elegiveis if c["nome_normalizado"] == nome_normalizado]
    if len(por_nome) == 1:
        return por_nome[0], "nome exato (sem CNPJ comparável)"
    if len(por_nome) > 1:
        return None, f"nome exato ambíguo ({len(por_nome)} candidatos)"

    pontuados = sorted(
        (
            (_jaccard(nome_normalizado, candidato["nome_normalizado"]), candidato)
            for candidato in elegiveis
        ),
        key=lambda item: (-item[0], item[1]["nome_normalizado"], item[1]["nome"]),
    )
    if not pontuados or pontuados[0][0] < FUZZY_THRESHOLD:
        melhor = pontuados[0][0] if pontuados else 0.0
        return None, f"melhor similaridade insuficiente ({melhor:.2f})"

    melhor_score, melhor = pontuados[0]
    segundo_score = pontuados[1][0] if len(pontuados) > 1 else 0.0
    margem = melhor_score - segundo_score
    if len(pontuados) > 1 and margem < FUZZY_MIN_MARGIN:
        return None, (
            f"fuzzy ambíguo (melhor={melhor_score:.2f}, "
            f"segundo={segundo_score:.2f}, margem={margem:.2f})"
        )
    return melhor, f"fuzzy (score={melhor_score:.2f}, margem={margem:.2f})"


def _guess_tipo(classificacao: str) -> str:
    try:
        first_digit = str(int(classificacao.split(".")[0]))[0]
        return {
            "1": "ativo",
            "2": "passivo",
            "3": "patrimonio_liquido",
            "4": "receita",
            "5": "despesa",
            "6": "custo",
        }.get(first_digit, "despesa")
    except Exception:
        return "despesa"


async def importar_empresa(
    session: AsyncSession,
    empresa: Empresa,
    contas_mrc: list[dict[str, Any]],
    aplicar: bool,
) -> dict[str, int]:
    """Calcula e, somente no modo de aplicação, inclui as contas inexistentes."""
    class_counter = Counter(conta["classificacao"] for conta in contas_mrc)
    duplicadas = {codigo for codigo, quantidade in class_counter.items() if quantidade > 1}

    result = await session.execute(
        select(PlanoConta.codigo).where(
            PlanoConta.empresa_id == empresa.id,
            PlanoConta.deleted_at.is_(None),
        )
    )
    codigos_existentes = {row[0] for row in result.all()}

    criadas = 0
    puladas = 0
    colisoes = 0
    codigos_processados = set(codigos_existentes)

    for conta in contas_mrc:
        classificacao = conta["classificacao"]
        conta_numero = str(conta["conta"])
        if classificacao in duplicadas:
            codigo = conta_numero
            colisoes += 1
        else:
            codigo = classificacao

        if codigo in codigos_processados:
            puladas += 1
            continue

        if aplicar:
            session.add(
                PlanoConta(
                    empresa_id=empresa.id,
                    codigo=codigo,
                    descricao=conta["descricao"] or f"Conta {conta['conta']}",
                    tipo=_guess_tipo(classificacao),
                    tipo_sa=conta["tipo"],
                )
            )
        codigos_processados.add(codigo)
        criadas += 1

    if aplicar:
        await session.flush()
    return {"criadas": criadas, "puladas": puladas, "colisoes": colisoes}


async def main(aplicar: bool = False) -> None:
    modo = "APLICAÇÃO" if aplicar else "DRY-RUN (nenhuma alteração será gravada)"
    print("=" * 70)
    print("  Importação Plano de Contas Completo - MrContador -> Contábil Core")
    print(f"  Modo: {modo}")
    print("=" * 70)

    with JSON_PATH.open(encoding="utf-8") as arquivo:
        mrc_data: dict[str, dict[str, Any]] = json.load(arquivo)
    candidatos = _indexar_mrcontador(mrc_data)

    engine = create_async_engine(DATABASE_URL, echo=False)
    async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with async_session() as session:
        result = await session.execute(select(Empresa).where(Empresa.deleted_at.is_(None)))
        empresas = result.scalars().all()

    print(f"\nEmpresas no banco : {len(empresas)}")
    print(f"Empresas no JSON  : {len(mrc_data)}\n")

    totais = {"matched": 0, "sem_match": 0, "criadas": 0, "puladas": 0, "colisoes": 0}
    sem_match: list[tuple[str, str]] = []

    for empresa in sorted(empresas, key=lambda item: item.razao_social):
        mrc_info, criterio = _casar_empresa(empresa.razao_social, empresa.cnpj, candidatos)
        if mrc_info is None:
            sem_match.append((empresa.razao_social, criterio))
            totais["sem_match"] += 1
            continue

        contas_mrc = mrc_info.get("contas", [])
        totais["matched"] += 1

        async with async_session() as session:
            async with session.begin():
                resultado = await importar_empresa(session, empresa, contas_mrc, aplicar)

        totais["criadas"] += resultado["criadas"]
        totais["puladas"] += resultado["puladas"]
        totais["colisoes"] += resultado["colisoes"]
        prefixo = "OK " if aplicar else "DRY"
        print(
            f"  {prefixo} {empresa.razao_social[:36]:<36} | "
            f"match={mrc_info['nome'][:24]:<24} | {criterio} | "
            f"mrc={len(contas_mrc):>4} novas={resultado['criadas']:>4} "
            f"puladas={resultado['puladas']:>4}"
        )

    await engine.dispose()

    print("\n" + "=" * 70)
    print("  RESULTADO FINAL")
    print("=" * 70)
    print(f"  Empresas com match      : {totais['matched']}")
    print(f"  Empresas sem match      : {totais['sem_match']}")
    print(f"  Contas criadas (novas)  : {totais['criadas']}")
    print(f"  Contas já existentes    : {totais['puladas']}")
    print(f"  Colisões de classificação: {totais['colisoes']} (código numérico mantido)")
    if sem_match:
        print("\n  Sem correspondência:")
        for nome, motivo in sem_match:
            print(f"    - {nome}: {motivo}")
    if not aplicar:
        print("\n  DRY-RUN concluído: nenhuma alteração foi gravada.")
        print("  Para gravar, execute novamente com --aplicar --confirmar-escrita.")
    print("=" * 70)


if __name__ == "__main__":
    argumentos = parse_args()
    asyncio.run(main(aplicar=argumentos.aplicar))
