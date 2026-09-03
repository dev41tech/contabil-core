# Auditoria técnica do contabil-core — Codex

Data: 2026-08-26  
Estado auditado: commit `583d15d` (`feat/neo-ordem-e-prefixo-rec`; o checkout local não estava em `main`, embora contenha os commits descritos para 24–26/08).  
Método: revisão estática, sem execução da suíte, sem alteração de `src/` e sem commit. Foram lidos código, migrations e testes; não foi validado banco de produção.

Severidades: **Crítica** = risco imediato de vazamento/corrupção; **Alta** = falha provável com impacto financeiro ou operacional; **Média** = dívida relevante ou falha em condições específicas.

## 1. Escopo e conclusão

As entregas de 24–26/08 corrigem a data bancária, estabilizam a ordem visível, criam lotes canceláveis, persistem jobs de NEO/extrato e endurecem o NEO contra valor suspeito. A migration 0031 elimina corretamente a comparação entre enums nominais diferentes, mas continua sem ser executada por teste algum.

Os riscos imediatos da auditoria anterior não desapareceram: o dump de produção continua versionado, o ConcilPro continua ignorando o consentimento de envio à OpenAI e ainda roda em `BackgroundTasks` sem coordenação. A ausência de workflow instalado em `.github/workflows` significa que nem mesmo o pipeline descrito em `pipelines/ci.yml` é executado automaticamente.

## 2. Reverificação dos achados de 21/08

### 2.1 Crítica — ConcilPro concorrente: **persiste**

- [src/api/v1/concilpro.py:321](../../src/api/v1/concilpro.py:321), [src/api/v1/concilpro.py:343](../../src/api/v1/concilpro.py:343), [src/api/v1/concilpro.py:430](../../src/api/v1/concilpro.py:430)
- [src/api/app.py:53](../../src/api/app.py:53)

Os jobs persistentes novos atendem apenas NEO e importação de extrato; o ConcilPro ainda faz `SELECT` antes de agir, agenda `BackgroundTasks` local e reseta estado no startup. Requests concorrentes continuam podendo iniciar processamento duplicado.

Correção: incluir ConcilPro numa fila durável, com aquisição atômica, `attempt_id`, heartbeat e publicação idempotente dos resultados; até lá, impor unicidade/lock transacional por empresa e hash do arquivo.

### 2.2 Alta — Política monetária: **persiste**

- [src/db/models.py:508](../../src/db/models.py:508), [src/schemas/notas.py:21](../../src/schemas/notas.py:21), [src/schemas/comprovantes.py:17](../../src/schemas/comprovantes.py:17), [src/domain/notas/service.py:451](../../src/domain/notas/service.py:451)

As colunas continuam em `NUMERIC(15,2)`, os schemas aceitam escala/precisão maiores e a quantização Python continua dependente do contexto padrão (`ROUND_HALF_EVEN`), diferente do empate do PostgreSQL.

Correção: centralizar um tipo monetário com precisão, escala e arredondamento explícitos antes de dedupe e persistência, além de `CHECK` e casos executados em PostgreSQL.

### 2.3 Alta — Estado de erro do Open Banking: **persiste**

- [src/domain/openbanking/service.py:240](../../src/domain/openbanking/service.py:240), [src/db/session.py:88](../../src/db/session.py:88)

O serviço ainda faz `flush()` de `status="erro"` e em seguida lança exceção; `get_db` reverte a transação inteira. O diagnóstico continua não persistido.

Correção: registrar a falha em transação independente após o rollback da sincronização, ou devolver resultado de falha sem lançar antes do commit desse estado.

### 2.4 Alta — Último dia do filtro: **mudou de forma**

- Corrigido para extrato/livro-caixa: [src/db/migrations/versions/0026_transacao_data_como_date.py:39](../../src/db/migrations/versions/0026_transacao_data_como_date.py:39), [src/domain/extrato/service.py:263](../../src/domain/extrato/service.py:263)
- Persiste em DRE/balancete: [src/api/v1/relatorios.py:30](../../src/api/v1/relatorios.py:30), [src/domain/relatorios/service.py:79](../../src/domain/relatorios/service.py:79), [src/domain/relatorios/service.py:184](../../src/domain/relatorios/service.py:184)

`Transacao.data` agora é `DATE` e os filtros sobre ela são inclusivos. DRE e balancete ainda aceitam `datetime`; `data_ate=2026-08-31` vira meia-noite e exclui o restante do dia.

Correção: expor `date` e aplicar intervalo fechado-aberto sobre `RegistroContabil.data_lancamento`, com limite exclusivo no dia seguinte.

### 2.5 Alta — Deduplicação concorrente de extrato/Open Banking: **persiste**

- [src/domain/extrato/service.py:96](../../src/domain/extrato/service.py:96), [src/domain/extrato/service.py:201](../../src/domain/extrato/service.py:201), [src/domain/openbanking/service.py:263](../../src/domain/openbanking/service.py:263)

Os dois fluxos ainda fazem `SELECT` seguido de `INSERT`. A restrição única protege o dado, mas uma colisão aborta o lote em vez de produzir resposta idempotente.

Correção: inserir em lote com `INSERT ... ON CONFLICT DO NOTHING RETURNING`, usando o retorno para contar importadas e duplicadas.

### 2.6 Alta — Lost update em associações: **mudou de forma**

- Persiste nos endpoints explícitos: [src/domain/comprovantes/service.py:144](../../src/domain/comprovantes/service.py:144), [src/domain/notas/service.py:184](../../src/domain/notas/service.py:184)
- Corrigido no caminho automático do NEO: [src/domain/neo/engine.py:1152](../../src/domain/neo/engine.py:1152), [src/domain/neo/engine.py:1225](../../src/domain/neo/engine.py:1225)

O NEO agora seleciona notas/comprovantes com `FOR UPDATE SKIP LOCKED`, mas as APIs manuais continuam lendo `transacao_id`, testando em Python e atualizando sem lock/`UPDATE` condicional.

Correção: usar `UPDATE ... WHERE transacao_id IS NULL RETURNING` ou travar o documento antes da verificação e responder 409 ao perdedor.

### 2.7 Média — PATCH não limpa opcionais: **persiste**

- [src/domain/aplicacoes/service.py:120](../../src/domain/aplicacoes/service.py:120), [src/domain/cartoes/service.py:158](../../src/domain/cartoes/service.py:158), [src/domain/contrapartes/service.py:131](../../src/domain/contrapartes/service.py:131), [src/schemas/cartoes.py:52](../../src/schemas/cartoes.py:52)

Os serviços ainda tratam `None` como “campo ausente”; não é possível limpar agência, limite, observação, vencimento, nome fantasia ou conta contábil quando o schema admite nulo. O `limite` do PATCH segue sem `ge=0`.

Correção: distinguir ausência de `null` via `model_fields_set` e repetir no schema de update as restrições do create.

### 2.8 Média — `FaturaCartao.valor_total`: **mudou de forma**

- [src/db/models.py:737](../../src/db/models.py:737), [src/domain/cartoes/service.py:75](../../src/domain/cartoes/service.py:75), [src/domain/cartoes/service.py:394](../../src/domain/cartoes/service.py:394)

A coluna denormalizada continua sem manutenção, mas as leituras principais passaram a calcular `SUM(LancamentoCartao.valor)`. O dado persistido permanece falso e pode contaminar consultas/scripts futuros.

Correção: remover a coluna numa migration se o total for sempre derivado; se ela for necessária, mantê-la atomicamente em toda mutação e conferir com constraint/trigger.

### 2.9 Crítica — Dump de produção no repositório/imagem: **persiste**

- [migrate_prod.sql](../../migrate_prod.sql)
- [Dockerfile:32](../../Dockerfile:32), [.dockerignore](../../.dockerignore), [.gitignore](../../.gitignore)

O arquivo de 28 MB continua rastreado, não é ignorado e entra em `COPY . .`. Nada no estado atual demonstra purga do histórico, invalidação de imagens/caches ou tratamento LGPD.

Correção: tratar como incidente: remover, purgar o histórico onde cabível, invalidar artefatos, avaliar notificação/impacto LGPD e impedir novos exports no repositório.

### 2.10 Crítica — Consentimento OpenAI no ConcilPro: **persiste**

- [src/core/config.py:102](../../src/core/config.py:102), [src/domain/concilpro/ai_classifier.py:222](../../src/domain/concilpro/ai_classifier.py:222), [src/domain/concilpro/ai_classifier.py:254](../../src/domain/concilpro/ai_classifier.py:254)

`_get_client()` ainda verifica somente pacote e chave. Texto, imagem e lançamentos incertos podem ser enviados mesmo com `ALLOW_FINANCIAL_DATA_TO_OPENAI=false`.

Correção: fazer o bloqueio no único ponto de obtenção do cliente e cobrir negativamente todas as três chamadas de IA.

### 2.11 Alta — Módulos fora da whitelist: **piorou de forma conhecida**

- [src/api/deps.py:132](../../src/api/deps.py:132), [src/schemas/permissoes.py:9](../../src/schemas/permissoes.py:9)
- Rotas: [src/api/v1/aplicacoes.py:20](../../src/api/v1/aplicacoes.py:20), [src/api/v1/auditoria.py:18](../../src/api/v1/auditoria.py:18), [src/api/v1/concilpro.py:45](../../src/api/v1/concilpro.py:45), [src/api/v1/permissoes.py:20](../../src/api/v1/permissoes.py:20)

São quatro prefixos inalcançáveis por concessão explícita: `aplicacoes_financeiras`, `auditoria`, `concilpro` e `permissoes`. Para `auditoria`/`permissoes`, o papel admin torna a falha pouco visível; para os outros, força `"*"`.

Correção: eliminar inferência por path e exigir permissão declarativa por rota; o plano específico está em `docs/planos/2026-08-26-plano-usuarios-e-permissoes.md`.

### 2.12 Alta — Dados financeiros/PII em logs: **persiste e ganhou novos pontos**

- [src/domain/concilpro/ai_classifier.py:279](../../src/domain/concilpro/ai_classifier.py:279), [src/api/v1/concilpro.py:202](../../src/api/v1/concilpro.py:202)
- [src/domain/neo/engine.py:750](../../src/domain/neo/engine.py:750), [src/domain/neo/engine.py:759](../../src/domain/neo/engine.py:759), [src/domain/neo/engine.py:1091](../../src/domain/neo/engine.py:1091)

ConcilPro continua imprimindo conteúdo bruto/valores. O NEO novo registra documentos completos, histórico bancário e valor de transação; CPF/CNPJ e narrativa financeira passam para o coletor de logs.

Correção: remover conteúdo bruto, mascarar documentos/valores e adotar uma allowlist de campos operacionais não sensíveis.

### 2.13 Alta — Segredo antigo no histórico Git: **estado externo não verificável**

- [prod_db.py:4](../../prod_db.py:4)

O código atual continua sem segredo hardcoded, mas o repositório ainda documenta que a senha chegou ao histórico. Revisão estática não confirma rotação nem purga.

Correção: registrar evidência de rotação/revogação e decidir formalmente sobre reescrita de histórico e invalidação de clones/artefatos.

### 2.14 Alta — ConcilPro monolítico: **persiste**

- [src/api/v1/concilpro.py:1](../../src/api/v1/concilpro.py:1) (977 linhas), [src/domain/concilpro/parser.py:1](../../src/domain/concilpro/parser.py:1) (1.387 linhas)

HTTP, background task, persistência e orquestração continuam no router; o parser mantém múltiplas estratégias, I/O e telemetria por `print` no mesmo arquivo.

Correção: separar controlador, serviço de ingestão, parser puro, job durável e repositório, com interfaces testáveis entre eles.

### 2.15 Alta — Fronteiras transacionais: **persiste**

- [src/db/session.py:88](../../src/db/session.py:88), [src/api/v1/usuarios.py:91](../../src/api/v1/usuarios.py:91), [src/api/v1/neo.py:90](../../src/api/v1/neo.py:90)

Há commit automático no fim da request, commits explícitos nos routers e sessões próprias nos jobs. O estado de erro do Open Banking e o lote de importação que some em rollback são sintomas da mesma ambiguidade.

Correção: definir a unidade transacional no serviço/use case; endpoints não devem commitar incidentalmente, e estados operacionais devem usar transações independentes quando precisam sobreviver à falha do trabalho.

### 2.16 Alta — Auditoria incompleta: **melhorou, mas persiste**

- Infra transacional: [src/domain/auditoria/service.py:51](../../src/domain/auditoria/service.py:51)
- Coberto agora: [src/domain/neo/cancelamento.py:199](../../src/domain/neo/cancelamento.py:199), [src/domain/extrato/importacoes.py:153](../../src/domain/extrato/importacoes.py:153), [src/domain/permissoes/service.py:100](../../src/domain/permissoes/service.py:100)
- Ainda sem evento: [src/domain/comprovantes/service.py:144](../../src/domain/comprovantes/service.py:144), [src/domain/notas/service.py:184](../../src/domain/notas/service.py:184), [src/api/v1/usuarios.py:62](../../src/api/v1/usuarios.py:62)

Cancelamento/reclassificação e permissões passaram a deixar trilha. Criação/desativação de usuário, associação manual de nota/comprovante e importação bem-sucedida ainda não têm evento completo.

Correção: manter matriz de mutações × evento obrigatório e testá-la; usuário/permissão deve incluir ator, alvo, escopo e antes/depois.

### 2.17 Crítica — Testes “de integração” em SQLite: **mudou para ausência efetiva de CI**

- [tests/conftest.py:38](../../tests/conftest.py:38), [pipelines/ci.yml:90](../../pipelines/ci.yml:90)

O fallback e o nome divergente (`TEST_DATABASE_URL_REAL` versus `TEST_DATABASE_URL`) persistem. Além disso, não existe `.github/workflows`: `pipelines/ci.yml` não é descoberto pelo GitHub Actions, portanto nenhuma suíte roda automaticamente.

Correção: instalar o workflow em `.github/workflows`, unificar a variável e falhar o job de integração se `engine.dialect.name != "postgresql"`.

### 2.18 Crítica — Migrations não são testadas: **persiste**

- [tests/conftest.py:42](../../tests/conftest.py:42), [src/db/migrations/versions/0031_dc_enum_unico.py:52](../../src/db/migrations/versions/0031_dc_enum_unico.py:52)

A suíte continua criando o schema por `Base.metadata.create_all()`. Nem a sequência 0001→0031, nem upgrade/downgrade, locks ou casts reais são executados.

Correção: criar job PostgreSQL vazio que rode `alembic upgrade head`, valide `alembic check` e faça smoke queries sobre o schema migrado.

### 2.19 Alta/Média — Lacunas de teste antigas: **persistem parcialmente**

- Monetário: [tests/unit/test_decimal_monetario.py:26](../../tests/unit/test_decimal_monetario.py:26)
- Último dia mascarado: [tests/integration/test_relatorios.py:166](../../tests/integration/test_relatorios.py:166)
- Cobertura sem gate: [pipelines/ci.yml:54](../../pipelines/ci.yml:54), [pyproject.toml:67](../../pyproject.toml:67)

Ainda se testa tipo `Decimal`, não arredondamento Postgres; o relatório passa `23:59:59` manualmente; não há gate de cobertura. Entraram testes bons para quarentena, ordem, fila e jobs, mas continuam em SQLite e não há concorrência real de conexões independentes.

Correção: adicionar matriz PostgreSQL/concorrência/negação e `fail_under`, depois tornar o workflow obrigatório para merge.

### 2.20 Média — Open Banking N+1: **persiste**

- [src/domain/openbanking/service.py:258](../../src/domain/openbanking/service.py:258)

Cada transação externa ainda dispara uma consulta de duplicidade. A recriação do serviço/provedor por request também permanece, mas o custo dominante é o round trip por linha.

Correção: pré-carregar hashes do período ou usar insert em lote com conflito ignorado.

### 2.21 Média — Conciliação FIFO O(n²): **persiste**

- [src/api/v1/concilpro.py:716](../../src/api/v1/concilpro.py:716)

O algoritmo detalhado continua fazendo buscas/remoções repetidas sobre coleções crescentes.

Correção: processar listas ordenadas com ponteiros/deques e medir em massa representativa antes do deploy.

### 2.22 Média — Relatórios/exports materializam tudo: **persiste**

- [src/domain/relatorios/service.py:301](../../src/domain/relatorios/service.py:301), [src/domain/exportacao/service.py:191](../../src/domain/exportacao/service.py:191), [src/domain/exportacao/service.py:331](../../src/domain/exportacao/service.py:331)

Livro-caixa e exports ainda carregam o período em memória; empresas grandes podem elevar latência e memória até OOM.

Correção: usar streaming/chunks, agregação SQL quando possível e limites operacionais explícitos.

### 2.23 Média — Background tasks locais: **melhorou para NEO/extrato; persiste no desenho**

- [src/domain/jobs.py:1](../../src/domain/jobs.py:1), [src/api/v1/concilpro.py:430](../../src/api/v1/concilpro.py:430)

NEO/extrato agora deixam job, heartbeat e falha recuperável, mas o trabalho ainda mora no processo web e não é retomado; ConcilPro nem entrou nesse mecanismo.

Correção: mover para worker/fila externa com entrega ao menos uma vez e handlers idempotentes; manter `jobs` como projeção de estado.

### 2.24 Média — Alembic concorrente no startup: **persiste**

- [entrypoint.sh:55](../../entrypoint.sh:55)

Cada réplica continua executando upgrade no próprio startup. A migration 0031 ainda acrescenta `ALTER TABLE ... TYPE` com lock exclusivo, ampliando o custo de uma corrida entre réplicas.

Correção: executar migration como etapa única de deploy com advisory lock e só liberar réplicas após sucesso.

### 2.25 Média — Isolamento só na aplicação: **persiste**

- [src/db/models.py:189](../../src/db/models.py:189), [src/db/models.py:522](../../src/db/models.py:522), [src/db/models.py:567](../../src/db/models.py:567)

FKs simples permitem, no banco, permissão usuário↔empresa de tenants distintos e filhos cujo `empresa_id` diverge do pai. Os serviços consultados filtram escopo, mas o schema não trava a invariante.

Correção: adicionar chaves únicas/FKs compostas por empresa/tenant nos vínculos críticos e considerar RLS como defesa adicional.

### 2.26 Média — Migrations legadas/backfills/downgrades: **persiste**

- [src/db/migrations/versions/0010_notas_identidade_empresa.py:24](../../src/db/migrations/versions/0010_notas_identidade_empresa.py:24), [src/db/migrations/versions/0020_export_formato_txt.py:34](../../src/db/migrations/versions/0020_export_formato_txt.py:34)

As migrations novas usam backfill SQL em conjunto e têm downgrade, mas o histórico antigo continua com processamento linha a linha e downgrade vazio.

Correção: documentar migrations irreversíveis e substituir backfills lentos antes de restaurar bases grandes ou fazer bootstrap novo.

### 2.27 Média — Build não reprodutível: **persiste**

- [Dockerfile:15](../../Dockerfile:15), [pyproject.toml:7](../../pyproject.toml:7)

A imagem continua instalando extras de desenvolvimento e dependências com limites inferiores abertos, sem lock.

Correção: gerar lock, instalar somente dependências de runtime e separar imagem/estágio de desenvolvimento.

## 3. Achados novos

### 3.1 Alta — Migration para `DATE` deixou binds incompatíveis com PostgreSQL

- Coluna `DATE`: [src/db/models.py:503](../../src/db/models.py:503), [src/db/migrations/versions/0026_transacao_data_como_date.py:39](../../src/db/migrations/versions/0026_transacao_data_como_date.py:39)
- Open Banking ainda envia `datetime`: [src/domain/openbanking/service.py:277](../../src/domain/openbanking/service.py:277)
- NEO envia `date` a `TIMESTAMPTZ`: [src/domain/neo/engine.py:917](../../src/domain/neo/engine.py:917), [src/db/models.py:582](../../src/db/models.py:582)

SQLite aceita a coerção nos dois sentidos. `asyncpg` faz bind por tipo e pode rejeitar `datetime` em `DATE` e `date` em `TIMESTAMPTZ`; assim, sincronização Open Banking ou criação das partidas pode falhar apenas em produção após a 0026.

Correção: gravar `t.data` diretamente no Open Banking e converter `Transacao.data` explicitamente para meia-noite UTC ao criar `RegistroContabil`; cobrir ambos no PostgreSQL real.

### 3.2 Alta — Lote de importação que deveria registrar falha é revertido junto com o parse

- [src/domain/extrato/importacoes.py:50](../../src/domain/extrato/importacoes.py:50), [src/domain/jobs.py:166](../../src/domain/jobs.py:166), [src/domain/jobs.py:184](../../src/domain/jobs.py:184)

`abrir_importacao()` declara que o lote nasce antes do parse para sobreviver a uma leitura recusada, mas criação, parse e importação compartilham a mesma transação. Se o parser lança, a sessão fecha sem commit e o lote é revertido; só o `Job` registra a falha.

Correção: persistir lote/estado inicial em transação própria e depois atualizar sucesso/erro, ou corrigir a promessa e manter toda a tentativa apenas em `jobs`.

### 3.3 Alta — Cancelamento concorrente lê partidas antes do lock

- [src/domain/neo/cancelamento.py:76](../../src/domain/neo/cancelamento.py:76), [src/domain/neo/cancelamento.py:106](../../src/domain/neo/cancelamento.py:106), [src/db/models.py:888](../../src/db/models.py:888)

Dois cancelamentos diretos podem ler as mesmas partidas ativas antes de um deles travar `Transacao`. O segundo espera, prossegue com a fotografia velha e tenta inserir outra decisão `sem_regra`; a unicidade parcial tende a transformar a repetição em `IntegrityError`/500, não em resultado idempotente.

Correção: travar a transação primeiro e só então reler/travar partidas ativas; após adquirir o lock, revalidar que ainda existe lançamento vigente e devolver 404/409 estável.

### 3.4 Alta — Quarentena não usa `saldo_apos` e depende da posição dos números no texto

- [src/domain/extrato/validacao.py:55](../../src/domain/extrato/validacao.py:55), [src/domain/neo/engine.py:210](../../src/domain/neo/engine.py:210), [src/db/models.py:509](../../src/db/models.py:509)

A barreira cobre os três caminhos do NEO, mas só detecta histórico com dois valores em formato brasileiro e presume “primeiro = transação, último = saldo”. Não compara `Transacao.valor` com `saldo_apos`; histórico limpo, layout com colunas em outra ordem ou valor legítimo no meio pode escapar ou ser bloqueado indevidamente.

Correção: quando `saldo_apos` existir, tratá-lo como evidência estruturada; para linha crua, persistir a confiança/origem do parser e colocar layouts desconhecidos em revisão, em vez de inferir somente pela posição textual.

### 3.5 Média — Busca de contraparte faz N+1 e varredura completa por transação pendente

- [src/domain/neo/engine.py:708](../../src/domain/neo/engine.py:708), [src/domain/neo/engine.py:767](../../src/domain/neo/engine.py:767), [src/domain/neo/engine.py:821](../../src/domain/neo/engine.py:821)

Para cada transação sem regra, cada documento encontrado dispara query; se não resolver, todas as contrapartes ativas da empresa são carregadas e normalizadas novamente. Com milhares de pendências e contrapartes, o custo cresce como pendências × cadastro, apesar dos índices de documento.

Correção: carregar uma vez mapas por documento e candidatos de nome por execução/empresa, ou resolver documentos em lote antes do loop.

### 3.6 Média — Ordem única exige sort que o índice atual não cobre

- [src/domain/extrato/ordenacao.py:34](../../src/domain/extrato/ordenacao.py:34), [src/db/models.py:543](../../src/db/models.py:543), [src/domain/neo/consultas.py:235](../../src/domain/neo/consultas.py:235)

A ordem correta intercala `ExtratoImportacao.created_at` entre `Transacao.data` e `Transacao.ordem`. O índice `(empresa_id, data, ordem)` não pode satisfazer essa ordenação entre tabelas; páginas de extrato/fila/log precisam ordenar todo o conjunto filtrado antes de `LIMIT`.

Correção: medir com `EXPLAIN (ANALYZE, BUFFERS)` em volume real; se relevante, denormalizar uma chave de ordem do lote em `transacoes` e indexar a sequência completa.

### 3.7 Média — Reaper executado só no startup pode deixar job órfão para sempre

- [src/api/app.py:89](../../src/api/app.py:89), [src/domain/jobs.py:230](../../src/domain/jobs.py:230)

O startup só marca jobs cujo heartbeat já tem mais de 120 segundos. Se o processo morrer e reiniciar antes disso, a tarefa local foi perdida, o job ainda parece recente e não é marcado; como não há varredura periódica, ele fica parado até outro restart.

Correção: executar reaper periódico em processo coordenado ou, preferencialmente, usar uma fila que retome a entrega; no mínimo, guardar identidade/lease do worker e expirar o lease continuamente.

## 4. O que a suíte não cobre e deveria

Não existe `.github/workflows`; portanto, qualquer item abaixo depende hoje de execução local voluntária. `pipelines/ci.yml` é apenas um arquivo inerte no layout atual e, mesmo se fosse movido, usaria a variável de banco errada.

### 4.1 Crítica — PostgreSQL e sequência real de migrations

- [tests/conftest.py:38](../../tests/conftest.py:38), [tests/conftest.py:42](../../tests/conftest.py:42), [src/db/migrations/versions/0031_dc_enum_unico.py:52](../../src/db/migrations/versions/0031_dc_enum_unico.py:52)

Faltam: banco vazio em Postgres 16; `alembic upgrade head`; upgrade a partir de snapshot anterior à 0026/0031; consulta que compara D/C sem cast; validação de tipos, índices e constraints; `alembic check`; e smoke de downgrade quando suportado. Esse job deve falhar se o dialeto não for PostgreSQL.

### 4.2 Alta — Binds de `DATE`/`TIMESTAMPTZ`

- [src/domain/openbanking/service.py:282](../../src/domain/openbanking/service.py:282), [src/domain/neo/engine.py:926](../../src/domain/neo/engine.py:926)

Faltam sincronização Open Banking e classificação NEO completas contra `asyncpg`, verificando que `Transacao.data` volta como `date` e `RegistroContabil.data_lancamento` como `datetime` UTC.

### 4.3 Alta — Concorrência e idempotência com conexões independentes

- [src/domain/extrato/service.py:96](../../src/domain/extrato/service.py:96), [src/domain/openbanking/service.py:263](../../src/domain/openbanking/service.py:263), [src/domain/neo/cancelamento.py:76](../../src/domain/neo/cancelamento.py:76)

Faltam barreiras reais para: duas importações iguais; dois syncs Open Banking; dois cancelamentos do mesmo lançamento/lote; associação simultânea da mesma nota/comprovante; dois jobs NEO; e cancelamento concorrente com classificação. As asserções devem cobrir resposta HTTP, número final de linhas, balanço D/C e ausência de 500.

### 4.4 Alta — Falhas e retomada de jobs

- [tests/integration/test_jobs.py:178](../../tests/integration/test_jobs.py:178), [src/domain/jobs.py:230](../../src/domain/jobs.py:230)

Há teste unitário do predicado de heartbeat, mas não de morte real entre commit e `BackgroundTasks`, restart antes/depois dos 120 segundos, falha ao atualizar o próprio status e lote de importação após erro de parse. Também falta provar que job de outra empresa/tenant nunca aparece por lista, detalhe ou filtro de módulo.

### 4.5 Alta — Matriz de negação e isolamento multi-tenant

- [tests/integration/test_permissoes.py:327](../../tests/integration/test_permissoes.py:327), [src/api/deps.py:106](../../src/api/deps.py:106)

Existem casos pontuais, mas não uma matriz automática sobre todas as rotas de empresa. Para cada endpoint de leitura e mutação, faltam: sem autenticação; contador sem empresa; empresa autorizada/módulo negado; UUID de recurso de outra empresa do mesmo tenant; empresa de outro tenant; usuário inativo; tenant inativo; e admin tentando cruzar tenant. A resposta não deve revelar se o recurso externo existe.

### 4.6 Alta — Completude declarativa da autorização

- [src/schemas/permissoes.py:9](../../src/schemas/permissoes.py:9), [src/api/v1/__init__.py:31](../../src/api/v1/__init__.py:31)

Falta um teste de introspecção que percorra `app.routes` e falhe se uma rota protegida por empresa não declarar recurso e ação válidos. Esse teste teria detectado os quatro prefixos fora da whitelist no mesmo commit em que nasceram.

### 4.7 Alta — Quarentena de valor suspeito fora do caso conhecido

- [tests/unit/test_extrato_validacao.py:30](../../tests/unit/test_extrato_validacao.py:30), [src/domain/extrato/validacao.py:61](../../src/domain/extrato/validacao.py:61)

Os testes cobrem bem a linha real que motivou a correção e os três caminhos do NEO. Faltam `saldo_apos == valor` com histórico limpo, três ou mais colunas monetárias, ordem saldo→valor, formatos sem separador brasileiro, valores negativos/zero, duas quantias legítimas na descrição e layouts de outros bancos; devem travar falsos positivos e falsos negativos.

### 4.8 Alta — Política monetária no banco real

- [tests/unit/test_decimal_monetario.py:26](../../tests/unit/test_decimal_monetario.py:26)

Faltam `1.005`, `-1.005`, excesso de escala, máximo de `NUMERIC(15,2)`, estouro e igualdade entre valor usado na dedupe e valor persistido. Os casos precisam comparar API, Python e PostgreSQL.

### 4.9 Alta — Consentimento e dados sensíveis

- [src/domain/concilpro/ai_classifier.py:222](../../src/domain/concilpro/ai_classifier.py:222), [src/domain/neo/engine.py:750](../../src/domain/neo/engine.py:750)

Faltam testes negativos que garantam zero chamadas OpenAI para texto, Vision e classificação com consentimento desligado, mesmo com chave configurada. Falta captura de logs que rejeite CPF/CNPJ completo, histórico bancário, conteúdo bruto, senha/token e valores financeiros.

### 4.10 Média — Filtros de data e ordem em Postgres

- [tests/integration/test_relatorios.py:166](../../tests/integration/test_relatorios.py:166), [tests/integration/test_neo.py:3616](../../tests/integration/test_neo.py:3616)

Os testes novos de ordem são bons, mas rodam em SQLite. Faltam DRE/balancete com `data_ate` enviado como dia puro, empate completo de data/lote/ordem, linhas legadas sem lote/ordem, paginação sem repetição e plano de execução com volume representativo.

### 4.11 Média — Auditoria das mutações e gestão de usuário

- [tests/integration/test_auditoria.py:53](../../tests/integration/test_auditoria.py:53), [src/api/v1/usuarios.py:62](../../src/api/v1/usuarios.py:62)

Há cobertura de concessão/alteração/revogação de permissão. Faltam criação/desativação/reativação de usuário, alteração de papel, revogação de sessão, associação de documento, importação e verificação de que rollback da mutação também reverte o audit log.

## 5. Pontos positivos verificados

- A migration 0031 resolve a causa nominal dos três enums e os models reutilizam um único `DC_ENUM`; o cast em consulta deixou de ser necessário.
- A classificação manual trava transações e usa o texto digitado como `RegistroContabil.historico`, preservando a linha original em `historico_extrato`.
- A fila de pendências parte de `Transacao`, portanto inclui importação recém-chegada sem `NeoDecisao`; a unicidade parcial limita a uma decisão aberta por transação.
- Cancelamento de lote e de lançamento mantém partidas, status, documentos e auditoria na mesma transação; a ordem conceitual de desfazer está correta.
- `FOR UPDATE SKIP LOCKED` no motor reduz dupla classificação entre jobs NEO e protege candidatos automáticos de nota/comprovante.
- Os models passaram a declarar os índices/colunas já existentes nas migrations, reduzindo drift detectável por `alembic check`.

## 6. Resumo executivo — prioridades

1. **Tratar o dump de produção como incidente** e impedir que continue em repositório/imagens.
2. **Bloquear ConcilPro quando o consentimento OpenAI estiver desligado** e remover PII/dados financeiros dos logs.
3. **Instalar CI real em `.github/workflows` com PostgreSQL e migrations**; hoje nenhuma suíte roda automaticamente.
4. **Corrigir os binds `DATE`/`TIMESTAMPTZ` introduzidos pela migration 0026** antes de confiar em Open Banking/NEO em Postgres.
5. **Tornar extrato/Open Banking idempotentes sob concorrência** e corrigir a ordem de lock do cancelamento.
6. **Levar ConcilPro e jobs locais para execução durável**, com reaper periódico/lease enquanto a fila externa não chega.
7. **Unificar a política monetária e corrigir último dia de DRE/balancete**.
8. **Substituir autorização por path por declaração de recurso × ação em cada rota**, seguindo o plano de usuários e permissões.
