"""Testes de integração dos jobs persistentes."""

from __future__ import annotations

import io
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest

from src.db.models import Empresa, Job
from src.domain.jobs import JobRuntime, recuperar_jobs_sem_heartbeat
from src.schemas.extrato import ImportacaoResult


async def _login(client, tenant, usuario) -> str:
    resposta = await client.post(
        "/api/v1/auth/login",
        json={"tenant_id": str(tenant.id), "email": usuario.email, "senha": "senha_segura_123"},
    )
    return resposta.json()["csrf_token"]


@pytest.mark.asyncio
async def test_neo_devolve_202_e_persiste_ciclo_completo(client, tenant, usuario, empresa):
    """O POST não pode devolver o resumo síncrono; o GET precisa preservá-lo.

    O transporte ASGI aguarda BackgroundTasks, por isso o POST ainda contém a
    fotografia ``na_fila`` serializada antes do trabalho e o GET já observa a
    conclusão, cobrindo o mesmo ciclo que a tela acompanhará por polling.
    """
    csrf = await _login(client, tenant, usuario)

    resposta = await client.post(
        f"/api/v1/empresas/{empresa.id}/neo/processar",
        json={},
        headers={"X-CSRF-Token": csrf},
    )

    assert resposta.status_code == 202
    assert resposta.json()["tipo"] == "neo_processar"
    assert resposta.json()["status"] == "na_fila"
    detalhe = await client.get(
        f"/api/v1/empresas/{empresa.id}/jobs/{resposta.json()['id']}"
    )
    assert detalhe.status_code == 200
    assert detalhe.json()["status"] == "concluido"
    assert detalhe.json()["total"] == 0
    assert detalhe.json()["processados"] == 0
    assert detalhe.json()["resultado"]["total_pendentes"] == 0
    assert detalhe.json()["iniciado_em"] is not None
    assert detalhe.json()["concluido_em"] is not None
    assert detalhe.json()["heartbeat_em"] is not None


@pytest.mark.asyncio
async def test_importacao_invalida_falha_no_job_com_erro_util(client, tenant, usuario, empresa):
    """Erro de parsing não pode sumir no log nem transformar o POST em espera síncrona."""
    csrf = await _login(client, tenant, usuario)
    agencia = (
        await client.post(
            f"/api/v1/empresas/{empresa.id}/agencias",
            json={"banco_sigla": "BB", "agencia": "9911", "numero": "77881"},
            headers={"X-CSRF-Token": csrf},
        )
    ).json()

    resposta = await client.post(
        f"/api/v1/empresas/{empresa.id}/extrato/importar?agencia_id={agencia['id']}",
        files={"arquivo": ("ruim.ofx", io.BytesIO(b"nao e ofx"), "application/octet-stream")},
        headers={"X-CSRF-Token": csrf},
    )

    assert resposta.status_code == 202
    detalhe = (
        await client.get(
            f"/api/v1/empresas/{empresa.id}/jobs/{resposta.json()['id']}"
        )
    ).json()
    assert detalhe["status"] == "falhou"
    assert detalhe["erro"]
    assert detalhe["resultado"] is None


@pytest.mark.asyncio
async def test_importacao_com_rejeicao_conclui_com_alertas(
    client, monkeypatch, tenant, usuario, empresa
):
    """Linhas aproveitadas com rejeição não podem parecer sucesso limpo nem falha total."""
    csrf = await _login(client, tenant, usuario)
    agencia = (
        await client.post(
            f"/api/v1/empresas/{empresa.id}/agencias",
            json={"banco_sigla": "CEF", "agencia": "9912", "numero": "77882"},
            headers={"X-CSRF-Token": csrf},
        )
    ).json()

    async def importar_com_rejeicao(self, conteudo, agencia_id):
        return ImportacaoResult(
            agencia_id=agencia_id,
            total_no_arquivo=2,
            importadas=1,
            duplicadas=0,
            erros=0,
            rejeitadas=1,
            motivos_rejeicao=["Valor inconsistente na linha 2."],
            transacoes=[],
        )

    monkeypatch.setattr(
        "src.domain.extrato.service.ExtratoService.importar_ofx",
        importar_com_rejeicao,
    )
    resposta = await client.post(
        f"/api/v1/empresas/{empresa.id}/extrato/importar?agencia_id={agencia['id']}",
        files={"arquivo": ("alerta.ofx", io.BytesIO(b"conteudo"), "application/octet-stream")},
        headers={"X-CSRF-Token": csrf},
    )
    detalhe = (
        await client.get(
            f"/api/v1/empresas/{empresa.id}/jobs/{resposta.json()['id']}"
        )
    ).json()

    assert resposta.status_code == 202
    assert detalhe["status"] == "concluido_com_alertas"
    assert detalhe["total"] == 2
    assert detalhe["processados"] == 2
    assert detalhe["resultado"]["rejeitadas"] == 1


@pytest.mark.asyncio
async def test_historico_filtra_e_pagina_jobs(client, db, tenant, usuario, empresa):
    """Filtros e paginação travam o contrato usado pela tela de histórico."""
    await _login(client, tenant, usuario)
    db.add_all(
        [
            Job(empresa_id=empresa.id, tipo="neo_processar", status="concluido", criado_por=usuario.id),
            Job(empresa_id=empresa.id, tipo="extrato_importar", status="falhou", criado_por=usuario.id),
            Job(empresa_id=empresa.id, tipo="neo_processar", status="falhou", criado_por=usuario.id),
        ]
    )
    await db.flush()

    resposta = await client.get(
        f"/api/v1/empresas/{empresa.id}/jobs?tipo=neo_processar&status=falhou&page=1&page_size=1"
    )

    assert resposta.status_code == 200
    assert resposta.json()["total"] == 1
    assert resposta.json()["page"] == 1
    assert resposta.json()["page_size"] == 1
    assert len(resposta.json()["items"]) == 1


@pytest.mark.asyncio
async def test_detalhe_nao_vaza_job_de_outra_empresa(client, db, tenant, usuario, empresa):
    """Conhecer um UUID não pode atravessar a fronteira entre empresas do tenant."""
    await _login(client, tenant, usuario)
    outra = Empresa(
        tenant_id=tenant.id,
        razao_social="Outra Empresa Ltda",
        cnpj="11.444.777/0001-61",
        regime_tributario="simples_nacional",
    )
    db.add(outra)
    await db.flush()
    job = Job(empresa_id=outra.id, tipo="neo_processar", status="na_fila", criado_por=usuario.id)
    db.add(job)
    await db.flush()

    resposta = await client.get(f"/api/v1/empresas/{empresa.id}/jobs/{job.id}")

    assert resposta.status_code == 404


@pytest.mark.asyncio
async def test_reaper_falha_so_heartbeat_expirado(db, usuario, empresa):
    """Startup de um worker não pode matar o job vivo executado por outro.

    O teste trava a razão do heartbeat: apenas o pulso parado além do limite é
    recuperado; status ``processando`` sozinho nunca é evidência de abandono.
    """
    antigo = Job(
        empresa_id=empresa.id, tipo="neo_processar", status="processando",
        criado_por=usuario.id, heartbeat_em=datetime.now(UTC) - timedelta(minutes=3),
    )
    vivo = Job(
        empresa_id=empresa.id, tipo="neo_processar", status="processando",
        criado_por=usuario.id, heartbeat_em=datetime.now(UTC),
    )
    db.add_all([antigo, vivo])
    await db.flush()

    @asynccontextmanager
    async def scope():
        yield db

    quantidade = await recuperar_jobs_sem_heartbeat(JobRuntime(scope, commit=False))
    await db.refresh(antigo)
    await db.refresh(vivo)

    assert quantidade == 1
    assert antigo.status == "falhou"
    assert antigo.erro
    assert vivo.status == "processando"
