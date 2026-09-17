"""Extrato da conta Iugu (instituição de pagamento).

    Data Descrição do lançamento bancário Conciliação bancária Valor Saldo
    01/04/2026 Saldo do dia R$ 19.271,55
    01/04/2026 Recebimento por PIX de M2 COMUNICACAO VISUAL ✓ Conciliado R$ 17.647,66
    01/04/2026 Pagamento por PIX para Luis Ricardo Dos Santos ✓ Conciliado - R$ 120,00

O `SALDO DO DIA` VEM NO TOPO E É O FECHAMENTO — COM A LISTA CRESCENTE

Cresol, BBC e Cora também abrem o dia com o saldo de fechamento, mas lá a lista
é decrescente. Aqui é crescente: o cabeçalho traz o fim do dia e os lançamentos
abaixo dele vão do primeiro ao último. Medido no extrato de abril/2026, quatro
dias seguidos fechando exato:

    saldo anterior 0,29 + 19.271,26 (15 lanc. de 01/04) = 19.271,55  ✓ impresso
    19.271,55 − 18.898,93 (51 lanc. de 02/04)           =    372,62  ✓ impresso

Então o saldo do cabeçalho pertence ao ÚLTIMO lançamento do dia, sem inverter
nada — e é justamente por isso que a inversão da Cora não pode ser copiada para
cá. As duas formas parecem a mesma no arquivo e ancoram em pontas opostas.

A COLUNA `SALDO` FICA VAZIA POR LANÇAMENTO

O cabeçalho de colunas anuncia `Valor Saldo`, mas a linha traz um número só.
O saldo por lançamento não existe aqui; a âncora é a do dia.

O STATUS DE CONCILIAÇÃO NÃO É PARTE DO NOME

`✓ Conciliado` fica entre a descrição e o valor. É coluna própria, e sai do
histórico: deixá-lo dentro poria a mesma palavra no fim de quase todo lançamento
e atrapalharia o casamento por nome do NEO.
"""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import date
from decimal import Decimal

from src.domain.extrato._comum import Bloco, gerar_fitid, parse_data, parse_valor
from src.domain.extrato.ofx_parser import TransacaoOFX

SIGLAS = frozenset({"IUGU", "401"})

_DATA = r"\d{2}/\d{2}/\d{4}"
_NUMERO = r"\d{1,3}(?:\.\d{3})*,\d{2}"

_CABECALHO_DIA = re.compile(
    rf"^({_DATA})\s+Saldo do dia\s+R\$\s*(-?{_NUMERO})\s*$", re.IGNORECASE
)
# O FECHO TAMBÉM COMEÇA COM DATA — E FOI LIDO COMO LANÇAMENTO
#
#     30/04/2026 Saldo final R$ 486,62
#
# Tem a forma exata de um crédito: data, texto, `R$` e valor. Sem esta linha
# reconhecida à parte, o extrato ganhava um crédito fantasma de 486,62 no fim,
# e a conta fechava com o dobro do saldo final. A cadeia pegou — é a mesma
# família da linha de rodapé do Itaú que entrou como crédito de R$ 19.070,30.
#
# Como o valor é o saldo de fechamento declarado, ele vira âncora da CAUDA, que
# é justamente a ponta onde lançamento inventado costuma entrar.
_SALDO_FINAL = re.compile(
    rf"^{_DATA}\s+Saldo final\s+R\$\s*(-?{_NUMERO})\s*$", re.IGNORECASE
)
# O sinal, quando existe, fica ANTES do cifrão e separado dele.
_LINHA = re.compile(rf"^({_DATA})\s+(.+?)\s*(-\s*)?R\$\s*({_NUMERO})\s*$")

# Marcador da coluna de conciliação, no fim da descrição.
_CONCILIACAO = re.compile(
    r"[\s✓✔]*(?:N[ãa]o\s+)?[Cc]onciliad[oa]\s*$", re.IGNORECASE
)

_ASSINATURA = re.compile(
    r"Iugu\s+Institui[çc][ãa]o de Pagamento|Extrato Conta Az[uú]", re.IGNORECASE
)

_IGNORAR = re.compile(
    r"^(Data\s+Descri|Saldo anterior|Saldo final|Saldo Bloqueado|"
    r"Total de entradas|Total de sa[ií]das|Iugu\s|P[áa]gina \d)",
    re.IGNORECASE,
)


def reconhece(linhas: list[str]) -> bool:
    return any(_ASSINATURA.search(linha) for linha in linhas)


def extrair(linhas: list[str], referencia_ano: int) -> list[Bloco]:
    dias: list[tuple[date, Decimal | None, list[TransacaoOFX]]] = []
    saldo_final: Decimal | None = None
    idx = 0

    for linha in linhas:
        limpa = linha.strip()
        if not limpa:
            continue

        fecho = _SALDO_FINAL.match(limpa)
        if fecho:
            saldo_final = parse_valor(fecho.group(1))
            continue

        cabecalho = _CABECALHO_DIA.match(limpa)
        if cabecalho:
            do_dia = parse_data(cabecalho.group(1), referencia_ano)
            if do_dia is None:
                continue
            if dias and dias[-1][0] == do_dia:
                continue
            dias.append((do_dia, parse_valor(cabecalho.group(2)), []))
            continue

        if _IGNORAR.match(limpa):
            continue

        casada = _LINHA.match(limpa)
        if not casada:
            continue

        data_str, descricao, sinal, valor_str = casada.groups()
        data_lida = parse_data(data_str, referencia_ano)
        valor = parse_valor(valor_str)
        if data_lida is None or valor is None or valor == 0:
            continue
        if sinal:
            valor = -valor

        historico = _CONCILIACAO.sub("", descricao).strip()
        historico = re.sub(r"\s+", " ", historico) or "SEM DESCRIÇÃO"

        # Lançamento sem cabeçalho de dia acima (primeira página cortada) ainda
        # é lançamento: entra num grupo próprio, só sem âncora de saldo.
        if not dias or dias[-1][0] != data_lida:
            dias.append((data_lida, None, []))

        dias[-1][2].append(
            TransacaoOFX(
                fitid=gerar_fitid(data_lida, historico, valor, idx),
                data=data_lida,
                valor=valor,
                historico=historico[:200],
                tipo_ofx="CREDIT" if valor > 0 else "DEBIT",
                saldo_apos=None,
                ordem=idx,
            )
        )
        idx += 1

    transacoes: list[TransacaoOFX] = []
    # A lista já está em ordem crescente — não inverter. O saldo do cabeçalho é
    # o fechamento, então pertence ao último lançamento do dia.
    for _do_dia, saldo, lancamentos in dias:
        if not lancamentos:
            continue
        if saldo is not None:
            lancamentos[-1] = replace(lancamentos[-1], saldo_apos=saldo)
        transacoes.extend(lancamentos)

    if not transacoes:
        return []
    return [Bloco(transacoes=transacoes, saldo_final=saldo_final)]
