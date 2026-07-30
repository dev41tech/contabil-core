"""
CONCILPRO — API de Conciliação de Razão de Fornecedores.

Endpoints montados em /api/v1/concilpro/*.

Endpoints de query usam AsyncSession do FastAPI.
O processamento pesado (parsing + conciliação) roda em BackgroundTasks com
SyncSessionLocal (psycopg2) para compatibilidade com o algoritmo FIFO síncrono.
"""
from __future__ import annotations

import io
import logging
import traceback
from datetime import datetime
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import (
    CpArquivo as ArquivoImportado,
    CpFornecedor as Fornecedor,
    CpLancamento as LancamentoFornecedor,
    CpDivergencia as Divergencia,
    CpConciliacao as ConciliacaoInterna,
)
from src.db.session import SyncSessionLocal, get_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/concilpro", tags=["concilpro"])

# ============================================================================
# UTILITÁRIOS
# ============================================================================

def _converter_data_br(valor) -> Optional[datetime]:
    """Converte string DD/MM/YYYY → datetime; passa datetime sem alterar."""
    if not valor:
        return None
    if isinstance(valor, datetime):
        return valor
    try:
        return datetime.strptime(str(valor), "%d/%m/%Y")
    except (ValueError, TypeError):
        return None


# ============================================================================
# BACKGROUND TASK (síncrono — usa psycopg2 via SyncSessionLocal)
# ============================================================================

def _processar_arquivo_background(arquivo_id: int, conteudo: bytes) -> None:
    """
    Processamento pesado em background — usa sessão própria (não a da request).
    Chamado pelo BackgroundTasks do FastAPI após o upload retornar ao cliente.
    """
    from src.domain.concilpro.parser import parsear_arquivo_razao
    from src.domain.concilpro.consolidador import consolidar_todos_fornecedores
    from src.domain.concilpro.ai_classifier import classificar_lancamentos_incertos
    from src.domain.concilpro.conciliacao_intel import conciliar_todos_fornecedores_inteligente

    db = None
    try:
        db = SyncSessionLocal()
        arquivo = db.query(ArquivoImportado).filter(ArquivoImportado.id == arquivo_id).first()
        if not arquivo:
            logger.error("❌ Background: arquivo_id=%d não encontrado", arquivo_id)
            return

        dados = parsear_arquivo_razao(conteudo)
        dados = consolidar_todos_fornecedores(dados)

        incertos = [
            lanc
            for forn in dados["fornecedores"]
            for lanc in forn.get("lancamentos", [])
            if lanc.get("classificacao_incerta")
        ]
        if incertos:
            logger.info("🤖 Enviando %d lançamentos incertos para classificação IA…", len(incertos))
            classificar_lancamentos_incertos(incertos)

        arquivo.data_inicio        = _converter_data_br(dados.get("periodo_inicio"))
        arquivo.data_fim           = _converter_data_br(dados.get("periodo_fim"))
        arquivo.empresa            = dados.get("empresa")
        arquivo.cnpj_empresa       = dados.get("cnpj")
        arquivo.total_fornecedores = dados.get("total_fornecedores", len(dados["fornecedores"]))
        arquivo.total_lancamentos  = dados.get(
            "total_lancamentos",
            sum(len(f.get("lancamentos", [])) for f in dados["fornecedores"]),
        )

        logger.info("💾 Inserindo %d fornecedores…", len(dados["fornecedores"]))

        for idx, forn_data in enumerate(dados["fornecedores"], 1):
            if idx % 50 == 0:
                logger.info("   Processados %d / %d", idx, len(dados["fornecedores"]))

            saldo_anterior = Decimal(str(forn_data.get("saldo_anterior", 0)))

            lancamentos_raw = forn_data.get("lancamentos", [])
            total_debito_calc  = sum(Decimal(str(l.get("valor_debito",  0))) for l in lancamentos_raw)
            total_credito_calc = sum(Decimal(str(l.get("valor_credito", 0))) for l in lancamentos_raw)

            total_debito_ia  = Decimal(str(forn_data.get("total_debito",  0)))
            total_credito_ia = Decimal(str(forn_data.get("total_credito", 0)))

            total_debito  = total_debito_calc  if total_debito_calc  > 0 else total_debito_ia
            total_credito = total_credito_calc if total_credito_calc > 0 else total_credito_ia

            # ── Guarda de divergência ───────────────────────────────────────
            # A soma dos lançamentos parseados vence o total declarado no PDF
            # (acima). Quando os dois discordam, ou quando algum lançamento foi
            # fabricado por _recuperar_lancamentos_ocultos, os números não são
            # confiáveis — marcar para revisão em vez de entregar silenciosamente.
            TOL_DIVERGENCIA = Decimal("0.01")
            divergencias: list[str] = []

            if total_debito_ia > 0 and abs(total_debito_calc - total_debito_ia) > TOL_DIVERGENCIA:
                divergencias.append(
                    f"débito: PDF declara {total_debito_ia}, lançamentos somam {total_debito_calc}"
                )
            if total_credito_ia > 0 and abs(total_credito_calc - total_credito_ia) > TOL_DIVERGENCIA:
                divergencias.append(
                    f"crédito: PDF declara {total_credito_ia}, lançamentos somam {total_credito_calc}"
                )

            qtd_sinteticos = sum(1 for l in lancamentos_raw if l.get("sintetico"))
            if qtd_sinteticos:
                divergencias.append(f"{qtd_sinteticos} lançamento(s) fabricado(s) por recuperação de saldo")

            if divergencias:
                logger.warning(
                    "⚠️ Divergência em '%s' (conta %s): %s",
                    forn_data["nome_fornecedor"][:40],
                    forn_data["codigo_conta"],
                    "; ".join(divergencias),
                )

            saldo_ant_tipo = (forn_data.get("saldo_anterior_tipo") or "")[:1]

            fornecedor = Fornecedor(
                arquivo_origem_id   = arquivo.id,
                codigo_conta        = forn_data["codigo_conta"][:10],
                conta_contabil      = forn_data["conta_contabil"][:50],
                nome_fornecedor     = forn_data["nome_fornecedor"],
                saldo_anterior      = saldo_anterior,
                saldo_anterior_tipo = saldo_ant_tipo,
                total_debito        = total_debito,
                total_credito       = total_credito,
                saldo_final         = saldo_anterior + total_credito - total_debito,
                divergencia_calculo = bool(divergencias),
                mensagem_erro       = "; ".join(divergencias)[:2000] or None,
            )
            db.add(fornecedor)
            db.flush()

            for lanc_data in forn_data.get("lancamentos", []):
                vd    = Decimal(str(lanc_data["valor_debito"]))
                vc    = Decimal(str(lanc_data["valor_credito"]))
                saldo = Decimal(str(lanc_data["saldo_apos_lancamento"]))

                lote_val          = (lanc_data.get("lote") or "")[:50] or None
                conta_partida_val = (str(lanc_data.get("conta_partida") or "")[:20]) or None
                saldo_tipo_val    = (lanc_data.get("saldo_tipo") or "")[:1]
                numero_nf_val     = (lanc_data.get("numero_nf") or "")[:50] or None
                tipo_op_val       = (lanc_data.get("tipo_operacao") or "OUTRO")[:20]

                db.add(LancamentoFornecedor(
                    fornecedor_id         = fornecedor.id,
                    data_lancamento       = lanc_data["data_lancamento"],
                    lote                  = lote_val,
                    historico             = lanc_data["historico"],
                    conta_partida         = conta_partida_val,
                    valor_debito          = vd,
                    valor_credito         = vc,
                    saldo_apos_lancamento = saldo,
                    saldo_tipo            = saldo_tipo_val,
                    tipo_operacao         = tipo_op_val,
                    numero_nf             = numero_nf_val,
                    cnpj_historico        = lanc_data.get("cnpj_historico"),
                    valor_saldo           = vc if lanc_data["tipo_operacao"] == "COMPRA" else Decimal("0"),
                    classificado_por_ia   = lanc_data.get("classificado_por_ia", False),
                ))

        db.commit()
        logger.info("✅ Dados salvos no banco.")

        logger.info("🔄 Iniciando conciliação inteligente…")
        conciliar_todos_fornecedores_inteligente(db, arquivo.id)

        for forn in db.query(Fornecedor).filter(Fornecedor.arquivo_origem_id == arquivo.id).all():
            forn.valor_a_pagar = forn.total_credito - forn.total_debito
            if abs(forn.valor_a_pagar) <= Decimal("0.01"):
                forn.status_pagamento = "QUITADO"
            elif forn.valor_a_pagar < 0:
                forn.status_pagamento = "ADIANTADO"
            else:
                forn.status_pagamento = "EM_ABERTO"

        arquivo.status = "CONCLUIDO"
        db.commit()
        logger.info("✅ Processamento concluído para arquivo_id=%d", arquivo_id)

    except Exception as exc:
        logger.error("❌ Background: erro no arquivo_id=%d:\n%s", arquivo_id, traceback.format_exc())
        if db is not None:
            try:
                arquivo = db.query(ArquivoImportado).filter(ArquivoImportado.id == arquivo_id).first()
                if arquivo:
                    arquivo.status = "ERRO"
                    arquivo.mensagem_erro = str(exc)
                    db.commit()
            except Exception:
                pass
    finally:
        if db is not None:
            db.close()


# ============================================================================
# UPLOAD E PROCESSAMENTO
# ============================================================================

@router.post("/upload")
async def upload_arquivo(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
):
    """
    Recebe o PDF e retorna IMEDIATAMENTE com arquivo_id e status=PROCESSANDO.
    O parsing pesado roda em background. Use GET /arquivos/{id}/status para polling.
    """
    from src.domain.concilpro.parser import calcular_hash_arquivo

    try:
        conteudo = await file.read()
        hash_arquivo = calcular_hash_arquivo(conteudo)

        # ── Verifica duplicata ──────────────────────────────────────────────────
        result = await db.execute(
            select(ArquivoImportado).where(ArquivoImportado.hash_arquivo == hash_arquivo)
        )
        existente = result.scalar_one_or_none()

        if existente:
            if existente.status == "PROCESSANDO" and (existente.total_fornecedores or 0) > 0:
                # Processamento em andamento com dados parciais — não interromper
                return {
                    "success": True,
                    "arquivo_id": existente.id,
                    "status": "PROCESSANDO",
                    "message": "Arquivo já está sendo processado.",
                }

            if existente.status == "PROCESSANDO" and (existente.total_fornecedores or 0) == 0:
                # PROCESSANDO com 0 fornecedores = servidor reiniciado durante processamento
                # Trata como ERRO e reprocessa automaticamente
                logger.info(
                    "⚠️ Arquivo id=%d travado em PROCESSANDO (0 fornecedores) — reprocessando…",
                    existente.id,
                )
                # cai no bloco de reprocessamento abaixo

            elif existente.status == "CONCLUIDO" and (existente.total_fornecedores or 0) > 0:
                raise HTTPException(
                    status_code=400,
                    detail="Arquivo já foi importado anteriormente",
                )

            # ERRO ou CONCLUIDO com 0 fornecedores → apaga e reprocessa
            logger.info(
                "♻️ Reprocessando arquivo_id=%d (status=%s, fornecedores=%d)",
                existente.id, existente.status, existente.total_fornecedores or 0,
            )

            # Busca fornecedor IDs para cascade delete
            forn_result = await db.execute(
                select(Fornecedor.id).where(Fornecedor.arquivo_origem_id == existente.id)
            )
            forn_ids = [r[0] for r in forn_result.all()]

            if forn_ids:
                from sqlalchemy import delete
                await db.execute(
                    delete(ConciliacaoInterna).where(ConciliacaoInterna.fornecedor_id.in_(forn_ids))
                )
                await db.execute(
                    delete(Divergencia).where(Divergencia.fornecedor_id.in_(forn_ids))
                )
                await db.execute(
                    delete(LancamentoFornecedor).where(LancamentoFornecedor.fornecedor_id.in_(forn_ids))
                )
                await db.execute(
                    delete(Fornecedor).where(Fornecedor.arquivo_origem_id == existente.id)
                )

            existente.status             = "PROCESSANDO"
            existente.total_fornecedores = 0
            existente.total_lancamentos  = 0
            existente.mensagem_erro      = None
            existente.empresa            = None
            existente.cnpj_empresa       = None
            existente.data_inicio        = None
            existente.data_fim           = None
            # Commit explícito: garante que o registro está visível antes de retornar
            # (get_db faz commit apenas após a resposta ser enviada — race condition)
            await db.commit()

            background_tasks.add_task(_processar_arquivo_background, existente.id, conteudo)
            return {
                "success": True,
                "arquivo_id": existente.id,
                "status": "PROCESSANDO",
                "message": "Reprocessamento iniciado — consulte o status para acompanhar.",
            }

        # ── Arquivo novo ────────────────────────────────────────────────────────
        arquivo = ArquivoImportado(
            nome_arquivo=file.filename,
            hash_arquivo=hash_arquivo,
            status="PROCESSANDO",
        )
        db.add(arquivo)
        await db.flush()
        await db.refresh(arquivo)
        arquivo_id_novo = arquivo.id
        # Commit explícito: garante que o registro está visível antes de retornar
        # (get_db faz commit apenas após a resposta ser enviada — race condition)
        await db.commit()

        background_tasks.add_task(_processar_arquivo_background, arquivo_id_novo, conteudo)

        return {
            "success": True,
            "arquivo_id": arquivo_id_novo,
            "status": "PROCESSANDO",
            "message": "Arquivo recebido. Processamento em andamento — consulte o status para acompanhar.",
        }

    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


# ============================================================================
# CONSULTAS
# ============================================================================

@router.get("/arquivos/{arquivo_id}/status")
async def status_arquivo(arquivo_id: int, db: AsyncSession = Depends(get_db)):
    """Retorna o status atual do processamento de um arquivo."""
    result = await db.execute(
        select(ArquivoImportado).where(ArquivoImportado.id == arquivo_id)
    )
    arquivo = result.scalar_one_or_none()
    if not arquivo:
        raise HTTPException(status_code=404, detail="Arquivo não encontrado")
    return {
        "id":                  arquivo.id,
        "status":              arquivo.status,
        "total_fornecedores":  arquivo.total_fornecedores or 0,
        "total_lancamentos":   arquivo.total_lancamentos or 0,
        "mensagem_erro":       arquivo.mensagem_erro,
    }


@router.get("/arquivos")
async def listar_arquivos(db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(ArquivoImportado).order_by(ArquivoImportado.created_at.desc())
    )
    arquivos = result.scalars().all()
    return [
        {
            "id":                  arq.id,
            "nome_arquivo":        arq.nome_arquivo,
            "status":              arq.status,
            "total_fornecedores":  arq.total_fornecedores,
            "total_lancamentos":   arq.total_lancamentos,
            "periodo_inicio":      arq.data_inicio.isoformat() if arq.data_inicio else None,
            "periodo_fim":         arq.data_fim.isoformat()    if arq.data_fim    else None,
            "created_at":          arq.created_at.isoformat() if arq.created_at else None,
        }
        for arq in arquivos
    ]


@router.get("/resumo/{arquivo_id}")
async def obter_resumo(arquivo_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(ArquivoImportado).where(ArquivoImportado.id == arquivo_id)
    )
    arquivo = result.scalar_one_or_none()
    if not arquivo:
        raise HTTPException(status_code=404, detail="Arquivo não encontrado")

    forn_result = await db.execute(
        select(Fornecedor).where(Fornecedor.arquivo_origem_id == arquivo_id)
    )
    fornecedores = forn_result.scalars().all()

    return {
        "arquivo": {
            "id":            arquivo.id,
            "nome":          arquivo.nome_arquivo,
            "periodo_inicio": arquivo.data_inicio.isoformat() if arquivo.data_inicio else None,
            "periodo_fim":    arquivo.data_fim.isoformat()    if arquivo.data_fim    else None,
        },
        "estatisticas": {
            "total_fornecedores":        len(fornecedores),
            "total_lancamentos":         arquivo.total_lancamentos,
            "fornecedores_quitados":     sum(1 for f in fornecedores if f.status_pagamento == "QUITADO"),
            "fornecedores_em_aberto":    sum(1 for f in fornecedores if f.status_pagamento == "EM_ABERTO"),
            "fornecedores_adiantados":   sum(1 for f in fornecedores if f.status_pagamento == "ADIANTADO"),
            "fornecedores_com_divergencia": sum(1 for f in fornecedores if f.divergencia_calculo),
            "valor_total_a_pagar":       float(sum(f.valor_a_pagar or 0 for f in fornecedores)),
        },
    }


@router.get("/fornecedores")
async def listar_fornecedores(
    arquivo_id: int,
    status: Optional[str] = None,
    skip: int = 0,
    limit: int = 500,
    db: AsyncSession = Depends(get_db),
):
    stmt = select(Fornecedor).where(Fornecedor.arquivo_origem_id == arquivo_id)

    if status:
        stmt = stmt.where(Fornecedor.status_pagamento == status)

    stmt = stmt.order_by(Fornecedor.valor_a_pagar.desc()).offset(skip).limit(limit)

    result = await db.execute(stmt)
    fornecedores = result.scalars().all()

    return [
        {
            "id":               f.id,
            "codigo_conta":     f.codigo_conta,
            "conta_contabil":   f.conta_contabil,
            "nome_fornecedor":  f.nome_fornecedor,
            "total_credito":    float(f.total_credito or 0),
            "total_debito":     float(f.total_debito or 0),
            "saldo_final":      float(f.saldo_final or 0),
            "valor_a_pagar":    float(f.valor_a_pagar or 0),
            "status_pagamento": f.status_pagamento,
            "qtd_nfs_pendentes": f.qtd_nfs_pendentes,
            "qtd_nfs_parciais":  f.qtd_nfs_parciais,
            "divergencia_calculo": f.divergencia_calculo,
        }
        for f in fornecedores
    ]


@router.get("/fornecedores/{fornecedor_id}")
async def obter_fornecedor_detalhado(fornecedor_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(Fornecedor).where(Fornecedor.id == fornecedor_id)
    )
    fornecedor = result.scalar_one_or_none()
    if not fornecedor:
        raise HTTPException(status_code=404, detail="Fornecedor não encontrado")

    lanc_result = await db.execute(
        select(LancamentoFornecedor)
        .where(LancamentoFornecedor.fornecedor_id == fornecedor_id)
        .order_by(LancamentoFornecedor.data_lancamento)
    )
    lancamentos = lanc_result.scalars().all()

    compras_pendentes = [
        l for l in lancamentos
        if l.tipo_operacao == "COMPRA" and (l.valor_saldo or 0) > 0
    ]

    return {
        "fornecedor": {
            "id":                 fornecedor.id,
            "codigo_conta":       fornecedor.codigo_conta,
            "conta_contabil":     fornecedor.conta_contabil,
            "nome_fornecedor":    fornecedor.nome_fornecedor,
            "cnpj":               fornecedor.cnpj,
            "saldo_anterior":     float(fornecedor.saldo_anterior or 0),
            "total_credito":      float(fornecedor.total_credito or 0),
            "total_debito":       float(fornecedor.total_debito or 0),
            "saldo_final":        float(fornecedor.saldo_final or 0),
            "valor_a_pagar":      float(fornecedor.valor_a_pagar or 0),
            "status_pagamento":   fornecedor.status_pagamento,
            "divergencia_calculo": fornecedor.divergencia_calculo,
            "divergencia_motivo":  fornecedor.mensagem_erro,
        },
        "compras_pendentes": [
            {
                "id":               c.id,
                "data_lancamento":  c.data_lancamento.isoformat() if c.data_lancamento else None,
                "numero_nf":        c.numero_nf,
                "historico":        c.historico,
                "valor_total":      float(c.valor_credito or 0),
                "valor_pago_parcial": float(c.valor_pago_parcial or 0),
                "valor_saldo":      float(c.valor_saldo or 0),
                "status_pagamento": c.status_pagamento,
            }
            for c in compras_pendentes
        ],
        "todos_lancamentos": [
            {
                "id":            l.id,
                "data":          l.data_lancamento.isoformat() if l.data_lancamento else None,
                "lote":          l.lote,
                "historico":     l.historico,
                "tipo_operacao": l.tipo_operacao,
                "valor_debito":  float(l.valor_debito or 0),
                "valor_credito": float(l.valor_credito or 0),
                "saldo_apos":    float(l.saldo_apos_lancamento or 0),
            }
            for l in lancamentos
        ],
    }


@router.get("/fornecedores/{fornecedor_id}/conciliacao-fifo")
async def conciliacao_fifo_detalhada(fornecedor_id: int, db: AsyncSession = Depends(get_db)):
    """Retorna compras com trace FIFO de pagamentos."""
    compras_result = await db.execute(
        select(LancamentoFornecedor)
        .where(
            LancamentoFornecedor.fornecedor_id == fornecedor_id,
            LancamentoFornecedor.tipo_operacao == "COMPRA",
        )
        .order_by(LancamentoFornecedor.data_lancamento)
    )
    compras = compras_result.scalars().all()

    pags_result = await db.execute(
        select(LancamentoFornecedor)
        .where(
            LancamentoFornecedor.fornecedor_id == fornecedor_id,
            LancamentoFornecedor.tipo_operacao == "PAGAMENTO",
        )
        .order_by(LancamentoFornecedor.data_lancamento)
    )
    pagamentos_db = pags_result.scalars().all()

    saldo = {c.id: Decimal(str(c.valor_credito or 0)) for c in compras}
    pags_por_nf: dict = {c.id: [] for c in compras}

    for pag in pagamentos_db:
        restante = Decimal(str(pag.valor_debito or 0))
        for compra in compras:
            if saldo[compra.id] <= Decimal("0.01"):
                continue
            if restante <= Decimal("0.01"):
                break
            aplicado = min(restante, saldo[compra.id])
            saldo[compra.id] -= aplicado
            restante -= aplicado
            pags_por_nf[compra.id].append({
                "data_pagamento": pag.data_lancamento.isoformat() if pag.data_lancamento else None,
                "historico": pag.historico,
                "valor_pago": float(aplicado),
                "saldo_restante": float(saldo[compra.id]),
            })

    result = []
    for compra in compras:
        pags = pags_por_nf[compra.id]
        result.append({
            "numero_nf":       compra.numero_nf or "—",
            "data_lancamento": compra.data_lancamento.isoformat() if compra.data_lancamento else None,
            "historico":       compra.historico,
            "valor_total":     float(compra.valor_credito or 0),
            "valor_pago":      float(compra.valor_pago_parcial or 0),
            "data_pagamento":  pags[-1]["data_pagamento"] if pags else None,
            "valor_saldo":     float(compra.valor_saldo or 0),
            "status":          compra.status_pagamento or "PENDENTE",
            "pagamentos":      pags,
        })

    return {"conciliacao": result}


@router.get("/divergencias")
async def listar_divergencias(arquivo_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(Divergencia)
        .join(Fornecedor, Divergencia.fornecedor_id == Fornecedor.id)
        .where(
            Fornecedor.arquivo_origem_id == arquivo_id,
            Divergencia.resolvido.is_(False),
        )
    )
    divergencias = result.scalars().all()
    return [
        {
            "id":            d.id,
            "fornecedor_id": d.fornecedor_id,
            "tipo":          d.tipo,
            "severidade":    d.severidade,
            "descricao":     d.descricao,
            "diferenca":     float(d.diferenca or 0),
            "created_at":    d.created_at.isoformat() if d.created_at else None,
        }
        for d in divergencias
    ]


# ============================================================================
# EXPORT
# ============================================================================

@router.get("/export/excel/{arquivo_id}")
async def exportar_excel(
    arquivo_id: int,
    tipo: str = Query("completo", pattern="^(completo|em_aberto|divergencias)$"),
    db: AsyncSession = Depends(get_db),
):
    """Exporta dados para Excel (.xlsx) em memória."""
    import openpyxl
    from openpyxl.styles import Font, PatternFill

    result = await db.execute(
        select(ArquivoImportado).where(ArquivoImportado.id == arquivo_id)
    )
    arquivo = result.scalar_one_or_none()
    if not arquivo:
        raise HTTPException(status_code=404, detail="Arquivo não encontrado")

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Conciliação Fornecedores"

    headers = ["Código", "Conta Contábil", "Fornecedor", "CNPJ",
               "Total Compras", "Total Pagamentos", "Saldo a Pagar",
               "Status", "NFs Pendentes", "Divergência"]

    header_font = Font(color="FFFFFF", bold=True)
    header_fill = PatternFill(start_color="4472C4", end_color="4472C4", fill_type="solid")

    for col, h in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col, value=h)
        cell.font = header_font
        cell.fill = header_fill

    stmt = select(Fornecedor).where(Fornecedor.arquivo_origem_id == arquivo_id)
    if tipo == "em_aberto":
        stmt = stmt.where(Fornecedor.status_pagamento == "EM_ABERTO")
    elif tipo == "divergencias":
        stmt = stmt.where(Fornecedor.divergencia_calculo.is_(True))

    stmt = stmt.order_by(Fornecedor.valor_a_pagar.desc())
    forn_result = await db.execute(stmt)
    fornecedores_list = forn_result.scalars().all()

    for row, f in enumerate(fornecedores_list, 2):
        ws.cell(row=row, column=1,  value=f.codigo_conta)
        ws.cell(row=row, column=2,  value=f.conta_contabil)
        ws.cell(row=row, column=3,  value=f.nome_fornecedor)
        ws.cell(row=row, column=4,  value=f.cnpj)
        ws.cell(row=row, column=5,  value=float(f.total_credito or 0))
        ws.cell(row=row, column=6,  value=float(f.total_debito or 0))
        ws.cell(row=row, column=7,  value=float(f.valor_a_pagar or 0))
        ws.cell(row=row, column=8,  value=f.status_pagamento)
        ws.cell(row=row, column=9,  value=f.qtd_nfs_pendentes)
        ws.cell(row=row, column=10, value="Sim" if f.divergencia_calculo else "Não")

    for col in range(1, len(headers) + 1):
        ws.column_dimensions[openpyxl.utils.get_column_letter(col)].width = 22

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)

    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename=conciliacao_{tipo}.xlsx"},
    )
