"""Testes de integração — CRUD de agências bancárias."""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import Empresa, Tenant, Usuario


# ── Helpers


async def _login(client: AsyncClient, tenant: Tenant, usuario: Usuario) -> str:
    r = await client.post(
        "/api/v1/auth/login",
        json={
            "tenant_id": str(tenant.id),
            "email": usuario.email,
            "senha": "senha_segura_123",
        },
    )
    assert r.status_code == 200
    return r.json()["csrf_token"]


def _url(empresa_id, agencia_id=None) -> str:
    base = f"/api/v1/empresas/{empresa_id}/agencias"
    return f"{base}/{agencia_id}" if agencia_id else base


# ── Listar


@pytest.mark.asyncio
async def test_listar_agencias_vazia(
    client: AsyncClient, tenant: Tenant, usuario: Usuario, empresa: Empresa
):
    await _login(client, tenant, usuario)
    r = await client.get(_url(empresa.id))
    assert r.status_code == 200
    body = r.json()
    assert body["items"] == []
    assert body["total"] == 0


@pytest.mark.asyncio
async def test_listar_sem_auth_rejeita(client: AsyncClient, empresa: Empresa):
    r = await client.get(_url(empresa.id))
    assert r.status_code == 401


# ── Criar


@pytest.mark.asyncio
async def test_criar_agencia_sucesso(
    client: AsyncClient, tenant: Tenant, usuario: Usuario, empresa: Empresa
):
    csrf = await _login(client, tenant, usuario)
    r = await client.post(
        _url(empresa.id),
        json={"banco_sigla": "BRADESCO", "agencia": "1234", "numero": "00012345"},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 201
    body = r.json()
    assert body["banco_sigla"] == "BRADESCO"
    assert body["agencia"] == "1234"
    assert body["numero"] == "00012345"
    assert body["digito"] is None
    assert body["ativa"] is True
    assert body["empresa_id"] == str(empresa.id)
    assert "id" in body
    # descricao computada deve estar presente
    assert "BRADESCO" in body["descricao"]


@pytest.mark.asyncio
async def test_criar_agencia_com_digito(
    client: AsyncClient, tenant: Tenant, usuario: Usuario, empresa: Empresa
):
    csrf = await _login(client, tenant, usuario)
    r = await client.post(
        _url(empresa.id),
        json={"banco_sigla": "ITAU", "agencia": "0001", "numero": "99887", "digito": "7"},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 201
    assert r.json()["digito"] == "7"
    assert "7" in r.json()["descricao"]


@pytest.mark.asyncio
async def test_criar_agencia_codigo_banco_convertido(
    client: AsyncClient, tenant: Tenant, usuario: Usuario, empresa: Empresa
):
    """Código numérico 237 deve ser salvo como BRADESCO."""
    csrf = await _login(client, tenant, usuario)
    r = await client.post(
        _url(empresa.id),
        json={"banco_sigla": "237", "agencia": "5555", "numero": "77777"},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 201
    assert r.json()["banco_sigla"] == "BRADESCO"


@pytest.mark.asyncio
async def test_criar_agencia_duplicada_rejeita(
    client: AsyncClient, tenant: Tenant, usuario: Usuario, empresa: Empresa
):
    csrf = await _login(client, tenant, usuario)
    payload = {"banco_sigla": "BB", "agencia": "0001", "numero": "11111"}

    r1 = await client.post(_url(empresa.id), json=payload, headers={"X-CSRF-Token": csrf})
    assert r1.status_code == 201

    r2 = await client.post(_url(empresa.id), json=payload, headers={"X-CSRF-Token": csrf})
    assert r2.status_code == 409
    assert r2.json()["error"] == "CONFLICT"


@pytest.mark.asyncio
async def test_criar_agencia_sem_csrf_rejeita(
    client: AsyncClient, tenant: Tenant, usuario: Usuario, empresa: Empresa
):
    await _login(client, tenant, usuario)
    r = await client.post(
        _url(empresa.id),
        json={"banco_sigla": "BB", "agencia": "0001", "numero": "99999"},
    )
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_criar_agencia_dados_invalidos(
    client: AsyncClient, tenant: Tenant, usuario: Usuario, empresa: Empresa
):
    csrf = await _login(client, tenant, usuario)
    r = await client.post(
        _url(empresa.id),
        json={"banco_sigla": "BB", "agencia": "ABC", "numero": "12345"},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 422


# ── Obter


@pytest.mark.asyncio
async def test_obter_agencia_existente(
    client: AsyncClient, tenant: Tenant, usuario: Usuario, empresa: Empresa
):
    csrf = await _login(client, tenant, usuario)
    criada = (
        await client.post(
            _url(empresa.id),
            json={"banco_sigla": "CEF", "agencia": "0010", "numero": "12345"},
            headers={"X-CSRF-Token": csrf},
        )
    ).json()

    r = await client.get(_url(empresa.id, criada["id"]))
    assert r.status_code == 200
    assert r.json()["id"] == criada["id"]


@pytest.mark.asyncio
async def test_obter_agencia_inexistente(
    client: AsyncClient, tenant: Tenant, usuario: Usuario, empresa: Empresa
):
    import uuid
    await _login(client, tenant, usuario)
    r = await client.get(_url(empresa.id, uuid.uuid4()))
    assert r.status_code == 404


# ── Atualizar


@pytest.mark.asyncio
async def test_atualizar_agencia_sucesso(
    client: AsyncClient, tenant: Tenant, usuario: Usuario, empresa: Empresa
):
    csrf = await _login(client, tenant, usuario)
    criada = (
        await client.post(
            _url(empresa.id),
            json={"banco_sigla": "SANTANDER", "agencia": "0100", "numero": "55555"},
            headers={"X-CSRF-Token": csrf},
        )
    ).json()

    r = await client.patch(
        _url(empresa.id, criada["id"]),
        json={"digito": "9"},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 200
    assert r.json()["digito"] == "9"
    # Campos não alterados permanecem
    assert r.json()["banco_sigla"] == "SANTANDER"
    assert r.json()["numero"] == "55555"


@pytest.mark.asyncio
async def test_atualizar_numero_duplicado_rejeita(
    client: AsyncClient, tenant: Tenant, usuario: Usuario, empresa: Empresa
):
    """Não deve ser possível atualizar para um número já existente na mesma agência."""
    csrf = await _login(client, tenant, usuario)

    a1 = (
        await client.post(
            _url(empresa.id),
            json={"banco_sigla": "NU", "agencia": "0001", "numero": "11111"},
            headers={"X-CSRF-Token": csrf},
        )
    ).json()

    await client.post(
        _url(empresa.id),
        json={"banco_sigla": "NU", "agencia": "0001", "numero": "22222"},
        headers={"X-CSRF-Token": csrf},
    )

    r = await client.patch(
        _url(empresa.id, a1["id"]),
        json={"numero": "22222"},  # já existe
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 409


# ── Desativar


@pytest.mark.asyncio
async def test_desativar_agencia(
    client: AsyncClient, tenant: Tenant, usuario: Usuario, empresa: Empresa
):
    csrf = await _login(client, tenant, usuario)
    criada = (
        await client.post(
            _url(empresa.id),
            json={"banco_sigla": "INTER", "agencia": "0001", "numero": "77777"},
            headers={"X-CSRF-Token": csrf},
        )
    ).json()

    r = await client.delete(_url(empresa.id, criada["id"]), headers={"X-CSRF-Token": csrf})
    assert r.status_code == 204

    # Ainda aparece na listagem (inativa), mas não na listagem de apenas_ativas
    lista_todas = await client.get(_url(empresa.id))
    lista_ativas = await client.get(_url(empresa.id) + "?apenas_ativas=true")

    todas_ids = [e["id"] for e in lista_todas.json()["items"]]
    ativas_ids = [e["id"] for e in lista_ativas.json()["items"]]

    assert criada["id"] in todas_ids
    assert criada["id"] not in ativas_ids


@pytest.mark.asyncio
async def test_desativar_agencia_idempotente(
    client: AsyncClient, tenant: Tenant, usuario: Usuario, empresa: Empresa
):
    """Desativar duas vezes não deve ser erro."""
    csrf = await _login(client, tenant, usuario)
    criada = (
        await client.post(
            _url(empresa.id),
            json={"banco_sigla": "SICOOB", "agencia": "0001", "numero": "33333"},
            headers={"X-CSRF-Token": csrf},
        )
    ).json()

    r1 = await client.delete(_url(empresa.id, criada["id"]), headers={"X-CSRF-Token": csrf})
    r2 = await client.delete(_url(empresa.id, criada["id"]), headers={"X-CSRF-Token": csrf})
    assert r1.status_code == 204
    assert r2.status_code == 204


@pytest.mark.asyncio
async def test_reativar_agencia_via_update(
    client: AsyncClient, tenant: Tenant, usuario: Usuario, empresa: Empresa
):
    """Uma conta desativada pode ser reativada via PATCH ativa=true."""
    csrf = await _login(client, tenant, usuario)
    criada = (
        await client.post(
            _url(empresa.id),
            json={"banco_sigla": "SAFRA", "agencia": "0001", "numero": "44444"},
            headers={"X-CSRF-Token": csrf},
        )
    ).json()

    await client.delete(_url(empresa.id, criada["id"]), headers={"X-CSRF-Token": csrf})

    r = await client.patch(
        _url(empresa.id, criada["id"]),
        json={"ativa": True},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 200
    assert r.json()["ativa"] is True


# ── Isolamento de tenant


@pytest.mark.asyncio
async def test_agencia_nao_visivel_em_outra_empresa(
    client: AsyncClient,
    db: AsyncSession,
    tenant: Tenant,
    usuario: Usuario,
    empresa: Empresa,
):
    """Agência de uma empresa não deve ser visível em outra empresa do mesmo tenant."""
    from src.db.models import Empresa as EmpresaModel

    empresa2 = EmpresaModel(
        tenant_id=tenant.id,
        razao_social="Outra Empresa LTDA",
        cnpj="99.888.777/0001-55",
        regime_tributario="lucro_real",
    )
    db.add(empresa2)
    await db.flush()

    csrf = await _login(client, tenant, usuario)

    # Cria agência na empresa original
    await client.post(
        _url(empresa.id),
        json={"banco_sigla": "BB", "agencia": "9999", "numero": "88888"},
        headers={"X-CSRF-Token": csrf},
    )

    # Lista na empresa2 — deve estar vazia
    r = await client.get(_url(empresa2.id))
    assert r.status_code == 200
    assert r.json()["items"] == []
