"""Serviço de Open Banking.

Orquestra o ciclo completo:
  1. Criação de connect_token (widget de autenticação do banco)
  2. Salvamento da conexão após autenticação bem-sucedida
  3. Sincronização de transações (manual ou futuramente agendada)

Provedores suportados:
  - Pluggy  : quando PLUGGY_CLIENT_ID e PLUGGY_CLIENT_SECRET estão configurados
  - Mock    : modo de desenvolvimento sem credenciais externas

Mapeamento Pluggy → nosso modelo:
  item         → ConexaoBancaria
  account      → ConexaoBancaria + AgenciaBancaria (criada na 1ª sync)
  transaction  → Transacao        (dedup via hash_dedup)
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from datetime import UTC, date, datetime, timedelta
from uuid import UUID

import structlog
from jose import JWTError, jwt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import get_settings
from src.core.errors import (
    ConflictError,
    NotFoundError,
    PluggyUnavailableError,
    ValidationError,
)
from src.db.models import AgenciaBancaria, ConexaoBancaria, Transacao
from src.domain.auditoria import registrar_auditoria
from src.domain.openbanking.providers.base import IOpenBankingProvider
from src.domain.openbanking.providers.mock import MockProvider
from src.domain.openbanking.providers.pluggy import PluggyProvider
from src.schemas.openbanking import (
    ConnectTokenResponse,
    ConexaoListResponse,
    ConexaoResponse,
    SalvarConexaoRequest,
    SincronizarRequest,
    SincronizarResponse,
)

logger = structlog.get_logger(__name__)

_CONNECT_SESSION_TYPE = "openbanking_connect"
_CONNECT_SESSION_TTL = timedelta(minutes=15)
_JWT_ALGORITHM = "HS256"


def _get_provider() -> tuple[IOpenBankingProvider, str]:
    """Retorna (provedor, nome_provedor) com base na configuração."""
    settings = get_settings()
    if settings.pluggy_enabled:
        return (
            PluggyProvider(
                settings.pluggy_client_id,  # type: ignore[arg-type]
                settings.pluggy_client_secret.get_secret_value(),  # type: ignore[union-attr]
            ),
            "pluggy",
        )
    if settings.is_development:
        return MockProvider(), "mock"
    raise PluggyUnavailableError(
        message=(
            "Open Banking não está configurado: defina PLUGGY_CLIENT_ID e "
            "PLUGGY_CLIENT_SECRET. O provedor mock só é permitido em desenvolvimento."
        )
    )


class OpenBankingService:
    def __init__(self, db: AsyncSession, empresa_id: UUID) -> None:
        self._db = db
        self._empresa_id = empresa_id
        self._provider, self._provedor_nome = _get_provider()

    # ── connect token ─────────────────────────────────────────────────────────

    async def criar_connect_token(
        self, item_id: str | None = None
    ) -> ConnectTokenResponse:
        """Cria token para o widget de autenticação bancária."""
        connection_session, client_user_id = self._criar_sessao_conexao()
        token = await self._provider.criar_connect_token(
            item_id, client_user_id=client_user_id
        )
        return ConnectTokenResponse(
            access_token=token,
            connection_session=connection_session,
            provedor=self._provedor_nome,
            mock_mode=self._provedor_nome == "mock",
        )

    # ── salvar conexão ────────────────────────────────────────────────────────

    async def salvar_conexao(self, data: SalvarConexaoRequest) -> ConexaoListResponse:
        """Persiste todas as contas após validar item, sessão e empresa."""
        client_user_id = self._validar_sessao_conexao(data.connection_session)

        # Evita duplicatas (mesmo item_id por empresa)
        existente = (
            await self._db.execute(
                select(ConexaoBancaria).where(
                    ConexaoBancaria.empresa_id == self._empresa_id,
                    ConexaoBancaria.item_id == data.item_id,
                    ConexaoBancaria.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        if existente:
            raise ConflictError(
                message="Esta conta já está conectada. Utilize 'Sincronizar' para atualizar."
            )

        # Confirma no provedor que o item nasceu do token emitido para esta empresa.
        try:
            item_valido = await self._provider.validar_item(
                data.item_id, client_user_id
            )
            if not item_valido:
                raise ValidationError(
                    message="O item bancário não pertence a esta sessão de conexão."
                )
            contas = await self._provider.obter_contas(data.item_id)
            nome_inst = await self._provider.obter_nome_instituicao(data.item_id)
        except ValidationError:
            raise
        except Exception as exc:
            logger.exception(
                "openbanking.validacao_item_falhou",
                empresa_id=str(self._empresa_id),
                provedor=self._provedor_nome,
                item_id=data.item_id,
            )
            raise ValidationError(
                message="Não foi possível validar a conexão bancária no momento."
            ) from exc

        if not contas:
            raise ValidationError(
                message="Nenhuma conta corrente/poupança encontrada para este item."
            )

        conexoes = []
        account_ids: set[str] = set()
        for conta in contas:
            if conta.account_id in account_ids:
                continue
            account_ids.add(conta.account_id)
            conexao = ConexaoBancaria(
                id=uuid.uuid4(),
                empresa_id=self._empresa_id,
                provedor=self._provedor_nome,
                item_id=data.item_id,
                account_id_externo=conta.account_id,
                instituicao_nome=(
                    data.instituicao_nome
                    if self._provedor_nome == "mock" and data.instituicao_nome
                    else nome_inst
                ),
                instituicao_codigo=conta.instituicao_codigo,
                banco_sigla=conta.banco_sigla,
                agencia_numero=conta.agencia,
                conta_numero=conta.numero,
                status="ativa",
            )
            self._db.add(conexao)
            conexoes.append(conexao)
        await self._db.flush()

        for conexao in conexoes:
            await registrar_auditoria(
                self._db,
                empresa_id=self._empresa_id,
                acao="openbanking.conexao_criada",
                entidade="conexao_bancaria",
                entidade_id=conexao.id,
                dados_depois=_snapshot_conexao(conexao),
            )

        logger.info(
            "openbanking.conexao_criada",
            empresa_id=str(self._empresa_id),
            item_id=data.item_id,
            total_contas=len(conexoes),
            provedor=self._provedor_nome,
        )
        return ConexaoListResponse(
            items=[_to_response(conexao) for conexao in conexoes],
            total=len(conexoes),
        )

    # ── listar ────────────────────────────────────────────────────────────────

    async def listar(self) -> ConexaoListResponse:
        rows = (
            await self._db.execute(
                select(ConexaoBancaria)
                .where(
                    ConexaoBancaria.empresa_id == self._empresa_id,
                    ConexaoBancaria.deleted_at.is_(None),
                )
                .order_by(ConexaoBancaria.instituicao_nome)
            )
        ).scalars().all()
        items = [_to_response(c) for c in rows]
        return ConexaoListResponse(items=items, total=len(items))

    # ── sincronizar ───────────────────────────────────────────────────────────

    async def sincronizar(
        self, conexao_id: UUID, req: SincronizarRequest
    ) -> SincronizarResponse:
        conexao = await self._get_or_404(conexao_id)
        conexao_antes = _snapshot_conexao(conexao)

        data_fim = date.today()
        data_inicio = data_fim - timedelta(days=req.dias)

        # Garante que existe uma AgenciaBancaria vinculada
        agencia = await self._garantir_agencia(conexao)

        # Busca transações no provedor
        try:
            transacoes_externas = await self._provider.obter_transacoes(
                conexao.account_id_externo or conexao.item_id,
                data_inicio,
                data_fim,
            )
        except Exception as exc:
            conexao.status = "erro"
            conexao.erro_msg = "Falha temporária ao sincronizar transações."
            await self._db.flush()
            logger.exception(
                "openbanking.sincronizacao_falhou",
                empresa_id=str(self._empresa_id),
                conexao_id=str(conexao_id),
                provedor=self._provedor_nome,
            )
            raise ValidationError(
                message="Não foi possível sincronizar as transações bancárias no momento."
            ) from exc

        importadas = 0
        duplicadas = 0
        erros = 0

        for t in transacoes_externas:
            # hash_dedup: sha256(empresa_id + id_externo)
            hash_raw = f"{self._empresa_id}{conexao.item_id}{t.id_externo}"
            hash_dedup = hashlib.sha256(hash_raw.encode()).hexdigest()

            # Pula duplicatas
            # Só linha VIVA duplica — mesmo motivo da importação de extrato
            # (ver migration 0033). Aqui pesa mais: a sincronização roda
            # sozinha, então uma transação cancelada bloquearia a volta do
            # lançamento sem ninguém olhando.
            existe = (
                await self._db.execute(
                    select(Transacao).where(
                        Transacao.empresa_id == self._empresa_id,
                        Transacao.hash_dedup == hash_dedup,
                        Transacao.deleted_at.is_(None),
                    )
                )
            ).scalar_one_or_none()

            if existe:
                duplicadas += 1
                continue

            try:
                transacao = Transacao(
                    id=uuid.uuid4(),
                    empresa_id=self._empresa_id,
                    agencia_id=agencia.id,
                    data=datetime.combine(t.data, datetime.min.time()).replace(tzinfo=UTC),
                    valor=t.valor,
                    historico=t.descricao[:500],
                    dc=t.dc,
                    status="pendente",
                    hash_dedup=hash_dedup,
                )
                self._db.add(transacao)
                importadas += 1
            except Exception:
                erros += 1

        # Atualiza estado da conexão
        now = datetime.now(UTC)
        conexao.last_sync_at = now
        conexao.next_sync_at = now + timedelta(hours=6)
        conexao.status = "ativa"
        conexao.erro_msg = None
        conexao.total_transacoes_sync += importadas
        await self._db.flush()
        await registrar_auditoria(
            self._db,
            empresa_id=self._empresa_id,
            acao="openbanking.conexao_sincronizada",
            entidade="conexao_bancaria",
            entidade_id=conexao.id,
            dados_antes=conexao_antes,
            dados_depois=_snapshot_conexao(conexao),
        )

        logger.info(
            "openbanking.sincronizado",
            conexao_id=str(conexao_id),
            empresa_id=str(self._empresa_id),
            importadas=importadas,
            duplicadas=duplicadas,
            erros=erros,
        )
        return SincronizarResponse(
            conexao_id=conexao_id,
            importadas=importadas,
            duplicadas=duplicadas,
            erros=erros,
            periodo_inicio=str(data_inicio),
            periodo_fim=str(data_fim),
            status="concluido",
        )

    # ── reconectar ────────────────────────────────────────────────────────────

    async def criar_reconnect_token(self, conexao_id: UUID) -> ConnectTokenResponse:
        """Token para re-autenticar uma conexão expirada."""
        conexao = await self._get_or_404(conexao_id)
        token = await self._provider.criar_connect_token(conexao.item_id)
        await registrar_auditoria(
            self._db,
            empresa_id=self._empresa_id,
            acao="openbanking.reconexao_iniciada",
            entidade="conexao_bancaria",
            entidade_id=conexao.id,
            dados_antes=_snapshot_conexao(conexao),
            dados_depois={"reautenticacao_solicitada": True},
        )
        return ConnectTokenResponse(
            access_token=token,
            provedor=self._provedor_nome,
            mock_mode=self._provedor_nome == "mock",
        )

    # ── remover ───────────────────────────────────────────────────────────────

    async def remover(self, conexao_id: UUID) -> None:
        conexao = await self._get_or_404(conexao_id)
        antes = _snapshot_conexao(conexao)
        conexao.deleted_at = datetime.now(UTC)
        await self._db.flush()
        await registrar_auditoria(
            self._db,
            empresa_id=self._empresa_id,
            acao="openbanking.conexao_removida",
            entidade="conexao_bancaria",
            entidade_id=conexao.id,
            dados_antes=antes,
            dados_depois=_snapshot_conexao(conexao),
        )
        logger.info("openbanking.conexao_removida", conexao_id=str(conexao_id))

    # ── helpers ───────────────────────────────────────────────────────────────

    async def _get_or_404(self, conexao_id: UUID) -> ConexaoBancaria:
        c = (
            await self._db.execute(
                select(ConexaoBancaria).where(
                    ConexaoBancaria.id == conexao_id,
                    ConexaoBancaria.empresa_id == self._empresa_id,
                    ConexaoBancaria.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        if not c:
            raise NotFoundError(message="Conexão bancária não encontrada.")
        return c

    def _criar_sessao_conexao(self) -> tuple[str, str]:
        settings = get_settings()
        now = datetime.now(UTC)
        client_user_id = secrets.token_urlsafe(24)
        token = jwt.encode(
            {
                "typ": _CONNECT_SESSION_TYPE,
                "empresa_id": str(self._empresa_id),
                "client_user_id": client_user_id,
                "iat": now,
                "exp": now + _CONNECT_SESSION_TTL,
            },
            settings.secret_key.get_secret_value(),
            algorithm=_JWT_ALGORITHM,
        )
        return token, client_user_id

    def _validar_sessao_conexao(self, token: str) -> str:
        settings = get_settings()
        try:
            payload = jwt.decode(
                token,
                settings.secret_key.get_secret_value(),
                algorithms=[_JWT_ALGORITHM],
            )
            empresa_id = payload.get("empresa_id")
            client_user_id = payload.get("client_user_id")
            tipo = payload.get("typ")
            if (
                tipo != _CONNECT_SESSION_TYPE
                or not isinstance(empresa_id, str)
                or not hmac.compare_digest(empresa_id, str(self._empresa_id))
                or not isinstance(client_user_id, str)
                or not client_user_id
            ):
                raise ValidationError(
                    message="Sessão de conexão inválida para esta empresa."
                )
            return client_user_id
        except ValidationError:
            raise
        except JWTError as exc:
            raise ValidationError(
                message="Sessão de conexão inválida ou expirada. Gere um novo token."
            ) from exc

    async def _garantir_agencia(self, conexao: ConexaoBancaria) -> AgenciaBancaria:
        """Cria AgenciaBancaria se ainda não existe, e vincula à conexão."""
        if conexao.agencia_id:
            ag = (
                await self._db.execute(
                    select(AgenciaBancaria).where(
                        AgenciaBancaria.id == conexao.agencia_id,
                        AgenciaBancaria.empresa_id == self._empresa_id,
                        AgenciaBancaria.deleted_at.is_(None),
                    )
                )
            ).scalar_one_or_none()
            if ag:
                return ag

        # Cria nova AgenciaBancaria automaticamente
        agencia = AgenciaBancaria(
            id=uuid.uuid4(),
            empresa_id=self._empresa_id,
            banco_sigla=conexao.banco_sigla,
            agencia=conexao.agencia_numero or "0001",
            numero=conexao.conta_numero or "000000",
            digito=None,
            ativa=True,
        )
        self._db.add(agencia)
        await self._db.flush()

        conexao.agencia_id = agencia.id
        await self._db.flush()

        logger.info(
            "openbanking.agencia_criada",
            agencia_id=str(agencia.id),
            banco=conexao.banco_sigla,
        )
        return agencia


# ── conversão ─────────────────────────────────────────────────────────────────

def _to_response(c: ConexaoBancaria) -> ConexaoResponse:
    return ConexaoResponse(
        id=c.id,
        empresa_id=c.empresa_id,
        agencia_id=c.agencia_id,
        provedor=c.provedor,
        item_id=c.item_id,
        instituicao_nome=c.instituicao_nome,
        instituicao_codigo=c.instituicao_codigo,
        banco_sigla=c.banco_sigla,
        agencia_numero=c.agencia_numero,
        conta_numero=c.conta_numero,
        status=c.status,
        last_sync_at=c.last_sync_at,
        next_sync_at=c.next_sync_at,
        total_transacoes_sync=c.total_transacoes_sync,
        erro_msg=c.erro_msg,
    )


def _snapshot_conexao(conexao: ConexaoBancaria) -> dict[str, object]:
    # Tokens e segredos do provedor nunca entram no snapshot.
    return {
        "provedor": conexao.provedor,
        "item_id": conexao.item_id,
        "account_id_externo": conexao.account_id_externo,
        "agencia_id": conexao.agencia_id,
        "status": conexao.status,
        "last_sync_at": conexao.last_sync_at,
        "next_sync_at": conexao.next_sync_at,
        "total_transacoes_sync": conexao.total_transacoes_sync,
        "erro_msg": conexao.erro_msg,
        "deleted_at": conexao.deleted_at,
    }
