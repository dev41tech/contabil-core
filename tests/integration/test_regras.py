"""Testes de integração — Regras de Categorização."""

from __future__ import annotations

import uuid

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
async def test_desativar_regra_via_patch(client, db, tenant, usuario, empresa):
    """Trava o caminho usado pelo front porque PATCH ativa=false é a única forma de desativação exposta."""
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

    r = await client.patch(
        _url(empresa.id, criada["id"]),
        json={"ativa": False},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 200
    assert r.json()["ativa"] is False

    # Regra desativada não aparece em apenas_ativas
    lista = await client.get(_url(empresa.id) + "?apenas_ativas=true")
    ids = [i["id"] for i in lista.json()["items"]]
    assert criada["id"] not in ids


@pytest.mark.asyncio
async def test_delete_regra_nao_e_mais_exposto(client, tenant, usuario, empresa):
    """Trava a remoção da rota redundante para não voltarem duas maneiras de executar o mesmo soft delete."""
    csrf = await _login(client, tenant, usuario)
    r = await client.delete(
        _url(empresa.id, uuid.uuid4()), headers={"X-CSRF-Token": csrf}
    )
    assert r.status_code == 405


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


# ── Regra para todos os bancos


@pytest.mark.asyncio
async def test_criar_regra_sem_agencia_vale_para_todos_os_bancos(
    client, db, tenant, usuario, empresa
):
    """A maior parte das regras do escritório não depende de banco.

    "TARIFA PACOTE DE SERVICOS" vai para a mesma conta venha do BB, da Caixa ou
    do Itaú — e exigir agência obrigava a cadastrar a mesma regra uma vez por
    banco, e a refazer todas quando a conta contábil mudasse.
    """
    csrf = await _login(client, tenant, usuario)
    conta = await _criar_conta(db, empresa)

    r = await client.post(
        _url(empresa.id),
        json={
            "conta_id": str(conta.id),
            "descricao": "Tarifas bancárias",
            "historico": "TARIFA PACOTE DE SERVICOS",
            "dc": "D",
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 201, r.text
    assert r.json()["agencia_id"] is None


@pytest.mark.asyncio
async def test_duas_regras_globais_com_o_mesmo_historico_rejeita(
    client, db, tenant, usuario, empresa
):
    """Duas globais iguais disputariam a transação sem critério.

    Em Postgres dois NULL são DISTINTOS num índice único, então o índice que
    protege o escopo por agência não alcança as globais — daí o índice irmão.
    """
    csrf = await _login(client, tenant, usuario)
    conta = await _criar_conta(db, empresa)
    payload = {
        "conta_id": str(conta.id),
        "descricao": "Tarifas",
        "historico": "TARIFA PACOTE",
        "dc": "D",
    }

    assert (await client.post(_url(empresa.id), json=payload,
                              headers={"X-CSRF-Token": csrf})).status_code == 201
    r2 = await client.post(_url(empresa.id), json=payload,
                           headers={"X-CSRF-Token": csrf})
    assert r2.status_code == 409
    assert "todos os bancos" in r2.json()["message"]


@pytest.mark.asyncio
async def test_global_e_de_agencia_com_mesmo_historico_convivem(
    client, db, tenant, usuario, empresa
):
    """É o padrão "regra geral mais exceção", e é permitido de propósito.

    A de agência vence no motor — ver `NeoEngine._carregar_regras`.
    """
    csrf = await _login(client, tenant, usuario)
    agencia = await _criar_agencia(client, empresa, csrf)
    conta = await _criar_conta(db, empresa)
    base = {
        "conta_id": str(conta.id),
        "descricao": "Tarifas",
        "historico": "TARIFA PACOTE",
        "dc": "D",
    }

    global_ = await client.post(_url(empresa.id), json=base,
                                headers={"X-CSRF-Token": csrf})
    assert global_.status_code == 201
    da_agencia = await client.post(
        _url(empresa.id), json={**base, "agencia_id": agencia["id"]},
        headers={"X-CSRF-Token": csrf},
    )
    assert da_agencia.status_code == 201, da_agencia.text


@pytest.mark.asyncio
async def test_filtrar_por_agencia_mostra_tambem_as_globais(
    client, db, tenant, usuario, empresa
):
    """Filtrar por um banco e não ver a regra que o classifica é enganoso."""
    csrf = await _login(client, tenant, usuario)
    agencia = await _criar_agencia(client, empresa, csrf)
    conta = await _criar_conta(db, empresa)

    await client.post(
        _url(empresa.id),
        json={"conta_id": str(conta.id), "descricao": "Global",
              "historico": "TARIFA GLOBAL", "dc": "D"},
        headers={"X-CSRF-Token": csrf},
    )

    r = await client.get(f"{_url(empresa.id)}?agencia_id={agencia['id']}")
    assert r.status_code == 200
    historicos = [i["historico"] for i in r.json()["items"]]
    assert "TARIFA GLOBAL" in historicos


# ── O índice, e não o serviço


@pytest.mark.asyncio
async def test_indice_barra_duas_regras_globais_iguais(db, empresa):
    """A prova de que o índice irmão morde — inserindo DIRETO, sem o serviço.

    O teste de 409 acima passa pela checagem do `RegraService`, que responde
    antes de o banco ser tocado: ele nunca exercita o índice. Se alguém removesse
    `uq_regra_empresa_historico_normalizado_global` amanhã, aquele teste
    continuaria verde e o banco ficaria sem a proteção — que é justamente a que
    impede duas regras "todos os bancos" de disputarem a mesma transação.

    Este roda contra Postgres no job `testes-postgres` da CI, que é onde o
    comportamento de NULL em índice único importa de verdade.
    """
    from sqlalchemy.exc import IntegrityError

    from src.db.models import Regra

    conta = await _criar_conta(db, empresa)
    comuns = dict(
        empresa_id=empresa.id, conta_id=conta.id, agencia_id=None,
        descricao="Tarifas", historico="TARIFA PACOTE",
        historico_normalizado="tarifa pacote", dc="D", tipo="automatica",
    )
    db.add(Regra(**comuns))
    await db.flush()

    db.add(Regra(**comuns))
    with pytest.raises(IntegrityError):
        await db.flush()
    await db.rollback()


@pytest.mark.asyncio
async def test_indice_permite_global_e_de_agencia_com_o_mesmo_historico(
    db, empresa, client, tenant, usuario
):
    """O outro lado do índice: "geral mais exceção" não pode ser barrado.

    É o motivo de existirem dois índices parciais em vez de um sobre
    `COALESCE(agencia_id, ...)`, que barraria este par.
    """
    from src.db.models import AgenciaBancaria, Regra

    conta = await _criar_conta(db, empresa)
    agencia = AgenciaBancaria(empresa_id=empresa.id, banco_sigla="BB",
                              agencia="0002", numero="22222")
    db.add(agencia)
    await db.flush()

    comuns = dict(
        empresa_id=empresa.id, conta_id=conta.id, descricao="Tarifas",
        historico="TARIFA PACOTE", historico_normalizado="tarifa pacote",
        dc="D", tipo="automatica",
    )
    db.add(Regra(**comuns, agencia_id=None))
    db.add(Regra(**comuns, agencia_id=agencia.id))
    await db.flush()  # não pode levantar
