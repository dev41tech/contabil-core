"""Testes de integração — limite de tamanho de upload.

Os parsers carregam o arquivo inteiro em memória, então o limite é o que separa
um upload grande de derrubar o worker. Duas barreiras: o middleware corta pelo
`Content-Length` declarado, e `ler_upload_limitado` conta os bytes que chegaram
para o caso de o header não existir.
"""

from __future__ import annotations

import io

import pytest
from fastapi import UploadFile

from src.api.uploads import ler_upload_limitado
from src.core.config import get_settings
from src.core.errors import PayloadTooLargeError


async def _login(client, tenant, usuario) -> str:
    r = await client.post(
        "/api/v1/auth/login",
        json={"tenant_id": str(tenant.id), "email": usuario.email, "senha": "senha_segura_123"},
    )
    assert r.status_code == 200
    return r.json()["csrf_token"]


@pytest.mark.asyncio
async def test_upload_acima_do_limite_retorna_413(client, tenant, usuario, empresa):
    csrf = await _login(client, tenant, usuario)
    limite = get_settings().max_upload_bytes
    gordo = b"x" * (limite + 1)

    r = await client.post(
        f"/api/v1/empresas/{empresa.id}/concilpro/upload",
        files={"file": ("razao.pdf", io.BytesIO(gordo), "application/pdf")},
        headers={"X-CSRF-Token": csrf},
    )

    assert r.status_code == 413
    body = r.json()
    assert body["error"] == "PAYLOAD_TOO_LARGE"
    assert body["details"]["limite_bytes"] == limite


@pytest.mark.asyncio
async def test_upload_dentro_do_limite_passa_pelo_middleware(client, tenant, usuario, empresa):
    """Arquivo pequeno não é barrado — o erro que vier é do parser, não do limite."""
    csrf = await _login(client, tenant, usuario)

    r = await client.post(
        f"/api/v1/empresas/{empresa.id}/concilpro/upload",
        files={"file": ("razao.pdf", io.BytesIO(b"conteudo pequeno"), "application/pdf")},
        headers={"X-CSRF-Token": csrf},
    )

    assert r.status_code != 413


@pytest.mark.asyncio
async def test_ler_upload_limitado_corta_sem_content_length():
    """Sem `Content-Length` o middleware não vê nada — a contagem real precisa cortar."""
    conteudo = b"y" * 5000
    file = UploadFile(filename="grande.csv", file=io.BytesIO(conteudo))

    with pytest.raises(PayloadTooLargeError) as exc:
        await ler_upload_limitado(file, max_bytes=1024)

    assert exc.value.http_status == 413
    assert exc.value.details["limite_bytes"] == 1024


@pytest.mark.asyncio
async def test_ler_upload_limitado_devolve_conteudo_integro():
    conteudo = b"z" * 5000
    file = UploadFile(filename="ok.csv", file=io.BytesIO(conteudo))

    assert await ler_upload_limitado(file, max_bytes=10_000) == conteudo
