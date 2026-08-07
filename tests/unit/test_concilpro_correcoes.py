from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from src.api.v1.concilpro import _calcular_posicao_fornecedor, _celula_texto_segura
from src.domain.concilpro import ai_classifier
from src.domain.concilpro.conciliacao_intel import (
    _is_open,
    conciliar_todos_fornecedores_inteligente,
)
from src.domain.concilpro.consolidador import consolidar_lancamentos_fornecedor


@pytest.mark.parametrize(
    ("tipo", "esperado", "status"),
    [
        ("C", Decimal("10000.00"), "EM_ABERTO"),
        ("D", Decimal("-10000.00"), "ADIANTADO"),
    ],
)
def test_saldo_anterior_sem_movimento_mantem_posicao(tipo, esperado, status):
    saldo, saldo_tipo, resultado = _calcular_posicao_fornecedor(
        Decimal("10000"), tipo, 0, 0, False
    )

    assert saldo == esperado
    assert saldo_tipo == tipo
    assert resultado == status


def test_conta_realmente_sem_saldo_e_movimento_tem_status_especifico():
    assert _calcular_posicao_fornecedor(0, "", 0, 0, False) == (
        Decimal("0.00"),
        "",
        "SEM_MOVIMENTO",
    )


def test_saldo_final_usa_abertura_creditos_e_debitos_na_mesma_equacao():
    assert _calcular_posicao_fornecedor(100, "C", 40, 20, True) == (
        Decimal("80.00"),
        "C",
        "EM_ABERTO",
    )


@pytest.mark.parametrize("prefixo", ["=", "+", "-", "@"])
def test_texto_exportado_nao_vira_formula(prefixo):
    valor = prefixo + "SOMA(A1:A2)"
    assert _celula_texto_segura(valor) == "'" + valor


def test_um_centavo_continua_em_aberto():
    compra = SimpleNamespace(valor_saldo=Decimal("0.01"))
    assert _is_open(compra)


def test_consolidador_nao_funde_nfs_de_series_diferentes():
    base = {
        "numero_nf": "123",
        "data_lancamento": datetime(2026, 8, 7),
        "tipo_operacao": "COMPRA",
        "valor_credito": Decimal("10.00"),
    }
    lancamentos = [
        {**base, "historico": "COMPRA NF 123 SÉRIE 1"},
        {**base, "historico": "COMPRA NF 123 SÉRIE 2"},
    ]

    assert len(consolidar_lancamentos_fornecedor(lancamentos)) == 2


def test_falha_em_um_batch_vision_invalida_toda_extracao(monkeypatch):
    chamadas = 0

    def batch(_client, _imagens, _texto=""):
        nonlocal chamadas
        chamadas += 1
        if chamadas == 2:
            raise ai_classifier.VisionBatchError("indisponível")
        return {
            "saldo_anterior": 0,
            "saldo_anterior_tipo": "",
            "total_debito": 0,
            "total_credito": 1,
            "lancamentos": [{"id": chamadas}],
        }

    monkeypatch.setattr(ai_classifier, "_get_client", lambda: object())
    monkeypatch.setattr(ai_classifier, "_visao_batch", batch)

    with pytest.raises(ai_classifier.VisionBatchError, match="batch Vision 2 de 3"):
        ai_classifier.parsear_bloco_fornecedor_ia_visao([b"png"] * 7)


def test_conciliacao_nao_commita_transacao_internamente():
    db = MagicMock()
    db.query.return_value.filter.return_value.all.return_value = []

    conciliar_todos_fornecedores_inteligente(db, arquivo_id=1, empresa_id=uuid4())

    db.commit.assert_not_called()
    db.flush.assert_called_once()
