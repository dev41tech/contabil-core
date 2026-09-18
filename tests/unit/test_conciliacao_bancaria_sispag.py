"""Leitura da consulta de pagamentos do SISPAG e os lotes na conciliação.

Os casos reproduzem abr/2025 da BLD com valores trocados: o Itaú junta TED e
crédito em conta em UMA linha "Sispag Fornecedores", e o razão tem cada
pagamento separado.
"""

from __future__ import annotations

import io
from datetime import date
from decimal import Decimal

import openpyxl
import pytest

from src.domain.conciliacao_bancaria.cruzamento import (
    CONCILIADO,
    LOTE_DO_DIA,
    LOTE_SISPAG,
    LOTE_SISPAG_DIVERGENTE,
    SO_EXTRATO,
    SO_RAZAO,
    Item,
    conciliar,
)
from src.domain.conciliacao_bancaria.sispag import PagamentoSispag, SispagInvalido, ler_sispag

D = Decimal
DIA = date(2025, 4, 8)


def _sispag_xlsx(linhas, *, conta="7285 / 12287-0", cnpj="12.345.678/0001-95") -> bytes:
    """Layout da consulta do internet banking: cabeçalho da conta, tabela, total."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append([])
    ws.append(["Dados da conta"])
    ws.append(["Agência/conta", conta, "Nome da empresa:", "EXEMPLO LOGISTICA LTDA"])
    ws.append(["CNPJ:", cnpj])
    ws.append(["Período:", "01/04/2025 - 30/04/2025", "Status:", "todos"])
    ws.append(["favorecido / beneficiário", "CPF/CNPJ", "tipo de pagamento",
               "referência da empresa", "data do pagamento", "valor (R$)", "status"])
    for fav, tipo, data, valor, status in linhas:
        ws.append([fav, "***.331.078-**", tipo, "-", data, valor, status])
    ws.append(["Total:", None, None, None, None, 999.99])
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


# ── leitura ──────────────────────────────────────────────────────────────────

def test_le_os_pagamentos_pelo_nome_da_coluna():
    consulta = ler_sispag(_sispag_xlsx([
        ("FULANO DE TAL", "Conta Corrente", "08/04/2025", 150.5, "Efetuado"),
        ("BOLETO EXEMPLO", "Boleto outros bancos", "08/04/2025", 40213.7, "Não efetuado"),
    ]))
    assert consulta.conta == "7285 / 12287-0"
    assert consulta.cnpj == "12345678000195"
    assert [(p.favorecido, p.tipo, p.data, p.valor, p.efetuado) for p in consulta.pagamentos] == [
        ("FULANO DE TAL", "Conta Corrente", DIA, D("150.50"), True),
        ("BOLETO EXEMPLO", "Boleto outros bancos", DIA, D("40213.70"), False),
    ]


def test_linha_de_total_nao_vira_pagamento():
    pix = ("A", "PIX Transferências", "08/04/2025", 10.0, "Efetuado")
    consulta = ler_sispag(_sispag_xlsx([pix]))
    assert len(consulta.pagamentos) == 1


def test_arquivo_sem_a_tabela_e_recusado():
    wb = openpyxl.Workbook()
    wb.active.append(["qualquer", "coisa"])
    out = io.BytesIO()
    wb.save(out)
    with pytest.raises(SispagInvalido, match="tabela de pagamentos"):
        ler_sispag(out.getvalue())


def test_formato_que_nao_e_planilha_e_recusado():
    with pytest.raises(SispagInvalido, match="XLS"):
        ler_sispag(b"%PDF-1.4 qualquer coisa")


# ── camadas ──────────────────────────────────────────────────────────────────

_NOMES = ["ANTONELLA", "RICARDO", "MAICON", "GENTIL", "DAIANE", "ADRIANO", "PEDRO", "RODRIGO"]
_VALORES = ["6454.65", "2470.92", "1456.00", "1028.00", "819.04", "717.04", "625.90", "367.92"]


def _lote_no_razao(inicio=0):
    return [
        Item(f"r{inicio + i}", DIA, -D(v), f"PGTO {nome} EXEMPLO")
        for i, (nome, v) in enumerate(zip(_NOMES, _VALORES, strict=True))
    ]


def _pagamentos(nomes=_NOMES, valores=_VALORES, tipo="Conta Corrente"):
    return [PagamentoSispag(DIA, D(v), tipo, f"{nome} EXEMPLO DA SILVA", "", True)
            for nome, v in zip(nomes, valores, strict=True)]


_TOTAL = sum(D(v) for v in _VALORES)  # 13.939,47


def test_sobra_inteira_do_dia_fecha_com_a_linha_do_extrato():
    """Sem arquivo nenhum: tudo o que sobrou no dia é a linha. Não há o que escolher."""
    res = conciliar(_lote_no_razao(), [Item("e1", DIA, -_TOTAL, "Sispag Fornecedores")])
    (grupo,) = res.conciliados
    assert grupo.tipo == LOTE_DO_DIA
    assert len(grupo.razao) == 8
    assert res.pendencias == []


def test_sobra_do_dia_que_nao_fecha_continua_pendencia():
    res = conciliar(_lote_no_razao(), [Item("e1", DIA, -_TOTAL + D("600"), "Sispag Fornecedores")])
    assert res.conciliados == []
    assert sorted({g.tipo for g in res.pendencias}) == [SO_EXTRATO, SO_RAZAO]


def test_sobra_do_dia_nao_mistura_entrada_com_saida():
    razao = [*_lote_no_razao(), Item("r99", DIA, D("500.00"), "RECEBIMENTO CLIENTE")]
    res = conciliar(razao, [Item("e1", DIA, -_TOTAL, "Sispag Fornecedores")])
    assert [g.tipo for g in res.conciliados] == [LOTE_DO_DIA]
    assert [g.razao[0].historico for g in res.pendencias] == ["RECEBIMENTO CLIENTE"]


def test_sispag_separa_dois_lotes_no_mesmo_dia():
    """Em 22/04 a sobra do dia fechava com DUAS linhas, e dividir pela soma era ambíguo."""
    # 7 TEDs: acima do limite do agrupamento pequeno (6), como os lotes reais.
    ted = [Item(f"r{50 + i}", DIA, -D(f"{10 + i}.00"), f"PGTO TED {i}") for i in range(7)]
    extrato = [
        Item("e1", DIA, -_TOTAL, "Sispag Fornecedores"),
        Item("e2", DIA, -D("91.00"), "Sispag Fornecedores TED"),
    ]
    res = conciliar([*_lote_no_razao(), *ted], extrato, sispag=_pagamentos())

    por_tipo = {g.tipo: g for g in res.conciliados}
    assert set(por_tipo) == {LOTE_SISPAG, LOTE_DO_DIA}
    assert [e.id for e in por_tipo[LOTE_SISPAG].extrato] == ["e1"]
    assert len(por_tipo[LOTE_SISPAG].razao) == 8
    assert [e.id for e in por_tipo[LOTE_DO_DIA].extrato] == ["e2"]
    assert res.pendencias == []


def test_pagamento_do_lote_que_nao_esta_no_razao_vira_pendencia_com_o_nome():
    razao = _lote_no_razao()[:-1]  # o razão não tem o RODRIGO (367,92)
    res = conciliar(razao, [Item("e1", DIA, -_TOTAL, "Sispag Fornecedores")], sispag=_pagamentos())

    (grupo,) = res.pendencias
    assert grupo.tipo == LOTE_SISPAG_DIVERGENTE
    assert len(grupo.razao) == 7
    assert [(p.favorecido, p.valor) for p in grupo.sispag_faltando] == [
        ("RODRIGO EXEMPLO DA SILVA", D("367.92"))
    ]
    assert grupo.diferenca == D("367.92")


def test_lote_partido_em_duas_linhas_de_sispag():
    """17/04: o crédito em conta do dia saiu em duas linhas que, juntas, são o lote."""
    parte = D("8000.00")
    extrato = [
        Item("e1", DIA, -parte, "Sispag Fornecedores"),
        Item("e2", DIA, -(_TOTAL - parte), "Sispag Fornecedores"),
    ]
    res = conciliar(_lote_no_razao(), extrato, sispag=_pagamentos())
    (grupo,) = res.conciliados
    assert grupo.tipo == LOTE_SISPAG
    assert sorted(e.id for e in grupo.extrato) == ["e1", "e2"]


def test_mesmo_valor_para_dois_favorecidos_decide_pelo_nome():
    razao = [*_lote_no_razao(), Item("r90", DIA, -D("367.92"), "PGTO CICLANO OUTRO")]
    res = conciliar(razao, [Item("e1", DIA, -_TOTAL, "Sispag Fornecedores")], sispag=_pagamentos())

    lote = next(g for g in res.conciliados if g.tipo == LOTE_SISPAG)
    assert "r90" not in {r.id for r in lote.razao}
    assert [g.razao[0].id for g in res.pendencias] == ["r90"]


def test_sispag_desfaz_o_par_que_a_camada_1_fez_pelo_valor():
    """17/04: o Claudio (20.000) casou com "Sispag Salários" (20.000) só pelo valor.

    O SISPAG mostra o Claudio no lote de crédito em conta; a linha de salários
    fica sem par — e ela é a pendência de verdade.
    """
    nomes = [*_NOMES, "CLAUDIO"]
    valores = [*_VALORES, "20000.00"]
    claudio = Item("r9", DIA, -D("20000.00"), "SISPAG FORNECEDORES CLAUDIO EXEMPLO")
    razao = [*_lote_no_razao(), claudio]
    extrato = [
        Item("e1", DIA, -(_TOTAL + D("20000.00")), "Sispag Fornecedores"),
        Item("e2", DIA, -D("20000.00"), "Sispag Salários"),
    ]
    res = conciliar(razao, extrato, sispag=_pagamentos(nomes, valores))

    lote = next(g for g in res.conciliados if g.tipo == LOTE_SISPAG)
    assert "r9" in {r.id for r in lote.razao}
    assert not any(g.tipo == CONCILIADO for g in res.conciliados)
    (sobra,) = res.pendencias
    assert (sobra.tipo, sobra.extrato[0].historico) == (SO_EXTRATO, "Sispag Salários")


def test_nao_desfaz_par_quando_o_extrato_tambem_nomeia_o_favorecido():
    nomes = [*_NOMES, "CLAUDIO"]
    valores = [*_VALORES, "20000.00"]
    razao = [*_lote_no_razao(), Item("r9", DIA, -D("20000.00"), "PGTO CLAUDIO EXEMPLO")]
    extrato = [
        Item("e1", DIA, -(_TOTAL + D("20000.00")), "Sispag Fornecedores"),
        Item("e2", DIA, -D("20000.00"), "PIX CLAUDIO EXEMPLO"),
    ]
    res = conciliar(razao, extrato, sispag=_pagamentos(nomes, valores))
    assert any(g.tipo == CONCILIADO and g.razao[0].id == "r9" for g in res.conciliados)
    (lote,) = res.pendencias
    assert lote.tipo == LOTE_SISPAG_DIVERGENTE


def test_pagamento_nao_efetuado_nao_entra_no_lote():
    pagamentos = [*_pagamentos(), PagamentoSispag(DIA, D("940262.12"), "Conta Corrente",
                                                  "INTEGRA FROTAS", "", False)]
    res = conciliar(_lote_no_razao(), [Item("e1", DIA, -_TOTAL, "Sispag Fornecedores")],
                    sispag=pagamentos)
    assert [g.tipo for g in res.conciliados] == [LOTE_SISPAG]


def test_duas_combinacoes_de_linhas_com_valores_diferentes_nao_viram_lote():
    """Se o lote pode ser {e1, e2} ou {e3, e4}, o SISPAG não diz qual."""
    extrato = [
        Item("e1", DIA, -D("8000.00"), "Sispag Fornecedores"),
        Item("e2", DIA, -(_TOTAL - D("8000.00")), "Sispag Fornecedores"),
        Item("e3", DIA, -D("7000.00"), "Sispag Fornecedores"),
        Item("e4", DIA, -(_TOTAL - D("7000.00")), "Sispag Fornecedores"),
    ]
    res = conciliar(_lote_no_razao(), extrato, sispag=_pagamentos())
    assert not any(g.tipo == LOTE_SISPAG for g in res.conciliados + res.pendencias)
