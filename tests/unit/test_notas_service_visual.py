"""NotaService.importar_visual — persistência de nota vinda de PDF/imagem (OCR)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import Empresa, NotaFiscal, Tenant
from src.domain.notas import visual_parser
from src.domain.notas.service import NotaService
from src.domain.notas.xml_parser import NotaParseada

# CNPJ próprio (não o hardcoded em tests/conftest.py) para não colidir com a
# constraint de unicidade de tenants.cnpj quando outro arquivo de teste do
# suite deixa um tenant "41.000.000/0001-79" comitado (fora do escopo deste
# arquivo consertar — só evitando a colisão aqui).
_CNPJ_EMPRESA = "12.345.678/0001-95"


@pytest_asyncio.fixture
async def _tenant_visual(db: AsyncSession) -> Tenant:
    t = Tenant(nome="Tenant Teste Visual", cnpj="11.444.777/0001-61")
    db.add(t)
    await db.flush()
    return t


@pytest_asyncio.fixture
async def _empresa_visual(db: AsyncSession, _tenant_visual: Tenant) -> Empresa:
    e = Empresa(
        tenant_id=_tenant_visual.id,
        razao_social="EMPRESA TESTE VISUAL LTDA",
        cnpj=_CNPJ_EMPRESA,
        regime_tributario="simples_nacional",
    )
    db.add(e)
    await db.flush()
    return e


def _nota_parseada(**overrides) -> NotaParseada:
    base = dict(
        tipo="nfe",
        numero="999",
        serie="1",
        cnpj_emitente=_CNPJ_EMPRESA,
        nome_emitente="Fornecedor Teste",
        cnpj_destinatario=None,
        valor=Decimal("100.00"),
        data_emissao=datetime(2026, 1, 5, tzinfo=UTC),
        chave_acesso=None,
        observacao="Extraído por OCR/Vision.",
    )
    base.update(overrides)
    return NotaParseada(**base)


@pytest.mark.asyncio
async def test_importar_visual_pdf_persiste_com_origem_ocr(
    db: AsyncSession, _empresa_visual: Empresa, monkeypatch
):
    monkeypatch.setattr(visual_parser, "parse_pdf", lambda conteudo: _nota_parseada())

    svc = NotaService(db=db, empresa_id=_empresa_visual.id)
    resultado = await svc.importar_visual(b"fake-pdf-bytes", "nota.pdf", ".pdf")

    assert resultado.importadas == 1
    assert resultado.erros == []

    nota = (
        await db.execute(select(NotaFiscal).where(NotaFiscal.empresa_id == _empresa_visual.id))
    ).scalar_one()
    assert nota.origem == "ocr"
    assert nota.numero == "999"


@pytest.mark.asyncio
async def test_importar_visual_imagem_persiste_com_origem_ocr(
    db: AsyncSession, _empresa_visual: Empresa, monkeypatch
):
    monkeypatch.setattr(
        visual_parser, "parse_imagem", lambda conteudo, content_type: _nota_parseada(numero="888")
    )

    svc = NotaService(db=db, empresa_id=_empresa_visual.id)
    resultado = await svc.importar_visual(b"fake-png-bytes", "nota.png", ".png")

    assert resultado.importadas == 1
    nota = (
        await db.execute(select(NotaFiscal).where(NotaFiscal.empresa_id == _empresa_visual.id))
    ).scalar_one()
    assert nota.origem == "ocr"
    assert nota.numero == "888"


@pytest.mark.asyncio
async def test_importar_visual_rejeita_cnpj_que_nao_e_da_empresa(
    db: AsyncSession, _empresa_visual: Empresa, monkeypatch
):
    monkeypatch.setattr(
        visual_parser,
        "parse_pdf",
        lambda conteudo: _nota_parseada(cnpj_emitente="11.222.333/0001-81"),
    )

    svc = NotaService(db=db, empresa_id=_empresa_visual.id)
    resultado = await svc.importar_visual(b"fake-pdf-bytes", "nota.pdf", ".pdf")

    assert resultado.importadas == 0
    assert "CNPJ da empresa" in resultado.erros[0]


@pytest.mark.asyncio
async def test_importar_visual_propaga_erro_do_parser(
    db: AsyncSession, _empresa_visual: Empresa, monkeypatch
):
    def _raise(conteudo: bytes) -> NotaParseada:
        raise visual_parser.VisualParseError("Não foi possível extrair os dados da nota.")

    monkeypatch.setattr(visual_parser, "parse_pdf", _raise)

    svc = NotaService(db=db, empresa_id=_empresa_visual.id)
    resultado = await svc.importar_visual(b"fake-pdf-bytes", "nota.pdf", ".pdf")

    assert resultado.importadas == 0
    assert "Não foi possível extrair os dados da nota." in resultado.erros[0]
