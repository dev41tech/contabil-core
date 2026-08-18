"""Testes de integração — CRUD de Contrapartes."""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import Empresa, PlanoConta, Tenant, Usuario


async def _login(client: AsyncClient, tenant: Tenant, usuario: Usuario) -> str:
    r = await client.post(
        "/api/v1/auth/login",
        json={"tenant_id": str(tenant.id), "email": usuario.email, "senha": "senha_segura_123"},
    )
    assert r.status_code == 200
    return r.json()["csrf_token"]


async def _criar_conta(db: AsyncSession, empresa: Empresa, codigo: str = "4.1.1") -> PlanoConta:
    conta = PlanoConta(
        empresa_id=empresa.id, codigo=codigo, descricao="Despesas Fornecedores", tipo="despesa"
    )
    db.add(conta)
    await db.flush()
    return conta


def _url(empresa_id, contraparte_id=None) -> str:
    base = f"/api/v1/empresas/{empresa_id}/contrapartes"
    return f"{base}/{contraparte_id}" if contraparte_id else base


async def _criar(client, empresa_id, conta_id, csrf, **over) -> dict:
    payload = {
        "tipo": "fornecedor",
        "documento": "52.540.787/0001-88",
        "razao_social": "Axel Tecnologia Ltda",
        "conta_contabil_id": str(conta_id),
    }
    payload.update(over)
    r = await client.post(_url(empresa_id), json=payload, headers={"X-CSRF-Token": csrf})
    assert r.status_code == 201, r.text
    return r.json()


# ── Criar


@pytest.mark.asyncio
async def test_criar_contraparte_sucesso(
    client: AsyncClient, db: AsyncSession, tenant: Tenant, usuario: Usuario, empresa: Empresa
):
    csrf = await _login(client, tenant, usuario)
    conta = await _criar_conta(db, empresa)

    body = await _criar(client, empresa.id, conta.id, csrf, nome_fantasia="Axel")
    assert body["tipo"] == "fornecedor"
    assert body["documento"] == "52540787000188"
    assert body["razao_social"] == "Axel Tecnologia Ltda"
    assert body["nome_fantasia"] == "Axel"
    assert body["conta_contabil_id"] == str(conta.id)
    assert body["conta_codigo"] == conta.codigo
    assert body["origem"] == "manual"
    assert body["confirmado_em"] is not None
    assert body["ativa"] is True


@pytest.mark.asyncio
async def test_criar_contraparte_documento_duplicado_rejeita(
    client: AsyncClient, db: AsyncSession, tenant: Tenant, usuario: Usuario, empresa: Empresa
):
    csrf = await _login(client, tenant, usuario)
    conta = await _criar_conta(db, empresa)
    await _criar(client, empresa.id, conta.id, csrf)

    r = await client.post(
        _url(empresa.id),
        json={
            "tipo": "fornecedor",
            "documento": "52540787000188",
            "razao_social": "Axel Tecnologia (duplicado)",
            "conta_contabil_id": str(conta.id),
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 409


@pytest.mark.asyncio
async def test_criar_contraparte_com_conta_de_outra_empresa_rejeita(
    client: AsyncClient, db: AsyncSession, tenant: Tenant, usuario: Usuario, empresa: Empresa
):
    outra_empresa = Empresa(
        tenant_id=tenant.id, razao_social="Outra LTDA",
        cnpj="11.222.333/0001-44", regime_tributario="lucro_real",
    )
    db.add(outra_empresa)
    await db.flush()
    conta_outra = await _criar_conta(db, outra_empresa)

    csrf = await _login(client, tenant, usuario)
    r = await client.post(
        _url(empresa.id),
        json={
            "tipo": "fornecedor",
            "documento": "52540787000188",
            "razao_social": "Axel Tecnologia Ltda",
            "conta_contabil_id": str(conta_outra.id),
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_criar_contraparte_documento_invalido_rejeita(
    client: AsyncClient, db: AsyncSession, tenant: Tenant, usuario: Usuario, empresa: Empresa
):
    csrf = await _login(client, tenant, usuario)
    conta = await _criar_conta(db, empresa)

    r = await client.post(
        _url(empresa.id),
        json={
            "tipo": "fornecedor",
            "documento": "123",
            "razao_social": "Axel Tecnologia Ltda",
            "conta_contabil_id": str(conta.id),
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_criar_contraparte_sem_csrf_rejeita(
    client: AsyncClient, db: AsyncSession, tenant: Tenant, usuario: Usuario, empresa: Empresa
):
    await _login(client, tenant, usuario)
    conta = await _criar_conta(db, empresa)
    r = await client.post(
        _url(empresa.id),
        json={
            "tipo": "fornecedor",
            "documento": "52540787000188",
            "razao_social": "Axel Tecnologia Ltda",
            "conta_contabil_id": str(conta.id),
        },
    )
    assert r.status_code == 403


# ── Listar / busca


@pytest.mark.asyncio
async def test_listar_contrapartes_vazia(
    client: AsyncClient, tenant: Tenant, usuario: Usuario, empresa: Empresa
):
    await _login(client, tenant, usuario)
    r = await client.get(_url(empresa.id))
    assert r.status_code == 200
    assert r.json()["items"] == []
    assert r.json()["total"] == 0


@pytest.mark.asyncio
async def test_buscar_por_termo_encontra_por_razao_social_nome_fantasia_e_documento(
    client: AsyncClient, db: AsyncSession, tenant: Tenant, usuario: Usuario, empresa: Empresa
):
    csrf = await _login(client, tenant, usuario)
    conta = await _criar_conta(db, empresa)
    await _criar(
        client, empresa.id, conta.id, csrf,
        documento="52.540.787/0001-88",
        razao_social="Axel Tecnologia Ltda",
        nome_fantasia="Axel Tech",
    )
    conta2 = await _criar_conta(db, empresa, codigo="4.1.2")
    await _criar(
        client, empresa.id, conta2.id, csrf,
        documento="12.810.326/0001-63",
        razao_social="Cargo Time Transportes",
        nome_fantasia="Cargo Time",
    )

    por_razao = (await client.get(_url(empresa.id) + "?termo=Axel")).json()
    assert [i["razao_social"] for i in por_razao["items"]] == ["Axel Tecnologia Ltda"]

    por_fantasia = (await client.get(_url(empresa.id) + "?termo=Cargo Time")).json()
    assert [i["razao_social"] for i in por_fantasia["items"]] == ["Cargo Time Transportes"]

    por_cnpj = (await client.get(_url(empresa.id) + "?termo=52540787000188")).json()
    assert [i["razao_social"] for i in por_cnpj["items"]] == ["Axel Tecnologia Ltda"]

    por_cnpj_formatado = (
        await client.get(_url(empresa.id) + "?termo=52.540.787/0001-88")
    ).json()
    assert [i["razao_social"] for i in por_cnpj_formatado["items"]] == ["Axel Tecnologia Ltda"]


# ── Atualizar


@pytest.mark.asyncio
async def test_atualizar_conta_contabil(
    client: AsyncClient, db: AsyncSession, tenant: Tenant, usuario: Usuario, empresa: Empresa
):
    csrf = await _login(client, tenant, usuario)
    conta1 = await _criar_conta(db, empresa, codigo="4.1.1")
    conta2 = await _criar_conta(db, empresa, codigo="4.1.2")
    criada = await _criar(client, empresa.id, conta1.id, csrf)

    r = await client.patch(
        _url(empresa.id, criada["id"]),
        json={"conta_contabil_id": str(conta2.id)},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 200
    assert r.json()["conta_contabil_id"] == str(conta2.id)
    assert r.json()["conta_codigo"] == conta2.codigo


@pytest.mark.asyncio
async def test_desativar_contraparte_via_update(
    client: AsyncClient, db: AsyncSession, tenant: Tenant, usuario: Usuario, empresa: Empresa
):
    csrf = await _login(client, tenant, usuario)
    conta = await _criar_conta(db, empresa)
    criada = await _criar(client, empresa.id, conta.id, csrf)

    r = await client.patch(
        _url(empresa.id, criada["id"]),
        json={"ativa": False},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 200
    assert r.json()["ativa"] is False

    lista_ativas = await client.get(_url(empresa.id) + "?apenas_ativas=true")
    assert criada["id"] not in [c["id"] for c in lista_ativas.json()["items"]]


@pytest.mark.asyncio
async def test_desativar_e_recriar_mesmo_documento(
    client: AsyncClient, db: AsyncSession, tenant: Tenant, usuario: Usuario, empresa: Empresa
):
    """Documento de uma contraparte desativada fica livre para um novo cadastro."""
    csrf = await _login(client, tenant, usuario)
    conta = await _criar_conta(db, empresa)
    criada = await _criar(client, empresa.id, conta.id, csrf)

    await client.patch(
        _url(empresa.id, criada["id"]),
        json={"ativa": False},
        headers={"X-CSRF-Token": csrf},
    )

    r = await client.post(
        _url(empresa.id),
        json={
            "tipo": "fornecedor",
            "documento": "52540787000188",
            "razao_social": "Axel Tecnologia Ltda (novo cadastro)",
            "conta_contabil_id": str(conta.id),
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 201


# ── Remover


@pytest.mark.asyncio
async def test_remover_contraparte(
    client: AsyncClient, db: AsyncSession, tenant: Tenant, usuario: Usuario, empresa: Empresa
):
    csrf = await _login(client, tenant, usuario)
    conta = await _criar_conta(db, empresa)
    criada = await _criar(client, empresa.id, conta.id, csrf)

    r = await client.delete(_url(empresa.id, criada["id"]), headers={"X-CSRF-Token": csrf})
    assert r.status_code == 204

    lista = await client.get(_url(empresa.id))
    assert criada["id"] not in [c["id"] for c in lista.json()["items"]]


@pytest.mark.asyncio
async def test_obter_contraparte_inexistente(
    client: AsyncClient, tenant: Tenant, usuario: Usuario, empresa: Empresa
):
    await _login(client, tenant, usuario)
    r = await client.get(_url(empresa.id, uuid.uuid4()))
    assert r.status_code == 404


# ── Isolamento de empresa


@pytest.mark.asyncio
async def test_contraparte_nao_visivel_em_outra_empresa(
    client: AsyncClient, db: AsyncSession, tenant: Tenant, usuario: Usuario, empresa: Empresa
):
    empresa2 = Empresa(
        tenant_id=tenant.id, razao_social="Outra Empresa LTDA",
        cnpj="99.888.777/0001-00", regime_tributario="lucro_real",
    )
    db.add(empresa2)
    await db.flush()

    csrf = await _login(client, tenant, usuario)
    conta = await _criar_conta(db, empresa)
    await _criar(client, empresa.id, conta.id, csrf)

    r = await client.get(_url(empresa2.id))
    assert r.status_code == 200
    assert r.json()["items"] == []
