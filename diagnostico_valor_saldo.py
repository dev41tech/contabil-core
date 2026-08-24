"""Diagnóstico: transações gravadas com o SALDO no lugar do VALOR.

Varre TODAS as empresas procurando o estrago do bug corrigido em 2026-08-21
(`_TX_LINE` não aceitava saldo negativo). Enquanto o bug esteve ativo, qualquer
extrato em PDF de conta no vermelho falhava na camada determinística, caía na
camada de IA, e a IA achatava a linha capturando a coluna de saldo:

    historico: "18/02/2026 TARIFA COM R LIQUIDACAO COB000001 -1,19 -54.881,83"
    valor:     54881.83   dc: C      (era uma tarifa de R$ 1,19 a débito)

A régua é a MESMA de `src/domain/extrato/validacao.py`, que hoje recusa esses
lançamentos na importação — o diagnóstico não pode divergir da barreira, senão
aponta um conjunto e a importação recusa outro.

O script é SOMENTE LEITURA: abre a sessão em read-only e não executa UPDATE,
DELETE nem INSERT. O reparo é decisão separada, e depende do lançamento já ter
virado partida contábil ou não.

Uso:
    python diagnostico_valor_saldo.py                     # todas as empresas
    python diagnostico_valor_saldo.py --empresa "TRECHO"  # filtra por razão social
    python diagnostico_valor_saldo.py --csv               # exporta o detalhamento
"""
from __future__ import annotations

import argparse
import csv
import sys
import tempfile
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from prod_db import conectar_producao, resumo_destino
from src.domain.extrato.validacao import (
    _MOEDA,  # mesma regex da barreira: o diagnóstico não pode usar outra régua
    motivo_valor_nao_confiavel,
    valores_na_linha,
)

# Pré-filtro no banco: só linhas que ainda carregam a linha crua do extrato
# (dois ou mais valores monetários). Extrato bem parseado nunca entra aqui.
#
# O padrão é de propósito mais largo que a régua real — "dois grupos de centavos"
# em vez do formato monetário completo. Pré-filtro que erra para MAIS só custa
# linhas a examinar; pré-filtro que erra para menos esconde transação afetada, e
# num diagnóstico de dado contábil esse é o erro que não se pode cometer.
# O veredito é sempre do Python, com `motivo_valor_nao_confiavel`.
_SQL = r"""
SELECT
    e.razao_social,
    a.banco_sigla,
    a.agencia,
    a.numero,
    t.id,
    t.data,
    t.historico,
    t.valor,
    t.dc,
    t.status,
    (SELECT COUNT(*) FROM registros_contabeis r
      WHERE r.transacao_id = t.id AND r.deleted_at IS NULL) AS registros
FROM transacoes t
JOIN empresas e ON e.id = t.empresa_id
LEFT JOIN agencias_bancarias a ON a.id = t.agencia_id
WHERE t.deleted_at IS NULL
  AND t.historico ~ ',[0-9]{2}.*,[0-9]{2}'
ORDER BY e.razao_social, t.data, t.historico
"""


def _preparar_saida() -> None:
    """Console do Windows abre em cp1252 e quebra nos caracteres de moldura.

    `errors="replace"` garante que o diagnóstico nunca morra por causa da
    formatação — perder um traço é aceitável, perder o relatório não.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass


def _reais(v: Decimal) -> str:
    inteiro, _, centavos = f"{abs(v):.2f}".partition(".")
    return f"{'-' if v < 0 else ''}{int(inteiro):,}".replace(",", ".") + f",{centavos}"


def _dc_esperado(historico: str) -> str | None:
    """D/C que a linha do extrato indica — o primeiro número traz o sinal real.

    `valores_na_linha` devolve os valores em módulo, então o sinal vem daqui.
    """
    achados = _MOEDA.findall(historico or "")
    if not achados:
        return None
    return "D" if achados[0].startswith("-") else "C"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--empresa", help="filtra por trecho do nome da empresa")
    ap.add_argument("--csv", action="store_true", help="exporta o detalhamento")
    _preparar_saida()  # antes do parse: --help também passa por aqui
    args = ap.parse_args()

    print(f"Conectando em {resumo_destino()}")
    conn = conectar_producao()
    conn.set_session(readonly=True)  # nenhuma escrita é possível nesta sessão
    cur = conn.cursor()
    cur.execute(_SQL)
    linhas = cur.fetchall()
    cur.close()
    conn.close()

    print(f"{len(linhas)} transações com linha crua no histórico (pré-filtro)\n")

    afetadas: list[dict] = []
    for (
        empresa, banco, ag, num, tid, data, historico, valor, dc, status, registros
    ) in linhas:
        if args.empresa and args.empresa.lower() not in (empresa or "").lower():
            continue

        motivo = motivo_valor_nao_confiavel(historico, valor)
        if motivo is None:
            continue  # valor bate com o primeiro número: lançamento está correto

        numeros = valores_na_linha(historico)
        provavel = numeros[0] if numeros else None
        esperado = _dc_esperado(historico)

        afetadas.append(
            {
                "empresa": empresa,
                "conta": f"{banco or '?'} {ag or ''}/{num or ''}".strip(),
                "transacao_id": str(tid),
                "data": data.date().isoformat() if data else "",
                "historico": historico,
                "valor_gravado": valor,
                "valor_provavel": provavel,
                "dc_gravado": dc,
                "dc_esperado": esperado,
                "status": status,
                "registros_contabeis": registros,
                "motivo": motivo,
            }
        )

    if not afetadas:
        print("✅ Nenhuma transação com valor não confiável encontrada.")
        return 0

    # ── Relatório por empresa ────────────────────────────────────────────────
    por_empresa: dict[str, list[dict]] = {}
    for a in afetadas:
        por_empresa.setdefault(a["empresa"], []).append(a)

    for empresa, itens in sorted(por_empresa.items()):
        contabilizadas = sum(1 for i in itens if i["registros_contabeis"] > 0)
        print(f"── {empresa}  ({len(itens)} afetadas, {contabilizadas} já contabilizadas)")
        for i in itens[:10]:
            marca = "⚠ CONTABILIZADA" if i["registros_contabeis"] > 0 else "reparável"
            prov = _reais(i["valor_provavel"]) if i["valor_provavel"] is not None else "?"
            print(
                f"   {i['data']}  gravado {_reais(i['valor_gravado']):>14}"
                f"  provável {prov:>14}"
                f"  {i['dc_gravado']}→{i['dc_esperado']}  [{marca}]"
            )
            print(f"      {i['historico'][:96]}")
        if len(itens) > 10:
            print(f"   ... e mais {len(itens) - 10}")
        print()

    total = len(afetadas)
    contabilizadas = sum(1 for a in afetadas if a["registros_contabeis"] > 0)
    dc_errado = sum(1 for a in afetadas if a["dc_gravado"] != a["dc_esperado"])

    print("═" * 72)
    print(f"TOTAL afetadas .................. {total}")
    print(f"  já viraram partida contábil ... {contabilizadas}  → exigem estorno auditado")
    print(f"  ainda não contabilizadas ...... {total - contabilizadas}  → reparo direto é seguro")
    print(f"  com D/C invertido ............. {dc_errado}")
    print(f"  empresas atingidas ............ {len(por_empresa)}")
    print("═" * 72)

    if args.csv:
        # Fora do repositório: o arquivo carrega dado financeiro de cliente.
        destino = (
            Path(tempfile.gettempdir())
            / f"diagnostico_valor_saldo_{datetime.now():%Y%m%d_%H%M%S}.csv"
        )
        with destino.open("w", newline="", encoding="utf-8-sig") as fh:
            w = csv.DictWriter(fh, fieldnames=list(afetadas[0].keys()), delimiter=";")
            w.writeheader()
            w.writerows(afetadas)
        print(f"\nDetalhamento: {destino}")
        print("⚠ Contém dado financeiro de cliente — não mover para dentro do repositório.")

    return 1  # exit != 0 sinaliza que há dado a tratar


if __name__ == "__main__":
    sys.exit(main())
