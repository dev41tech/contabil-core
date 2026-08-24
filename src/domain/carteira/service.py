"""Agrega o trabalho corrente de todas as empresas visíveis ao usuário."""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID

from sqlalchemy import and_, case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.dates import bounds_do_mes_data
from src.db.models import Empresa, Permissao, Transacao
from src.schemas.carteira import CarteiraEmpresaResponse, CarteiraResponse


async def listar_carteira(
    db: AsyncSession,
    *,
    tenant_id: UUID,
    user_id: UUID,
    role: str,
    mes: str,
) -> CarteiraResponse:
    inicio, fim = bounds_do_mes_data(mes)

    total = func.count(Transacao.id).label("transacoes_importadas")
    pendentes = func.sum(
        case((Transacao.status == "pendente", 1), else_=0)
    ).label("pendentes")
    classificadas = func.sum(
        case((Transacao.status == "processada", 1), else_=0)
    ).label("classificadas")
    erros = func.sum(case((Transacao.status == "erro", 1), else_=0)).label("erros")
    valor_pendente = func.coalesce(
        func.sum(
            case(
                (Transacao.status == "pendente", Transacao.valor),
                else_=Decimal("0.00"),
            )
        ),
        Decimal("0.00"),
    ).label("valor_total_pendente")

    consulta = select(
        Empresa.id,
        Empresa.razao_social,
        total,
        pendentes,
        classificadas,
        erros,
        valor_pendente,
    ).select_from(Empresa)
    if role != "admin":
        consulta = consulta.join(
            Permissao,
            and_(
                Permissao.empresa_id == Empresa.id,
                Permissao.usuario_id == user_id,
            ),
        )

    # Os limites ficam no ON do LEFT JOIN: colocá-los no WHERE apagaria justo
    # as empresas sem extrato, que são um dos estados que a carteira deve expor.
    consulta = (
        consulta.outerjoin(
            Transacao,
            and_(
                Transacao.empresa_id == Empresa.id,
                Transacao.data >= inicio,
                Transacao.data <= fim,   # `fim` é o último DIA do mês, inclusivo
                Transacao.deleted_at.is_(None),
            ),
        )
        .where(
            Empresa.tenant_id == tenant_id,
            Empresa.ativa == True,
            Empresa.deleted_at.is_(None),
        )
        .group_by(Empresa.id, Empresa.razao_social)
        .order_by(pendentes.desc(), Empresa.razao_social)
    )

    # Uma única execução agregada evita a consulta por empresa que esta tela
    # naturalmente induziria se cada contador fosse calculado separadamente.
    linhas = (await db.execute(consulta)).all()
    return CarteiraResponse(
        mes=mes,
        items=[
            CarteiraEmpresaResponse(
                empresa_id=linha.id,
                razao_social=linha.razao_social,
                transacoes_importadas=linha.transacoes_importadas,
                pendentes=linha.pendentes,
                classificadas=linha.classificadas,
                erros=linha.erros,
                ha_extrato_importado=linha.transacoes_importadas > 0,
                valor_total_pendente=linha.valor_total_pendente,
            )
            for linha in linhas
        ],
    )
