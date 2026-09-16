"""De que período é o extrato — lido do próprio documento, não do relógio.

O ANO ERA O DO DIA DA IMPORTAÇÃO

Vários bancos imprimem só dia e mês na linha do lançamento — o Itaú escreve
"03/02 Aquisição Fornecedores" e deixa o ano no cabeçalho ("fev 2025"). O
`parse_pdf` completava esse ano com `datetime.now().year`. Encontrado em
16/09/2026, ao conciliar o razão de fevereiro/2025 da BLD com o extrato: todas as
3.280 transações saíram com data de 2026. Importado agora, um extrato do ano
anterior entra no período errado — no razão, no NEO e na exportação.

O QUE CONTA COMO FIM DO PERÍODO, EM ORDEM DE CONFIANÇA

1. um intervalo explícito: "Período: 01/02/2025 a 28/02/2025";
2. mês e ano por extenso: "fev 2025", "fevereiro de 2025" (cabeçalho do Itaú);
3. a data do saldo final: "saldo em 31/01/25".

Linha de emissão fica de fora. "Emitido em 12/05/2026" é a data em que o PDF foi
gerado — num extrato de 2025 baixado hoje, é exatamente o ano errado.
"""

from __future__ import annotations

import calendar
import re
from dataclasses import replace
from datetime import date, timedelta

# Linhas que carregam a data da impressão, não a do período.
_EMISSAO = re.compile(
    r"emiti|emiss[aã]o|gerad[oa]\s+em|impress|consulta\s+realizada|data/hora", re.IGNORECASE
)

_DATA = r"(\d{2})/(\d{2})/(\d{4}|\d{2})\b"
_INTERVALO = re.compile(rf"{_DATA}\s*(?:a|at[eé]|à|-|–)\s*{_DATA}", re.IGNORECASE)
_MESES = {
    "jan": 1, "fev": 2, "mar": 3, "abr": 4, "mai": 5, "jun": 6,
    "jul": 7, "ago": 8, "set": 9, "out": 10, "nov": 11, "dez": 12,
}
_MES_ANO = re.compile(
    r"\b(jan|fev|mar|abr|mai|jun|jul|ago|set|out|nov|dez)[a-zç]*\.?(?:\s+de)?\s+(20\d{2})\b",
    re.IGNORECASE,
)
_SALDO_EM = re.compile(rf"saldo\s+em\s+{_DATA}", re.IGNORECASE)

# Só o topo do documento: o cabeçalho é onde o período é declarado, e varrer o
# arquivo inteiro faria a data de um lançamento ou de um rodapé competir com ele.
_LINHAS_DO_CABECALHO = 80


def _data(d: str, m: str, a: str) -> date | None:
    ano = int(a) + 2000 if len(a) == 2 else int(a)
    try:
        return date(ano, int(m), int(d))
    except ValueError:
        return None


def fim_do_periodo(linhas: list[str]) -> date | None:
    """Última data do período do extrato, ou None se o documento não disser."""
    topo = [l for l in linhas[:_LINHAS_DO_CABECALHO] if not _EMISSAO.search(l)]

    for linha in topo:
        m = _INTERVALO.search(linha)
        if m:
            fim = _data(m.group(4), m.group(5), m.group(6))
            if fim:
                return fim

    for linha in topo:
        m = _MES_ANO.search(linha)
        if m:
            mes, ano = _MESES[m.group(1).lower()[:3]], int(m.group(2))
            return date(ano, mes, calendar.monthrange(ano, mes)[1])

    saldos = [
        d for linha in topo for m in _SALDO_EM.finditer(linha)
        if (d := _data(m.group(1), m.group(2), m.group(3)))
    ]
    return max(saldos) if saldos else None


# Mais longe que isso depois do fim do período não é lançamento futuro: é o ano
# errado. Um extrato de janeiro que começa em "31/12" recebe o ano de janeiro e
# fica ~11 meses no futuro.
_FUTURO_PLAUSIVEL = timedelta(days=120)


def ajustar_virada_de_ano(transacoes: list, fim: date | None) -> list:
    """Devolve ao ano anterior o lançamento que caiu longe demais no futuro."""
    if fim is None:
        return transacoes
    ajustadas = []
    for t in transacoes:
        if t.data - fim > _FUTURO_PLAUSIVEL:
            try:
                t = replace(t, data=t.data.replace(year=t.data.year - 1))
            except ValueError:  # 29/02 num ano que não tem
                t = replace(t, data=date(t.data.year - 1, 2, 28))
        ajustadas.append(t)
    return ajustadas
