"""Extrato do Banco Cora.

    Transações
    29/12/2025 Saldo do dia R$ 196,50
    Pagamento recebido Manchester - Joinville 04.153.428/0001-25 + R$ 196,50
    28/12/2025 Saldo do dia R$ 0,00
    Transf Pix enviada BANGA PET COMER… 30.655.038/0001-63 - R$ 290,77
    26/12/2025 Saldo do dia R$ 290,77

O DIA ABRE COM O SALDO DE FECHAMENTO DELE

A lista é decrescente e o cabeçalho de cada dia traz o saldo do FIM daquele dia,
com os lançamentos logo abaixo. Confere: 29/12 fecha em 196,50 e tem um crédito
de 196,50 — o que dá 0,00 de abertura, exatamente o fechamento de 28/12 impresso
na linha seguinte.

É a mesma forma já tratada em Cresol e BBC, e o erro que ela convida é sempre o
mesmo: ancorar o saldo no PRIMEIRO lançamento que aparece embaixo do cabeçalho
desloca a cadeia por um dia inteiro de movimento. Aqui ele pertence ao ÚLTIMO
lançamento do dia em ordem cronológica.

O LANÇAMENTO NÃO TEM DATA PRÓPRIA

Só o cabeçalho do dia tem. Cada lançamento herda a data do cabeçalho acima dele,
e é por isso que o cabeçalho abre um grupo em vez de virar transação.

O SINAL VEM SEPARADO DO VALOR

`+ R$ 196,50` e `- R$ 290,77`, com espaço entre o sinal e o cifrão.
"""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import date
from decimal import Decimal

from src.domain.extrato._comum import Bloco, gerar_fitid, parse_data, parse_valor
from src.domain.extrato.ofx_parser import TransacaoOFX

SIGLAS = frozenset({"CORA", "403"})

_DATA = r"\d{2}/\d{2}/\d{4}"
_MOEDA = r"[+-]\s?R\$\s?\d{1,3}(?:\.\d{3})*,\d{2}"

_CABECALHO_DIA = re.compile(
    rf"^({_DATA})\s+Saldo do dia\s+R\$\s*(-?\d{{1,3}}(?:\.\d{{3}})*,\d{{2}})\s*$",
    re.IGNORECASE,
)
_LINHA = re.compile(rf"^(.*?)\s*({_MOEDA})\s*$")

# A assinatura é o nome da instituição no rodapé, que se repete em toda página.
# "Cora" solto não serve: aparece dentro de razão social de contraparte.
_ASSINATURA = re.compile(r"Cora\s+SCFI|Cora\s+SCD", re.IGNORECASE)

_IGNORAR = re.compile(
    r"^(CNPJ\s|Ag[êe]ncia:|Extrato do per[ií]odo|Saldo inicial|Saldo final|"
    r"Total de entradas|Total de sa[ií]das|Transa[çc][õo]es\s*$|Cora\s|"
    r"Ouvidoria:|Extrato gerado|p[áa]g \d)",
    re.IGNORECASE,
)


def reconhece(linhas: list[str]) -> bool:
    tem_marca = any(_ASSINATURA.search(linha) for linha in linhas)
    tem_dia = any(_CABECALHO_DIA.match(linha.strip()) for linha in linhas)
    return tem_marca and tem_dia


def extrair(linhas: list[str], referencia_ano: int) -> list[Bloco]:
    dias: list[tuple[date, Decimal | None, list[TransacaoOFX]]] = []
    idx = 0

    for linha in linhas:
        limpa = linha.strip()
        if not limpa:
            continue

        cabecalho = _CABECALHO_DIA.match(limpa)
        if cabecalho:
            do_dia = parse_data(cabecalho.group(1), referencia_ano)
            if do_dia is None:
                continue
            # Dia que atravessa a quebra de página reaparece no topo da página
            # seguinte com o mesmo saldo; abrir um grupo novo ali ancoraria o
            # mesmo valor duas vezes.
            if dias and dias[-1][0] == do_dia:
                continue
            dias.append((do_dia, parse_valor(cabecalho.group(2)), []))
            continue

        if _IGNORAR.match(limpa) or not dias:
            continue

        casada = _LINHA.match(limpa)
        if not casada:
            continue

        descricao, valor_str = casada.groups()
        valor = parse_valor(valor_str.replace(" ", ""))
        if valor is None or valor == 0:
            continue

        historico = re.sub(r"\s+", " ", descricao).strip() or "SEM DESCRIÇÃO"
        dias[-1][2].append(
            TransacaoOFX(
                fitid=gerar_fitid(dias[-1][0], historico, valor, idx),
                data=dias[-1][0],
                valor=valor,
                historico=historico[:200],
                tipo_ofx="CREDIT" if valor > 0 else "DEBIT",
                saldo_apos=None,
                ordem=idx,
            )
        )
        idx += 1

    transacoes: list[TransacaoOFX] = []
    # Do dia mais antigo para o mais recente, e dentro do dia idem: o arquivo é
    # decrescente nos dois níveis.
    for _do_dia, saldo, lancamentos in reversed(dias):
        if not lancamentos:
            continue
        cronologicos = list(reversed(lancamentos))
        if saldo is not None:
            cronologicos[-1] = replace(cronologicos[-1], saldo_apos=saldo)
        transacoes.extend(cronologicos)

    if not transacoes:
        return []
    return [Bloco(transacoes=transacoes)]
