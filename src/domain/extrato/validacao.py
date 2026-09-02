"""Barreira contra transação cujo valor não é confiável.

Existe por um caso real (SINCOPEÇAS, agosto/2026): um extrato em PDF caiu na
camada de IA do parser porque o layout não casou com nenhuma regra
determinística. A IA devolveu a linha inteira do extrato como descrição e
capturou a coluna de **saldo** no lugar da coluna de valor. Resultado no banco:

    historico: "18/02/2026 TARIFA COM R LIQUIDACAO COB000001 -1,19 -54.881,83"
    valor:     54881.83   dc: C      (era uma tarifa de R$ 1,19 a débito)

Vinte e nove transações assim. Nenhuma chegou a virar lançamento contábil, mas
só porque ninguém as classificou antes de o problema aparecer — e a caixa de
classificação nova torna tentador resolver dezesseis de uma vez.

A checagem só se aplica quando o histórico ainda carrega a linha crua (dois ou
mais valores monetários). Extrato bem parseado tem descrição limpa e nunca
entra aqui; OFX, que traz campo estruturado, também não.

A régua é o próprio conteúdo da linha: se o valor gravado bate com o último
número, é o saldo; se não bate com nenhum, veio de lugar nenhum. Nos dois casos
o número não é confiável e a transação não deve ser persistida — importar com
valor errado é silencioso e contamina o razão, enquanto a falta da linha aparece
na conciliação e pode ser corrigida.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

# Valor monetário no formato brasileiro: 1.234,56 / -1,19 / 84.908,13
_MOEDA = re.compile(r"-?\d{1,3}(?:\.\d{3})*,\d{2}")


def _para_decimal(texto: str) -> Decimal | None:
    try:
        return Decimal(texto.replace(".", "").replace(",", "."))
    except InvalidOperation:
        return None


def reais(valor: Decimal) -> str:
    """Formata no padrão brasileiro — a mensagem é lida por contador."""
    inteiro, _, centavos = f"{valor:.2f}".partition(".")
    milhar = f"{int(inteiro):,}".replace(",", ".")
    return f"R$ {milhar},{centavos}"


def valores_na_linha(historico: str) -> list[Decimal]:
    """Valores monetários presentes no texto, em módulo e na ordem de leitura."""
    achados = (_para_decimal(m) for m in _MOEDA.findall(historico or ""))
    return [abs(v) for v in achados if v is not None]


def motivo_valor_nao_confiavel(historico: str, valor: Decimal) -> str | None:
    """Descreve por que o valor não pode ser aceito, ou `None` se estiver ok.

    A mensagem vai para o contador no resultado da importação, então diz o que
    houve e o que fazer — não é log técnico.
    """
    numeros = valores_na_linha(historico)
    if len(numeros) < 2:
        # Descrição limpa (ou com um número só): nada a comparar. O parser fez
        # o trabalho dele e não há evidência contra o valor.
        return None

    gravado = abs(Decimal(valor))
    primeiro, ultimo = numeros[0], numeros[-1]

    if gravado == primeiro:
        return None

    if gravado == ultimo:
        return (
            f"o valor lido ({reais(ultimo)}) é o último número da linha, que num "
            f"extrato é o saldo da conta — o valor da transação parece ser "
            f"{reais(primeiro)}"
        )

    return (
        f"o valor lido ({reais(gravado)}) não corresponde a nenhum valor da "
        f"linha do extrato"
    )


def historico_parece_linha_crua(historico: str) -> bool:
    """Histórico que ainda tem data e mais de um valor — o parser não separou.

    Não é motivo para rejeitar: quando o valor confere, o lançamento é
    aproveitável e só a descrição fica feia. Serve para avisar que aquele
    arquivo passou pelo caminho heurístico e merece conferência.
    """
    tem_data = bool(re.search(r"\b\d{2}/\d{2}/\d{4}\b", historico or ""))
    return tem_data and len(valores_na_linha(historico)) >= 2
