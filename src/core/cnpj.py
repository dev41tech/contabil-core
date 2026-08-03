"""Validação e formatação de CNPJ.

Existe porque o cadastro só checava duplicidade e comprimento: 72 das 77 empresas
migradas entraram com CNPJ sintético gerado a partir do índice da pasta
(`scripts/import_mrcont.py`). O efeito não é cosmético — a exportação fiscal filtra
notas comparando o CNPJ da empresa com o emitente/destinatário, então um CNPJ que
não existe faz a exportação voltar vazia, sem erro.
"""

from __future__ import annotations

_PESOS_DV1 = (5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2)
_PESOS_DV2 = (6, 5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2)


def somente_digitos(valor: str) -> str:
    """Remove pontuação e qualquer caractere não numérico."""
    return "".join(c for c in (valor or "") if c.isdigit())


def formatar(valor: str) -> str:
    """Devolve no formato `00.000.000/0000-00`. Não valida — use `valido` para isso."""
    d = somente_digitos(valor)
    if len(d) != 14:
        return valor
    return f"{d[:2]}.{d[2:5]}.{d[5:8]}/{d[8:12]}-{d[12:]}"


def _dv(digitos: str, pesos: tuple[int, ...]) -> int:
    soma = sum(int(d) * p for d, p in zip(digitos, pesos))
    resto = soma % 11
    return 0 if resto < 2 else 11 - resto


def valido(valor: str) -> bool:
    """True se os 14 dígitos formam um CNPJ com dígitos verificadores corretos.

    Aceita com ou sem formatação. Rejeita sequências de dígito repetido
    (`00.000.000/0000-00`), que passam no cálculo mas não existem na Receita.
    """
    d = somente_digitos(valor)
    if len(d) != 14 or d == d[0] * 14:
        return False
    return int(d[12]) == _dv(d[:12], _PESOS_DV1) and int(d[13]) == _dv(d[:13], _PESOS_DV2)
