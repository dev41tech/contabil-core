"""Testes de integração — export de lançamentos do ConciliaPro no layout de
importação contábil (débito/crédito pareados, mesmo layout de
`_COLUNAS_LANCAMENTOS_IMPORTACAO` em src/domain/exportacao/service.py)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from httpx import AsyncClient
from openpyxl import load_workbook
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import CpArquivo, CpFornecedor, CpLancamento, Empresa


async def _login(client: AsyncClient, tenant, usuario) -> None:
    r = await client.post(
        "/api/v1/auth/login",
        json={"tenant_id": str(tenant.id), "email": usuario.email, "senha": "senha_segura_123"},
    )
    assert r.status_code == 200


async def _setup_arquivo_com_lancamentos(db: AsyncSession, empresa) -> CpArquivo:
    arquivo = CpArquivo(
        empresa_id=empresa.id,
        nome_arquivo="razao.xlsx",
        hash_arquivo="c" * 64,
        status="CONCLUIDO",
    )
    db.add(arquivo)
    await db.flush()

    fornecedor = CpFornecedor(
        empresa_id=empresa.id,
        arquivo_origem_id=arquivo.id,
        codigo_conta="1667",
        conta_contabil="Fornecedores Nacionais",
        nome_fornecedor="ACME LTDA",
    )
    db.add(fornecedor)
    await db.flush()

    db.add_all([
        CpLancamento(
            empresa_id=empresa.id,
            fornecedor_id=fornecedor.id,
            data_lancamento=date(2026, 1, 5),
            lote="99",
            historico="COMPRA NF 123",
            conta_partida="3.1.1",
            valor_debito=Decimal("0.00"),
            valor_credito=Decimal("1000.00"),
            tipo_operacao="COMPRA",
        ),
        CpLancamento(
            empresa_id=empresa.id,
            fornecedor_id=fornecedor.id,
            data_lancamento=date(2026, 1, 10),
            lote=None,
            historico="PAGAMENTO NF 123",
            conta_partida="1.1.B.banco",
            valor_debito=Decimal("1000.00"),
            valor_credito=Decimal("0.00"),
            tipo_operacao="PAGAMENTO",
        ),
    ])
    await db.flush()
    return arquivo


@pytest.mark.asyncio
async def test_exportar_lancamentos_pareia_conta_fornecedor_e_contrapartida(
    client: AsyncClient, db: AsyncSession, tenant, usuario, empresa
):
    await _login(client, tenant, usuario)
    arquivo = await _setup_arquivo_com_lancamentos(db, empresa)

    r = await client.get(
        f"/api/v1/empresas/{empresa.id}/concilpro/export/lancamentos/{arquivo.id}"
    )
    assert r.status_code == 200
    assert "spreadsheetml" in r.headers["content-type"]
    assert r.content[:2] == b"PK"

    wb = load_workbook_from_bytes(r.content)
    ws = wb.active
    header = [c.value for c in ws[1]]
    assert header == [
        "Data", "Cód. Conta Debito", "Cód. Conta Credito", "Valor",
        "Cód. Histórico", "Complemento Histórico", "Inicia Lote",
        "Código Matriz/Filial", "Centro de Custo Débito", "Centro de Custo Crédito",
    ]

    linhas = list(ws.iter_rows(min_row=2, values_only=True))
    assert len(linhas) == 2

    compra = linhas[0]
    assert compra[0] == "05/01/2026"
    assert compra[1] == "3.1.1"       # débito: contrapartida (conta_partida)
    assert compra[2] == "1667"        # crédito: conta do fornecedor
    assert compra[3] == Decimal("1000.00")
    assert compra[5] == "COMPRA NF 123"
    assert compra[6] == "99"          # Inicia Lote

    pagamento = linhas[1]
    assert pagamento[0] == "10/01/2026"
    assert pagamento[1] == "1667"           # débito: conta do fornecedor
    assert pagamento[2] == "1.1.B.banco"    # crédito: contrapartida
    assert pagamento[3] == Decimal("1000.00")
    assert pagamento[6] is None             # sem lote nesse lançamento


@pytest.mark.asyncio
async def test_exportar_lancamentos_arquivo_inexistente_404(
    client: AsyncClient, tenant, usuario, empresa
):
    await _login(client, tenant, usuario)
    r = await client.get(
        f"/api/v1/empresas/{empresa.id}/concilpro/export/lancamentos/999999"
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_exportar_lancamentos_nao_cruza_empresas(
    client: AsyncClient, db: AsyncSession, tenant, usuario, empresa
):
    outra_empresa = Empresa(
        tenant_id=tenant.id,
        razao_social="OUTRA EMPRESA LTDA",
        cnpj="98.765.432/0001-10",
        regime_tributario="simples_nacional",
    )
    db.add(outra_empresa)
    await db.flush()

    arquivo_alheio = await _setup_arquivo_com_lancamentos(db, outra_empresa)

    await _login(client, tenant, usuario)
    r = await client.get(
        f"/api/v1/empresas/{empresa.id}/concilpro/export/lancamentos/{arquivo_alheio.id}"
    )
    assert r.status_code == 404


def load_workbook_from_bytes(content: bytes):
    import io

    return load_workbook(io.BytesIO(content), data_only=True)
