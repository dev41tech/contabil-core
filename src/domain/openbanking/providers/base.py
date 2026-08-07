"""Abstração de provedor Open Banking.

Qualquer provedor (Pluggy, Belvo, Mock) deve implementar esta interface.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date
from decimal import Decimal


@dataclass
class ContaInfo:
    """Dados de uma conta bancária retornados pelo provedor."""
    account_id: str
    banco_sigla: str
    instituicao_nome: str
    instituicao_codigo: str | None
    agencia: str | None
    numero: str | None
    tipo: str  # "CHECKING" | "SAVINGS" | "CREDIT"
    saldo: Decimal | None


@dataclass
class TransacaoInfo:
    """Dados de uma transação bancária retornados pelo provedor."""
    id_externo: str      # ID único no provedor (usado para dedup)
    data: date
    valor: Decimal       # sempre positivo
    dc: str              # "D" ou "C"
    descricao: str
    categoria: str | None


class IOpenBankingProvider(ABC):
    """Interface que todo provedor deve implementar."""

    @abstractmethod
    async def criar_connect_token(
        self,
        item_id: str | None = None,
        client_user_id: str | None = None,
    ) -> str:
        """Retorna token para abrir o widget de autenticação do banco.

        Se `item_id` é passado, cria um token para re-autenticar um item existente.
        """
        ...

    @abstractmethod
    async def validar_item(self, item_id: str, client_user_id: str) -> bool:
        """Confirma que o item foi criado para a sessão local informada."""
        ...

    @abstractmethod
    async def obter_contas(self, item_id: str) -> list[ContaInfo]:
        """Retorna as contas bancárias disponíveis para um item."""
        ...

    @abstractmethod
    async def obter_transacoes(
        self,
        account_id: str,
        data_inicio: date,
        data_fim: date,
    ) -> list[TransacaoInfo]:
        """Retorna transações de uma conta no período informado."""
        ...

    @abstractmethod
    async def obter_nome_instituicao(self, item_id: str) -> str:
        """Retorna o nome legível da instituição financeira."""
        ...
