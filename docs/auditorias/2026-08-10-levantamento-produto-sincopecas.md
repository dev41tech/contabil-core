# Levantamento de produto — análise em cliente (SINCOPEÇAS)

Registrado em 10/08/2026, a partir de uma análise manual feita pelo usuário sobre o
sistema em uso pelo cliente SINCOPEÇAS. Ao contrário da [auditoria estática de
06/08/2026](2026-08-06-auditoria-codex.md) (achados de segurança/correção no código),
esta lista mistura bugs de UX e pedidos de melhoria de produto. Nada aqui foi
implementado ainda — é só o registro do backlog, para priorização posterior.

## Confirmado e já corrigido

### 8 — Fornecedores/valores de outra empresa aparecendo no ConcilPro

Observado **antes** do deploy de 07-08/08/2026. É o mesmo problema dos achados
**C1** (IDOR entre tenants) e **C2** (ConcilPro sem escopo por empresa) da auditoria
estática — já corrigidos e em produção desde o merge de hoje.

Verificado no `main` atual (`66750d7`):
- `src/api/v1/concilpro.py`: `router = APIRouter(..., dependencies=[Depends(get_company_context)])`
  — toda rota do módulo valida que a empresa do path pertence ao tenant do usuário (C1).
- Toda query `select(Fornecedor)`/`select(LancamentoFornecedor)` no arquivo é filtrada
  por `empresa_id == empresa_id` (C2).

**Ação sugerida**: pedir para o cliente reproduzir o cenário de novo com dados atuais,
para confirmar visualmente que sumiu. Se ainda aparecer, é um caso novo — não o mesmo bug.

## Precisa de mais contexto para investigar

### 6 — Informação de IPI incompatível com a empresa

`IPI` só aparece no código como texto de exemplo dentro do prompt do classificador de
IA do ConcilPro (`src/domain/concilpro/ai_classifier.py`, `src/domain/concilpro/parser.py`)
— não é um valor fixo exibido ao cliente. Não deu para confirmar se é um bug de código
sem saber em qual tela/relatório exatamente a informação de IPI apareceu para a
SINCOPEÇAS. Pode ser:
- um artefato de parsing do próprio arquivo importado pelo cliente (ex.: a razão
  contábil enviada tem uma linha de IPI legada/residual que o parser está extraindo
  corretamente, mas que não deveria estar no arquivo);
- uma opção de imposto que aparece por padrão em algum formulário/relatório
  independente do regime tributário da empresa.

**Próximo passo**: pedir print/tela específica de onde o IPI aparece.

## Backlog de melhorias (não são bugs de segurança/corretude)

### 1 — Importação e manutenção do plano de contas
- Conta contábil não é preenchida automaticamente na importação.
- Falta função de exclusão total do plano de contas, ou seleção múltipla para
  exclusão em lote.

### 2 — Importação e integração de notas fiscais
- Falta busca de notas fiscais.
- Falta drag-and-drop na tela de importação.
- Falta importação em lote / ZIP (nota: `A10` da auditoria já limitou o tamanho de
  ZIP aceito para notas — a importação em lote em si, se não existir, é feature nova,
  não teria relação com esse achado).

### 3 — Cadastro de comprovantes
- Inclusão é manual mesmo quando o comprovante já existe em PDF; usuário precisa
  preencher os campos à mão. Pedido: extração automática de dados do PDF
  (relacionado ao que já existe para extrato bancário em `pdf_parser.py`, mas para
  comprovantes — módulo separado, não construído ainda).

### 4 — Importação de faturas de cartão
- Só aceita Excel/CSV; clientes normalmente fornecem fatura em PDF. Pedido: suporte
  a importação de fatura de cartão em PDF.

### 5 — Conciliação automática e aplicações financeiras
- Falta opção de rodar a conciliação filtrando por um mês específico.
- Falta exportação dos resultados da conciliação.
- Falta um local para registrar/tratar aplicações financeiras da empresa (não existe
  hoje como conceito no sistema).

### 7 — Upload da razão contábil
- Pedido para aceitar `.xls` (formato antigo do Excel) além do que já é aceito hoje,
  no upload da razão contábil.

## Observação geral

Itens 1-5 e 7 são pedidos de funcionalidade nova ou melhoria de UX, não bugs de
corretude/segurança — não competem com a auditoria estática. Vale priorizar com o
time de produto antes de qualquer implementação.
