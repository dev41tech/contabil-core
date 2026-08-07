"""Provedor Mock — desenvolvimento sem credenciais Pluggy.

Gera dados realistas para testar o fluxo completo de Open Banking
sem precisar de credenciais reais ou conexão com bancos.

Transações são determinísticas baseadas no account_id, então
re-sincronizações não duplicam dados.
"""

from __future__ import annotations

import hashlib
import random
import secrets
from datetime import date, timedelta

from src.domain.openbanking.providers.base import (
    ContaInfo,
    IOpenBankingProvider,
    TransacaoInfo,
)

_MOCK_BANKS = [
    ("NUBANK",   "NU PAGAMENTOS S.A.",         "260"),
    ("ITAU",     "Itaú Unibanco S.A.",          "341"),
    ("BRADESCO", "Banco Bradesco S.A.",          "237"),
    ("BB",       "Banco do Brasil S.A.",         "001"),
    ("SICOOB",   "Banco Cooperativo Sicoob",     "756"),
    ("INTER",    "Banco Inter S.A.",             "077"),
    ("C6",       "C6 Bank S.A.",                 "336"),
    ("SANTANDER","Banco Santander (Brasil) S.A.","033"),
]

_HISTORICOS_DEBITO = [
    "PAGTO FORNECEDOR", "COMPRA COMBUSTIVEL", "ENERGIA ELETRICA",
    "INTERNET FIBRA", "ALUGUEL IMOVEL", "SEGURO EMPRESARIAL",
    "MATERIAL ESCRITORIO", "SERVICO CONTABILIDADE", "FOLHA PAGAMENTO",
    "FGTS COMPETENCIA", "IMPOSTO ISS", "IMPOSTO DAS",
    "TED SAIDA", "DOC SAIDA", "PIX SAIDA", "TAXA MANUTENCAO CC",
]

_HISTORICOS_CREDITO = [
    "RECEB CLIENTE",    "BOLETO RECEBIDO",    "PIX ENTRADA",
    "TED ENTRADA",      "DOC ENTRADA",        "DEPOSITO CAIXA",
    "JUROS CDB",        "RENDIMENTO POUPANCA","RECEBIMENTO NF",
    "VENDA SERVICOS",   "RECEB DUPLICATA",    "CREDITO CLIENTES",
]


class MockProvider(IOpenBankingProvider):
    """Provedor mock — não faz nenhuma chamada de rede."""

    async def criar_connect_token(
        self,
        item_id: str | None = None,
        client_user_id: str | None = None,
    ) -> str:
        return f"mock_{secrets.token_hex(12)}"

    async def validar_item(self, item_id: str, client_user_id: str) -> bool:
        # O mock não possui backend externo; a sessão assinada é a fronteira de segurança.
        return bool(item_id and client_user_id)

    async def obter_contas(self, item_id: str) -> list[ContaInfo]:
        banco = _banco_from_item(item_id)
        sigla, nome, codigo = banco
        rng = random.Random(item_id)
        agencia = str(rng.randint(1000, 9999))
        numero = str(rng.randint(10000, 99999)) + "-" + str(rng.randint(0, 9))
        return [
            ContaInfo(
                account_id=f"mock_acc_{item_id[:8]}",
                banco_sigla=sigla,
                instituicao_nome=nome,
                instituicao_codigo=codigo,
                agencia=agencia,
                numero=numero,
                tipo="CHECKING",
                saldo=round(rng.uniform(5_000, 200_000), 2),
            )
        ]

    async def obter_transacoes(
        self, account_id: str, data_inicio: date, data_fim: date
    ) -> list[TransacaoInfo]:
        rng = random.Random(account_id + str(data_inicio))
        transacoes: list[TransacaoInfo] = []
        cur = data_inicio

        while cur <= data_fim:
            # 0–3 transações por dia útil
            n = rng.randint(0, 3) if cur.weekday() < 5 else rng.randint(0, 1)
            for i in range(n):
                dc = "D" if rng.random() < 0.65 else "C"
                historicos = _HISTORICOS_DEBITO if dc == "D" else _HISTORICOS_CREDITO
                historico = rng.choice(historicos)
                valor = round(rng.uniform(50, 15_000), 2)

                id_ext = hashlib.md5(
                    f"{account_id}{cur}{i}".encode()
                ).hexdigest()

                transacoes.append(
                    TransacaoInfo(
                        id_externo=id_ext,
                        data=cur,
                        valor=valor,
                        dc=dc,
                        descricao=historico,
                        categoria=None,
                    )
                )
            cur += timedelta(days=1)

        return transacoes

    async def obter_nome_instituicao(self, item_id: str) -> str:
        _, nome, _ = _banco_from_item(item_id)
        return nome


# ── helpers ───────────────────────────────────────────────────────────────────

def _banco_from_item(item_id: str) -> tuple[str, str, str]:
    """Seleciona um banco deterministicamente pelo item_id."""
    idx = int(hashlib.md5(item_id.encode()).hexdigest(), 16) % len(_MOCK_BANKS)
    return _MOCK_BANKS[idx]
