"""
Propõe o CNPJ real das empresas cadastradas com placeholder, cruzando com os
razões já processados pelo ConcilPro (`cp_arquivo.cnpj_empresa`).

Contexto: `scripts/import_mrcont.py` nunca leu CNPJ de fonte alguma — razão social
vem do nome da pasta e o CNPJ é sintetizado do índice (`_cnpj_placeholder`). Das 77
empresas, só 5 têm CNPJ real. Para as demais a exportação de notas volta vazia,
porque o filtro casa o CNPJ da empresa com o emitente/destinatário da nota.

Este script NÃO adivinha. Só propõe quando o nome normalizado casa com exatamente
uma empresa e exatamente um CNPJ do ConcilPro — existem duas empresas "AXEL" no
cadastro, e casamento cego por nome erraria. Tudo que for ambíguo é listado para
conferência manual, nunca aplicado.

Execução:
    .venv/Scripts/python conciliar_cnpj.py             # dry-run (padrão)
    .venv/Scripts/python conciliar_cnpj.py --aplicar   # grava no banco
"""
from __future__ import annotations

import argparse
import sys
import unicodedata
from collections import defaultdict

from prod_db import conectar_producao, resumo_destino
from src.core.cnpj import formatar, valido


def normalizar_nome(nome: str) -> str:
    """Uppercase sem acento e com espaços colapsados — só para comparar, nunca para gravar."""
    sem_acento = "".join(
        c for c in unicodedata.normalize("NFD", nome or "")
        if unicodedata.category(c) != "Mn"
    )
    return " ".join(sem_acento.upper().split())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--aplicar",
        action="store_true",
        help="grava os CNPJs propostos. Sem esta flag o script só mostra o que faria.",
    )
    args = parser.parse_args()

    print(f"Conectando em {resumo_destino()}")
    print(f"Modo: {'APLICAR (grava no banco)' if args.aplicar else 'dry-run (não grava nada)'}\n")

    conn = conectar_producao()
    cur = conn.cursor()

    cur.execute("SELECT id, razao_social, cnpj FROM empresas WHERE deleted_at IS NULL")
    empresas = cur.fetchall()

    invalidas = [e for e in empresas if not valido(e[2])]
    print(f"Empresas cadastradas : {len(empresas)}")
    print(f"Com CNPJ inválido    : {len(invalidas)}\n")

    if not invalidas:
        print("Nada a conciliar.")
        cur.close()
        conn.close()
        return 0

    # ── Candidatos vindos do ConcilPro ────────────────────────────────────────
    cur.execute(
        "SELECT DISTINCT empresa, cnpj_empresa FROM cp_arquivo "
        "WHERE cnpj_empresa IS NOT NULL AND empresa IS NOT NULL"
    )
    candidatos: dict[str, set[str]] = defaultdict(set)
    for nome, cnpj in cur.fetchall():
        if valido(cnpj):
            candidatos[normalizar_nome(nome)].add(formatar(cnpj))

    print(f"Nomes com CNPJ real no ConcilPro: {len(candidatos)}\n")

    # Quantas empresas do cadastro respondem por cada nome normalizado — é isso
    # que impede o script de escolher entre as duas "AXEL".
    por_nome: dict[str, list] = defaultdict(list)
    for e in empresas:
        por_nome[normalizar_nome(e[1])].append(e)

    cnpjs_em_uso = {formatar(e[2]) for e in empresas if valido(e[2])}

    propostas: list[tuple] = []
    ambiguas: list[tuple[str, str]] = []

    for empresa_id, razao_social, cnpj_atual in invalidas:
        chave = normalizar_nome(razao_social)
        opcoes = candidatos.get(chave)

        if not opcoes:
            continue
        if len(opcoes) > 1:
            ambiguas.append((razao_social, f"{len(opcoes)} CNPJs diferentes no ConcilPro: {sorted(opcoes)}"))
            continue
        if len(por_nome[chave]) > 1:
            ambiguas.append((razao_social, f"{len(por_nome[chave])} empresas com este mesmo nome no cadastro"))
            continue

        novo = opcoes.pop()
        if novo in cnpjs_em_uso:
            ambiguas.append((razao_social, f"{novo} já pertence a outra empresa do cadastro"))
            continue

        propostas.append((empresa_id, razao_social, cnpj_atual, novo))

    # ── Relatório ─────────────────────────────────────────────────────────────
    print("── Propostas ────────────────────────────────────────────────")
    if propostas:
        for _, razao, atual, novo in propostas:
            print(f"  {razao}")
            print(f"    {atual}  ->  {novo}")
    else:
        print("  (nenhuma)")

    print("\n── Precisam de conferência manual ───────────────────────────")
    if ambiguas:
        for razao, motivo in ambiguas:
            print(f"  {razao}: {motivo}")
    else:
        print("  (nenhuma)")

    sem_fonte = len(invalidas) - len(propostas) - len(ambiguas)
    print(
        f"\n── Resumo ───────────────────────────────────────────────────\n"
        f"  Propostas automáticas : {len(propostas)}\n"
        f"  Ambíguas              : {len(ambiguas)}\n"
        f"  Sem fonte de CNPJ     : {sem_fonte}  (precisam vir de contrato, "
        f"certificado A1 ou do sistema do escritório)"
    )

    if not args.aplicar:
        print("\nDry-run — nada foi gravado. Use --aplicar para efetivar as propostas.")
        cur.close()
        conn.close()
        return 0

    if not propostas:
        print("\nNada a aplicar.")
        cur.close()
        conn.close()
        return 0

    for empresa_id, _, _, novo in propostas:
        cur.execute("UPDATE empresas SET cnpj = %s WHERE id = %s", (novo, empresa_id))
    conn.commit()
    print(f"\n✅ {len(propostas)} empresa(s) atualizada(s).")

    cur.close()
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
