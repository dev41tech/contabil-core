"""Testes de integração — Regras de Categorização."""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from src.db.models import AgenciaBancaria, Empresa, PlanoConta, Tenant, Usuario


# ── Helpers


async def _login(client: AsyncClient, tenant: Tenant, usuario: Usuario) -> str:
    r = await client.post(
        "/api/v1/auth/login",
        json={"tenant_id": str(tenant.id), "email": usuario.email, "senha": "senha_segura_123"},
    )
    assert r.status_code == 200
    return r.json()["csrf_token"]


async def _criar_agencia(client, empresa, csrf) -> dict:
    r = await client.post(
        f"/api/v1/empresas/{empresa.id}/agencias",
        json={"banco_sigla": "BB", "agencia": "0001", "numero": "11111"},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 201
    return r.json()


async def _criar_conta(db, empresa: Empresa) -> PlanoConta:
    conta = PlanoConta(
        empresa_id=empresa.id,
        codigo="1.1.1",
        descricao="Bancos",
        tipo="ativo",
    )
    db.add(conta)
    await db.flush()
    return conta


def _url(empresa_id, regra_id=None) -> str:
    base = f"/api/v1/empresas/{empresa_id}/regras"
    return f"{base}/{regra_id}" if regra_id else base


# ── Listar


@pytest.mark.asyncio
async def test_listar_regras_vazia(client: AsyncClient, tenant: Tenant, usuario: Usuario, empresa: Empresa):
    await _login(client, tenant, usuario)
    r = await client.get(_url(empresa.id))
    assert r.status_code == 200
    assert r.json()["items"] == []
    assert r.json()["total"] == 0


# ── Criar


@pytest.mark.asyncio
async def test_criar_regra_sucesso(client, db, tenant, usuario, empresa):
    csrf = await _login(client, tenant, usuario)
    agencia = await _criar_agencia(client, empresa, csrf)
    conta = await _criar_conta(db, empresa)

    r = await client.post(
        _url(empresa.id),
        json={
            "conta_id": str(conta.id),
            "agencia_id": agencia["id"],
            "descricao": "Transferência Recebida",
            "historico": "TED RECEBIDA",
            "dc": "C",
            "manter_historico": False,
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 201
    body = r.json()
    assert body["historico"] == "TED RECEBIDA"
    assert body["dc"] == "C"
    assert body["tipo"] == "automatica"
    assert body["ativa"] is True


@pytest.mark.asyncio
async def test_criar_regra_duplicada_rejeita(client, db, tenant, usuario, empresa):
    csrf = await _login(client, tenant, usuario)
    agencia = await _criar_agencia(client, empresa, csrf)
    conta = await _criar_conta(db, empresa)

    payload = {
        "conta_id": str(conta.id),
        "agencia_id": agencia["id"],
        "descricao": "Pagamento",
        "historico": "BOLETO PAGO",
        "dc": "D",
        "tipo": "automatica",
    }
    r1 = await client.post(_url(empresa.id), json=payload, headers={"X-CSRF-Token": csrf})
    assert r1.status_code == 201

    r2 = await client.post(_url(empresa.id), json=payload, headers={"X-CSRF-Token": csrf})
    assert r2.status_code == 409


@pytest.mark.asyncio
async def test_criar_regra_duplicada_por_caixa_rejeita(
    client, db, tenant, usuario, empresa
):
    csrf = await _login(client, tenant, usuario)
    agencia = await _criar_agencia(client, empresa, csrf)
    conta = await _criar_conta(db, empresa)

    payload = {
        "conta_id": str(conta.id),
        "agencia_id": agencia["id"],
        "descricao": "PIX ACME",
        "historico": "PIX ACME",
        "dc": "D",
        "tipo": "automatica",
    }
    primeira = await client.post(
        _url(empresa.id), json=payload, headers={"X-CSRF-Token": csrf}
    )
    payload["historico"] = "pix acme"
    segunda = await client.post(
        _url(empresa.id), json=payload, headers={"X-CSRF-Token": csrf}
    )

    assert primeira.status_code == 201
    assert segunda.status_code == 409


@pytest.mark.asyncio
async def test_criar_regra_dc_invalido(client, db, tenant, usuario, empresa):
    csrf = await _login(client, tenant, usuario)
    agencia = await _criar_agencia(client, empresa, csrf)
    conta = await _criar_conta(db, empresa)

    r = await client.post(
        _url(empresa.id),
        json={
            "conta_id": str(conta.id),
            "agencia_id": agencia["id"],
            "descricao": "Desc",
            "historico": "TEST",
            "dc": "X",
            "tipo": "automatica",
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 422


# ── Obter


@pytest.mark.asyncio
async def test_obter_regra_existente(client, db, tenant, usuario, empresa):
    csrf = await _login(client, tenant, usuario)
    agencia = await _criar_agencia(client, empresa, csrf)
    conta = await _criar_conta(db, empresa)

    criada = (
        await client.post(
            _url(empresa.id),
            json={
                "conta_id": str(conta.id),
                "agencia_id": agencia["id"],
                "descricao": "Desc",
                "historico": "PAGTO SAL",
                "dc": "D",
                "tipo": "automatica",
            },
            headers={"X-CSRF-Token": csrf},
        )
    ).json()

    r = await client.get(_url(empresa.id, criada["id"]))
    assert r.status_code == 200
    assert r.json()["id"] == criada["id"]


# ── Atualizar


@pytest.mark.asyncio
async def test_atualizar_regra(client, db, tenant, usuario, empresa):
    csrf = await _login(client, tenant, usuario)
    agencia = await _criar_agencia(client, empresa, csrf)
    conta = await _criar_conta(db, empresa)

    criada = (
        await client.post(
            _url(empresa.id),
            json={
                "conta_id": str(conta.id),
                "agencia_id": agencia["id"],
                "descricao": "Desc Antiga",
                "historico": "PIX RECEBIDO",
                "dc": "C",
                "tipo": "automatica",
            },
            headers={"X-CSRF-Token": csrf},
        )
    ).json()

    r = await client.patch(
        _url(empresa.id, criada["id"]),
        json={"descricao": "Desc Nova", "manter_historico": True},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 200
    assert r.json()["descricao"] == "Desc Nova"
    assert r.json()["manter_historico"] is True


# ── Desativar


@pytest.mark.asyncio
async def test_desativar_regra(client, db, tenant, usuario, empresa):
    csrf = await _login(client, tenant, usuario)
    agencia = await _criar_agencia(client, empresa, csrf)
    conta = await _criar_conta(db, empresa)

    criada = (
        await client.post(
            _url(empresa.id),
            json={
                "conta_id": str(conta.id),
                "agencia_id": agencia["id"],
                "descricao": "Desc",
                "historico": "DOC ENVIADO",
                "dc": "D",
                "tipo": "automatica",
            },
            headers={"X-CSRF-Token": csrf},
        )
    ).json()

    r = await client.delete(_url(empresa.id, criada["id"]), headers={"X-CSRF-Token": csrf})
    assert r.status_code == 204

    # Regra desativada não aparece em apenas_ativas
    lista = await client.get(_url(empresa.id) + "?apenas_ativas=true")
    ids = [i["id"] for i in lista.json()["items"]]
    assert criada["id"] not in ids


@pytest.mark.asyncio
async def test_criar_regra_sem_csrf_rejeita(client, db, tenant, usuario, empresa):
    csrf = await _login(client, tenant, usuario)
    agencia = await _criar_agencia(client, empresa, csrf)
    conta = await _criar_conta(db, empresa)

    r = await client.post(
        _url(empresa.id),
        json={
            "conta_id": str(conta.id),
            "agencia_id": agencia["id"],
            "descricao": "Desc",
            "historico": "SEM CSRF",
            "dc": "D",
            "tipo": "automatica",
        },
    )
    assert r.status_code == 403
