"""Serviço de Exportação para ERP.

Gera arquivos CSV ou XLSX de:
  - lancamentos          : registros contábeis (existente)
  - nfe_entrada          : NF-e onde empresa é destinatária
  - nfe_saida            : NF-e onde empresa é emitente
  - nfse_tomado          : NFS-e tomado (empresa é destinatária)
  - nfse_prestado        : NFS-e prestado (empresa é emitente)
  - conferencia          : conciliação cruzada transação × nota × comprovante
"""

from __future__ import annotations

import csv
import io
import re
import uuid
from datetime import UTC, datetime
from uuid import UUID

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.errors import ValidationError
from src.db.models import (
    Comprovante,
    Empresa,
    ExportJob,
    NotaFiscal,
    RegistroContabil,
    Transacao,
)
from src.schemas.contabil import ExportJobCreate, ExportJobResponse

logger = structlog.get_logger(__name__)

# ── colunas por tipo ──────────────────────────────────────────────────────────

_COLUNAS_LANCAMENTOS = [
    "id", "data_lancamento", "historico", "historico_extrato",
    "dc", "valor", "tipo_regra", "conta_id", "agencia_id", "transacao_id",
]

_COLUNAS_NOTAS = [
    "data_emissao", "numero", "serie", "cnpj_emitente", "nome_emitente",
    "cnpj_destinatario", "valor", "chave_acesso", "status",
    "data_transacao", "valor_transacao",
]

_COLUNAS_CONFERENCIA = [
    "data_transacao", "historico_extrato", "dc", "valor_transacao",
    "status_transacao", "nota_numero", "tipo_nota", "valor_nota",
    "favorecido_comprovante", "valor_pago_comprovante", "status_conciliacao",
]

_VALID_TIPOS = {
    "lancamentos", "nfe_entrada", "nfe_saida",
    "nfse_tomado", "nfse_prestado", "conferencia",
}


def _sanitize_cnpj(cnpj: str | None) -> str:
    return re.sub(r"\D", "", cnpj or "")


class ExportacaoService:
    def __init__(self, db: AsyncSession, empresa_id: UUID, usuario_id: UUID) -> None:
        self._db = db
        self._empresa_id = empresa_id
        self._usuario_id = usuario_id

    # ── entrypoint ────────────────────────────────────────────────────────────

    async def exportar(self, data: ExportJobCreate) -> tuple[ExportJobResponse, bytes]:
        fmt = data.formato
        if fmt not in ("csv", "xlsx"):
            raise ValidationError(message="Formato deve ser 'csv' ou 'xlsx'.")

        tipo = data.tipo or "lancamentos"
        if tipo not in _VALID_TIPOS:
            raise ValidationError(message=f"Tipo de exportação inválido: '{tipo}'.")

        if tipo == "lancamentos":
            rows, conteudo = await self._exportar_lancamentos(data, fmt)
            total = len(rows)
        elif tipo in ("nfe_entrada", "nfe_saida", "nfse_tomado", "nfse_prestado"):
            rows, conteudo = await self._exportar_notas(tipo, data, fmt)
            total = len(rows)
        else:  # conferencia
            rows, conteudo = await self._exportar_conferencia(data, fmt)
            total = len(rows)

        job = ExportJob(
            id=uuid.uuid4(),
            empresa_id=self._empresa_id,
            formato=fmt,
            status="concluido",
            filtro_de=data.data_de,
            filtro_ate=data.data_ate,
            total_registros=total,
            arquivo_path=None,
            criado_por=self._usuario_id,
            finished_at=datetime.now(UTC),
        )
        self._db.add(job)
        await self._db.flush()

        logger.info(
            "exportacao.gerada",
            empresa_id=str(self._empresa_id),
            tipo=tipo,
            formato=fmt,
            total=total,
        )

        response = ExportJobResponse(
            job_id=job.id,
            status="concluido",
            formato=fmt,
            total_registros=total,
            download_url=None,
        )
        return response, conteudo

    # ── lançamentos contábeis (original) ─────────────────────────────────────

    async def _exportar_lancamentos(
        self, data: ExportJobCreate, fmt: str
    ) -> tuple[list, bytes]:
        q = select(RegistroContabil).where(
            RegistroContabil.empresa_id == self._empresa_id
        )
        if data.data_de:
            q = q.where(RegistroContabil.data_lancamento >= data.data_de)
        if data.data_ate:
            q = q.where(RegistroContabil.data_lancamento <= data.data_ate)

        rows = (
            await self._db.execute(q.order_by(RegistroContabil.data_lancamento))
        ).scalars().all()

        if fmt == "csv":
            conteudo = self._lancamentos_csv(rows)
        else:
            conteudo = self._lancamentos_xlsx(rows)
        return rows, conteudo

    def _lancamentos_csv(self, rows: list) -> bytes:
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=_COLUNAS_LANCAMENTOS, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({
                "id": str(r.id),
                "data_lancamento": r.data_lancamento.isoformat(),
                "historico": r.historico,
                "historico_extrato": r.historico_extrato,
                "dc": r.dc,
                "valor": float(r.valor),
                "tipo_regra": r.tipo_regra,
                "conta_id": str(r.conta_id),
                "agencia_id": str(r.agencia_id),
                "transacao_id": str(r.transacao_id) if r.transacao_id else "",
            })
        return buf.getvalue().encode("utf-8-sig")

    def _lancamentos_xlsx(self, rows: list) -> bytes:
        wb, ws = self._new_workbook("Lançamentos Contábeis", _COLUNAS_LANCAMENTOS)
        for r in rows:
            ws.append([
                str(r.id), r.data_lancamento.isoformat(),
                r.historico, r.historico_extrato,
                r.dc, float(r.valor), r.tipo_regra,
                str(r.conta_id), str(r.agencia_id),
                str(r.transacao_id) if r.transacao_id else "",
            ])
        return self._save_workbook(wb)

    # ── notas fiscais (NF-e / NFS-e) ─────────────────────────────────────────

    async def _exportar_notas(
        self, tipo: str, data: ExportJobCreate, fmt: str
    ) -> tuple[list, bytes]:
        # Determina o CNPJ da empresa para filtrar entrada vs saída
        empresa_row = (
            await self._db.execute(
                select(Empresa).where(Empresa.id == self._empresa_id)
            )
        ).scalar_one()
        cnpj_empresa = _sanitize_cnpj(empresa_row.cnpj)

        tipo_nota = "nfe" if tipo.startswith("nfe") else "nfse"

        q = select(NotaFiscal).where(
            NotaFiscal.empresa_id == self._empresa_id,
            NotaFiscal.tipo == tipo_nota,
            NotaFiscal.deleted_at.is_(None),
        )

        if data.data_de:
            q = q.where(NotaFiscal.data_emissao >= data.data_de)
        if data.data_ate:
            q = q.where(NotaFiscal.data_emissao <= data.data_ate)

        notas = (
            await self._db.execute(q.order_by(NotaFiscal.data_emissao))
        ).scalars().all()

        # Filtra entrada (empresa = destinatária) ou saída (empresa = emitente)
        is_entrada = tipo in ("nfe_entrada", "nfse_tomado")
        if is_entrada:
            notas = [
                n for n in notas
                if _sanitize_cnpj(n.cnpj_destinatario) == cnpj_empresa
            ]
        else:
            notas = [
                n for n in notas
                if _sanitize_cnpj(n.cnpj_emitente) == cnpj_empresa
            ]

        # Busca transações associadas (em lote)
        transacao_ids = {n.transacao_id for n in notas if n.transacao_id}
        transacoes_map: dict = {}
        if transacao_ids:
            t_rows = (
                await self._db.execute(
                    select(Transacao).where(Transacao.id.in_(transacao_ids))
                )
            ).scalars().all()
            transacoes_map = {t.id: t for t in t_rows}

        linhas = []
        for n in notas:
            t = transacoes_map.get(n.transacao_id) if n.transacao_id else None
            linhas.append({
                "data_emissao": n.data_emissao.date().isoformat() if n.data_emissao else "",
                "numero": n.numero or "",
                "serie": n.serie or "",
                "cnpj_emitente": n.cnpj_emitente or "",
                "nome_emitente": n.nome_emitente or "",
                "cnpj_destinatario": n.cnpj_destinatario or "",
                "valor": float(n.valor),
                "chave_acesso": n.chave_acesso or "",
                "status": n.status or "",
                "data_transacao": t.data.date().isoformat() if t else "",
                "valor_transacao": float(t.valor) if t else "",
            })

        if fmt == "csv":
            conteudo = self._dicts_to_csv(linhas, _COLUNAS_NOTAS)
        else:
            sheet_name = {
                "nfe_entrada": "NF-e Entrada",
                "nfe_saida": "NF-e Saída",
                "nfse_tomado": "NFS-e Tomado",
                "nfse_prestado": "NFS-e Prestado",
            }[tipo]
            conteudo = self._dicts_to_xlsx(linhas, _COLUNAS_NOTAS, sheet_name)

        return linhas, conteudo

    # ── simples conferência ───────────────────────────────────────────────────

    async def _exportar_conferencia(
        self, data: ExportJobCreate, fmt: str
    ) -> tuple[list, bytes]:
        q = select(Transacao).where(Transacao.empresa_id == self._empresa_id)
        if data.data_de:
            q = q.where(Transacao.data >= data.data_de)
        if data.data_ate:
            q = q.where(Transacao.data <= data.data_ate)

        transacoes = (
            await self._db.execute(q.order_by(Transacao.data))
        ).scalars().all()
        t_ids = [t.id for t in transacoes]

        # Notas e comprovantes associados
        notas_map: dict = {}
        comp_map: dict = {}

        if t_ids:
            notas_rows = (
                await self._db.execute(
                    select(NotaFiscal).where(
                        NotaFiscal.transacao_id.in_(t_ids),
                        NotaFiscal.deleted_at.is_(None),
                    )
                )
            ).scalars().all()
            for n in notas_rows:
                notas_map.setdefault(n.transacao_id, []).append(n)

            comp_rows = (
                await self._db.execute(
                    select(Comprovante).where(
                        Comprovante.transacao_id.in_(t_ids),
                        Comprovante.deleted_at.is_(None),
                    )
                )
            ).scalars().all()
            for c in comp_rows:
                comp_map.setdefault(c.transacao_id, []).append(c)

        linhas = []
        for t in transacoes:
            notas = notas_map.get(t.id, [None])  # pelo menos uma linha por transação
            comps = comp_map.get(t.id, [None])

            # Máximo de linhas = max(len(notas), len(comps))
            max_rows = max(len(notas), len(comps))
            notas_padded = notas + [None] * (max_rows - len(notas))
            comps_padded = comps + [None] * (max_rows - len(comps))

            tem_nota = bool(notas_map.get(t.id))
            tem_comp = bool(comp_map.get(t.id))
            if tem_nota and tem_comp:
                status_conc = "Conciliado"
            elif tem_nota or tem_comp:
                status_conc = "Parcial"
            else:
                status_conc = "Pendente"

            for idx, (n, c) in enumerate(zip(notas_padded, comps_padded)):
                row: dict = {
                    "data_transacao": t.data.date().isoformat() if idx == 0 else "",
                    "historico_extrato": t.historico if idx == 0 else "",
                    "dc": t.dc if idx == 0 else "",
                    "valor_transacao": float(t.valor) if idx == 0 else "",
                    "status_transacao": t.status if idx == 0 else "",
                    "nota_numero": n.numero if n else "",
                    "tipo_nota": n.tipo if n else "",
                    "valor_nota": float(n.valor) if n else "",
                    "favorecido_comprovante": c.favorecido if c else "",
                    "valor_pago_comprovante": float(c.valor_pago) if c else "",
                    "status_conciliacao": status_conc if idx == 0 else "",
                }
                linhas.append(row)

        if fmt == "csv":
            conteudo = self._dicts_to_csv(linhas, _COLUNAS_CONFERENCIA)
        else:
            conteudo = self._dicts_to_xlsx(linhas, _COLUNAS_CONFERENCIA, "Conferência")

        return linhas, conteudo

    # ── helpers xlsx / csv ────────────────────────────────────────────────────

    def _new_workbook(self, title: str, colunas: list[str]):
        try:
            import openpyxl
            from openpyxl.styles import Font
        except ImportError as exc:
            raise ValidationError(message="openpyxl não instalado. Use formato csv.") from exc

        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = title
        ws.append(colunas)
        for cell in ws[1]:
            cell.font = Font(bold=True)
        return wb, ws

    def _save_workbook(self, wb) -> bytes:
        buf = io.BytesIO()
        wb.save(buf)
        return buf.getvalue()

    def _dicts_to_csv(self, linhas: list[dict], colunas: list[str]) -> bytes:
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=colunas, extrasaction="ignore")
        w.writeheader()
        w.writerows(linhas)
        return buf.getvalue().encode("utf-8-sig")

    def _dicts_to_xlsx(
        self, linhas: list[dict], colunas: list[str], sheet_name: str
    ) -> bytes:
        wb, ws = self._new_workbook(sheet_name, colunas)
        for linha in linhas:
            ws.append([linha.get(col, "") for col in colunas])
        return self._save_workbook(wb)
