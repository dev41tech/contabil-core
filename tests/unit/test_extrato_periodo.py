"""O ano do lançamento vem do extrato, não do dia da importação.

Encontrado em 16/09/2026: o extrato do Itaú de fevereiro/2025 da BLD, que imprime
"03/02" sem ano, saiu com todas as 3.280 transações em 2026.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from src.domain.extrato.periodo import ajustar_virada_de_ano, fim_do_periodo


def test_cabecalho_do_itau_com_mes_e_ano():
    linhas = [
        "extrato mensal ag 7285 cc 12287-0 fev 2025 001|052",
        "fev 2025 Minha conta12287-0Minha agência7285 - Curitiba Hauer Ext",
        "saldo em 31/01/25 saldo em 28/02/25",
        "03/02 Aquisição Fornecedores 4.209.036,70",
    ]
    assert fim_do_periodo(linhas) == date(2025, 2, 28)


def test_intervalo_explicito_tem_prioridade():
    linhas = ["EXTRATO", "Período: 01/02/2025 a 28/02/2025", "jan 2024 qualquer coisa"]
    assert fim_do_periodo(linhas) == date(2025, 2, 28)


def test_data_de_emissao_nao_decide_o_ano():
    """O PDF de 2025 baixado hoje diz "Emitido em ... 2026" — o ano errado."""
    linhas = [
        "Emitido em 12/05/2026 às 12:08:08",
        "EXTRATO",
        "saldo em 30/04/25",
    ]
    assert fim_do_periodo(linhas) == date(2025, 4, 30)


def test_documento_que_nao_diz_o_periodo():
    assert fim_do_periodo(["03/02 PIX 10,00", "04/02 TARIFA 1,00"]) is None


@dataclass
class _T:
    data: date


def test_lancamento_de_dezembro_num_extrato_de_janeiro_volta_um_ano():
    """Extrato de janeiro que começa em "31/12" recebe o ano de janeiro."""
    fim = date(2025, 1, 31)
    ajustadas = ajustar_virada_de_ano([_T(date(2025, 12, 31)), _T(date(2025, 1, 2))], fim)
    assert [t.data for t in ajustadas] == [date(2024, 12, 31), date(2025, 1, 2)]


def test_lancamento_futuro_proximo_nao_e_tocado():
    """Agendado para dias depois do fim do período é futuro de verdade."""
    fim = date(2025, 2, 28)
    assert ajustar_virada_de_ano([_T(date(2025, 3, 20))], fim)[0].data == date(2025, 3, 20)


def test_sem_periodo_nada_muda():
    t = _T(date(2025, 12, 31))
    assert ajustar_virada_de_ano([t], None) == [t]
