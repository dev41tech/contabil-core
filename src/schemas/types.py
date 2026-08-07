"""Tipos e normalizadores compartilhados pelos schemas de entrada."""

from __future__ import annotations

import re
from typing import Annotated

from pydantic import BeforeValidator

from src.core.cnpj import formatar as formatar_cnpj
from src.core.cnpj import somente_digitos, valido as cnpj_valido

_BANCOS_CONHECIDOS: dict[str, str] = {
    "001": "BB",
    "033": "SANTANDER",
    "077": "INTER",
    "104": "CEF",
    "237": "BRADESCO",
    "341": "ITAU",
    "356": "BMG",
    "389": "MERCANTIL",
    "422": "SAFRA",
    "745": "CITIBANK",
    "756": "SICOOB",
    "084": "UNIPRIME",
    "748": "SICREDI",
    "336": "C6",
    "260": "NU",
    "290": "PAGSEGURO",
}


def _texto(value: object, campo: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{campo} deve ser texto.")
    return value.strip()


def normalizar_cnpj(value: object) -> str:
    texto = _texto(value, "CNPJ")
    digits = somente_digitos(texto)
    if len(digits) != 14:
        raise ValueError("CNPJ deve ter 14 dígitos.")
    if not cnpj_valido(digits):
        raise ValueError("CNPJ inválido — dígitos verificadores não conferem.")
    return formatar_cnpj(digits)


def normalizar_banco(value: object) -> str:
    texto = _texto(value, "Banco").upper()
    texto = _BANCOS_CONHECIDOS.get(texto, texto)
    if not 2 <= len(texto) <= 20:
        raise ValueError("Sigla do banco deve ter entre 2 e 20 caracteres.")
    return texto


def normalizar_agencia(value: object) -> str:
    texto = _texto(value, "Agência")
    if not re.fullmatch(r"\d{1,10}", texto):
        raise ValueError("Agência deve conter apenas dígitos (máx. 10).")
    return texto


def normalizar_numero_conta(value: object) -> str:
    texto = _texto(value, "Número da conta")
    # Contas empresariais de alguns bancos admitem letras e hífen.
    if not re.fullmatch(r"[\w-]{1,20}", texto):
        raise ValueError("Número da conta inválido.")
    return texto


def normalizar_competencia(value: object) -> str:
    texto = _texto(value, "Competência")
    match = re.fullmatch(r"(?P<ano>\d{4})-(?P<mes>\d{2})", texto)
    if not match or int(match.group("ano")) == 0 or not 1 <= int(match.group("mes")) <= 12:
        raise ValueError("Competência deve estar no formato AAAA-MM, com mês entre 01 e 12.")
    return texto


CNPJ = Annotated[str, BeforeValidator(normalizar_cnpj)]
BancoSigla = Annotated[str, BeforeValidator(normalizar_banco)]
NumeroAgencia = Annotated[str, BeforeValidator(normalizar_agencia)]
NumeroConta = Annotated[str, BeforeValidator(normalizar_numero_conta)]
Competencia = Annotated[str, BeforeValidator(normalizar_competencia)]
