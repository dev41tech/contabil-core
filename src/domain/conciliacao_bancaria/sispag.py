"""Consulta de pagamentos do Itaú (SISPAG): a composição dos lotes do extrato.

O extrato do Itaú mostra "Sispag Fornecedores" com UM valor, e o razão tem cada
pagamento separado. A consulta de pagamentos do internet banking lista os
pagamentos um a um — favorecido, CPF/CNPJ, tipo, data, valor e status —, mas não
diz de qual linha do extrato cada um saiu.

O que liga uma coisa à outra, medido em abr/2025 na BLD (3.536 pagamentos): o
banco junta **por dia e por tipo**. Boleto e PIX saem um por linha; TED e
crédito em conta Itaú ("Conta Corrente") saem em lote. Em 08/04, os 14
pagamentos "Conta Corrente" somam exatamente os R$ 150.430,09 da linha do
extrato. Quem cruza isso é o `cruzamento.py`; aqui é só a leitura.

A planilha é lida pelo NOME da coluna, como o leitor de extrato em planilha: a
linha de cabeçalho é a primeira que tem "favorecido" e "valor".
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from io import BytesIO


class SispagInvalido(Exception):
    """O arquivo não é uma consulta de pagamentos legível."""


@dataclass(frozen=True)
class PagamentoSispag:
    data: date
    valor: Decimal  # positivo: é o valor pago
    tipo: str
    favorecido: str
    documento: str
    efetuado: bool


@dataclass
class ConsultaSispag:
    conta: str  # "7285 / 12287-0", como impresso
    cnpj: str   # só dígitos
    pagamentos: list[PagamentoSispag]


def _sem_acento(texto: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFKD", texto) if not unicodedata.combining(c)
    ).lower().strip()


def _linhas(conteudo: bytes) -> list[list[object]]:
    if conteudo[:2] == b"PK":
        import openpyxl

        wb = openpyxl.load_workbook(BytesIO(conteudo), read_only=True, data_only=True)
        return [list(linha) for linha in wb.worksheets[0].iter_rows(values_only=True)]
    if conteudo[:4] == b"\xd0\xcf\x11\xe0":
        import xlrd

        folha = xlrd.open_workbook(file_contents=conteudo).sheet_by_index(0)
        return [folha.row_values(i) for i in range(folha.nrows)]
    raise SispagInvalido("Envie a consulta de pagamentos do SISPAG em XLS ou XLSX.")


# Colunas pelo começo do nome, sem acento — "data do pagamento", "valor (R$)".
_PAPEIS = {
    "favorecido": "favorecido",
    "cpf": "documento",
    "tipo de pagamento": "tipo",
    "data do pagamento": "data",
    "valor": "valor",
    "status": "status",
}


def _mapear(linha: list[object]) -> dict[str, int] | None:
    colunas: dict[str, int] = {}
    for i, celula in enumerate(linha):
        texto = _sem_acento(str(celula or ""))
        for prefixo, papel in _PAPEIS.items():
            if texto.startswith(prefixo) and papel not in colunas:
                colunas[papel] = i
    obrigatorias = {"favorecido", "data", "valor", "tipo"}
    return colunas if obrigatorias <= colunas.keys() else None


def _data(valor: object) -> date | None:
    if isinstance(valor, datetime):
        return valor.date()
    if isinstance(valor, date):
        return valor
    try:
        return datetime.strptime(str(valor).strip(), "%d/%m/%Y").date()
    except ValueError:
        return None


def _decimal(valor: object) -> Decimal | None:
    if isinstance(valor, (int, float)) and not isinstance(valor, bool):
        return Decimal(str(valor)).quantize(Decimal("0.01"))
    texto = str(valor or "").strip().replace(".", "").replace(",", ".")
    try:
        return Decimal(texto).quantize(Decimal("0.01"))
    except InvalidOperation:
        return None


def ler_sispag(conteudo: bytes) -> ConsultaSispag:
    linhas = _linhas(conteudo)
    conta = cnpj = ""
    colunas: dict[str, int] | None = None
    pagamentos: list[PagamentoSispag] = []

    for linha in linhas:
        textos = [str(c).strip() for c in linha if c not in (None, "")]
        if colunas is None:
            for i, texto in enumerate(textos[:-1]):
                rotulo = _sem_acento(texto)
                if rotulo.startswith("agencia/conta") and not conta:
                    conta = textos[i + 1]
                if rotulo.startswith("cnpj") and not cnpj:
                    cnpj = re.sub(r"\D", "", textos[i + 1])
            colunas = _mapear(linha)
            continue

        def celula(papel: str, linha=linha, colunas=colunas) -> object:
            i = colunas.get(papel)
            return linha[i] if i is not None and i < len(linha) else None

        data = _data(celula("data"))
        valor = _decimal(celula("valor"))
        if data is None or valor is None:
            continue  # rodapé ("Total:"), linha em branco
        status = _sem_acento(str(celula("status") or "efetuado"))
        pagamentos.append(
            PagamentoSispag(
                data=data,
                valor=abs(valor),
                tipo=str(celula("tipo") or "").strip(),
                favorecido=str(celula("favorecido") or "").strip(),
                documento=str(celula("documento") or "").strip(),
                efetuado=status.startswith("efetuado"),
            )
        )

    if colunas is None:
        raise SispagInvalido(
            "Não encontrei a tabela de pagamentos (colunas favorecido, tipo de pagamento, "
            "data do pagamento e valor). Envie a consulta de pagamentos do SISPAG."
        )
    if not pagamentos:
        raise SispagInvalido("A consulta de pagamentos não tem nenhum pagamento.")
    return ConsultaSispag(conta=conta, cnpj=cnpj, pagamentos=pagamentos)
