"""Conciliação bancária: razão da conta banco × extrato importado.

/api/v1/empresas/{empresa_id}/concilpro/razao-extrato

Aba nova da tela do ConcilPro (que para o usuário passou a se chamar
"Conciliação Bancária"), mas fluxo separado da importação de razão de
fornecedores: não grava arquivo, fornecedor nem lançamento, e não roda o FIFO.

O caminho fica sob `/concilpro` de propósito: o acesso por módulo da empresa é
deduzido do caminho, e a permissão desta rota é a do módulo `concilpro`
(`test_recurso_declarado_bate_com_o_modulo_do_caminho` trava essa coerência).
Cada chamada lê o razão, cruza com o extrato e devolve o relatório.
"""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, File, Query, UploadFile
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from src.api.autorizacao import requer
from src.api.deps import get_company_context, require_csrf
from src.api.uploads import ler_upload_limitado
from src.core.errors import ValidationError
from src.db.session import get_db
from src.domain.conciliacao_bancaria.exportar import gerar_planilha
from src.domain.conciliacao_bancaria.razao import RazaoInvalido, ler_razao
from src.domain.conciliacao_bancaria.service import conciliar_razao_extrato
from src.domain.conciliacao_bancaria.sispag import SispagInvalido, ler_sispag
from src.schemas.conciliacao_bancaria import RelatorioConciliacao

router = APIRouter(
    prefix="/empresas/{empresa_id}/concilpro",
    tags=["concilpro"],
    dependencies=[Depends(get_company_context)],
)


@router.post(
    "/razao-extrato",
    response_model=RelatorioConciliacao,
    dependencies=[requer("concilpro.execute"), Depends(require_csrf)],
)
async def conciliar_razao_com_extrato(
    empresa_id: UUID,
    agencia_id: UUID = Query(..., description="Conta bancária cujo extrato já está importado"),
    formato: Literal["json", "xlsx"] = Query("json"),
    arquivo: UploadFile = File(..., description="Razão da conta banco (XLSX, XLS ou PDF)"),
    sispag: UploadFile | None = File(
        None, description="Opcional: consulta de pagamentos do SISPAG (XLS/XLSX) do período"
    ),
    db: AsyncSession = Depends(get_db),
):
    conteudo = await ler_upload_limitado(arquivo)
    try:
        # pdfplumber e openpyxl são síncronos; um razão de 80 páginas trava o
        # event loop se lido aqui.
        razao = await run_in_threadpool(ler_razao, conteudo, arquivo.filename or "")
    except RazaoInvalido as exc:
        raise ValidationError(message=str(exc)) from exc

    consulta = None
    if sispag is not None and sispag.filename:
        try:
            consulta = await run_in_threadpool(ler_sispag, await ler_upload_limitado(sispag))
        except SispagInvalido as exc:
            raise ValidationError(message=str(exc)) from exc

    relatorio = await conciliar_razao_extrato(
        db, empresa_id=empresa_id, agencia_id=agencia_id, razao=razao, sispag=consulta
    )
    if formato == "json":
        return relatorio

    planilha = await run_in_threadpool(gerar_planilha, relatorio)
    nome = f"conciliacao_{razao.conta_codigo or 'conta'}_{razao.periodo_inicio:%Y-%m}.xlsx"
    return Response(
        content=planilha,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{nome}"'},
    )
