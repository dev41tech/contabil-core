"""Router de Exportação para ERP — /api/v1/empresas/{empresa_id}/exportacao"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import AuthContext, get_company_context, require_csrf
from src.db.session import get_db
from src.domain.exportacao.service import ExportacaoService
from src.schemas.contabil import ExportJobCreate

router = APIRouter(
    prefix="/empresas/{empresa_id}/exportacao",
    tags=["exportacao"],
)

_CONTENT_TYPES = {
    "csv": "text/csv; charset=utf-8-sig",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}

_EXTENSIONS = {"csv": "csv", "xlsx": "xlsx"}


@router.post(
    "/gerar",
    dependencies=[Depends(require_csrf)],
)
async def gerar_exportacao(
    empresa_id: UUID,
    body: ExportJobCreate,
    ctx: AuthContext = Depends(get_company_context),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Gera e retorna o arquivo de registros contábeis para importação no ERP.

    O arquivo é devolvido inline (Content-Disposition: attachment).
    """
    svc = ExportacaoService(db=db, empresa_id=empresa_id, usuario_id=ctx.user_id)
    job_response, conteudo = await svc.exportar(body)

    fmt = job_response.formato
    filename = f"registros_{empresa_id}_{fmt}.{_EXTENSIONS[fmt]}"

    return Response(
        content=conteudo,
        media_type=_CONTENT_TYPES[fmt],
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Total-Registros": str(job_response.total_registros or 0),
            "X-Job-Id": str(job_response.job_id),
        },
    )
