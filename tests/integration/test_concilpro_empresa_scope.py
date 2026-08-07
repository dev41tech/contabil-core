from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import CpArquivo, CpFornecedor, Empresa


async def _login(client: AsyncClient, tenant, usuario) -> None:
    resposta = await client.post(
        "/api/v1/auth/login",
        json={
            "tenant_id": str(tenant.id),
            "email": usuario.email,
            "senha": "senha_segura_123",
        },
    )
    assert resposta.status_code == 200


@pytest.mark.asyncio
async def test_listagem_e_detalhe_nao_cruzam_empresas(
    client: AsyncClient,
    db: AsyncSession,
    tenant,
    usuario,
    empresa,
):
    outra_empresa = Empresa(
        tenant_id=tenant.id,
        razao_social="OUTRA EMPRESA LTDA",
        cnpj="98.765.432/0001-10",
        regime_tributario="simples_nacional",
    )
    db.add(outra_empresa)
    await db.flush()

    arquivo_visivel = CpArquivo(
        empresa_id=empresa.id,
        nome_arquivo="visivel.xlsx",
        hash_arquivo="a" * 64,
        status="CONCLUIDO",
    )
    arquivo_alheio = CpArquivo(
        empresa_id=outra_empresa.id,
        nome_arquivo="alheio.xlsx",
        hash_arquivo="b" * 64,
        status="CONCLUIDO",
    )
    db.add_all([arquivo_visivel, arquivo_alheio])
    await db.flush()

    fornecedor_alheio = CpFornecedor(
        empresa_id=outra_empresa.id,
        arquivo_origem_id=arquivo_alheio.id,
        codigo_conta="1",
        conta_contabil="2.1.1",
        nome_fornecedor="FORNECEDOR ALHEIO",
    )
    db.add(fornecedor_alheio)
    await db.flush()

    await _login(client, tenant, usuario)

    base = f"/api/v1/empresas/{empresa.id}/concilpro"
    listagem = await client.get(f"{base}/arquivos")
    assert listagem.status_code == 200
    assert [item["nome_arquivo"] for item in listagem.json()] == ["visivel.xlsx"]

    detalhe = await client.get(f"{base}/fornecedores/{fornecedor_alheio.id}")
    assert detalhe.status_code == 404
