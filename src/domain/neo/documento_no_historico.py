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

CADA BANCO PONTUA DE UM JEITO

A primeira versão exigia a barra do CNPJ canônico, e isso não é o que os bancos
imprimem. O Sicoob escreve `81.450.538 0001-08`, com ESPAÇO no lugar da barra —
e, nos seis extratos de jan–jun/2026 de uma conta só, isso era **661 CNPJs que
o extrator achava zero**. Os padrões abaixo afrouxam os dois últimos separadores
e mantêm os dois primeiros pontos obrigatórios; a nota junto de cada um explica
por que essa é a linha certa entre abrangente e frouxo.

O QUE NUNCA VAI CASAR, E TUDO BEM

CPF de pessoa física vem MASCARADO em PIX (`***.674.379-**`). Não há como
recuperar os dígitos, então essas linhas não resolvem por documento — e, nos
mesmos seis extratos, são 442 das 1.103. Resolver PIX para pessoa física
depende do nome, que é evidência mais fraca, ou de classificação manual.

Este módulo não decide nada: devolve os candidatos, e quem chama confronta com
o cadastro. Casar só contra contraparte cadastrada é o que impede uma sequência
de 14 dígitos qualquer de virar classificação.
"""

from __future__ import annotations

import re

# Corrida máxima de dígitos: as bordas `(?<!\d)`/`(?!\d)` impedem que um
# pedaço de 14 dígitos de um número de 20 seja lido como CNPJ.
_SEM_PONTUACAO = re.compile(r"(?<!\d)(\d{11}|\d{14})(?!\d)")

# Os DOIS primeiros pontos são obrigatórios; os dois separadores seguintes, não.
#
# É o que torna o padrão abrangente sem torná-lo frouxo. Exigir os pontos
# ancora o achado numa string que já se declara pontuada, e por isso um par de
# números soltos separados por espaço (`12 345 678 9012 34`) não vira candidato.
# Afrouxar só as duas últimas posições cobre o que os bancos realmente imprimem:
#
#     09.033.833/0001-23   canônico
#     81.450.538 0001-08   Sicoob — espaço no lugar da barra
#     09.033.833.0001-23   ponto no lugar da barra
#     09.033.8330001-23    sem separador nenhum ali
#
# Os tamanhos dos grupos continuam fixos (2-3-3-4-2), então nenhum afrouxamento
# de separador deixa o padrão engolir um dígito a mais ou a menos.
_CNPJ_PONTUADO = re.compile(
    r"(?<!\d)\d{2}\.\d{3}\.\d{3}[./\s-]?\d{4}[-./\s]?\d{2}(?!\d)"
)
_CPF_PONTUADO = re.compile(r"(?<!\d)\d{3}\.\d{3}\.\d{3}[-./\s]?\d{2}(?!\d)")


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
