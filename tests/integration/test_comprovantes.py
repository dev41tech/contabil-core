"""Testes de integração — Comprovantes de Pagamento.

O comprovante é a prova de que o pagamento saiu. O que precisa estar certo é o
vínculo com a transação bancária: associar duas vezes, associar a transação de
outra empresa, ou o arquivo voltar como tipo errado são os jeitos de a conciliação
apontar para a prova errada.
"""

from __future__ import annotations

import base64
import io
import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import AgenciaBancaria, Comprovante, Empresa, Tenant, Transacao, Usuario

_PDF_BYTES = b"%PDF-1.4 fake comprovante"


async def _login(client: AsyncClient, tenant: Tenant, usuario: Usuario) -> str:
    r = await client.post(
        "/api/v1/auth/login",
        json={"tenant_id": str(tenant.id), "email": usuario.email, "senha": "senha_segura_123"},
    )
    assert r.status_code == 200
    return r.json()["csrf_token"]


def _url(empresa_id, sufixo: str = "") -> str:
    return f"/api/v1/empresas/{empresa_id}/comprovantes{sufixo}"


@pytest_asyncio.fixture
async def agencia(db: AsyncSession, empresa: Empresa) -> AgenciaBancaria:
    a = AgenciaBancaria(empresa_id=empresa.id, banco_sigla="ITAU", agencia="7285", numero="12287")
    db.add(a)
    await db.flush()
    return a


@pytest_asyncio.fixture
async def transacao(db: AsyncSession, empresa: Empresa, agencia: AgenciaBancaria) -> Transacao:
    t = Transacao(
        empresa_id=empresa.id,
        agencia_id=agencia.id,
        data=datetime(2026, 3, 10, tzinfo=UTC),
        valor=1_500,
        historico="PAGAMENTO FORNECEDOR ACME",
        dc="D",
        hash_dedup="hash_comprovante_1",
    )
    db.add(t)
    await db.flush()
    return t


async def _criar(client: AsyncClient, empresa: Empresa, csrf: str, **overrides) -> dict:
    payload = {
        "favorecido": "ACME SERVICOS LTDA",
        "valor_pago": 1_500,
        "data_pagamento": "2026-03-10T00:00:00Z",
    }
    payload.update(overrides)
    r = await client.post(_url(empresa.id), json=payload, headers={"X-CSRF-Token": csrf})
    assert r.status_code == 201, r.text
    return r.json()


# ── acesso ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_listar_sem_autenticacao_rejeita(client: AsyncClient, empresa: Empresa):
    assert (await client.get(_url(empresa.id))).status_code == 401


@pytest.mark.asyncio
async def test_criar_sem_csrf_rejeita(
    client: AsyncClient, tenant: Tenant, usuario: Usuario, empresa: Empresa
):
    await _login(client, tenant, usuario)
    r = await client.post(_url(empresa.id), json={"valor_pago": 100})
    assert r.status_code == 403


# ── CRUD ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_criar_e_obter(
    client: AsyncClient, tenant: Tenant, usuario: Usuario, empresa: Empresa
):
    csrf = await _login(client, tenant, usuario)
    criado = await _criar(client, empresa, csrf)

    assert criado["favorecido"] == "ACME SERVICOS LTDA"
    assert criado["tem_arquivo"] is False

    r = await client.get(_url(empresa.id, f"/{criado['id']}"))
    assert r.status_code == 200
    assert r.json()["id"] == criado["id"]


@pytest.mark.asyncio
async def test_valor_pago_precisa_ser_positivo(
    client: AsyncClient, tenant: Tenant, usuario: Usuario, empresa: Empresa
):
    csrf = await _login(client, tenant, usuario)
    r = await client.post(
        _url(empresa.id),
        json={"valor_pago": 0, "favorecido": "X"},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_obter_inexistente_retorna_404(
    client: AsyncClient, tenant: Tenant, usuario: Usuario, empresa: Empresa
):
    await _login(client, tenant, usuario)
    r = await client.get(_url(empresa.id, f"/{uuid.uuid4()}"))
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_deletar_e_soft_delete(
    client: AsyncClient, tenant: Tenant, usuario: Usuario, empresa: Empresa
):
    """Comprovante é prova de pagamento — apagar de verdade perderia o histórico."""
    csrf = await _login(client, tenant, usuario)
    criado = await _criar(client, empresa, csrf)

    r = await client.delete(_url(empresa.id, f"/{criado['id']}"), headers={"X-CSRF-Token": csrf})
    assert r.status_code == 204

    assert (await client.get(_url(empresa.id, f"/{criado['id']}"))).status_code == 404
    assert (await client.get(_url(empresa.id))).json()["total"] == 0


# ── associação com transação ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_associar_e_desassociar(
    client: AsyncClient, tenant: Tenant, usuario: Usuario, empresa: Empresa, transacao: Transacao
):
    csrf = await _login(client, tenant, usuario)
    criado = await _criar(client, empresa, csrf)

    r = await client.post(
        _url(empresa.id, f"/{criado['id']}/associar"),
        json={"transacao_id": str(transacao.id)},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 200
    assert r.json()["transacao_id"] == str(transacao.id)

    r = await client.delete(
        _url(empresa.id, f"/{criado['id']}/associar"), headers={"X-CSRF-Token": csrf}
    )
    assert r.status_code == 200
    assert r.json()["transacao_id"] is None


@pytest.mark.asyncio
async def test_associar_duas_vezes_exige_desassociar_antes(
    client: AsyncClient, tenant: Tenant, usuario: Usuario, empresa: Empresa, transacao: Transacao
):
    """Sem isso, um comprovante trocaria de transação em silêncio."""
    csrf = await _login(client, tenant, usuario)
    criado = await _criar(client, empresa, csrf)
    payload = {"transacao_id": str(transacao.id)}

    await client.post(
        _url(empresa.id, f"/{criado['id']}/associar"), json=payload, headers={"X-CSRF-Token": csrf}
    )
    r = await client.post(
        _url(empresa.id, f"/{criado['id']}/associar"), json=payload, headers={"X-CSRF-Token": csrf}
    )
    assert r.status_code == 409


@pytest.mark.asyncio
async def test_desassociar_sem_associacao_retorna_409(
    client: AsyncClient, tenant: Tenant, usuario: Usuario, empresa: Empresa
):
    csrf = await _login(client, tenant, usuario)
    criado = await _criar(client, empresa, csrf)

    r = await client.delete(
        _url(empresa.id, f"/{criado['id']}/associar"), headers={"X-CSRF-Token": csrf}
    )
    assert r.status_code == 409


@pytest.mark.asyncio
async def test_associar_transacao_de_outra_empresa_rejeita(
    client: AsyncClient,
    db: AsyncSession,
    tenant: Tenant,
    usuario: Usuario,
    empresa: Empresa,
):
    """O vínculo não pode atravessar a fronteira da empresa."""
    outra = Empresa(
        tenant_id=tenant.id,
        razao_social="OUTRA EMPRESA LTDA",
        cnpj="52.540.787/0001-88",
        regime_tributario="lucro_real",
    )
    db.add(outra)
    await db.flush()

    ag = AgenciaBancaria(empresa_id=outra.id, banco_sigla="BB", agencia="0001", numero="12345")
    db.add(ag)
    await db.flush()

    t_outra = Transacao(
        empresa_id=outra.id,
        agencia_id=ag.id,
        data=datetime(2026, 3, 11, tzinfo=UTC),
        valor=800,
        historico="MOVIMENTO DA OUTRA",
        dc="D",
        hash_dedup="hash_outra_empresa_comp",
    )
    db.add(t_outra)
    await db.flush()

    csrf = await _login(client, tenant, usuario)
    criado = await _criar(client, empresa, csrf)

    r = await client.post(
        _url(empresa.id, f"/{criado['id']}/associar"),
        json={"transacao_id": str(t_outra.id)},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_criar_com_transacao_de_outra_empresa_rejeita(
    client: AsyncClient, tenant: Tenant, usuario: Usuario, empresa: Empresa
):
    csrf = await _login(client, tenant, usuario)
    r = await client.post(
        _url(empresa.id),
        json={"valor_pago": 100, "transacao_id": str(uuid.uuid4())},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_criar_com_agencia_de_outra_empresa_rejeita(
    client: AsyncClient,
    db: AsyncSession,
    tenant: Tenant,
    usuario: Usuario,
    empresa: Empresa,
):
    outra = Empresa(
        tenant_id=tenant.id,
        razao_social="OUTRA EMPRESA AGENCIA LTDA",
        cnpj="38.896.497/0001-08",
        regime_tributario="lucro_real",
    )
    db.add(outra)
    await db.flush()
    agencia_outra = AgenciaBancaria(
        empresa_id=outra.id,
        banco_sigla="BB",
        agencia="0001",
        numero="54321",
    )
    db.add(agencia_outra)
    await db.flush()

    csrf = await _login(client, tenant, usuario)
    r = await client.post(
        _url(empresa.id),
        json={"valor_pago": 100, "agencia_id": str(agencia_outra.id)},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 404


# ── filtros ───────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_filtro_por_status_de_associacao(
    client: AsyncClient, tenant: Tenant, usuario: Usuario, empresa: Empresa, transacao: Transacao
):
    csrf = await _login(client, tenant, usuario)
    associado = await _criar(client, empresa, csrf, favorecido="COM VINCULO")
    await _criar(client, empresa, csrf, favorecido="SEM VINCULO")

    await client.post(
        _url(empresa.id, f"/{associado['id']}/associar"),
        json={"transacao_id": str(transacao.id)},
        headers={"X-CSRF-Token": csrf},
    )

    r = await client.get(_url(empresa.id), params={"status": "associado"})
    assert r.json()["total"] == 1
    assert r.json()["items"][0]["favorecido"] == "COM VINCULO"

    r = await client.get(_url(empresa.id), params={"status": "nao_associado"})
    assert r.json()["total"] == 1
    assert r.json()["items"][0]["favorecido"] == "SEM VINCULO"


@pytest.mark.asyncio
async def test_filtro_por_transacao(
    client: AsyncClient, tenant: Tenant, usuario: Usuario, empresa: Empresa, transacao: Transacao
):
    csrf = await _login(client, tenant, usuario)
    criado = await _criar(client, empresa, csrf)
    await client.post(
        _url(empresa.id, f"/{criado['id']}/associar"),
        json={"transacao_id": str(transacao.id)},
        headers={"X-CSRF-Token": csrf},
    )

    r = await client.get(_url(empresa.id), params={"transacao_id": str(transacao.id)})
    assert r.json()["total"] == 1


# ── download do arquivo ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_baixar_arquivo_devolve_bytes_e_content_type(
    client: AsyncClient, tenant: Tenant, usuario: Usuario, empresa: Empresa
):
    csrf = await _login(client, tenant, usuario)
    criado = await _criar(
        client,
        empresa,
        csrf,
        arquivo_nome="comprovante.pdf",
        arquivo_base64=base64.b64encode(_PDF_BYTES).decode(),
    )
    assert criado["tem_arquivo"] is True

    r = await client.get(_url(empresa.id, f"/{criado['id']}/arquivo"))
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/pdf"
    assert r.content == _PDF_BYTES


@pytest.mark.asyncio
async def test_content_type_segue_a_extensao(
    client: AsyncClient, tenant: Tenant, usuario: Usuario, empresa: Empresa
):
    csrf = await _login(client, tenant, usuario)
    conteudo = base64.b64encode(b"\x89PNG fake").decode()

    for nome, esperado in [
        ("recibo.png", "image/png"),
        ("recibo.jpg", "image/jpeg"),
        ("recibo.desconhecido", "application/octet-stream"),
    ]:
        criado = await _criar(
            client, empresa, csrf, arquivo_nome=nome, arquivo_base64=conteudo
        )
        r = await client.get(_url(empresa.id, f"/{criado['id']}/arquivo"))
        assert r.headers["content-type"] == esperado, nome


@pytest.mark.asyncio
async def test_baixar_arquivo_de_comprovante_sem_anexo_retorna_404(
    client: AsyncClient, tenant: Tenant, usuario: Usuario, empresa: Empresa
):
    csrf = await _login(client, tenant, usuario)
    criado = await _criar(client, empresa, csrf)

    r = await client.get(_url(empresa.id, f"/{criado['id']}/arquivo"))
    assert r.status_code == 404


# ── extração automática de PDF ──────────────────────────────────────────────


async def _extrair_pdf(client, empresa, csrf) -> object:
    return await client.post(
        _url(empresa.id, "/extrair-pdf"),
        files={"arquivo": ("comprovante.pdf", io.BytesIO(_PDF_BYTES), "application/pdf")},
        headers={"X-CSRF-Token": csrf},
    )


@pytest.mark.asyncio
async def test_extrair_pdf_devolve_campos_pre_preenchidos_sem_persistir(
    client: AsyncClient, tenant: Tenant, usuario: Usuario, empresa: Empresa, monkeypatch
):
    from src.domain.comprovantes import pdf_parser as pdf_parser_module

    monkeypatch.setattr(
        pdf_parser_module,
        "parse_pdf",
        lambda conteudo: pdf_parser_module.ComprovantePDF(
            favorecido="ACME SERVICOS LTDA",
            cpf_cnpj="12.345.678/0001-95",
            valor_pago=Decimal("1500.00"),
            data_pagamento=datetime(2026, 3, 10, tzinfo=UTC),
            confianca="regex",
        ),
    )

    csrf = await _login(client, tenant, usuario)
    r = await _extrair_pdf(client, empresa, csrf)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["favorecido"] == "ACME SERVICOS LTDA"
    assert body["cpf_cnpj"] == "12.345.678/0001-95"
    assert body["confianca"] == "regex"

    # nada foi persistido
    lista = await client.get(_url(empresa.id))
    assert lista.json()["total"] == 0


@pytest.mark.asyncio
async def test_extrair_pdf_falha_de_extracao_retorna_422(
    client: AsyncClient, tenant: Tenant, usuario: Usuario, empresa: Empresa, monkeypatch
):
    from src.domain.comprovantes import pdf_parser as pdf_parser_module

    def _raise(conteudo: bytes):
        raise pdf_parser_module.PDFParseError(
            "Não foi possível identificar o valor pago neste comprovante."
        )

    monkeypatch.setattr(pdf_parser_module, "parse_pdf", _raise)

    csrf = await _login(client, tenant, usuario)
    r = await _extrair_pdf(client, empresa, csrf)
    assert r.status_code == 422
    assert "PDF inválido" in r.json()["message"]


@pytest.mark.asyncio
async def test_extrair_pdf_sem_csrf_rejeita(
    client: AsyncClient, tenant: Tenant, usuario: Usuario, empresa: Empresa
):
    await _login(client, tenant, usuario)
    r = await client.post(
        _url(empresa.id, "/extrair-pdf"),
        files={"arquivo": ("c.pdf", io.BytesIO(_PDF_BYTES), "application/pdf")},
    )
    assert r.status_code == 403
