"""Testes de integração — o ConcilPro não pode responder sem autenticação.

Até 2026-08-03 as 9 rotas deste módulo estavam abertas na internet: era possível
listar arquivos, fornecedores e baixar o export em Excel com nome, CNPJ e saldo
de todos os fornecedores, sem token nenhum. Estes testes existem para que isso
não volte silenciosamente.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

EMPRESA_ID = "00000000-0000-0000-0000-000000000001"
BASE = f"/api/v1/empresas/{EMPRESA_ID}/concilpro"

# Toda rota GET do módulo. Se uma rota nova aparecer e não estiver aqui,
# test_todas_as_rotas_get_estao_cobertas falha.
ROTAS_GET = [
    "/arquivos",
    "/arquivos/1/status",
    "/resumo/1",
    "/fornecedores?arquivo_id=1",
    "/fornecedores/1",
    "/fornecedores/1/conciliacao-fifo",
    "/divergencias?arquivo_id=1",
    "/export/excel/1",
]


@pytest.mark.asyncio
@pytest.mark.parametrize("rota", ROTAS_GET)
async def test_get_sem_auth_rejeita(client: AsyncClient, rota: str):
    r = await client.get(f"{BASE}{rota}")
    assert r.status_code == 401, (
        f"{rota} respondeu {r.status_code} sem autenticação — deveria ser 401"
    )


@pytest.mark.asyncio
async def test_upload_sem_auth_rejeita(client: AsyncClient):
    """O upload aberto permitia enviar arquivo e disparar processamento."""
    r = await client.post(
        f"{BASE}/upload",
        files={"file": ("razao.pdf", b"%PDF-1.7 conteudo", "application/pdf")},
    )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_export_excel_nao_vaza_dados_sem_auth(client: AsyncClient):
    """O caso mais grave: a planilha traz nome, CNPJ e saldo de cada fornecedor."""
    r = await client.get(f"{BASE}/export/excel/1")
    assert r.status_code == 401
    assert b"PK" not in r.content[:2], "não pode devolver um arquivo XLSX"


@pytest.mark.asyncio
async def test_todas_as_rotas_get_estao_cobertas():
    """
    Guarda contra rota nova sem teste: compara as rotas GET registradas no
    router com a lista acima. A proteção é aplicada no router inteiro, então
    uma rota nova nasce protegida — este teste garante que continue verdade.
    """
    from src.api.v1.concilpro import router

    registradas = {
        rota.path
        for rota in router.routes
        if "GET" in getattr(rota, "methods", set())
    }
    cobertas = {
        f"/empresas/{{empresa_id}}/concilpro{r.split('?')[0]}"
        for r in ROTAS_GET
    }

    # normaliza os path params ({arquivo_id} vs 1) comparando a quantidade de segmentos
    def forma(p: str) -> str:
        partes = []
        for seg in p.strip("/").split("/"):
            partes.append("*" if seg.isdigit() or seg.startswith("{") else seg)
        return "/".join(partes)

    faltando = {forma(p) for p in registradas} - {forma(p) for p in cobertas}
    assert not faltando, f"rotas GET sem teste de autenticação: {sorted(faltando)}"
