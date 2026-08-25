"""Serviço de Exportação para ERP.

Gera arquivos CSV ou XLSX de:
  - lancamentos           : registros contábeis (existente)
  - lancamentos_importacao: lançamentos no layout padrão de importação contábil
                            (débito/crédito pareados na mesma linha). Único tipo
                            que também aceita TXT — layout do arquivo modelo do
                            escritório: mesmas colunas, delimitado por ';', SEM
                            cabeçalho, valor com vírgula decimal.
  - extrato               : transações bancárias importadas, com os filtros da tela
  - nfe_entrada           : NF-e onde empresa é destinatária
  - nfe_saida             : NF-e onde empresa é emitente
  - nfse_tomado           : NFS-e tomado (empresa é destinatária)
  - nfse_prestado         : NFS-e prestado (empresa é emitente)
  - conferencia           : conciliação cruzada transação × nota × comprovante
"""

from __future__ import annotations

import csv
import io
import re
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.cnpj import valido as cnpj_valido
from src.core.dates import bounds_do_mes as _bounds_do_mes
from src.core.errors import ValidationError
from src.db.models import (
    AgenciaBancaria,
    Comprovante,
    Empresa,
    ExportJob,
    NotaFiscal,
    PlanoConta,
    RegistroContabil,
    Transacao,
)
from src.domain.exportacao.formatos import (
    COLUNAS_LANCAMENTOS_IMPORTACAO,
    _celula_segura,
    _dicts_to_csv,
    _dicts_to_txt,
    _dicts_to_xlsx,
    _new_workbook,
    _save_workbook,
)
from src.domain.extrato.ordenacao import ordenar_como_o_extrato
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
    "favorecido_comprovante", "valor_pago_comprovante", "total_documentos",
    "divergencia_residual", "status_conciliacao",
]

_COLUNAS_EXTRATO = [
    "data", "agencia", "historico", "dc", "valor", "status",
]

_VALID_TIPOS = {
    "lancamentos", "lancamentos_importacao", "extrato", "nfe_entrada",
    "nfe_saida", "nfse_tomado", "nfse_prestado", "conferencia",
}

# Os valores persistidos têm escala de centavos. Exigir igualdade exata evita
# esconder uma divergência real de R$ 0,01 sob uma tolerância implícita.
_TOLERANCIA_CONCILIACAO = Decimal("0.00")


def _sanitize_cnpj(cnpj: str | None) -> str:
    return re.sub(r"\D", "", cnpj or "")


def _as_decimal(value: Decimal | int | float | str | None) -> Decimal:
    """Converte entradas legadas sem materializar a representação binária do float."""
    if value is None:
        return Decimal("0.00")
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _valor_assinado(value: Decimal, dc: str) -> Decimal:
    return -value if dc == "D" else value


class ExportacaoService:
    def __init__(self, db: AsyncSession, empresa_id: UUID, usuario_id: UUID) -> None:
        self._db = db
        self._empresa_id = empresa_id
        self._usuario_id = usuario_id

    # ── entrypoint ────────────────────────────────────────────────────────────

    async def exportar(self, data: ExportJobCreate) -> tuple[ExportJobResponse, bytes]:
        if data.mes:
            data_de, data_ate = _bounds_do_mes(data.mes)
            data = data.model_copy(update={"data_de": data_de, "data_ate": data_ate})

        fmt = data.formato
        if fmt not in ("csv", "xlsx", "txt"):
            raise ValidationError(message="Formato deve ser 'csv', 'xlsx' ou 'txt'.")

        tipo = data.tipo or "lancamentos"
        if tipo not in _VALID_TIPOS:
            raise ValidationError(message=f"Tipo de exportação inválido: '{tipo}'.")

        # .txt só existe para o layout de importação de lançamentos — é o
        # único tipo pedido com esse formato até agora (planilha modelo
        # fornecida pelo escritório). Os outros tipos continuam csv/xlsx.
        if fmt == "txt" and tipo != "lancamentos_importacao":
            raise ValidationError(
                message=(
                    "O formato 'txt' só está disponível para "
                    "'lancamentos_importacao' por enquanto."
                )
            )

        if tipo == "lancamentos":
            rows, conteudo = await self._exportar_lancamentos(data, fmt)
            total = len(rows)
        elif tipo == "lancamentos_importacao":
            rows, conteudo = await self._exportar_lancamentos_importacao(data, fmt)
            total = len(rows)
        elif tipo == "extrato":
            rows, conteudo = await self._exportar_extrato(data, fmt)
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
            linha = {
                "id": str(r.id),
                "data_lancamento": r.data_lancamento.isoformat(),
                "historico": r.historico,
                "historico_extrato": r.historico_extrato,
                "dc": r.dc,
                "valor": _as_decimal(r.valor),
                "tipo_regra": r.tipo_regra,
                "conta_id": str(r.conta_id),
                "agencia_id": str(r.agencia_id),
                "transacao_id": str(r.transacao_id) if r.transacao_id else "",
            }
            w.writerow({chave: _celula_segura(valor) for chave, valor in linha.items()})
        return buf.getvalue().encode("utf-8-sig")

    def _lancamentos_xlsx(self, rows: list) -> bytes:
        wb, ws = self._new_workbook("Lançamentos Contábeis", _COLUNAS_LANCAMENTOS)
        for r in rows:
            ws.append([_celula_segura(valor) for valor in [
                str(r.id), r.data_lancamento.isoformat(),
                r.historico, r.historico_extrato,
                r.dc, _as_decimal(r.valor), r.tipo_regra,
                str(r.conta_id), str(r.agencia_id),
                str(r.transacao_id) if r.transacao_id else "",
            ]])
        return self._save_workbook(wb)

    # ── lançamentos contábeis (layout de importação) ─────────────────────────

    async def _exportar_lancamentos_importacao(
        self, data: ExportJobCreate, fmt: str
    ) -> tuple[list, bytes]:
        """Lançamentos no layout padrão de importação: débito e crédito pareados
        na mesma linha, via `lancamento_id`.

        Campos sem correspondente no modelo atual (Cód. Histórico, Inicia Lote,
        Código Matriz/Filial, Centro de Custo) saem em branco — a planilha ainda
        é válida para import, só sem esses dados preenchidos.
        """
        q = select(RegistroContabil).where(
            RegistroContabil.empresa_id == self._empresa_id
        )
        if data.data_de:
            q = q.where(RegistroContabil.data_lancamento >= data.data_de)
        if data.data_ate:
            q = q.where(RegistroContabil.data_lancamento <= data.data_ate)

        registros = (
            await self._db.execute(q.order_by(RegistroContabil.data_lancamento))
        ).scalars().all()

        # Resolve o código da conta em lote: a alternativa é uma query por linha.
        # Este layout importa num sistema contábil externo (legado do MrContador),
        # que espera o número de conta antigo (`conta_numero`, ex: 1026) — não o
        # código hierárquico usado internamente pro Plano de Contas do
        # contabil-core (`codigo`, ex: 3.4.2.05.0009). Cai pro código hierárquico
        # só quando a conta não tem `conta_numero` (ex: criada após a migração).
        contas: dict = {}
        conta_ids = {r.conta_id for r in registros}
        if conta_ids:
            conta_rows = (
                await self._db.execute(
                    select(PlanoConta).where(PlanoConta.id.in_(conta_ids))
                )
            ).scalars().all()
            contas = {
                c.id: str(c.conta_numero) if c.conta_numero is not None else c.codigo
                for c in conta_rows
            }

        # Agrupa por lancamento_id preservando a ordem de chegada (por data).
        grupos: dict = {}
        for r in registros:
            grupos.setdefault(r.lancamento_id, {})[r.dc] = r

        linhas = []
        for lancamento_id, pernas in grupos.items():
            debito = pernas.get("D")
            credito = pernas.get("C")
            if debito is None or credito is None:
                # Layout exige débito e crédito na mesma linha; um lançamento
                # sem as duas pernas não tem como virar uma linha válida aqui.
                logger.warning(
                    "exportacao.lancamento_sem_par",
                    empresa_id=str(self._empresa_id),
                    lancamento_id=str(lancamento_id),
                )
                continue

            linhas.append({
                "Data": debito.data_lancamento.strftime("%d/%m/%Y"),
                "Cód. Conta Debito": contas.get(debito.conta_id, ""),
                "Cód. Conta Credito": contas.get(credito.conta_id, ""),
                "Valor": _as_decimal(debito.valor),
                "Cód. Histórico": "",
                "Complemento Histórico": debito.historico,
                "Inicia Lote": "",
                "Código Matriz/Filial": "",
                "Centro de Custo Débito": "",
                "Centro de Custo Crédito": "",
            })

        if fmt == "csv":
            conteudo = self._dicts_to_csv(linhas, COLUNAS_LANCAMENTOS_IMPORTACAO)
        elif fmt == "txt":
            conteudo = self._dicts_to_txt(linhas, COLUNAS_LANCAMENTOS_IMPORTACAO)
        else:
            conteudo = self._dicts_to_xlsx(
                linhas, COLUNAS_LANCAMENTOS_IMPORTACAO, "Importação Lançamentos"
            )
        return linhas, conteudo

    # ── extrato bancário ──────────────────────────────────────────────────────

    async def _exportar_extrato(
        self, data: ExportJobCreate, fmt: str
    ) -> tuple[list, bytes]:
        """Transações bancárias importadas, com os mesmos filtros da tela.

        Espelha `ExtratoService.listar`: agência, status e período. O arquivo
        precisa bater linha a linha com o que o usuário está vendo — se divergir,
        vira conferência manual em cima de um relatório em que ninguém confia.
        A paginação da tela não se aplica: o relatório traz o período inteiro.
        """
        q = select(Transacao).where(
            Transacao.empresa_id == self._empresa_id,
            Transacao.deleted_at.is_(None),
        )
        if data.agencia_id:
            q = q.where(Transacao.agencia_id == data.agencia_id)
        if data.status:
            q = q.where(Transacao.status == data.status)
        # `Transacao.data` é `Date`; o filtro chega como datetime porque o mesmo
        # schema serve ao razão, cujo `data_lancamento` é instante. Converter aqui
        # evita que o Postgres resolva a comparação pelo fuso da sessão.
        if data.data_de:
            q = q.where(Transacao.data >= data.data_de.date())
        if data.data_ate:
            q = q.where(Transacao.data <= data.data_ate.date())
        if data.dc:
            q = q.where(Transacao.dc == data.dc)
        if data.valor_min is not None:
            q = q.where(Transacao.valor >= data.valor_min)
        if data.valor_max is not None:
            q = q.where(Transacao.valor <= data.valor_max)
        if data.historico:
            q = q.where(Transacao.historico.ilike(f"%{data.historico.strip()}%"))

        transacoes = (
            # Mesma ordenação da tela, pela mesma função: o arquivo tem de bater
            # linha a linha com o que o contador está vendo.
            await self._db.execute(ordenar_como_o_extrato(q))
        ).scalars().all()

        # Resolve o nome da agência em lote: a alternativa é uma query por linha.
        agencias: dict = {}
        ag_ids = {t.agencia_id for t in transacoes if t.agencia_id}
        if ag_ids:
            ag_rows = (
                await self._db.execute(
                    select(AgenciaBancaria).where(AgenciaBancaria.id.in_(ag_ids))
                )
            ).scalars().all()
            agencias = {a.id: a.descricao for a in ag_rows}

        linhas = [
            {
                "data": t.data.strftime("%d/%m/%Y"),
                "agencia": agencias.get(t.agencia_id, ""),
                "historico": t.historico,
                "dc": t.dc,
                "valor": _as_decimal(t.valor),
                "status": t.status,
            }
            for t in transacoes
        ]

        if fmt == "csv":
            conteudo = self._dicts_to_csv(linhas, _COLUNAS_EXTRATO)
        else:
            conteudo = self._dicts_to_xlsx(linhas, _COLUNAS_EXTRATO, "Extrato Bancário")
        return transacoes, conteudo

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

        # O filtro abaixo é uma comparação de CNPJ. Se o CNPJ da empresa não é um
        # CNPJ real — 72 das 77 empresas migradas entraram com placeholder gerado
        # pelo scripts/import_mrcont.py — ele nunca casa com nenhum emitente e a
        # exportação volta vazia, sem erro. Falhar aqui é o que torna isso visível.
        if not cnpj_valido(empresa_row.cnpj):
            logger.warning(
                "exportacao.cnpj_empresa_invalido",
                empresa_id=str(self._empresa_id),
                cnpj=empresa_row.cnpj,
                tipo=tipo,
            )
            raise ValidationError(
                message=(
                    f"A empresa '{empresa_row.razao_social}' está cadastrada com o CNPJ "
                    f"{empresa_row.cnpj}, que não é um CNPJ válido. A exportação de notas "
                    "identifica as notas da empresa comparando esse CNPJ com o emitente e o "
                    "destinatário, então o resultado sairia vazio. Corrija o CNPJ no cadastro "
                    "da empresa e exporte novamente."
                ),
                details={"empresa_id": str(self._empresa_id), "cnpj": empresa_row.cnpj},
            )

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
                "valor": _as_decimal(n.valor),
                "chave_acesso": n.chave_acesso or "",
                "status": n.status or "",
                "data_transacao": t.data.isoformat() if t else "",
                "valor_transacao": _as_decimal(t.valor) if t else "",
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
        q = select(Transacao).where(
            Transacao.empresa_id == self._empresa_id,
            Transacao.deleted_at.is_(None),
        )
        # `Transacao.data` é `Date`; o filtro chega como datetime porque o mesmo
        # schema serve ao razão, cujo `data_lancamento` é instante. Converter aqui
        # evita que o Postgres resolva a comparação pelo fuso da sessão.
        if data.data_de:
            q = q.where(Transacao.data >= data.data_de.date())
        if data.data_ate:
            q = q.where(Transacao.data <= data.data_ate.date())

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

            tem_documento = bool(notas_map.get(t.id) or comp_map.get(t.id))
            total_notas = sum((_as_decimal(n.valor) for n in notas if n), Decimal("0.00"))
            total_comprovantes = sum(
                (_as_decimal(c.valor_pago) for c in comps if c), Decimal("0.00")
            )
            total_documentos = total_notas + total_comprovantes

            # Notas e comprovantes vinculados herdam a direção da transação. O
            # residual fica assinado: positivo aumenta o saldo, negativo reduz.
            valor_transacao = _valor_assinado(_as_decimal(t.valor), t.dc)
            valor_documentos = _valor_assinado(total_documentos, t.dc)
            divergencia = valor_documentos - valor_transacao

            if not tem_documento:
                status_conc = "Pendente"
            elif abs(divergencia) <= _TOLERANCIA_CONCILIACAO:
                status_conc = "Conciliado"
            else:
                status_conc = "Divergente"

            for idx, (n, c) in enumerate(zip(notas_padded, comps_padded)):
                row: dict = {
                    "data_transacao": t.data.isoformat() if idx == 0 else "",
                    "historico_extrato": t.historico if idx == 0 else "",
                    "dc": t.dc if idx == 0 else "",
                    "valor_transacao": _as_decimal(t.valor) if idx == 0 else "",
                    "status_transacao": t.status if idx == 0 else "",
                    "nota_numero": n.numero if n else "",
                    "tipo_nota": n.tipo if n else "",
                    "valor_nota": _as_decimal(n.valor) if n else "",
                    "favorecido_comprovante": c.favorecido if c else "",
                    "valor_pago_comprovante": _as_decimal(c.valor_pago) if c else "",
                    "total_documentos": total_documentos if idx == 0 else "",
                    "divergencia_residual": divergencia if idx == 0 else "",
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
        return _new_workbook(title, colunas)

    def _save_workbook(self, wb) -> bytes:
        return _save_workbook(wb)

    def _dicts_to_csv(self, linhas: list[dict], colunas: list[str]) -> bytes:
        return _dicts_to_csv(linhas, colunas)

    def _dicts_to_txt(self, linhas: list[dict], colunas: list[str]) -> bytes:
        """Layout exato do arquivo modelo fornecido pelo escritório para
        importar direto no sistema contábil deles: delimitado por ';', SEM
        linha de cabeçalho, e "Valor" com vírgula decimal e sem zeros à
        direita (`700` em vez de `700.00`; `1,19` em vez de `1.19`) — padrão
        brasileiro, e diferente do .csv/.xlsx (que mantêm ponto e 2 casas).
        Só a coluna "Valor" leva essa conversão — colunas de código de conta
        podem ter ponto de verdade (ex: '3.4.2.05.0009') e não podem ser
        tocadas.
        """
        return _dicts_to_txt(linhas, colunas)

    def _dicts_to_xlsx(
        self, linhas: list[dict], colunas: list[str], sheet_name: str
    ) -> bytes:
        return _dicts_to_xlsx(linhas, colunas, sheet_name)
