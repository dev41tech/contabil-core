# Auditoria técnica do contabil-core — Codex (gpt-5.6-sol)

Data: 2026-08-21
Método: revisão estática somente leitura (sem execução de testes) via Codex MCP, com verificação cruzada manual dos achados críticos (itens 2.1, 2.5 e 4.1 confirmados diretamente no repositório).

Severidades: **Crítica** = risco imediato de vazamento/corrupção; **Alta** = falha provável com impacto financeiro ou operacional; **Média** = dívida relevante ou falha em condições específicas.

## 1. Bugs e problemas de correção

### 1.1 Crítica — Processamento concorrente pode duplicar lançamentos no ConcilPro
- [src/api/v1/concilpro.py:343](../../src/api/v1/concilpro.py:343), [:352](../../src/api/v1/concilpro.py:352), [:430](../../src/api/v1/concilpro.py:430), [:168](../../src/api/v1/concilpro.py:168)
- [src/api/app.py:53](../../src/api/app.py:53)

Upload verifica estado e age sem lock transacional; um `PROCESSANDO` sem fornecedores é tratado como abandonado e resetado, disparando um segundo worker. Dois requests concorrentes (ou um restart durante processamento) podem duplicar dados.

Correção: job durável com `attempt_id`, `locked_at`, heartbeat; aquisição atômica via `SELECT ... FOR UPDATE SKIP LOCKED`; chave de idempotência; resultados em staging publicados atomicamente.

### 1.2 Alta — Política monetária não alinhada entre API, Python e PostgreSQL
- Colunas `NUMERIC(15,2)`: [src/db/models.py:298](../../src/db/models.py:298)
- Schemas sem `max_digits`/`decimal_places`: [notas.py:21](../../src/schemas/notas.py:21), [cartoes.py:27](../../src/schemas/cartoes.py:27), [comprovantes.py:17](../../src/schemas/comprovantes.py:17), [aplicacoes.py:21](../../src/schemas/aplicacoes.py:21)
- Quantização Python (`ROUND_HALF_EVEN`): [src/domain/notas/service.py:399](../../src/domain/notas/service.py:399)

`1.005` pode virar `1.00` na dedupe Python mas `1.01` no PostgreSQL (que arredonda afastando de zero em empates). Valores fora da escala só falham no flush (500).

Correção: tipo monetário único com limite de dígitos/escala e regra de arredondamento explícita (ex.: `ROUND_HALF_UP`), aplicada antes de dedupe/cálculo/persistência; `CHECK` no banco; testes reais em PostgreSQL.

### 1.3 Alta — Estado de erro do Open Banking é sempre desfeito pelo rollback
- [src/domain/openbanking/service.py:240](../../src/domain/openbanking/service.py:240) seta `status="erro"` e faz `flush()` antes de lançar exceção; a dependência de sessão faz rollback de qualquer exceção em [src/db/session.py:88](../../src/db/session.py:88).

O erro nunca é persistido — o diagnóstico se perde.

### 1.4 Alta — Filtros por data excluem quase todo o último dia
- [src/domain/relatorios/service.py:79](../../src/domain/relatorios/service.py:79), [:184](../../src/domain/relatorios/service.py:184), [:301](../../src/domain/relatorios/service.py:301)
- [src/domain/extrato/service.py:245](../../src/domain/extrato/service.py:245)

`data_ate` vira `00:00:00` do dia — uma transação às 10h do último dia some do relatório. Correção: aceitar `date` puro e usar intervalo semiaberto `>= início` / `< dia_seguinte`. Ver implementação próxima do correto em [src/domain/contabil/service.py:61](../../src/domain/contabil/service.py:61).

### 1.5 Alta — Deduplicação "SELECT depois INSERT" não é idempotente sob concorrência
- [src/domain/extrato/service.py:84](../../src/domain/extrato/service.py:84), [src/domain/openbanking/service.py:258](../../src/domain/openbanking/service.py:258)

Dois uploads/sincronizações iguais concorrentes geram `IntegrityError` só no flush final, abortando o lote (500) mesmo sendo operação idempotente por natureza. Usar `INSERT ... ON CONFLICT DO NOTHING RETURNING`.

### 1.6 Alta — Associações financeiras permitem lost update
- [src/domain/comprovantes/service.py:131](../../src/domain/comprovantes/service.py:131), [src/domain/notas/service.py:176](../../src/domain/notas/service.py:176)

Verifica `transacao_id is None` e atualiza sem `FOR UPDATE`; dois usuários associando simultaneamente geram vencedor silencioso do último commit. Usar update condicional atômico (`UPDATE ... WHERE transacao_id IS NULL RETURNING`) e responder 409 em conflito.

### 1.7 Média — PATCH não remove valores opcionais (null é ignorado)
- [src/domain/aplicacoes/service.py:120](../../src/domain/aplicacoes/service.py:120), [src/domain/cartoes/service.py:158](../../src/domain/cartoes/service.py:158), [src/domain/contrapartes/service.py:138](../../src/domain/contrapartes/service.py:138)

Não dá pra desassociar/limpar campo enviando `null`. Usar `model_fields_set` (já feito em [src/domain/agencias/service.py:116](../../src/domain/agencias/service.py:116)). Bônus: [src/schemas/cartoes.py:48](../../src/schemas/cartoes.py:48) não valida `ge=0` no limite via PATCH.

### 1.8 Média — `FaturaCartao.valor_total` fica desatualizado
- [src/domain/cartoes/service.py:249](../../src/domain/cartoes/service.py:249), [src/db/models.py:605](../../src/db/models.py:605)

Coluna denormalizada não é mantida em inserções/exclusões de lançamentos de fatura.

## 2. Segurança

### 2.1 Crítica — CONFIRMADO: dump de produção versionado e copiado para a imagem Docker
- `migrate_prod.sql`: **28 MB, rastreado pelo git** (`git ls-files` confirma), último touch no commit `5ffeed5` ("OPENAI FIX").
- `Dockerfile:32` faz `COPY . .`; `.dockerignore` **não exclui** o arquivo.
- Gerado por [migrate_export.py](../../migrate_export.py) com dados reais de empresas (CNPJs, nomes, financeiro).

**Ação recomendada:** tratar como incidente — remover do repo, avaliar purga de histórico, invalidar imagens/caches já publicados, avaliação LGPD, adicionar a `.gitignore`/`.dockerignore`, gerar exports fora do repo.

### 2.2 Crítica — ConcilPro ignora consentimento para envio de dados à OpenAI
- Config padrão desabilitada: [src/core/config.py:102](../../src/core/config.py:102)
- `_get_client()` do ConcilPro só checa se a API key existe, não o flag de consentimento: [src/domain/concilpro/ai_classifier.py:222](../../src/domain/concilpro/ai_classifier.py:222)
- Envia texto ([:245](../../src/domain/concilpro/ai_classifier.py:245)), imagens/PDF ([:297](../../src/domain/concilpro/ai_classifier.py:297)) e históricos/valores ([:424](../../src/domain/concilpro/ai_classifier.py:424))

Basta ter `OPENAI_API_KEY` configurada — mesmo com `ALLOW_FINANCIAL_DATA_TO_OPENAI=false` — para dados financeiros vazarem para a OpenAI via ConcilPro.

### 2.3 Alta — Módulos `aplicacoes` e `concilpro` não existem na lista de permissões
- [src/api/deps.py:132](../../src/api/deps.py:132) infere módulo pela URL e exige presença em [src/schemas/permissoes.py:9](../../src/schemas/permissoes.py:9), que não lista esses dois módulos.

Admin não consegue conceder acesso granular — força uso de perfil admin completo como workaround.

### 2.4 Alta — Dados financeiros e PII em logs
- `print` de amostra crua de transação: [src/domain/concilpro/ai_classifier.py:279](../../src/domain/concilpro/ai_classifier.py:279)
- Nomes de fornecedores e valores logados: [src/api/v1/concilpro.py:192](../../src/api/v1/concilpro.py:192)

### 2.5 Alta — CONFIRMADO: segredo de produção já esteve hardcoded e chegou ao histórico do git
[prod_db.py:4-6](../../prod_db.py:4) (raiz do repo, não em `src/db/scripts/` como o Codex citou originalmente) documenta: *"Até 2026-07-30 a senha de produção estava escrita em quatro scripts deste diretório e chegou ao histórico do git"*. Mitigado no código atual (URL vem de `PROD_DATABASE_URL`), mas a credencial antiga permanece recuperável no histórico Git a menos que já tenha sido rotacionada/purgada — vale confirmar se a rotação foi feita.

## 3. Qualidade e manutenibilidade

### 3.1 Alta — ConcilPro concentra HTTP, jobs, transações e lógica em arquivos enormes
[src/api/v1/concilpro.py](../../src/api/v1/concilpro.py) (~977 linhas) e [src/domain/concilpro/parser.py](../../src/domain/concilpro/parser.py) (~1387 linhas). Contribuiu diretamente para os bugs de consentimento (2.2) e concorrência (1.1). Separar em controlador/serviço de ingestão/parser puro/job durável/repositório.

### 3.2 Alta — Fronteiras transacionais inconsistentes
Mistura de commits explícitos com dependência do commit automático da sessão; caso Open Banking (1.3) é sintoma direto disso.

### 3.3 Alta — Auditoria não cobre todas as mutações financeiras
Infra existe em [src/domain/auditoria/service.py:48](../../src/domain/auditoria/service.py:48), mas associação de notas/comprovantes e importação de extrato não geram evento completo.

## 4. Testes

### 4.1 Crítica — CONFIRMADO: CI roda testes de "integração" contra SQLite, não PostgreSQL
- `tests/conftest.py:37` lê `TEST_DATABASE_URL_REAL` com fallback `sqlite+aiosqlite:///:memory:`.
- `pipelines/ci.yml:94` define `TEST_DATABASE_URL` (**sem o sufixo `_REAL`**).

**Confirmado diretamente**: o nome da variável não bate, então mesmo com um serviço Postgres de pé na CI, os testes caem no fallback SQLite silenciosamente. Isso já era suspeitado ([[deploy-easypanel-contabil-core]] — falhas de migration em produção que os testes SQLite nunca pegariam) e agora está confirmado como causa raiz de por que a suíte não detecta bugs específicos de Postgres (NUMERIC/arredondamento, `ON CONFLICT`, `FOR UPDATE SKIP LOCKED`, constraints, timezone).

**Correção:** renomear para bater os dois lados, e falhar explicitamente se o job de integração não estiver em dialect `postgresql`.

### 4.2 Crítica — Nenhum teste roda a sequência real de migrations
`tests/conftest.py:40` usa `Base.metadata.create_all()` em vez de `alembic upgrade head` — migrations nunca são exercitadas nos testes.

### 4.3–4.6 Alta/Média
- Testes monetários checam tipo, não regra de arredondamento (`tests/unit/test_decimal_monetario.py:26`).
- Testes de relatório mascaram o bug do item 1.4 passando `23:59:59` manualmente (`tests/integration/test_relatorios.py:173`).
- Sem testes de concorrência ou de consentimento negativo (ConcilPro + OpenAI).
- Cobertura não é gate: Codecov com `continue-on-error` (`pipelines/ci.yml:54`), sem `fail_under` (`pyproject.toml:67`).

## 5. Performance

- **5.1** Open Banking: uma query de duplicidade por transação ([src/domain/openbanking/service.py:258](../../src/domain/openbanking/service.py:258)) — 20 mil transações = 20 mil round trips. Provider/cache de auth recriado por serviço ([:81](../../src/domain/openbanking/service.py:81)).
- **5.2** Conciliação FIFO detalhada é O(n²) ([src/api/v1/concilpro.py:716](../../src/api/v1/concilpro.py:716)) — fornecedores com dezenas de milhares de lançamentos podem travar um worker por minutos.
- **5.3** Relatórios/exports materializam o período inteiro em memória ([src/domain/relatorios/service.py:296](../../src/domain/relatorios/service.py:296), [src/domain/exportacao/service.py:191](../../src/domain/exportacao/service.py:191)/[:331](../../src/domain/exportacao/service.py:331)) — risco de OOM em empresas grandes.

## 6. Arquitetura e dívida técnica

- **6.1** Background tasks críticas (ConcilPro) são locais ao processo web — restart/deploy perde trabalho, sem coordenação entre workers ([src/api/v1/concilpro.py:430](../../src/api/v1/concilpro.py:430)).
- **6.2** Alembic roda no startup de cada container ([entrypoint.sh:55](../../entrypoint.sh:55)) — rolling deploy com múltiplas réplicas pode rodar DDL simultâneo.
- **6.3** Isolamento de tenant depende da aplicação, sem FK composta garantindo `empresa_id` consistente entre pai/filho (ex.: [src/db/models.py:599](../../src/db/models.py:599)).
- **6.4** Migrations com backfill linha a linha ([0010_notas_identidade_empresa.py:24](../../src/db/migrations/versions/0010_notas_identidade_empresa.py:24)) e downgrades vazios ([0020_export_formato_txt.py:34](../../src/db/migrations/versions/0020_export_formato_txt.py:34)).
- **6.5** Imagem de produção instala `.[dev]` sem lock reprodutível ([Dockerfile:15](../../Dockerfile:15), deps com `>=` aberto em [pyproject.toml:7](../../pyproject.toml:7)).

## Pontos positivos

- Uso consistente de `Decimal`/`NUMERIC` (evita o erro clássico de `float` para dinheiro).
- Sem superfície clara de SQL injection nas rotas principais (SQLAlchemy parametrizado).
- Escopo por empresa presente na maior parte dos serviços (falta reforço no banco).
- Autenticação com verificação de usuário ativo, CSRF, rate limiting, refresh token.

## Resumo executivo — prioridades imediatas

1. **Remover `migrate_prod.sql` do repositório e tratar como incidente** (histórico Git, imagens Docker, avaliação LGPD). *(confirmado)*
2. **Bloquear o ConcilPro de chamar a OpenAI sem consentimento explícito**, cobrindo texto, imagem e classificação.
3. **Corrigir a CI para rodar de fato contra PostgreSQL** — hoje cai silenciosamente em SQLite. *(confirmado — causa raiz de vários bugs Postgres-específicos não detectados)*
4. **Eliminar a concorrência insegura do ConcilPro** (background tasks locais → jobs duráveis idempotentes).
5. **Unificar a política monetária** (validação + arredondamento Python + `NUMERIC(15,2)` Postgres).
6. **Corrigir filtros de data que excluem o último dia** em relatórios/extrato.
7. **Tornar imports/sincronizações/associações atômicos sob concorrência** (`ON CONFLICT`, locks condicionais).
8. **Serializar migrations no deploy** e adicionar testes reais de `alembic upgrade head`.
