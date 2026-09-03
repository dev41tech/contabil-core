# Plano técnico — Relatório Unificado de Melhorias (Agosto/2026)

Data da análise: 2026-08-21
Base: `contabil-core` @ `574025d` (backend). Frontend fica em `contabil-front` (repo separado, não analisado).
Método: cruzamento de cada demanda do relatório com o código real, via Codex (`gpt-5.6-sol`), com verificação manual dos achados que mudam o direcionamento do trabalho.

Documento de origem: `Relatorio_Unificado_Melhorias_Sistema_TI.pdf` — 14 páginas, ~40 demandas em 10 áreas.

Companheiro: [auditoria técnica de 2026-08-21](2026-08-21-auditoria-codex-contabil-core.md).

---

## Achado principal: metade das demandas não é backend

Das ~40 demandas, boa parte já é suportada pelo backend e está travada no consumo pelo frontend. Isso muda a alocação de esforço:

| Classificação | Qtd | Significado |
|---|---:|---|
| **JÁ EXISTE** | 9 | Backend pronto — é implementar/consumir no `contabil-front` |
| **FRONT** | 3 | UI pura, nenhum trabalho neste repo |
| **AMBOS** | 13 | Backend precisa complementar + UI |
| **BACK** | 12 | Trabalho real neste repositório |

**Recomendação:** abrir uma frente paralela no `contabil-front` com os 12 itens JÁ EXISTE/FRONT — são ganhos rápidos e visíveis para quem fez os testes, sem depender das ondas pesadas de backend.

---

## Bugs críticos confirmados

### 🔴 3.3 — Valor vs. saldo: dados ruins JÁ ESTÃO em produção

**Confirmado no histórico Git.** O commit `4ac77cf` documenta o caso exato do relatório:

```
historico: "18/02/2026 TARIFA COM R LIQUIDACAO COB000001 -1,19 -54.881,83"
valor:     54881.83   dc: C     (era uma tarifa de R$ 1,19 a débito)
```

E quantifica o estrago: **"Das 432 transações da empresa, 37 tinham a linha crua no histórico e 29 delas foram gravadas com o saldo no lugar do valor. Trinta e quatro linhas com valor negativo entraram como crédito."**

Causa raiz: o PDF não casou com nenhuma regra determinística → caiu na camada de IA → a IA capturou a última coluna (saldo) como valor.

A barreira em [validacao.py:55](../../src/domain/extrato/validacao.py:55) (aceita se o valor == primeiro número da linha, recusa se == último) **impede casos novos, mas não repara os antigos**. E só funciona quando a linha crua sobreviveu no histórico.

**O commit alerta:** *"Nenhuma virou lançamento contábil — mas só porque ninguém as classificou antes de o problema aparecer, e a caixa de classificação nova torna tentador resolver dezesseis de uma vez."*

**Ação prioritária:**
1. Query de diagnóstico varrendo todas as empresas (não só a SINCOPEÇAS) por transações com ≥2 valores no histórico onde `valor == último número`.
2. Bloquear a contabilização dessas transações até revisão.
3. Reparo automático apenas onde não houver `RegistroContabil`; onde houver, exige estorno auditado.
4. Persistir metadados de extração (parser usado, confiança, linha original) para rastreabilidade futura.

### 🔴 3.2 — Não existe parser de extrato do Sicredi

**Confirmado.** Busca no repo por "sicredi" só encontra: parser de *cartão* ([cartoes/pdf_parser.py:82](../../src/domain/cartoes/pdf_parser.py:82)), parser de *comprovante PIX* ([test_comprovantes_pdf_parser.py:91](../../tests/unit/test_comprovantes_pdf_parser.py:91)) e o código de banco 748 em [types.py:26](../../src/schemas/types.py:26). **Nenhum adapter de extrato.**

O suporte atual tem 3 camadas:
1. OFX genérico ([ofx_parser.py:43](../../src/domain/extrato/ofx_parser.py:43))
2. PDF determinístico genérico, documentado para Bradesco X-One ([pdf_parser.py:1](../../src/domain/extrato/pdf_parser.py:1))
3. Fallback OpenAI para layouts não mapeados ([pdf_parser.py:322](../../src/domain/extrato/pdf_parser.py:322))

Existe código de OCR/Vision em [pdf_parser.py:417](../../src/domain/extrato/pdf_parser.py:417), **mas `parse_pdf()` nunca o chama** — PDF escaneado vai direto para o erro em [pdf_parser.py:573](../../src/domain/extrato/pdf_parser.py:573).

Além disso, a detecção de formato é **só pela extensão do arquivo**: `.pdf` usa parser PDF, qualquer outra coisa é tratada como OFX ([extrato.py:47](../../src/api/v1/extrato.py:47)).

**Este item e o 3.3 são o mesmo problema.** O Sicredi cai na IA porque não tem parser → a IA erra a coluna. Resolver 3.2 resolve a causa de 3.3.

**Ação:** obter o arquivo real de 07/2026 sanitizado como fixture → adapter determinístico Sicredi (data, histórico, valor, saldo, D/C) → validar soma dos movimentos contra saldo inicial/final → detectar formato por conteúdo, não extensão → tornar o OCR alcançável.

### 🔴 4.4 — Contrapartes nunca casam por nome, só por CNPJ vindo de documento

**Confirmado no código.** A classificação automática por contraparte em [engine.py:511](../../src/domain/neo/engine.py:511) exige uma cadeia inteira:

1. Existir uma **nota fiscal ou comprovante candidato único** casado por valor (±R$ 0,01) e data (±3 dias)
2. Esse documento conter CNPJ/CPF
3. Esse documento casar exatamente com `Contraparte.documento`

A busca é **exclusivamente por documento** ([engine.py:567](../../src/domain/neo/engine.py:567)). **O NEO nunca compara `Transacao.historico` com a razão social ou nome fantasia da contraparte.**

**Causa raiz do teste relatado:** o fornecedor estava cadastrado, mas o lançamento bancário só tinha o *nome* no histórico, sem NF/comprovante que fornecesse o CNPJ. Nesse cenário a implementação atual **necessariamente** deixa a transação pendente — não é intermitência, é o comportamento projetado.

Agravantes: uma regra automática tem prioridade e deixa a contraparte só em shadow mode ([engine.py:362](../../src/domain/neo/engine.py:362)); e criar/editar uma contraparte **não reprocessa** as pendências existentes.

**Ação:** matching normalizado por razão social/nome fantasia + aliases, com limiar de confiança e checagem de colisão entre contrapartes. Documento continua sendo evidência forte; nome gera sugestão quando ambíguo. Reprocessar pendências ao confirmar contraparte.

### 🟠 5.2 — ConcilPro não enxerga o extrato bancário (por arquitetura)

O ConcilPro opera em models isolados (`cp_arquivo`, `cp_fornecedor`, `cp_lancamento` — [models.py:840](../../src/db/models.py:840)) e concilia **Razão de Fornecedores contra ele mesmo**: créditos `COMPRA` vs. débitos `PAGAMENTO` do mesmo fornecedor, em FIFO ([conciliacao_intel.py:48](../../src/domain/concilpro/conciliacao_intel.py:48)).

**Se o pagamento da UNIMED existe apenas no extrato bancário, o ConcilPro jamais o verá** — não há nenhum ponto de contato entre `Transacao` e as tabelas `cp_*`.

Decisão de produto necessária: o ConcilPro continua sendo "razão de fornecedores puro", ou passa a reconciliar banco × razão? A segunda opção é uma feature arquitetural grande (migration de vínculo, matching por valor/data/CNPJ/nome, pagamentos parciais e agregados, fila de ambiguidades).

---

## Resposta à dúvida do item 4.5

> *"Na conciliação, o sistema considera apenas os movimentos dos bancos ou também utiliza os dados do razão contábil?"*

**Depende do módulo:**

- **NEO** — usa **apenas os movimentos bancários**. O motor carrega só `Transacao` com `status="pendente"` ([engine.py:977](../../src/domain/neo/engine.py:977)). Regras, notas, comprovantes e contrapartes entram como *evidência* para decidir a classificação, e então o NEO **gera** o razão, criando as duas partidas em `RegistroContabil` ([engine.py:617](../../src/domain/neo/engine.py:617)). **O razão é saída, não entrada.**
- **ConcilPro** — o oposto: consome um **Razão de Fornecedores** importado e **não lê** as transações bancárias.

Ou seja: nenhum dos dois cruza banco com razão preexistente. É exatamente essa lacuna que produz o sintoma do item 5.2.

---

## Tabela completa de classificação

| # | Item | Classe | Situação hoje | Esforço | Risco |
|---|---|---|---|:--:|---|
| 2.1 | Busca por número/código da conta | **JÁ EXISTE** | API retorna `conta_numero`, `codigo` e `descricao` ([plano_contas.py:142](../../src/schemas/plano_contas.py:142)); busca é client-side | P | Baixo |
| 2.2 | Reimportação do Plano de Contas | BACK | Fixes recentes ok; upsert ainda identifica só por `codigo` ([service.py:399](../../src/domain/plano_contas/service.py:399)) | G | Alto |
| 2.2 | Excluir conta com lançamentos | BACK | Bloqueio é proteção contábil intencional ([service.py:212](../../src/domain/plano_contas/service.py:212)); falta fluxo de encerramento/substituição | G | Alto |
| 2.2 | Filtro do plano de contas | **JÁ EXISTE** | Endpoint devolve tudo, sem `q`; filtro é UI | P | Baixo |
| 2.3 | Colunas redimensionáveis | FRONT | — | P/M | Baixo |
| 3.1 | Filtros de transação (histórico, valor, D/C, saldo) | AMBOS | Agência/status/período existem; falta histórico/valor/DC. **Saldo não é persistido** — é lido e descartado ([pdf_parser.py:132](../../src/domain/extrato/pdf_parser.py:132)) | M/G | Médio/alto |
| 3.1 | Escolher registros por página | **JÁ EXISTE** | `page_size` 1–200, default 50 ([extrato.py:82](../../src/api/v1/extrato.py:82)). O limite de 20 é do front | P | Baixo |
| 3.2 | Leitura Sicredi | BACK | **Sem adapter**; OCR inalcançável; detecção por extensão | G | **Alto** |
| 3.3 | Valor vs. saldo | BACK | Barreira nova bloqueia casos futuros; **dados ruins persistem em produção** | G | **Crítico** |
| 4.1 | Filtros NEO | AMBOS | Termo, resultado, estratégia, D/C, agência, conta, competência, valor min/max e paginação já existem ([neo.py:70](../../src/api/v1/neo.py:70)); faltam data livre e motivo | M | Médio |
| 4.2 | Visualização do lançamento | AMBOS | Histórico/valor/D-C/motivo já na resposta; **falta a data** em `NeoDecisaoResponse` | M | Baixo |
| 4.3 | Conciliação manual prioritária | AMBOS | Manual e lote existem com `FOR UPDATE` ([neo.py:341](../../src/api/v1/neo.py:341)); não há override nem reclassificação do que já foi automático | G | Alto |
| 4.4 | Contrapartes automáticas | BACK | **Só casa por CNPJ vindo de NF/comprovante, nunca por nome** | G | **Alto** |
| 4.5 | Fonte da conciliação | **JÁ EXISTE** | Respondido acima | — | — |
| 5.1 | Navegação da lista de empresas | **JÁ EXISTE** | Backend pagina até 200 ([empresas.py:33](../../src/api/v1/empresas.py:33)); front precisa paginar, não só alargar o dropdown | M (front) | Baixo |
| 5.1 | Visual estilo "Mister" | FRONT | — | M/G | Baixo |
| 5.2 | Vincular pagamentos de fornecedores | BACK | **ConcilPro não consome extrato bancário** | G | **Alto** |
| 6 | Leitura incompleta de comprovantes | BACK | Parser para na 1ª camada que acha `valor_pago`, mesmo sem favorecido/documento/datas ([pdf_parser.py:523](../../src/domain/comprovantes/pdf_parser.py:523)) | M/G | Médio |
| 6 | Impedir duplicidade | AMBOS | Sem hash nem dedup key no model ([models.py:531](../../src/db/models.py:531)) | G | Alto |
| 6 | Processar em lote | AMBOS | Só endpoints unitários (extrair + criar) | M/G | Médio |
| 6 | Editar comprovante | AMBOS | **Não existe PATCH** | M | Médio |
| 6 | Excluir comprovante | **JÁ EXISTE** | `DELETE /comprovantes/{id}` com soft delete ([comprovantes.py:190](../../src/api/v1/comprovantes.py:190)) | P | Baixo |
| 6 | Associação manual | AMBOS | Ação já existe ([comprovantes.py:159](../../src/api/v1/comprovantes.py:159)); a busca automática usa valor ±R$0,01 e data ±3d, **não histórico** ([engine.py:808](../../src/domain/neo/engine.py:808)) | M | Médio |
| 7 | Filtro por valor em NF | BACK | Ausente ([notas.py:33](../../src/api/v1/notas.py:33)) | P | Baixo |
| 7 | Visualizar a NF | AMBOS | **Arquivo original não é armazenado** ([models.py:486](../../src/db/models.py:486)) | G | Médio |
| 7 | Entrada vs. saída | AMBOS | Regra já existe na exportação ([exportacao/service.py:389](../../src/domain/exportacao/service.py:389)); falta expor `direcao` na resposta de consulta | M | Baixo |
| 8 | Excluir fatura | BACK | Há DELETE de cartão e de lançamento, **não de fatura** | M | Médio |
| 8 | Vinculação automática do cartão | BACK | Só quitação manual com valor exato ([cartoes/service.py:292](../../src/domain/cartoes/service.py:292)); lançamento não tem FK para contraparte/NF | G | Alto |
| 8 | Exportar cartão | AMBOS | Exportador não tem tipo cartão ([exportacao/service.py:82](../../src/domain/exportacao/service.py:82)) | M | Médio |
| 9 | Leitura de aplicações | AMBOS | Só CRUD de posição estática; sem parser nem movimentos | G | Alto |
| 10 | Excluir regras | **JÁ EXISTE** | `DELETE /regras/{id}` ([regras.py:88](../../src/api/v1/regras.py:88)) — desativa preservando histórico | P | Baixo |
| 10 | Excluir agências | **JÁ EXISTE** | `DELETE /agencias/{id}` ([agencias.py:86](../../src/api/v1/agencias.py:86)) | P | Baixo |
| 10 | Excluir razão | BACK | `/contabil` só tem GET | G | Alto |
| 10 | Excluir extratos | BACK | Sem DELETE **e sem entidade de importação** — impossível saber quais linhas vieram do mesmo arquivo | G | Alto |

---

## Plano de execução em ondas

### Onda 0 — Destravar (pré-requisito das demais)
Os achados críticos da [auditoria anterior](2026-08-21-auditoria-codex-contabil-core.md) bloqueiam boa parte deste plano:
1. Corrigir a CI para rodar contra PostgreSQL de verdade — **11 dos itens acima exigem migration**, e hoje nenhuma migration é testada
2. Bloquear a chamada à OpenAI sem consentimento no ConcilPro
3. Remover o dump de produção versionado
4. Definir o tipo monetário e a política de arredondamento
5. Tornar ConcilPro/importações jobs duráveis e idempotentes

### Onda 1 — Recuperar a confiança contábil
1. **3.3** — Diagnosticar e sanear as transações valor/saldo já gravadas
2. **3.2** — Parser Sicredi determinístico com fixture real (resolve a causa de 3.3)
3. **2.2** — Identidade/upsert seguro do Plano de Contas + relatório das contas 16, 17, 21, 22, 27, 49, 64
4. Corrigir o filtro de data que descarta o último dia (afeta extrato, NEO, notas, relatórios e qualquer export por período)

### Onda 2 — Classificação e conciliação confiáveis
1. **4.4** — Matching de contraparte por nome/alias com confiança e revisão
2. **4.3** — Reclassificação manual auditada com prioridade explícita
3. **5.2** — Decidir e implementar banco × razão no ConcilPro
4. **6** — Extração completa, dedup e associação manual pesquisável
5. **8.2** — Quitação automática de fatura e vínculo controlado de compras

### Onda 3 — Lifecycle e exclusões seguras
1. Modelar importações de extrato e razão como lotes rastreáveis
2. Cancelamento/reversão auditada (nunca DELETE de uma perna solta de partida dobrada)
3. DELETE de fatura, PATCH de comprovante
4. Encerramento/substituição de conta contábil referenciada

### Onda 4 — Consultas, exports e documentos
Filtros faltantes (extrato/NEO/notas) · direção entrada/saída da NF · visualização do original da NF · exportação de cartão · ingestão de extratos de aplicações

### Onda 5 — `contabil-front` (pode correr em paralelo desde já)
Busca por código de conta · colunas redimensionáveis · seleção de page size · paginação da lista de empresas · abas, tooltips, visual Mister, fila de upload

---

## Itens que exigem migration de banco

- **2.2** unicidade/identidade de `conta_numero`; estado de encerramento
- **3.1** saldo por transação + metadados de origem/qualidade
- **3.3** metadados de extração + script de saneamento de dados
- **4.3** estado/versionamento de revisão manual
- **4.4** aliases, confiança e evidências de contraparte
- **5.2** vínculo persistente `Transacao` ↔ `cp_*`
- **6** hash/dedup key de comprovante + entidade de batch
- **7** object key/metadata do arquivo original da NF
- **8.2** FKs de contraparte/NF no lançamento de cartão
- **9** movimentos de aplicação e vínculo bancário
- **10** lote de importação, cancelamento e reversão

**Não exigem migration:** itens FRONT, filtros simples, page size, direção computada da NF, DELETE de fatura (usa `deleted_at` existente), PATCH de comprovante, exportação de cartão.

---

## Como os achados da auditoria anterior afetam estas demandas

| Achado anterior | Demandas afetadas |
|---|---|
| CI testa em SQLite, não Postgres | **Todas as 11 migrations acima** + testes de concorrência/constraints |
| Precisão monetária inconsistente | Extrato, Plano de Contas, comprovantes, cartões, aplicações, ConcilPro |
| ConcilPro ignora consentimento OpenAI | Bloqueia a evolução do item 5.2 |
| ConcilPro concorrente/não durável | Bloqueia reprocessamento e matching banco×razão |
| Trilha de auditoria incompleta | Bloqueia exclusões (item 10), reparo valor/saldo e reclassificação manual |
| Lost update em associações | Comprovantes, notas, automações de cartão |
| `valor_total` de fatura desatualizado | Precisa ser resolvido antes de excluir/vincular/exportar faturas |
| Permissões sem `aplicacoes` e `concilpro` | Novas funções ficarão inacessíveis a usuários não-admin |
| Filtro de data do último dia | Extrato, NEO, notas, relatórios e qualquer novo export por período |
