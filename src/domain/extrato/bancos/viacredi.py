"""Extrato da Viacredi (cooperativa do sistema Ailos, banco 085).

O layout é o mais direto desta base — uma linha por lançamento, com o saldo
corrente na ponta:

    DATA DESCRIÇÃO DOCUMENTO CRÉDITO (R$) DÉBITO (R$) SALDO (R$)
    SALDO ANTERIOR 7.919,58
    01/04/2026 CREDITO PIX - GOBBO SERVICOS ADMIN 806790.512 90.000,00 97.919,58
    01/04/2026 IOF S/ C-C 897.753 -59,52 102.173,33
    ...
    TOTAL 219.845,35 -237.045,87 -9.280,94

DUAS COLUNAS DE VALOR, UM VALOR SÓ IMPRESSO

O cabeçalho anuncia `CRÉDITO` e `DÉBITO` como colunas separadas, mas a linha
traz **um** valor: crédito sem sinal, débito com `-` na frente. Não é preciso
descobrir em que coluna o número caiu — o sinal já diz, e é o que este
adaptador usa. Ler por coordenada aqui seria trabalho sem ganho.

O SALDO PODE SER NEGATIVO

Esta conta fecha o mês em `-9.280,94`, com limite de crédito usado. O saldo
negativo é impresso com `-` na frente como qualquer outro valor. Um padrão que
exigisse dígito logo após a coluna recusaria o extrato inteiro a partir do dia
em que a conta entra no vermelho — foi o defeito que já custou o layout
"Gerenciador" da Caixa em 31/08.

A LINHA `TOTAL` NÃO É LANÇAMENTO

Ela traz três valores e casaria com o padrão de dados se a data não fosse
obrigatória. Como todo lançamento começa com data completa e a `TOTAL` não,
a âncora `^\\d{2}/\\d{2}/\\d{4}` já a descarta — mas o teste cobre isso
explicitamente, porque somá-la duplicaria o mês inteiro.
"""

from __future__ import annotations

import re
from decimal import Decimal

from src.domain.extrato._comum import Bloco, gerar_fitid, parse_data, parse_valor
from src.domain.extrato.ofx_parser import TransacaoOFX

SIGLAS = frozenset({"VIACREDI", "AILOS", "085"})

_DATA = r"\d{2}/\d{2}/\d{4}"
_VALOR = r"-?\d{1,3}(?:\.\d{3})*,\d{2}"

# Data, texto (descrição + nº do documento), valor do lançamento e saldo após.
# Os dois últimos números da linha são sempre valor e saldo, nessa ordem.
_LINHA = re.compile(rf"^({_DATA})\s+(.+?)\s+({_VALOR})\s+({_VALOR})\s*$")

_SALDO_ANTERIOR = re.compile(rf"^SALDO\s+ANTERIOR\s+({_VALOR})\s*$", re.IGNORECASE)

# A assinatura é a linha de identificação da cooperativa. "Cooperativa:" sozinho
# não serve — a Unicred e o Sicredi também a imprimem, com outro layout de
# colunas; o que identifica é o nome do sistema junto do código do banco.
_ASSINATURA = re.compile(
    r"Cooperativa:\s*VIACREDI|VIACREDI\s*\|\s*Banco:\s*085", re.IGNORECASE
)

_IGNORAR = re.compile(
    r"^(TOTAL\s|DATA\s+DESCRI|Emitido em|Per[ií]odo\s|Nome:|Cooperativa:|"
    r"Os dados acima|SAC\s|OUVIDORIA|Finais de Semana|P[áa]gina\s)",
    re.IGNORECASE,
)


def reconhece(linhas: list[str]) -> bool:
    return any(_ASSINATURA.search(linha) for linha in linhas)


def extrair(linhas: list[str], referencia_ano: int) -> list[Bloco]:
    transacoes: list[TransacaoOFX] = []
    saldo_anterior: Decimal | None = None
    idx = 0

    for linha in linhas:
        limpa = linha.strip()
        if not limpa:
            continue

        if saldo_anterior is None:
            abertura = _SALDO_ANTERIOR.match(limpa)
            if abertura:
                saldo_anterior = parse_valor(abertura.group(1))
                continue

        if _IGNORAR.match(limpa):
            continue

        casada = _LINHA.match(limpa)
        if not casada:
            continue

        data_str, meio, valor_str, saldo_str = casada.groups()
        data_lida = parse_data(data_str, referencia_ano)
        valor = parse_valor(valor_str)
        saldo = parse_valor(saldo_str)
        if data_lida is None or valor is None or valor == 0:
            continue

        historico = re.sub(r"\s+", " ", meio).strip() or "SEM DESCRIÇÃO"
        transacoes.append(
            TransacaoOFX(
                fitid=gerar_fitid(data_lida, historico, valor, idx),
                data=data_lida,
                valor=valor,
                historico=historico[:200],
                tipo_ofx="CREDIT" if valor > 0 else "DEBIT",
                saldo_apos=saldo,
                ordem=idx,
            )
        )
        idx += 1

    if not transacoes:
        return []
    return [Bloco(transacoes=transacoes, saldo_anterior=saldo_anterior)]
