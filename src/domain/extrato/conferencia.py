"""Conferência de completude pelo saldo que o arquivo declara.

POR QUE ISTO EXISTE, SE JÁ HÁ A CADEIA DE SALDOS

A cadeia de saldos do PDF confere `saldo[n] - saldo[n-1] == valor[n]` linha a
linha. Ela prova que os valores PRESENTES são consistentes entre si — e só isso.
Lançamento que entra ou some ENTRE duas âncoras, desde que se compense, passa
sem acusar nada. Aconteceu duas vezes em dois dias, em 31/08 e 01/09/2026: o
modelo de visão perdeu cinco lançamentos com a cadeia fechando, e o
`SALDO ANTERIOR` do Sicoob entrou como crédito porque o valor falso era
exatamente o saldo de abertura, na primeira posição, e a soma dava certo.

O saldo de fechamento do período é uma segunda fonte: um número que o banco
AFIRMA e que não deriva dos lançamentos. Encadeado entre arquivos consecutivos
da mesma conta, ele responde a pergunta que a cadeia não responde — se o
conjunto está completo.

    fechamento do arquivo anterior + movimento deste arquivo = fechamento deste

POR QUE AVISO E NÃO RECUSA

O caso legítimo mais comum de divergência é período faltando: o contador sobe
fevereiro e depois abril, e a conta não fecha por março inteiro. Recusar abril
travaria um arquivo correto. Por isso a frase nomeia a data da âncora — é o que
faz o buraco aparecer em vez de virar um número sem explicação.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from src.domain.extrato.validacao import reais


def conferir_fechamento(
    *,
    saldo_anterior: Decimal | None,
    data_anterior: date | None,
    movimento: Decimal,
    saldo_declarado: Decimal | None,
    data_declarada: date | None,
) -> str | None:
    """Frase de alerta quando o fechamento não confere, ou ``None``.

    Devolve ``None`` sem alegar conferência sempre que faltar uma das pontas:
    arquivo sem `LEDGERBAL`, ou primeiro arquivo da conta (que não tem âncora
    anterior). Silêncio aqui significa "não verificado", não "verificado e ok" —
    a mesma semântica que a cadeia de saldos usa para bloco parcial.
    """
    if saldo_declarado is None or saldo_anterior is None:
        return None

    esperado = saldo_anterior + movimento
    diferenca = saldo_declarado - esperado
    if diferenca == 0:
        return None

    quando_anterior = _dia(data_anterior)
    quando_declarada = _dia(data_declarada)

    frase = (
        f"O saldo de fechamento declarado no arquivo ({reais(saldo_declarado)}) "
        f"não confere: partindo do fechamento anterior desta conta"
        f"{f' em {quando_anterior}' if quando_anterior else ''} "
        f"({reais(saldo_anterior)}) mais {reais(movimento)} movimentados neste "
        f"arquivo, o esperado era {reais(esperado)} — diferença de "
        f"{reais(abs(diferenca))}."
    )
    if quando_anterior and quando_declarada:
        frase += (
            f" Confira se falta importar algum período entre {quando_anterior} "
            f"e {quando_declarada}."
        )
    else:
        frase += " Confira se falta importar algum período entre os dois arquivos."
    return frase[:500]


def _dia(valor: date | None) -> str | None:
    return valor.strftime("%d/%m/%Y") if valor else None
