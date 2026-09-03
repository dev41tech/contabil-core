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
o extrator achava zero**.

A segunda versão trocou a barra por "os dois primeiros pontos são obrigatórios",
e ainda era estreita: `41250201 0001-24` tem a raiz sem pontuação nenhuma e
continuava de fora. Duas tentativas com a mesma forma de erro — descrever os
formatos vistos até ali em vez da propriedade que os distingue.

A que vale é a terceira: **todo separador é opcional, e basta que UM deles seja
ponto, barra ou traço.** Não é onde o separador está, é qual ele é. Ponto, barra
e traço aparecem porque alguém formatou um documento; espaço aparece entre dois
números quaisquer, e por isso `12 345 678 9012 34` continua não sendo candidato.

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

# Todo separador é opcional aqui; o que segura o padrão é a guarda logo abaixo.
#
# Os grupos, sim, têm tamanho fixo (2-3-3-4-2 e 3-3-3-2), então nenhum
# afrouxamento deixa o padrão engolir um dígito a mais ou a menos. O que os
# bancos deste escritório imprimem:
#
#     09.033.833/0001-23   canônico
#     81.450.538 0001-08   Sicoob — espaço no lugar da barra
#     41.250.201-0001-24   traço no lugar da barra
#     41250201 0001-24     raiz sem pontuação, separador só no fim
#     09.033.8330001-23    sem separador entre raiz e ordem
_CNPJ_PONTUADO = re.compile(
    r"(?<!\d)\d{2}\.?\d{3}\.?\d{3}[./\s-]?\d{4}[-./\s]?\d{2}(?!\d)"
)
_CPF_PONTUADO = re.compile(r"(?<!\d)\d{3}\.?\d{3}\.?\d{3}[-./\s]?\d{2}(?!\d)")

# A guarda que substituiu "os dois pontos são obrigatórios".
#
# Exigir os pontos deixava de fora `41250201 0001-24` — raiz sem pontuação e
# separador só nas duas últimas posições, que é uma das formas que o escritório
# recebe. Mas largar a exigência inteira faria `12 345 678 9012 34` virar
# candidato, e aí o padrão casaria qualquer fileira de números separados por
# espaço.
#
# O que separa um do outro não é ONDE o separador está, é QUAL ele é: ponto,
# barra ou traço aparecem porque alguém formatou um documento; espaço aparece
# entre dois números quaisquer. Basta um separador forte em qualquer posição.
#
# Documento sem separador nenhum não passa por aqui e nem precisa: `_SEM_PONTUACAO`
# já cobre a corrida crua de 11 ou 14 dígitos, com a mesma guarda de borda.
_SEPARADOR_FORTE = re.compile(r"[./-]")


def documentos_no_historico(historico: str | None) -> list[str]:
    """CPFs/CNPJs achados na linha, só dígitos, na ordem de leitura e sem repetir.

    Repetição some porque a mesma linha às vezes traz o documento duas vezes
    (pontuado e cru); dois achados iguais não são ambiguidade.
    """
    if not historico:
        return []

    encontrados: list[str] = []
    for padrao in (_CNPJ_PONTUADO, _CPF_PONTUADO, _SEM_PONTUACAO):
        exige_separador = padrao is not _SEM_PONTUACAO
        for achado in padrao.finditer(historico):
            texto = achado.group(0)
            if exige_separador and not _SEPARADOR_FORTE.search(texto):
                # Só espaços entre os grupos: é fileira de números, não documento.
                continue
            digitos = re.sub(r"\D", "", texto)
            if digitos not in encontrados:
                encontrados.append(digitos)
    return encontrados
