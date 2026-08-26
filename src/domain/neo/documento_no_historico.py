"""CPF/CNPJ impresso pelo banco na própria linha do extrato.

POR QUE ISTO EXISTE

A classificação por contraparte tinha duas portas: documento vindo de uma nota
fiscal ou comprovante candidato, e o nome da contraparte dentro do histórico.
Faltava a evidência mais direta que existe nesses lançamentos — o CNPJ que o
banco escreveu na linha:

    PAGAMENTO PIX 09033833000123 PERFORMANCE ENGENHA PIX_DEB
    LIQUIDACAO BOLETO SICREDI 31052957000105 041 CON 252080163

Nos dois casos o fornecedor estava cadastrado, com esse CNPJ exato, e a
transação ficava pendente para sempre: sem nota nem comprovante não havia
documento, e o nome não casava porque o banco trunca ("PERFORMANCE ENGENHA",
"041 CON"). Documento não trunca.

O QUE CONTA COMO DOCUMENTO AQUI

Só sequência de dígitos com exatamente 11 (CPF) ou 14 (CNPJ) posições, medida
em corrida MÁXIMA de dígitos — um pedaço de um número maior não é documento.
É o que separa o CNPJ do resto do lixo numérico da linha ("252080163",
"251009131", nosso número de boleto, id de convênio). A forma pontuada
(`09.033.833/0001-23`) é reconhecida à parte, porque limpar pontuação da linha
inteira grudaria números vizinhos e inventaria documentos que não existem.

Este módulo não decide nada: devolve os candidatos, e quem chama confronta com
o cadastro. Casar só contra contraparte cadastrada é o que impede uma sequência
de 14 dígitos qualquer de virar classificação.
"""

from __future__ import annotations

import re

# Corrida máxima de dígitos: as bordas `(?<!\d)`/`(?!\d)` impedem que um
# pedaço de 14 dígitos de um número de 20 seja lido como CNPJ.
_SEM_PONTUACAO = re.compile(r"(?<!\d)(\d{11}|\d{14})(?!\d)")
_CNPJ_PONTUADO = re.compile(r"(?<!\d)\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2}(?!\d)")
_CPF_PONTUADO = re.compile(r"(?<!\d)\d{3}\.\d{3}\.\d{3}-\d{2}(?!\d)")


def documentos_no_historico(historico: str | None) -> list[str]:
    """CPFs/CNPJs achados na linha, só dígitos, na ordem de leitura e sem repetir.

    Repetição some porque a mesma linha às vezes traz o documento duas vezes
    (pontuado e cru); dois achados iguais não são ambiguidade.
    """
    if not historico:
        return []

    encontrados: list[str] = []
    for padrao in (_CNPJ_PONTUADO, _CPF_PONTUADO, _SEM_PONTUACAO):
        for achado in padrao.finditer(historico):
            digitos = re.sub(r"\D", "", achado.group(0))
            if digitos not in encontrados:
                encontrados.append(digitos)
    return encontrados
