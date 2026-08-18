# Plano técnico — demandas de identificação de contraparte, histórico automático e busca no NEO

**Status:** Proposto
**Origem:** PDF "Alteracoes no sistema" (feedback dos contadores usuários), 2026-08-17
**Escopo:** Backend (`contabil-core`) para os itens 1–3; itens 4–5 são majoritariamente frontend (`contabil-front`)

## As 5 demandas originais

1. **Identificação automática do fornecedor e da conta** — localizar automaticamente o fornecedor por NF/CNPJ/Razão Social/Nome Fantasia usando cadastros existentes, Plano de Contas ou Notas Fiscais; ao encontrar, vincular a conta contábil.
2. **Histórico automático** — `PGTO REF RAZÃO SOCIAL` / `RECEBIMENTO REF RAZÃO SOCIAL` (sem NF); `PGTO REF NF XXX – RAZÃO SOCIAL` / `RECEBIMENTO REF NF XXX – RAZÃO SOCIAL` (com NF).
3. **Padronização dos históricos** — LETRAS MAIÚSCULAS automáticas; corrigir espaços duplicados/a mais.
4. **Busca e filtros no NEO** — UI, mas precisa de filtros novos na API.
5. **Comprovantes — arrastar arquivos** — UI pura, sem mudança de contrato obrigatória.

## Diagnóstico do código atual (validado por leitura real do repo)

- Não existe cadastro de "fornecedor" fora do ConcilPro. `CpFornecedor` é uma tabela isolada do ConcilPro (razão de fornecedores), desconectada do Plano de Contas e do NEO.
- `Regra` ([`src/db/models.py:319`](../src/db/models.py)) mapeia apenas `historico` (texto do extrato) → `conta_id`, por `(empresa_id, agencia_id)`. Sem CNPJ.
- `NeoEngine` ([`src/domain/neo/engine.py`](../src/domain/neo/engine.py)) decide conta e histórico **antes** de tentar associar comprovante/nota fiscal (`_registrar_match` roda antes de `_tentar_associar_comprovante`/`_tentar_associar_nota_fiscal`). Isso significa que hoje o CNPJ/razão social do documento associado não está disponível no momento em que o histórico é gerado.
- `Comprovante` já tem `favorecido` e `cpf_cnpj`. `NotaFiscal` já tem `cnpj_emitente`/`nome_emitente`/`cnpj_destinatario`, mas **não persiste o nome do destinatário** (o parser de NF-e joga isso em `observacao` como texto livre — perda de informação já existente, independente desta demanda).
- Não existe normalização de maiúsculas/espaços em nenhum lugar do histórico de exibição hoje. `Regra._normalizar_historico` só faz `strip().lower()` no campo de busca (`historico_normalizado`), não no texto exibido.
- NEO usa `FOR UPDATE SKIP LOCKED` e conjuntos `_comprovantes_consumidos`/`_notas_consumidas` para evitar disputa entre workers — qualquer mudança de pipeline precisa preservar isso.

## Decisão de modelagem — Item 1

**Recomendação: nova tabela `contrapartes`** (não `fornecedores`), porque o mesmo cadastro precisa cobrir pagamento (fornecedor) e recebimento (cliente) — os templates de histórico do item 2 já preveem os dois sentidos (`PGTO` e `RECEBIMENTO`).

```text
contrapartes
  id, empresa_id, tipo (fornecedor|cliente|ambos)
  documento (CPF/CNPJ só dígitos), razao_social, nome_fantasia
  conta_contabil_id, origem, confirmado_em, confirmado_por, ativa
```

Por que não estender `Regra` com um campo `cnpj` (alternativa mais simples descartada): misturaria "padrão de movimentação bancária por agência" com "identidade fiscal + conta padrão da empresa", forçaria repetir o mesmo CNPJ em regras de várias agências, e não resolveria nome de destinatário de nota de saída nem aliases.

Estado do cadastro: `origem` (`manual`/`nota_fiscal`/`comprovante`/`historico_extrato`/`backfill`) + `confirmado_em` — contraparte não confirmada só **sugere**, nunca sobrepõe uma `Regra` existente até revisão humana.

## Pipeline do NEO — mudança necessária

Não basta mover os métodos de associação para o início (documento ficaria associado mesmo se a transação continuar `sem_regra`). Pipeline correto:

```text
1. bloquear transação pendente
2. selecionar (sem associar) candidatos de nota/comprovante
3. resolver contraparte → resolver conta → gerar histórico
4. criar as duas partidas
5. só então vincular nota/comprovante (associação definitiva)
6. atualizar transação e NeoDecisao
```

Separar claramente **selecionar candidato** (não muda `transacao_id`) de **vincular candidato** (muda). Isso preserva as garantias atuais: `sem_regra` não consome documento, documento ambíguo não é associado, savepoint reverte tudo junto.

Ordem de decisão da conta: contraparte confirmada > regra textual existente > contraparte não confirmada (sugestão) > sem conta → `sem_regra`.

## Item 3 — maiúsculas e espaços: onde aplicar

Dois grupos de texto que **não podem ser confundidos**:
- **Evidência original** (`Transacao.historico`, `RegistroContabil.historico_extrato`, texto bruto de XML/PDF/OFX) — nunca normalizar, é auditoria/dedup/debug.
- **Texto contábil gerado** (`RegistroContabil.historico`/`descricao`, histórico automático) — normalizar em novos lançamentos com `" ".join(texto.split()).upper()`.

`historico_normalizado` (campo de busca da `Regra`) é outro caso: mudar de `strip().lower()` para colapsar espaços também exige migration de dados com relatório de colisões antes (ex.: `"TED  RECEBIDA"` e `"TED RECEBIDA"` coexistem hoje e colidiriam).

Recomendação: normalização automática (não configurável por empresa) para não gerar inconsistência entre clientes, aplicada só a novos registros — sem backfill destrutivo do histórico já existente.

## Itens 4 e 5 — o que realmente precisa de backend

- **Item 4 (busca/filtro NEO):** `GET /neo/decisoes` hoje só tem `resultado`, `page`, `page_size`. **Precisa de extensão de API** — novos filtros (`termo`, `estrategia`, `agencia_id`, `conta_id`, `contraparte_id`, `dc`, `mes`, `data_de`, `data_ate`), busca textual com `ILIKE` escapado, query composta no banco (não filtrar em memória os itens já paginados).
- **Item 5 (drag-and-drop):** backend aceita 1 arquivo por chamada em `POST /comprovantes/extrair-pdf`. **Não precisa de endpoint novo** para a primeira versão — o frontend pode soltar vários arquivos e disparar uploads individuais com concorrência limitada (2–4 simultâneos). Um endpoint batch só se justifica se isso virar gargalo real depois.

## Sequência incremental recomendada

| # | Entrega | Depende de | Migration? |
|---|---|---|---|
| 1 | Normalização de novos históricos (maiúsculas/espaços), feature-flagged | — | Não |
| 2 | Persistir nome do destinatário na Nota Fiscal (corrige perda de dado já existente) | — | Sim (`notas_fiscais`) |
| 3 | Cadastro de `contrapartes` (tabela + CRUD + validação + backfill dry-run) | — | Sim |
| 4 | Refatorar NEO para separar seleção/associação de candidato, **sem** mudar decisões ainda | — | Não |
| 5 | Resolver contraparte em *shadow mode* (calcula mas não aplica; só métricas) | 3, 4 | Não |
| 6 | Ativar conta por contraparte sob feature flag | 5 | Não |
| 7 | Ativar histórico automático (4 templates) | 6 | Não |
| 8 | Busca e filtros do NEO (API) | — (independente) | Não |
| 9 | Drag-and-drop (frontend, `contabil-front`) | — | Não |

Entregas 1, 8 e 9 são independentes entre si e podem começar imediatamente. Entregas 2→3→4→5→6→7 formam uma cadeia.

## Testes a proteger / criar

Não pode regredir: match exato/substring/prefixo, desempate por regra mais específica, idempotência, duas partidas balanceadas, associação manual não recontabiliza, comprovante não reutilizado, `historico_extrato` preservado, isolamento cross-company.

Novos testes necessários: normalização de texto (espaços/acentos/truncamento), os 4 templates de histórico + casos de borda (NF vazia, razão social ausente), CRUD e unicidade de `contrapartes`, resolução de contraparte por CNPJ/NF/nome, pipeline do NEO com seleção antes de associação (incluindo concorrência real em PostgreSQL — SQLite não simula `SKIP LOCKED` corretamente), contract tests dos novos filtros do NEO.

---
*Plano elaborado em conjunto com Codex (GPT-5.6 Sol), na mesma sessão do plano de fechamento contábil (ver [`plano-fechamento-contabil.md`](./plano-fechamento-contabil.md)), a partir da leitura direta do código de `src/domain/neo/engine.py`, `src/domain/regras/`, `src/db/models.py`.*
