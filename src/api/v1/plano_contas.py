"""Router de Plano de Contas — /api/v1/empresas/{empresa_id}/plano-contas"""

from __future__ import annotations

import zipfile
from uuid import UUID

from fastapi import APIRouter, Depends, File, UploadFile
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import AuthContext, get_company_context, require_csrf
from src.api.uploads import ler_upload_limitado
from src.db.session import get_db
from src.domain.plano_contas.service import PlanoContaService
from src.schemas.plano_contas import (
    PlanoContaCreate,
    PlanoContaExclusaoLoteRequest,
    PlanoContaExclusaoLoteResultado,
    PlanoContaListResponse,
    PlanoContaResponse,
    PlanoContaTreeResponse,
    PlanoContaUpdate,
)


class ImportacaoLinhaErro(BaseModel):
    linha: int
    codigo: str | None = None
    erro: str


class ImportacaoPlanoResult(BaseModel):
    importadas: int
    duplicadas: int
    erros: list[ImportacaoLinhaErro]

router = APIRouter(
    prefix="/empresas/{empresa_id}/plano-contas",
    tags=["plano-contas"],
)


def _svc(empresa_id: UUID, db: AsyncSession) -> PlanoContaService:
    return PlanoContaService(db=db, empresa_id=empresa_id)


@router.get("", response_model=PlanoContaListResponse)
async def listar_contas(
    empresa_id: UUID,
    ctx: AuthContext = Depends(get_company_context),
    db: AsyncSession = Depends(get_db),
) -> PlanoContaListResponse:
    """Lista todas as contas em ordem por código."""
    return await _svc(empresa_id, db).listar()


@router.get("/arvore", response_model=PlanoContaTreeResponse)
async def arvore_contas(
    empresa_id: UUID,
    ctx: AuthContext = Depends(get_company_context),
    db: AsyncSession = Depends(get_db),
) -> PlanoContaTreeResponse:
    """Retorna o plano de contas em estrutura de árvore hierárquica."""
    return await _svc(empresa_id, db).arvore()


@router.get("/{conta_id}", response_model=PlanoContaResponse)
async def obter_conta(
    empresa_id: UUID,
    conta_id: UUID,
    ctx: AuthContext = Depends(get_company_context),
    db: AsyncSession = Depends(get_db),
) -> PlanoContaResponse:
    return await _svc(empresa_id, db).obter(conta_id)


@router.post(
    "",
    response_model=PlanoContaResponse,
    status_code=201,
    dependencies=[Depends(require_csrf)],
)
async def criar_conta(
    empresa_id: UUID,
    body: PlanoContaCreate,
    ctx: AuthContext = Depends(get_company_context),
    db: AsyncSession = Depends(get_db),
) -> PlanoContaResponse:
    """Cria uma conta contábil. Se pai_id informado, a conta é filha."""
    return await _svc(empresa_id, db).criar(body)


@router.patch(
    "/{conta_id}",
    response_model=PlanoContaResponse,
    dependencies=[Depends(require_csrf)],
)
async def atualizar_conta(
    empresa_id: UUID,
    conta_id: UUID,
    body: PlanoContaUpdate,
    ctx: AuthContext = Depends(get_company_context),
    db: AsyncSession = Depends(get_db),
) -> PlanoContaResponse:
    """Atualiza descrição e/ou tipo. O código é imutável após criação."""
    return await _svc(empresa_id, db).atualizar(conta_id, body)


@router.delete(
    "/{conta_id}",
    status_code=204,
    dependencies=[Depends(require_csrf)],
)
async def remover_conta(
    empresa_id: UUID,
    conta_id: UUID,
    ctx: AuthContext = Depends(get_company_context),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Remove a conta (soft delete). Bloqueado se houver filhos ou regras referenciando."""
    await _svc(empresa_id, db).remover(conta_id)


@router.post(
    "/excluir-lote",
    response_model=PlanoContaExclusaoLoteResultado,
    status_code=200,
    dependencies=[Depends(require_csrf)],
)
async def excluir_contas_em_lote(
    empresa_id: UUID,
    body: PlanoContaExclusaoLoteRequest,
    ctx: AuthContext = Depends(get_company_context),
    db: AsyncSession = Depends(get_db),
) -> PlanoContaExclusaoLoteResultado:
    """Remove várias contas de uma vez (soft delete), ou todo o plano com `todas=true`.

    Cada conta passa pelas mesmas validações da remoção individual (filhos
    ativos, regras, movimentações); contas bloqueadas não impedem a remoção
    das demais — o resultado lista o que foi removido e o que ficou bloqueado.
    """
    return await _svc(empresa_id, db).remover_lote(body.ids, todas=body.todas)


@router.post(
    "/importar",
    response_model=ImportacaoPlanoResult,
    status_code=200,
    dependencies=[Depends(require_csrf)],
)
async def importar_plano_contas(
    empresa_id: UUID,
    arquivo: UploadFile = File(..., description="Arquivo XLSX ou CSV com colunas: codigo, descricao, tipo, tipo_sa"),
    ctx: AuthContext = Depends(get_company_context),
    db: AsyncSession = Depends(get_db),
) -> ImportacaoPlanoResult:
    """Importa contas em lote a partir de arquivo XLSX ou CSV.

    Colunas esperadas: codigo, descricao, tipo, tipo_sa (opcional, padrão A)
    """
    conteudo = await ler_upload_limitado(arquivo)
    nome = (arquivo.filename or "").lower()

    rows = _parse_planilha(conteudo, nome)
    svc = _svc(empresa_id, db)
    return await svc.importar_lote(rows)


# Normaliza nomes de coluna (aceita variantes).
_ALIAS_COLUNA = {
    "classificacao": "codigo", "classificação": "codigo",
    "code": "codigo", "cod": "codigo",
    "description": "descricao", "descrição": "descricao", "nome": "descricao",
    "type": "tipo",
    "sa": "tipo_sa", "s/a": "tipo_sa",
    "id": "conta_numero", "conta": "conta_numero",
}

# Relatórios reais de razão trazem linhas de empresa/CNPJ/página/emissão
# antes do cabeçalho de verdade — sem isso, a primeira linha (quase sempre
# vazia ou metadado) vira "cabeçalho" e toda linha de dado cai como
# "completamente vazia" silenciosamente. Ver relato de 08/2026: usuário
# precisou apagar as primeiras linhas manualmente pra importar.
_MAX_LINHAS_BUSCA_CABECALHO = 30


def _normaliza_linha_cabecalho(linha) -> list[str]:
    """Normaliza os rótulos de uma linha de cabeçalho candidata.

    "código" (acentuado) é ambíguo: em planilhas tipo MrContador que também
    têm "Classificação" separada, "Código" é o ID numérico de origem — vira
    conta_numero. Sem "Classificação" na mesma linha, "Código" é a própria
    hierarquia — vira codigo, igual à variante sem acento.
    """
    raw = [str(c).strip().lower() if c else "" for c in linha]
    tem_classificacao = any(v in ("classificacao", "classificação") for v in raw)
    header = []
    for v in raw:
        if v == "código":
            header.append("conta_numero" if tem_classificacao else "codigo")
        else:
            header.append(_ALIAS_COLUNA.get(v, v))
    return header


def _parece_cabecalho(header_normalizado: list[str]) -> bool:
    return "codigo" in header_normalizado and "descricao" in header_normalizado


def _parse_planilha(conteudo: bytes, nome: str) -> list[dict]:
    """Parseia XLSX ou CSV e retorna lista de dicts com {linha, codigo, descricao, tipo, tipo_sa}."""
    if nome.endswith(".xlsx") or nome.endswith(".xls"):
        return _parse_xlsx(conteudo)
    else:
        return _parse_csv(conteudo)


def _parse_xlsx(conteudo: bytes) -> list[dict]:
    from io import BytesIO
    from itertools import chain, islice

    from src.core.errors import ValidationError as AppValidationError

    _validar_xlsx_compactado(conteudo)
    try:
        import openpyxl
        wb = openpyxl.load_workbook(BytesIO(conteudo), read_only=True, data_only=True)
        ws = wb.active
        rows = ws.iter_rows(values_only=True)
    except Exception as e:
        raise AppValidationError(message=f"Erro ao ler XLSX: {e}")

    buffer = list(islice(rows, _MAX_LINHAS_BUSCA_CABECALHO))
    header = None
    start_line = 2
    for i, linha in enumerate(buffer):
        if not linha:
            continue
        candidato = _normaliza_linha_cabecalho(linha)
        if _parece_cabecalho(candidato):
            header = candidato
            resto_buffer = buffer[i + 1:]
            start_line = i + 2
            break

    if header is None:
        wb.close()
        raise AppValidationError(
            message=(
                f"Cabeçalho não encontrado nas primeiras {_MAX_LINHAS_BUSCA_CABECALHO} "
                'linhas — a planilha precisa ter uma linha com as colunas '
                '"Código"/"Classificação" e "Descrição".'
            )
        )

    if len(header) > _MAX_XLSX_COLUNAS:
        wb.close()
        raise AppValidationError(
            message=f"Planilha excede o limite de {_MAX_XLSX_COLUNAS} colunas."
        )
    try:
        return _rows_to_dicts(
            header,
            chain(resto_buffer, rows),
            start_line=start_line,
            max_rows=_MAX_XLSX_LINHAS,
            max_cells=_MAX_XLSX_CELULAS,
        )
    finally:
        wb.close()


_MAX_XLSX_ENTRADAS = 1_000
_MAX_XLSX_DESCOMPACTADO = 100 * 1024 * 1024
_MAX_XLSX_RAZAO_COMPRESSAO = 200
_MAX_XLSX_LINHAS = 50_000
_MAX_XLSX_COLUNAS = 100
_MAX_XLSX_CELULAS = 500_000


def _validar_xlsx_compactado(conteudo: bytes) -> None:
    from io import BytesIO

    from src.core.errors import ValidationError as AppValidationError

    try:
        with zipfile.ZipFile(BytesIO(conteudo)) as zf:
            infos = zf.infolist()
            if len(infos) > _MAX_XLSX_ENTRADAS:
                raise AppValidationError(message="XLSX contém entradas internas demais.")
            total = sum(info.file_size for info in infos)
            if total > _MAX_XLSX_DESCOMPACTADO:
                raise AppValidationError(message="XLSX excede o limite descompactado de 100 MiB.")
            for info in infos:
                if info.file_size and (
                    info.compress_size == 0
                    or info.file_size / info.compress_size > _MAX_XLSX_RAZAO_COMPRESSAO
                ):
                    raise AppValidationError(message="XLSX possui razão de compressão insegura.")
    except zipfile.BadZipFile as exc:
        raise AppValidationError(message=f"XLSX inválido: {exc}") from exc


def _parse_csv(conteudo: bytes) -> list[dict]:
    import csv
    from io import StringIO
    try:
        text = conteudo.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = conteudo.decode("latin-1")

    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
    except csv.Error:
        # Linhas de metadados sem vírgula/ponto-e-vírgula suficiente antes do
        # cabeçalho real confundem o sniffer — vírgula é a aposta mais segura.
        dialect = csv.excel
    reader = csv.reader(StringIO(text), dialect)
    rows = list(reader)

    if not rows:
        from src.core.errors import ValidationError as AppValidationError
        raise AppValidationError(message="CSV vazio.")

    header = None
    start_line = 2
    for i, linha in enumerate(rows[:_MAX_LINHAS_BUSCA_CABECALHO]):
        if not any(c.strip() for c in linha):
            continue
        candidato = _normaliza_linha_cabecalho(linha)
        if _parece_cabecalho(candidato):
            header = candidato
            data_rows = rows[i + 1:]
            start_line = i + 2
            break

    if header is None:
        from src.core.errors import ValidationError as AppValidationError
        raise AppValidationError(
            message=(
                f"Cabeçalho não encontrado nas primeiras {_MAX_LINHAS_BUSCA_CABECALHO} "
                'linhas — o CSV precisa ter uma linha com as colunas '
                '"Código"/"Classificação" e "Descrição".'
            )
        )

    return _rows_to_dicts(header, data_rows, start_line=start_line)


def _rows_to_dicts(
    normalized: list[str],
    data_rows,
    start_line: int,
    max_rows: int | None = None,
    max_cells: int | None = None,
) -> list[dict]:
    """`normalized` já passou por `_normaliza_linha_cabecalho` — não renormaliza aqui."""
    header = normalized
    result = []
    for i, row in enumerate(data_rows, start=start_line):
        if max_rows is not None and i - start_line >= max_rows:
            from src.core.errors import ValidationError as AppValidationError
            raise AppValidationError(message=f"Planilha excede o limite de {max_rows} linhas.")
        if max_cells is not None and (i - start_line + 1) * len(header) > max_cells:
            from src.core.errors import ValidationError as AppValidationError
            raise AppValidationError(message=f"Planilha excede o limite de {max_cells} células.")
        cells = [str(c).strip() if c is not None else "" for c in row]
        d = {normalized[j]: cells[j] for j in range(min(len(normalized), len(cells)))}
        # Preenche colunas ausentes
        d.setdefault("codigo", "")
        d.setdefault("descricao", "")
        d.setdefault("tipo", "")
        d.setdefault("tipo_sa", "A")
        d["_linha"] = i
        # Ignora linhas completamente vazias
        if not d["codigo"] and not d["descricao"]:
            continue
        result.append(d)
    return result
