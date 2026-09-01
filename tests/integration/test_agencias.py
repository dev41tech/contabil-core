"""Testes de integração — CRUD de agências bancárias."""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import Empresa, PlanoConta, Tenant, Usuario


async def _criar_plano_conta(
    db: AsyncSession, empresa: Empresa, *, codigo: str, conta_numero: int | None = None
) -> PlanoConta:
    conta = PlanoConta(
        empresa_id=empresa.id,
        codigo=codigo,
        conta_numero=conta_numero,
        descricao=f"Conta {codigo}",
        tipo="ativo",
        tipo_sa="A",
    )
    db.add(conta)
    await db.flush()
    return conta


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


# ── Vínculo com o Plano de Contas (conta_contabil_id)


@pytest.mark.asyncio
async def test_vincular_conta_contabil_sucesso(
    client: AsyncClient, db: AsyncSession, tenant: Tenant, usuario: Usuario, empresa: Empresa
):
    conta = await _criar_plano_conta(db, empresa, codigo="1.1.02.0001", conta_numero=1379)
    csrf = await _login(client, tenant, usuario)
    criada = (
        await client.post(
            _url(empresa.id),
            json={"banco_sigla": "BB", "agencia": "0001", "numero": "10101"},
            headers={"X-CSRF-Token": csrf},
        )
    ).json()

    r = await client.patch(
        _url(empresa.id, criada["id"]),
        json={"conta_contabil_id": str(conta.id)},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["conta_contabil_id"] == str(conta.id)
    assert body["conta_contabil_codigo"] == "1.1.02.0001"
    assert body["conta_contabil_descricao"] == "Conta 1.1.02.0001"


@pytest.mark.asyncio
async def test_vincular_conta_contabil_inexistente_retorna_404(
    client: AsyncClient, tenant: Tenant, usuario: Usuario, empresa: Empresa
):
    import uuid

    csrf = await _login(client, tenant, usuario)
    criada = (
        await client.post(
            _url(empresa.id),
            json={"banco_sigla": "BB", "agencia": "0002", "numero": "20202"},
            headers={"X-CSRF-Token": csrf},
        )
    ).json()

    r = await client.patch(
        _url(empresa.id, criada["id"]),
        json={"conta_contabil_id": str(uuid.uuid4())},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_vincular_conta_contabil_de_outra_empresa_retorna_404(
    client: AsyncClient, db: AsyncSession, tenant: Tenant, usuario: Usuario, empresa: Empresa
):
    """Isolamento: não pode vincular a uma conta do Plano de Contas de outra empresa."""
    outra_empresa = Empresa(
        tenant_id=tenant.id,
        razao_social="Outra Empresa LTDA",
        cnpj="99.888.777/0001-00",
        regime_tributario="lucro_real",
    )
    db.add(outra_empresa)
    await db.flush()
    conta_de_outra = await _criar_plano_conta(db, outra_empresa, codigo="1.1.01.0001")

    csrf = await _login(client, tenant, usuario)
    criada = (
        await client.post(
            _url(empresa.id),
            json={"banco_sigla": "BB", "agencia": "0003", "numero": "30303"},
            headers={"X-CSRF-Token": csrf},
        )
    ).json()

    r = await client.patch(
        _url(empresa.id, criada["id"]),
        json={"conta_contabil_id": str(conta_de_outra.id)},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_vincular_conta_contabil_ja_vinculada_a_outra_agencia_rejeita(
    client: AsyncClient, db: AsyncSession, tenant: Tenant, usuario: Usuario, empresa: Empresa
):
    """A constraint de unicidade em conta_contabil_id impede duas agências
    apontarem pra mesma conta bancária no Plano de Contas."""
    conta = await _criar_plano_conta(db, empresa, codigo="1.1.02.0002", conta_numero=1380)
    csrf = await _login(client, tenant, usuario)

    a1 = (
        await client.post(
            _url(empresa.id),
            json={"banco_sigla": "BB", "agencia": "0004", "numero": "40404"},
            headers={"X-CSRF-Token": csrf},
        )
    ).json()
    a2 = (
        await client.post(
            _url(empresa.id),
            json={"banco_sigla": "BB", "agencia": "0005", "numero": "50505"},
            headers={"X-CSRF-Token": csrf},
        )
    ).json()

    r1 = await client.patch(
        _url(empresa.id, a1["id"]),
        json={"conta_contabil_id": str(conta.id)},
        headers={"X-CSRF-Token": csrf},
    )
    assert r1.status_code == 200

    r2 = await client.patch(
        _url(empresa.id, a2["id"]),
        json={"conta_contabil_id": str(conta.id)},
        headers={"X-CSRF-Token": csrf},
    )
    assert r2.status_code == 409


@pytest.mark.asyncio
async def test_desvincular_conta_contabil(
    client: AsyncClient, db: AsyncSession, tenant: Tenant, usuario: Usuario, empresa: Empresa
):
    conta = await _criar_plano_conta(db, empresa, codigo="1.1.02.0003", conta_numero=1381)
    csrf = await _login(client, tenant, usuario)
    criada = (
        await client.post(
            _url(empresa.id),
            json={"banco_sigla": "BB", "agencia": "0006", "numero": "60606"},
            headers={"X-CSRF-Token": csrf},
        )
    ).json()
    await client.patch(
        _url(empresa.id, criada["id"]),
        json={"conta_contabil_id": str(conta.id)},
        headers={"X-CSRF-Token": csrf},
    )

    r = await client.patch(
        _url(empresa.id, criada["id"]),
        json={"conta_contabil_id": None},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["conta_contabil_id"] is None
    assert body["conta_contabil_codigo"] is None


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
        cnpj="99.888.777/0001-00",
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


# ── Reapontar a conta bancária sintética


@pytest.mark.asyncio
async def test_reapontar_move_o_razao_da_sintetica_para_a_conta_vinculada(
    client: AsyncClient, db: AsyncSession, tenant: Tenant, usuario: Usuario,
    empresa: Empresa,
):
    """O motor cria `1.1.B.<uuid>` sem `conta_numero`, e a exportação quebra.

    A conta sintética nasce quando a agência não tem conta contábil vinculada.
    Ela não tem número abreviado, e o layout de importação do sistema contábil
    externo usa justamente o abreviado — então o lado bancário de todo
    lançamento saía como `1.1.B.949a6741df4e4031`.

    Vincular a conta conserta o FUTURO; o razão já gravado é o que esta rotina
    move.
    """
    from datetime import UTC, datetime
    from decimal import Decimal

    from sqlalchemy import select

    from src.db.models import AgenciaBancaria, RegistroContabil

    csrf = await _login(client, tenant, usuario)
    real = PlanoConta(empresa_id=empresa.id, conta_numero=8, codigo="1.1.1.001",
                      descricao="BANCO - DO BRASIL", tipo="ativo", tipo_sa="A")
    agencia = AgenciaBancaria(empresa_id=empresa.id, banco_sigla="BB",
                              agencia="1622", numero="19530")
    db.add_all([real, agencia])
    await db.flush()

    sintetica = PlanoConta(
        empresa_id=empresa.id, codigo=f"1.1.B.{agencia.id.hex[:16]}",
        descricao="Conta bancária BANCO DO BRASIL", tipo="ativo", tipo_sa="A",
    )
    db.add(sintetica)
    await db.flush()

    registro = RegistroContabil(
        empresa_id=empresa.id, conta_id=sintetica.id, agencia_id=agencia.id,
        lancamento_id=__import__("uuid").uuid4(), dc="C",
        valor=Decimal("100.00"), data_lancamento=datetime(2026, 3, 1, tzinfo=UTC),
        descricao="PGTO IOF", historico="PGTO IOF",
        historico_extrato="PGTO IOF", tipo_regra="automatica",
    )
    db.add(registro)
    agencia.conta_contabil_id = real.id
    await db.flush()

    r = await client.post(
        f"{_url(empresa.id)}/reapontar-contas-sinteticas",
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 200, r.text
    (relato,) = r.json()
    assert relato["registros_movidos"] == 1
    assert relato["sintetica_desativada"] is True

    await db.refresh(registro)
    assert registro.conta_id == real.id

    apagada = (
        await db.execute(select(PlanoConta).where(PlanoConta.id == sintetica.id))
    ).scalar_one()
    assert apagada.deleted_at is not None


@pytest.mark.asyncio
async def test_reapontar_nao_apaga_a_sintetica_com_referencia_inesperada(
    client: AsyncClient, db: AsyncSession, tenant: Tenant, usuario: Usuario,
    empresa: Empresa,
):
    """Regra apontando para a conta bancária não é situação esperada.

    E é por não ser esperada que não pode ser reescrita em silêncio: apagar a
    sintética às cegas trocaria um problema visível na exportação por uma
    referência órfã no banco. A rotina move o que conhece, deixa a conta de pé
    e diz o que sobrou.
    """
    from src.db.models import AgenciaBancaria, Regra

    csrf = await _login(client, tenant, usuario)
    real = PlanoConta(empresa_id=empresa.id, conta_numero=8, codigo="1.1.1.002",
                      descricao="BANCO", tipo="ativo", tipo_sa="A")
    agencia = AgenciaBancaria(empresa_id=empresa.id, banco_sigla="BB",
                              agencia="1623", numero="19531")
    db.add_all([real, agencia])
    await db.flush()

    sintetica = PlanoConta(
        empresa_id=empresa.id, codigo=f"1.1.B.{agencia.id.hex[:16]}",
        descricao="Conta bancária BB", tipo="ativo", tipo_sa="A",
    )
    db.add(sintetica)
    await db.flush()
    db.add(Regra(
        empresa_id=empresa.id, conta_id=sintetica.id, agencia_id=agencia.id,
        descricao="Tarifa", historico="TARIFA", historico_normalizado="tarifa",
        dc="D", tipo="automatica",
    ))
    agencia.conta_contabil_id = real.id
    await db.flush()

    r = await client.post(
        f"{_url(empresa.id)}/reapontar-contas-sinteticas",
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 200, r.text
    (relato,) = r.json()
    assert relato["sintetica_desativada"] is False
    assert relato["referencias_restantes"] == {"regras": 1}
