"""Testes de integração — CRUD e hierarquia do Plano de Contas."""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import AgenciaBancaria, Empresa, Regra, Tenant, Usuario


# ── Helpers


async def _login(client: AsyncClient, tenant: Tenant, usuario: Usuario) -> str:
    r = await client.post(
        "/api/v1/auth/login",
        json={"tenant_id": str(tenant.id), "email": usuario.email, "senha": "senha_segura_123"},
    )
    assert r.status_code == 200
    return r.json()["csrf_token"]


def _url(empresa_id, conta_id=None, extra="") -> str:
    base = f"/api/v1/empresas/{empresa_id}/plano-contas"
    if conta_id:
        return f"{base}/{conta_id}{extra}"
    return f"{base}{extra}"


async def _criar(client, empresa_id, csrf, codigo, descricao, tipo="ativo", pai_id=None):
    body = {"codigo": codigo, "descricao": descricao, "tipo": tipo}
    if pai_id:
        body["pai_id"] = str(pai_id)
    r = await client.post(_url(empresa_id), json=body, headers={"X-CSRF-Token": csrf})
    assert r.status_code == 201, r.json()
    return r.json()


# ── CRUD básico


@pytest.mark.asyncio
async def test_listar_plano_vazio(
    client: AsyncClient, tenant: Tenant, usuario: Usuario, empresa: Empresa
):
    await _login(client, tenant, usuario)
    r = await client.get(_url(empresa.id))
    assert r.status_code == 200
    assert r.json()["items"] == []
    assert r.json()["total"] == 0


@pytest.mark.asyncio
async def test_criar_conta_raiz(
    client: AsyncClient, tenant: Tenant, usuario: Usuario, empresa: Empresa
):
    csrf = await _login(client, tenant, usuario)
    r = await client.post(
        _url(empresa.id),
        json={"codigo": "1", "descricao": "Ativo", "tipo": "ativo"},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 201
    body = r.json()
    assert body["codigo"] == "1"
    assert body["descricao"] == "Ativo"
    assert body["tipo"] == "ativo"
    assert body["pai_id"] is None
    assert body["nivel"] == 1
    assert body["empresa_id"] == str(empresa.id)


@pytest.mark.asyncio
async def test_criar_conta_filho(
    client: AsyncClient, tenant: Tenant, usuario: Usuario, empresa: Empresa
):
    csrf = await _login(client, tenant, usuario)
    pai = await _criar(client, empresa.id, csrf, "1", "Ativo", "ativo")

    r = await client.post(
        _url(empresa.id),
        json={"codigo": "1.1", "descricao": "Ativo Circulante", "tipo": "ativo", "pai_id": pai["id"]},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 201
    body = r.json()
    assert body["pai_id"] == pai["id"]
    assert body["nivel"] == 2


@pytest.mark.asyncio
async def test_obter_conta(
    client: AsyncClient, tenant: Tenant, usuario: Usuario, empresa: Empresa
):
    csrf = await _login(client, tenant, usuario)
    criada = await _criar(client, empresa.id, csrf, "2", "Passivo", "passivo")

    r = await client.get(_url(empresa.id, criada["id"]))
    assert r.status_code == 200
    assert r.json()["id"] == criada["id"]


@pytest.mark.asyncio
async def test_atualizar_descricao(
    client: AsyncClient, tenant: Tenant, usuario: Usuario, empresa: Empresa
):
    csrf = await _login(client, tenant, usuario)
    criada = await _criar(client, empresa.id, csrf, "3", "Receita Bruta", "receita")

    r = await client.patch(
        _url(empresa.id, criada["id"]),
        json={"descricao": "Receita Operacional Bruta"},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 200
    assert r.json()["descricao"] == "Receita Operacional Bruta"
    assert r.json()["codigo"] == "3"  # código imutável


@pytest.mark.asyncio
async def test_remover_conta_folha(
    client: AsyncClient, tenant: Tenant, usuario: Usuario, empresa: Empresa
):
    csrf = await _login(client, tenant, usuario)
    criada = await _criar(client, empresa.id, csrf, "4", "Despesa", "despesa")

    r = await client.delete(_url(empresa.id, criada["id"]), headers={"X-CSRF-Token": csrf})
    assert r.status_code == 204

    # Não aparece mais na listagem
    lista = await client.get(_url(empresa.id))
    ids = [c["id"] for c in lista.json()["items"]]
    assert criada["id"] not in ids


# ── Regras de hierarquia


@pytest.mark.asyncio
async def test_codigo_filho_deve_ter_prefixo_do_pai(
    client: AsyncClient, tenant: Tenant, usuario: Usuario, empresa: Empresa
):
    csrf = await _login(client, tenant, usuario)
    pai = await _criar(client, empresa.id, csrf, "1", "Ativo", "ativo")

    # Código "2.1" não é filho de "1"
    r = await client.post(
        _url(empresa.id),
        json={"codigo": "2.1", "descricao": "Sub de 1?", "tipo": "ativo", "pai_id": pai["id"]},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 422
    assert "filho" in r.json()["message"].lower()


@pytest.mark.asyncio
async def test_nivel_maximo_bloqueado(
    client: AsyncClient, tenant: Tenant, usuario: Usuario, empresa: Empresa
):
    csrf = await _login(client, tenant, usuario)

    # Cria cadeia até o nível 5
    pai = await _criar(client, empresa.id, csrf, "1", "N1", "ativo")
    pai = await _criar(client, empresa.id, csrf, "1.1", "N2", "ativo", pai["id"])
    pai = await _criar(client, empresa.id, csrf, "1.1.1", "N3", "ativo", pai["id"])
    pai = await _criar(client, empresa.id, csrf, "1.1.1.1", "N4", "ativo", pai["id"])
    pai = await _criar(client, empresa.id, csrf, "1.1.1.1.1", "N5", "ativo", pai["id"])

    # Nível 6 deve ser bloqueado
    r = await client.post(
        _url(empresa.id),
        json={"codigo": "1.1.1.1.1.1", "descricao": "N6", "tipo": "ativo", "pai_id": pai["id"]},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 422
    assert "nível máximo" in r.json()["message"].lower()


@pytest.mark.asyncio
async def test_remover_conta_com_filhos_bloqueado(
    client: AsyncClient, tenant: Tenant, usuario: Usuario, empresa: Empresa
):
    csrf = await _login(client, tenant, usuario)
    pai = await _criar(client, empresa.id, csrf, "1", "Ativo", "ativo")
    await _criar(client, empresa.id, csrf, "1.1", "Ativo Circ.", "ativo", pai["id"])

    r = await client.delete(_url(empresa.id, pai["id"]), headers={"X-CSRF-Token": csrf})
    assert r.status_code == 409
    assert "subconta" in r.json()["message"].lower()


@pytest.mark.asyncio
async def test_remover_conta_pai_apos_remover_filho(
    client: AsyncClient, tenant: Tenant, usuario: Usuario, empresa: Empresa
):
    csrf = await _login(client, tenant, usuario)
    pai = await _criar(client, empresa.id, csrf, "5", "Custo", "custo")
    filho = await _criar(client, empresa.id, csrf, "5.1", "CMV", "custo", pai["id"])

    # Remove filho primeiro
    await client.delete(_url(empresa.id, filho["id"]), headers={"X-CSRF-Token": csrf})

    # Agora pode remover o pai
    r = await client.delete(_url(empresa.id, pai["id"]), headers={"X-CSRF-Token": csrf})
    assert r.status_code == 204


# ── Código duplicado


@pytest.mark.asyncio
async def test_codigo_duplicado_rejeita(
    client: AsyncClient, tenant: Tenant, usuario: Usuario, empresa: Empresa
):
    csrf = await _login(client, tenant, usuario)
    await _criar(client, empresa.id, csrf, "1", "Ativo", "ativo")

    r = await client.post(
        _url(empresa.id),
        json={"codigo": "1", "descricao": "Outro Ativo", "tipo": "passivo"},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 409
    assert r.json()["error"] == "CONFLICT"


# ── Listagem ordenada


@pytest.mark.asyncio
async def test_listagem_ordenada_por_codigo(
    client: AsyncClient, tenant: Tenant, usuario: Usuario, empresa: Empresa
):
    csrf = await _login(client, tenant, usuario)
    # Cria fora de ordem
    await _criar(client, empresa.id, csrf, "3", "Resultado", "resultado")
    await _criar(client, empresa.id, csrf, "1", "Ativo", "ativo")
    await _criar(client, empresa.id, csrf, "2", "Passivo", "passivo")

    r = await client.get(_url(empresa.id))
    codigos = [c["codigo"] for c in r.json()["items"]]
    assert codigos == ["1", "2", "3"]


# ── Árvore hierárquica


@pytest.mark.asyncio
async def test_arvore_retorna_estrutura_correta(
    client: AsyncClient, tenant: Tenant, usuario: Usuario, empresa: Empresa
):
    csrf = await _login(client, tenant, usuario)

    # Estrutura:
    # 1 Ativo
    #   1.1 Ativo Circulante
    #     1.1.1 Caixa
    #   1.2 Ativo Imobilizado
    # 2 Passivo

    a1 = await _criar(client, empresa.id, csrf, "1", "Ativo", "ativo")
    a11 = await _criar(client, empresa.id, csrf, "1.1", "Ativo Circulante", "ativo", a1["id"])
    await _criar(client, empresa.id, csrf, "1.1.1", "Caixa", "ativo", a11["id"])
    await _criar(client, empresa.id, csrf, "1.2", "Ativo Imobilizado", "ativo", a1["id"])
    await _criar(client, empresa.id, csrf, "2", "Passivo", "passivo")

    r = await client.get(_url(empresa.id, extra="/arvore"))
    assert r.status_code == 200

    tree = r.json()["tree"]
    assert len(tree) == 2  # 2 raízes: 1 e 2

    ativo = tree[0]
    assert ativo["codigo"] == "1"
    assert len(ativo["filhos"]) == 2  # 1.1 e 1.2

    circ = next(f for f in ativo["filhos"] if f["codigo"] == "1.1")
    assert len(circ["filhos"]) == 1  # 1.1.1
    assert circ["filhos"][0]["codigo"] == "1.1.1"
    assert circ["filhos"][0]["filhos"] == []  # folha

    assert r.json()["total"] == 5


# ── Bloqueio por referência em regras


@pytest.mark.asyncio
async def test_remover_conta_referenciada_em_regra_bloqueado(
    client: AsyncClient,
    tenant: Tenant,
    usuario: Usuario,
    empresa: Empresa,
    db: AsyncSession,
):
    csrf = await _login(client, tenant, usuario)
    conta = await _criar(client, empresa.id, csrf, "1", "Ativo", "ativo")

    # Cria agência e regra diretamente no banco para vincular à conta
    from src.db.models import AgenciaBancaria, PlanoConta, Regra
    import uuid as uuid_mod

    agencia = AgenciaBancaria(
        empresa_id=empresa.id,
        banco_sigla="BB",
        agencia="0001",
        numero="12345",
    )
    db.add(agencia)
    await db.flush()

    from sqlalchemy import select
    conta_obj = (
        await db.execute(
            select(PlanoConta).where(PlanoConta.id == uuid_mod.UUID(conta["id"]))
        )
    ).scalar_one()

    regra = Regra(
        empresa_id=empresa.id,
        conta_id=conta_obj.id,
        agencia_id=agencia.id,
        descricao="Regra teste",
        historico="PAGAMENTO",
        dc="D",
        tipo="automatica",
    )
    db.add(regra)
    await db.flush()

    r = await client.delete(_url(empresa.id, conta["id"]), headers={"X-CSRF-Token": csrf})
    assert r.status_code == 409
    assert "regra" in r.json()["message"].lower()


# ── Isolamento de empresa


@pytest.mark.asyncio
async def test_conta_de_outra_empresa_nao_visivel(
    client: AsyncClient,
    db: AsyncSession,
    tenant: Tenant,
    usuario: Usuario,
    empresa: Empresa,
):
    from src.db.models import Empresa as EmpresaModel
    empresa2 = EmpresaModel(
        tenant_id=tenant.id,
        razao_social="Empresa 2 LTDA",
        cnpj="77.666.555/0001-00",
        regime_tributario="lucro_real",
    )
    db.add(empresa2)
    await db.flush()

    csrf = await _login(client, tenant, usuario)
    await _criar(client, empresa.id, csrf, "1", "Ativo", "ativo")

    r = await client.get(_url(empresa2.id))
    assert r.status_code == 200
    assert r.json()["items"] == []
