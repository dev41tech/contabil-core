"""Testes de integração — CRUD e hierarquia do Plano de Contas."""

from __future__ import annotations

import io
from datetime import UTC, datetime
from uuid import UUID

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import (
    AgenciaBancaria,
    Empresa,
    RegistroContabil,
    Regra,
    Tenant,
    Usuario,
)


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
async def test_codigo_e_imutavel(client, tenant, usuario, empresa):
    csrf = await _login(client, tenant, usuario)
    conta = await _criar(client, empresa.id, csrf, "8", "Conta Imutável", "ativo")

    r = await client.patch(
        _url(empresa.id, conta["id"]),
        json={"codigo": "9"},
        headers={"X-CSRF-Token": csrf},
    )

    assert r.status_code == 409


@pytest.mark.asyncio
async def test_conta_movimentada_nao_muda_tipo_nem_e_removida(
    client, db, tenant, usuario, empresa
):
    csrf = await _login(client, tenant, usuario)
    conta = await _criar(client, empresa.id, csrf, "7", "Conta Movimentada", "receita")
    agencia = AgenciaBancaria(
        empresa_id=empresa.id,
        banco_sigla="BB",
        agencia="1234",
        numero="99999",
    )
    db.add(agencia)
    await db.flush()
    db.add(
        RegistroContabil(
            empresa_id=empresa.id,
            conta_id=UUID(conta["id"]),
            agencia_id=agencia.id,
            descricao="Movimento",
            historico="Movimento",
            historico_extrato="Movimento",
            dc="C",
            tipo_regra="manual",
            valor=100,
            data_lancamento=datetime.now(UTC),
        )
    )
    await db.flush()

    alteracao = await client.patch(
        _url(empresa.id, conta["id"]),
        json={"tipo": "despesa"},
        headers={"X-CSRF-Token": csrf},
    )
    remocao = await client.delete(
        _url(empresa.id, conta["id"]), headers={"X-CSRF-Token": csrf}
    )

    assert alteracao.status_code == 409
    assert remocao.status_code == 409


@pytest.mark.asyncio
async def test_importacao_infere_hierarquia_mesmo_com_filho_antes_do_pai(
    client, tenant, usuario, empresa
):
    csrf = await _login(client, tenant, usuario)
    csv = (
        "codigo,descricao,tipo\n"
        "1.1.1,Caixa,ativo\n"
        "1,Ativo,ativo\n"
        "1.1,Ativo Circulante,ativo\n"
    )

    r = await client.post(
        _url(empresa.id, extra="/importar"),
        files={"arquivo": ("plano.csv", io.BytesIO(csv.encode()), "text/csv")},
        headers={"X-CSRF-Token": csrf},
    )
    arvore = await client.get(_url(empresa.id, extra="/arvore"))

    assert r.status_code == 200
    assert r.json()["importadas"] == 3
    raiz = arvore.json()["tree"][0]
    assert raiz["codigo"] == "1"
    assert raiz["filhos"][0]["codigo"] == "1.1"
    assert raiz["filhos"][0]["filhos"][0]["codigo"] == "1.1.1"


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


# ── Auto-preenchimento de conta_numero


@pytest.mark.asyncio
async def test_criar_conta_auto_preenche_conta_numero(
    client: AsyncClient, tenant: Tenant, usuario: Usuario, empresa: Empresa
):
    csrf = await _login(client, tenant, usuario)
    primeira = await _criar(client, empresa.id, csrf, "1", "Ativo", "ativo")
    segunda = await _criar(client, empresa.id, csrf, "2", "Passivo", "passivo")

    assert primeira["conta_numero"] == 1
    assert segunda["conta_numero"] == 2


@pytest.mark.asyncio
async def test_criar_conta_respeita_conta_numero_explicito_e_continua_sequencia(
    client: AsyncClient, tenant: Tenant, usuario: Usuario, empresa: Empresa
):
    csrf = await _login(client, tenant, usuario)
    r = await client.post(
        _url(empresa.id),
        json={"codigo": "1", "descricao": "Ativo", "tipo": "ativo", "conta_numero": 50},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 201
    assert r.json()["conta_numero"] == 50

    seguinte = await _criar(client, empresa.id, csrf, "2", "Passivo", "passivo")
    assert seguinte["conta_numero"] == 51


@pytest.mark.asyncio
async def test_importacao_auto_preenche_conta_numero_quando_ausente(
    client: AsyncClient, tenant: Tenant, usuario: Usuario, empresa: Empresa
):
    csrf = await _login(client, tenant, usuario)
    csv = "codigo,descricao,tipo\n1,Ativo,ativo\n2,Passivo,passivo\n"

    r = await client.post(
        _url(empresa.id, extra="/importar"),
        files={"arquivo": ("plano.csv", io.BytesIO(csv.encode()), "text/csv")},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 200
    assert r.json()["importadas"] == 2

    lista = await client.get(_url(empresa.id))
    numeros = {c["codigo"]: c["conta_numero"] for c in lista.json()["items"]}
    assert numeros["1"] is not None
    assert numeros["2"] is not None
    assert numeros["1"] != numeros["2"]


@pytest.mark.asyncio
async def test_importacao_preserva_conta_numero_informado_no_arquivo(
    client: AsyncClient, tenant: Tenant, usuario: Usuario, empresa: Empresa
):
    csrf = await _login(client, tenant, usuario)
    csv = "codigo,descricao,tipo,conta_numero\n1,Ativo,ativo,900\n"

    r = await client.post(
        _url(empresa.id, extra="/importar"),
        files={"arquivo": ("plano.csv", io.BytesIO(csv.encode()), "text/csv")},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 200

    lista = await client.get(_url(empresa.id))
    assert lista.json()["items"][0]["conta_numero"] == 900


# ── Exclusão em lote


@pytest.mark.asyncio
async def test_excluir_lote_selecao_multipla(
    client: AsyncClient, tenant: Tenant, usuario: Usuario, empresa: Empresa
):
    csrf = await _login(client, tenant, usuario)
    a = await _criar(client, empresa.id, csrf, "1", "Ativo", "ativo")
    b = await _criar(client, empresa.id, csrf, "2", "Passivo", "passivo")
    c = await _criar(client, empresa.id, csrf, "3", "Resultado", "resultado")

    r = await client.post(
        _url(empresa.id, extra="/excluir-lote"),
        json={"ids": [a["id"], b["id"]]},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["removidas"] == 2
    assert body["bloqueadas"] == []

    lista = await client.get(_url(empresa.id))
    codigos = {item["codigo"] for item in lista.json()["items"]}
    assert codigos == {"3"}
    assert c["id"]  # sanity: conta c segue existindo


@pytest.mark.asyncio
async def test_excluir_lote_todas_remove_hierarquia_completa(
    client: AsyncClient, tenant: Tenant, usuario: Usuario, empresa: Empresa
):
    csrf = await _login(client, tenant, usuario)
    pai = await _criar(client, empresa.id, csrf, "1", "Ativo", "ativo")
    filho = await _criar(client, empresa.id, csrf, "1.1", "Ativo Circ.", "ativo", pai["id"])
    await _criar(client, empresa.id, csrf, "1.1.1", "Caixa", "ativo", filho["id"])

    r = await client.post(
        _url(empresa.id, extra="/excluir-lote"),
        json={"todas": True},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["removidas"] == 3
    assert body["bloqueadas"] == []

    lista = await client.get(_url(empresa.id))
    assert lista.json()["items"] == []


@pytest.mark.asyncio
async def test_criar_conta_com_codigo_de_conta_excluida_nao_colide(
    client: AsyncClient, tenant: Tenant, usuario: Usuario, empresa: Empresa
):
    """"Excluir Todas" é soft delete — o código não pode ficar soterrado.

    Constraint original era global (empresa_id, codigo), sem excluir
    deleted_at; reimportar o mesmo plano depois de uma exclusão em massa
    esbarrava em "duplicate key" pra cada código que já existiu.
    """
    csrf = await _login(client, tenant, usuario)
    original = await _criar(client, empresa.id, csrf, "9", "Conta Original", "ativo")

    r = await client.post(
        _url(empresa.id, extra="/excluir-lote"),
        json={"ids": [original["id"]]},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 200
    assert r.json()["removidas"] == 1

    recriada = await _criar(client, empresa.id, csrf, "9", "Conta Recriada", "passivo")
    assert recriada["id"] != original["id"]
    assert recriada["codigo"] == "9"


@pytest.mark.asyncio
async def test_excluir_lote_bloqueada_por_movimentacao_nao_impede_as_demais(
    client: AsyncClient,
    db: AsyncSession,
    tenant: Tenant,
    usuario: Usuario,
    empresa: Empresa,
):
    csrf = await _login(client, tenant, usuario)
    movimentada = await _criar(client, empresa.id, csrf, "7", "Conta Movimentada", "receita")
    livre = await _criar(client, empresa.id, csrf, "8", "Conta Livre", "receita")

    agencia = AgenciaBancaria(
        empresa_id=empresa.id, banco_sigla="BB", agencia="1234", numero="99999"
    )
    db.add(agencia)
    await db.flush()
    db.add(
        RegistroContabil(
            empresa_id=empresa.id,
            conta_id=UUID(movimentada["id"]),
            agencia_id=agencia.id,
            descricao="Movimento",
            historico="Movimento",
            historico_extrato="Movimento",
            dc="C",
            tipo_regra="manual",
            valor=100,
            data_lancamento=datetime.now(UTC),
        )
    )
    await db.flush()

    r = await client.post(
        _url(empresa.id, extra="/excluir-lote"),
        json={"ids": [movimentada["id"], livre["id"]]},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["removidas"] == 1
    assert len(body["bloqueadas"]) == 1
    assert body["bloqueadas"][0]["codigo"] == "7"
    assert "movimenta" in body["bloqueadas"][0]["erro"].lower()

    lista = await client.get(_url(empresa.id))
    codigos = {item["codigo"] for item in lista.json()["items"]}
    assert codigos == {"7"}


@pytest.mark.asyncio
async def test_excluir_lote_exige_ids_ou_todas(
    client: AsyncClient, tenant: Tenant, usuario: Usuario, empresa: Empresa
):
    csrf = await _login(client, tenant, usuario)
    r = await client.post(
        _url(empresa.id, extra="/excluir-lote"),
        json={"ids": []},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 422
