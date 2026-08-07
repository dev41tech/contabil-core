"""
Conciliação FIFO flexível — aceita adiantamentos e pagamentos excedentes.

Regras:
- Pagamentos podem ocorrer ANTES de compras (adiantamentos)
- Pagamentos excedentes geram "crédito do cliente" (saldo devedor do fornecedor)
- FIFO puro aplicado nas NFs existentes
- Adiantamentos são contabilizados separadamente
"""

from collections import deque
from decimal import Decimal
from uuid import UUID

from sqlalchemy.orm import Session

from src.db.models import CpFornecedor as Fornecedor, CpLancamento as LancamentoFornecedor


CENTAVO = Decimal("0.01")


def _d(x) -> Decimal:
    valor = x if isinstance(x, Decimal) else Decimal(str(x or "0"))
    return valor.quantize(CENTAVO)


def _is_open(compra: LancamentoFornecedor) -> bool:
    return _d(compra.valor_saldo) > 0


def conciliar_fornecedor_inteligente(
    db: Session,
    fornecedor_id: int,
    empresa_id: UUID,
):
    """
    Concilia um fornecedor aplicando TODOS os pagamentos via FIFO flexível.

    COMPRA (crédito): cria/representa uma NF (imutável no valor_credito)
    PAGAMENTO (débito): aplicado em cascata nas NFs mais antigas abertas
    ADIANTAMENTO: pagamento que não encontra NF = crédito do cliente

    Saídas:
    - Atualiza em cada COMPRA: valor_pago_parcial, valor_saldo, status_pagamento
    - Retorna (qtd_pendentes, qtd_parciais)
    """
    compras = db.query(LancamentoFornecedor).filter(
        LancamentoFornecedor.fornecedor_id == fornecedor_id,
        LancamentoFornecedor.empresa_id == empresa_id,
        LancamentoFornecedor.tipo_operacao == "COMPRA"
    ).order_by(LancamentoFornecedor.data_lancamento).all()

    pagamentos = db.query(LancamentoFornecedor).filter(
        LancamentoFornecedor.fornecedor_id == fornecedor_id,
        LancamentoFornecedor.empresa_id == empresa_id,
        LancamentoFornecedor.tipo_operacao == "PAGAMENTO"
    ).order_by(LancamentoFornecedor.data_lancamento).all()

    if not compras:
        print("   ⚠️ Nenhuma COMPRA encontrada; apenas pagamentos (adiantamentos)")
        return 0, 0

    for compra in compras:
        compra.valor_pago_parcial = Decimal("0")
        compra.valor_saldo = _d(compra.valor_credito)
        compra.status_pagamento = "PENDENTE"

    print(f"   📋 {len(compras)} NFs (COMPRA) encontradas")
    print(f"   💳 {len(pagamentos)} pagamentos (PAGAMENTO) encontrados")

    events = []
    for c in compras:
        events.append((c.data_lancamento, 0, c))
    for p in pagamentos:
        events.append((p.data_lancamento, 1, p))

    events.sort(key=lambda t: (t[0], t[1]))

    fila = deque()
    adiantamento_total = Decimal("0")

    for _, tipo, obj in events:
        if tipo == 0:
            if adiantamento_total > 0 and _d(obj.valor_saldo) > 0:
                abate = min(adiantamento_total, _d(obj.valor_saldo))
                obj.valor_pago_parcial = abate
                obj.valor_saldo = _d(obj.valor_credito) - abate
                adiantamento_total -= abate

                if _d(obj.valor_saldo) == 0:
                    obj.valor_saldo = Decimal("0")
                    obj.status_pagamento = "PAGO"
                else:
                    obj.status_pagamento = "PARCIAL"
                    fila.append(obj)

                print(f"   💰 Adiantamento aplicado: R$ {abate:,.2f} na NF {obj.numero_nf or 'S/N'}")
            else:
                if _d(obj.valor_saldo) > 0:
                    fila.append(obj)
            continue

        valor_pagamento = _d(obj.valor_debito)
        if valor_pagamento <= 0:
            continue

        restante = valor_pagamento

        while fila and not _is_open(fila[0]):
            fila.popleft()

        while restante > 0:
            if not fila:
                adiantamento_total += restante
                print(f"   ⚡ Adiantamento: R$ {restante:,.2f} (data={obj.data_lancamento.strftime('%Y-%m-%d')})")
                restante = Decimal("0")
                break

            nf_atual = fila[0]
            falta = _d(nf_atual.valor_saldo)
            abate = falta if falta < restante else restante

            nf_atual.valor_pago_parcial = _d(nf_atual.valor_pago_parcial) + abate
            nf_atual.valor_saldo = _d(nf_atual.valor_credito) - _d(nf_atual.valor_pago_parcial)

            if _d(nf_atual.valor_saldo) == 0:
                nf_atual.valor_saldo = Decimal("0")
                nf_atual.status_pagamento = "PAGO"
                fila.popleft()
            else:
                nf_atual.status_pagamento = "PARCIAL"

            restante -= abate

    pendentes = [c for c in compras if _d(c.valor_saldo) > 0]
    parciais  = [c for c in compras if c.status_pagamento == "PARCIAL"]

    if adiantamento_total > 0:
        print(f"   💰 Adiantamento total acumulado: R$ {adiantamento_total:,.2f}")
        print(f"   ℹ️  Fornecedor tem CRÉDITO (empresa pagou a mais)")

    print(f"   ✅ {len(pendentes)} NFs com saldo pendente | {len(parciais)} NFs parciais")

    return len(pendentes), len(parciais)


def conciliar_todos_fornecedores_inteligente(
    db: Session,
    arquivo_id: int,
    empresa_id: UUID,
):
    """
    Concilia todos os fornecedores de um arquivo usando FIFO flexível.
    """
    fornecedores = db.query(Fornecedor).filter(
        Fornecedor.arquivo_origem_id == arquivo_id,
        Fornecedor.empresa_id == empresa_id,
    ).all()

    print(f"\n🔄 Iniciando conciliação FIFO flexível para {len(fornecedores)} fornecedores...")

    for forn in fornecedores:
        if _d(forn.total_credito) > 0 or _d(forn.total_debito) > 0:
            print(f"\n📌 {forn.codigo_conta} - {forn.nome_fornecedor[:40]}")
            pendentes, parciais = conciliar_fornecedor_inteligente(
                db,
                forn.id,
                empresa_id,
            )
            forn.qtd_nfs_pendentes = pendentes
            forn.qtd_nfs_parciais = parciais

    db.flush()
    print("\n✅ Conciliação FIFO flexível concluída!")
