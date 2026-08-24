"""Reconhece a contraparte pelo nome que aparece no histórico do extrato.

POR QUE ISTO EXISTE

A classificação por contraparte só acontecia quando havia uma nota fiscal ou um
comprovante candidato ÚNICO, que carregasse CNPJ, e que casasse exatamente com
`Contraparte.documento`. O histórico bancário nunca era olhado.

Consequência prática, e a queixa registrada no relatório de melhorias: o
fornecedor está cadastrado em Contrapartes, o lançamento traz o nome dele no
extrato, e mesmo assim a transação fica pendente — sempre. Não é intermitência,
é o comportamento projetado.

NOME É EVIDÊNCIA MAIS FRACA QUE DOCUMENTO

Documento é identidade: o CNPJ bate ou não bate. Nome é heurística — bancos
truncam, abreviam e escrevem de formas diferentes o mesmo fornecedor. Por isso
o casamento por nome só é tentado quando não há evidência documental, e é
recusado em qualquer sinal de ambiguidade. Classificar errado num sistema
contábil custa mais caro do que deixar pendente: pendente aparece na fila,
errado entra no razão em silêncio.

AS TRÊS GUARDAS

1. **Núcleo do nome.** "Forcecar Automotive Ltda." vira "forcecar automotive":
   sufixos societários são ruído e impediriam o casamento, já que o extrato
   raramente os traz.
2. **Núcleo curto demais não vale.** Um nome de um token curto ("ABC") casaria
   dentro de palavras maiores e de dezenas de históricos sem relação.
3. **Empate recusa.** Se dois cadastros casam com o mesmo histórico, nenhum é
   usado — escolher um seria adivinhar qual, e a chance de acertar é metade.
"""

from __future__ import annotations

from dataclasses import dataclass

from src.core.texto import normalizar_para_match

# Sufixos societários e formas jurídicas. Aparecem no cadastro e quase nunca no
# extrato, então mantê-los no confronto só produziria falso negativo.
_SUFIXOS = frozenset(
    {
        "ltda", "me", "epp", "eireli", "sa", "s", "a", "cia", "companhia",
        "mei", "ei", "eirl", "in", "filial", "matriz",
    }
)

# Abaixo disto o núcleo casa por acaso. Dois tokens já dão especificidade
# suficiente ("unimed curitiba"); com um token só, exige-se um nome longo.
_MIN_CARACTERES_UM_TOKEN = 8
_MIN_CARACTERES_TOTAL = 5


@dataclass(frozen=True)
class CandidataPorNome:
    contraparte_id: object
    nucleo: str


def nucleo_do_nome(nome: str | None) -> str:
    """Forma comparável do nome: normalizada e sem sufixo societário.

    'Forcecar Automotive Ltda.' → 'forcecar automotive'
    """
    if not nome:
        return ""
    tokens = normalizar_para_match(nome).split()
    # Só remove sufixo do FIM: "Cia Brasileira de Alimentos" começa com um
    # token da lista e perderia a primeira palavra se a limpeza fosse global.
    while tokens and tokens[-1] in _SUFIXOS:
        tokens.pop()
    return " ".join(tokens)


def nucleo_utilizavel(nucleo: str) -> bool:
    """Núcleos curtos demais casam por acaso e não devem ser usados."""
    if not nucleo:
        return False
    if len(nucleo.replace(" ", "")) < _MIN_CARACTERES_TOTAL:
        return False
    tokens = nucleo.split()
    if len(tokens) == 1 and len(tokens[0]) < _MIN_CARACTERES_UM_TOKEN:
        return False
    return True


def casar_por_nome(
    historico: str,
    candidatas: list[CandidataPorNome],
) -> tuple[CandidataPorNome | None, str | None]:
    """Devolve `(candidata, None)` no acerto, ou `(None, motivo)` na recusa.

    O motivo volta para virar texto na fila: o contador precisa saber que houve
    um quase-acerto e por que ele não foi aceito, senão a transação parece
    simplesmente ignorada.
    """
    alvo = normalizar_para_match(historico)
    if not alvo:
        return None, None

    achadas = [
        candidata
        for candidata in candidatas
        if nucleo_utilizavel(candidata.nucleo) and candidata.nucleo in alvo
    ]
    if not achadas:
        return None, None

    # Empate real: dois cadastros distintos casando o mesmo histórico. Nomes
    # iguais apontando para o MESMO cadastro (razão social e fantasia parecidas)
    # não são empate — por isso a contagem é por contraparte, não por núcleo.
    ids = {candidata.contraparte_id for candidata in achadas}
    if len(ids) > 1:
        nomes = ", ".join(sorted({c.nucleo for c in achadas})[:3])
        return None, (
            f"Mais de uma contraparte casa com este histórico ({nomes}). "
            "Classifique manualmente ou ajuste os cadastros."
        )

    # Entre núcleos do mesmo cadastro, o mais longo é o mais específico.
    return max(achadas, key=lambda c: len(c.nucleo)), None
