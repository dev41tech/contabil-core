"""Ordem em que as transações são lidas — uma definição só.

A tela e o arquivo exportado precisam sair na MESMA ordem. Quando divergem, o
contador confere linha a linha um relatório que não bate com o que ele está
vendo, e para de confiar nos dois.

A ordem reproduz o papel do banco, do mais antigo para o mais recente:

1. `data` — o dia impresso no extrato.
2. o lote (`ExtratoImportacao.created_at`) — qual arquivo chegou primeiro.
3. `ordem` — a posição da linha DENTRO do arquivo.
4. `id` — desempate final, para a paginação não repetir nem pular registros.

O passo 2 é o que faltava. `ordem` é posição dentro de um arquivo, então dois
uploads que cobrem o mesmo dia começam ambos do zero: sem o lote, as linhas do
segundo arquivo se intercalam com as do primeiro na posição 0, 1, 2… e o
desempate caía no `id`, que é UUID aleatório. Na prática o extrato aparecia
embaralhado dentro do dia exatamente quando havia mais de um arquivo — que é o
caso do extrato reimportado depois de uma correção.

Transação sem lote (`importacao_id` NULL) vem primeiro no dia: são as
anteriores à migration 0028, importadas antes de existir qualquer lote
rastreado. Nem saldo nem valor servem como critério: em dia só de débitos o
saldo cai, em dia com crédito ele sobe, e o extrato não é ordenado por valor.
"""

from __future__ import annotations

from sqlalchemy import Select

from src.db.models import ExtratoImportacao, Transacao


def ordenar_como_o_extrato(q: Select) -> Select:
    """Aplica a ordem de leitura do extrato a uma query de `Transacao`.

    Recebe a query já filtrada e devolve com o join do lote e o `ORDER BY`. O
    join é `outerjoin` porque `importacao_id` é nulo nas linhas anteriores a
    0028 — um join interno as sumiria da tela, que é bem pior do que ordená-las
    primeiro.

    Não aplicar a contagem sobre o resultado disto: o join não muda o total
    (é muitos-para-um), mas contar sobre a query sem join é mais barato e não
    depende dessa garantia continuar valendo.
    """
    return q.outerjoin(
        ExtratoImportacao, ExtratoImportacao.id == Transacao.importacao_id
    ).order_by(
        Transacao.data.asc(),
        ExtratoImportacao.created_at.asc().nullsfirst(),
        Transacao.ordem.asc().nullslast(),
        Transacao.id.asc(),
    )
