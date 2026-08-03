# DECISIONS.md — Registro de Decisões Arquiteturais

> Cada ADR responde: O quê? Por quê? Trade-offs? Quando revisar?

---

## ADR-001: JWT em HttpOnly Cookie, não sessionStorage

**Status:** Ativo
**Decisão:** Autenticação via HttpOnly + Secure + SameSite=Strict cookies, com CSRF token separado (não HttpOnly) para mutações.

**Contexto:** O sistema legado (MrContador JHipster) armazenava o JWT em `sessionStorage`, expondo o token a qualquer XSS da página. Qualquer script injetado conseguia roubar a sessão de todos os escritórios contábeis gerenciados.

**Por quê esta decisão:**
- Cookie HttpOnly é inacessível ao JavaScript — XSS não consegue ler o token
- SameSite=Strict previne CSRF via requests cross-site
- CSRF token extra em cookie não-HttpOnly + header X-CSRF-Token protege mutações
- Padrão atual recomendado por OWASP para SPAs

**Trade-offs:**
- Requer CSRF token em toda requisição de mutação (POST/PUT/PATCH/DELETE)
- Frontend precisa ler o cookie `csrf_token` (não-HttpOnly) e enviar como header
- Refresh token limitado ao path `/api/v1/auth/refresh` para minimizar exposição

**Quando revisar:** Se migrar para autenticação via passkey/WebAuthn.

---

## ADR-002: API Versionada — `/api/v1/`

**Status:** Ativo
**Decisão:** Todo endpoint no formato `/api/v{N}/{recurso}`. Minor versions são compatíveis (só adicionam campos). Major versions coexistem por 90 dias antes de deprecação.

**Contexto:** O sistema legado tinha todos os endpoints em `/api/{recurso}` sem versão. Toda mudança era um breaking change silencioso para o scraper e outras integrações.

**Por quê esta decisão:**
- Permite evolução do contrato sem quebrar clientes existentes
- FastAPI gera OpenAPI automaticamente por versão
- Padrão que os consumidores da API (scraper, ERPs) podem depender

**Trade-offs:**
- URLs mais longas
- Requer disciplina: não criar `/api/v2/` sem deprecar v1 primeiro
- Overhead de manter múltiplas versões simultaneamente

**Quando revisar:** Na primeira major version (v2).

---

## ADR-003: Backend em Python/FastAPI

**Status:** Ativo
**Decisão:** Novo backend em Python 3.12 com FastAPI, substituindo o JHipster (Java/Spring Boot) gradualmente.

**Contexto:** O sistema legado usava JHipster, que gera um monolito Java com muitas convenções implícitas. A equipe já usa Python para o scraper e tem maior fluência no stack.

**Por quê esta decisão:**
- Stack já dominado pela equipe
- FastAPI gera OpenAPI automaticamente (resolve ADR-002 sem custo extra)
- `asyncpg` + `SQLAlchemy async` — performance superior para I/O bound
- `pydantic` — validação de entrada tipada e automática
- Menor curva de aprendizado para novos devs

**Trade-offs:**
- Dois backends em coexistência durante migração (JHipster + FastAPI)
- Python tem performance inferior ao Java em CPU-bound (irrelevante aqui)
- Ecossistema menor que Spring para funcionalidades enterprise

**Quando revisar:** Se necessário integrar com sistemas que só falam JVM.

---

## ADR-004: Banco Compartilhado Durante Migração

**Status:** Temporário (até migração completa)
**Decisão:** Novo backend e JHipster acessam o mesmo PostgreSQL durante a transição. Cada módulo migrado "toma posse" das tabelas que gerencia.

**Contexto:** Migrar dados ao mesmo tempo em que migra o código dobra o risco.

**Por quê esta decisão:**
- Rollback imediato: desabilitar feature flag → JHipster volta a servir o módulo
- Sem period de inconsistência de dados entre sistemas
- Zero downtime durante migração

**Trade-offs:**
- Dois sistemas escrevendo no mesmo banco — requer disciplina de ownership por tabela
- Schema evolui por Alembic (novo) mas pode conflitar com Flyway/Liquibase (JHipster)
- Dependência de coordenação entre os dois sistemas

**Quando revisar:** Ao desligar o JHipster — remover este ADR e documentar que a migração foi concluída.

---

## ADR-005: Processamento Assíncrono para NEO e Importação

**Status:** Planejado (implementação futura)
**Decisão:** Importação de extrato OFX e processamento NEO são jobs assíncronos que retornam `job_id`. O cliente faz polling ou recebe webhook com o resultado.

**Contexto:** Importar um extrato OFX de 12 meses com 500+ transações e processar o NEO pode levar 10–30 segundos. HTTP síncrono com timeout longo é frágil e bloqueia conexões.

**Por quê esta decisão:**
- Libera a conexão HTTP imediatamente
- Permite retry automático em caso de falha
- Visibilidade: cada job tem status (pendente/processando/concluído/erro) rastreável
- Escalável: workers podem ser aumentados independentemente da API

**Trade-offs:**
- Aumenta complexidade: requer job queue (Redis/RQ ou Celery)
- Frontend precisa de polling ou WebSocket para mostrar progresso
- Debugging de jobs assíncronos é mais difícil

**Quando revisar:** Ao implementar o módulo de extrato.

---

## ADR-006: Deduplicação por Hash SHA-256

**Status:** Ativo
**Decisão:** Cada transação importada tem um `hash_dedup` calculado como `SHA-256(empresa_id + data + valor + historico + dc)`. UniqueConstraint no banco garante idempotência.

**Contexto:** O sistema legado não tinha deduplicação documentada. Reimportar o mesmo OFX poderia gerar lançamentos duplicados silenciosamente.

**Por quê esta decisão:**
- Banco garante unicidade — sem lógica de dedup no código
- Idempotente: reimportar o mesmo arquivo n vezes = resultado idêntico
- Hash determinístico: mesmo resultado em qualquer instância

**Trade-offs:**
- Hash precisa ser definido cuidadosamente (campos) — histórico do banco pode ter variações de whitespace
- Falsos positivos impossíveis, falsos negativos possíveis se banco variar o texto do histórico

**Quando revisar:** Ao implementar o módulo de extrato.

---

## ADR-007: Multi-tenancy por `tenant_id` em toda tabela de domínio

**Status:** Ativo
**Decisão:** Toda tabela de domínio tem coluna `tenant_id` com FK para `tenants`. Middleware valida que o usuário pertence ao tenant antes de qualquer operação. Queries sempre filtram por `tenant_id`.

**Contexto:** O sistema legado usava `parceiroId` como parâmetro de query HTTP — implícito e sem garantia de isolamento no banco.

**Por quê esta decisão:**
- Isolamento garantido no banco — impossível vazar dados entre escritórios
- Queries diretas ao banco sempre retornam dados do tenant correto
- Auditoria clara: todo registro sabe a qual escritório pertence

**Trade-offs:**
- Toda query precisa incluir `tenant_id` no WHERE — risco de esquecer
- Índices compostos `(tenant_id, outro_campo)` em toda tabela

**Quando revisar:** Se migrar para schema-per-tenant no PostgreSQL.

---

## ADR-008: Auditoria Imutável de Toda Mutação

**Status:** Ativo
**Decisão:** Toda operação de escrita (criar/atualizar/desativar) gera um registro na tabela `audit_logs`. A tabela é append-only — nenhum registro é atualizado ou deletado.

**Contexto:** Sistema contábil — rastreabilidade é requisito implícito de conformidade. O sistema legado não tinha auditoria.

**Por quê esta decisão:**
- Permite responder "quem mudou isso, quando e para quê"
- Suporte a incidentes: reproduzir estado anterior
- Requisito implícito de conformidade fiscal

**Trade-offs:**
- Volume crescente de dados (requer política de retenção)
- Overhead por operação de escrita
- Não implementa event sourcing completo — apenas log de mutações

**Quando revisar:** Se volume de audit_logs impactar performance de escrita.

---

## ADR-009: `parceiroId` Estável na URL

**Status:** Planejado (implementação no frontend)
**Decisão:** URL no formato `/empresa/:empresaId/modulo` em vez de estado implícito do Angular Router.

**Contexto:** O sistema legado perdia o contexto de empresa ao recarregar a página, pois o `parceiroId` era estado interno do Angular sem URL correspondente.

**Por quê esta decisão:**
- Deep link e bookmark funcionam
- Recarregar a página não perde contexto
- URL é o contrato com o usuário — deve ser estável

**Trade-offs:**
- Requer migração do roteamento Angular
- URLs mais longas

**Quando revisar:** Ao iniciar o desenvolvimento do frontend.

---

## ADR-010: Feature Flags por Tenant para Migração Gradual

**Status:** Planejado
**Decisão:** Cada módulo migrado do JHipster para FastAPI é habilitado por feature flag configurável por tenant. Rollout: 5% → 25% → 100%.

**Contexto:** Migrar todos os tenants ao mesmo tempo multiplica o risco. Um bug em um módulo migrado afetaria todos os escritórios.

**Por quê esta decisão:**
- Rollback cirúrgico: desabilitar flag do tenant problemático sem afetar outros
- Validação gradual com tenants reais antes do rollout total
- Observabilidade: comparar comportamento do módulo antigo vs. novo com tráfego real

**Trade-offs:**
- Requer mecanismo de feature flags (tabela no banco ou Redis)
- Aumenta casos de teste (comportamento com flag on/off)
- Período de manutenção dupla enquanto flags coexistem

**Quando revisar:** Ao desligar o JHipster — remover toda lógica de feature flag.

---

## ADR-011: Limite de Upload em Duas Barreiras

**Status:** Ativo
**Decisão:** `LimiteUploadMiddleware` recusa requests cujo `Content-Length` passa de `MAX_UPLOAD_MB` (padrão 25 MB), e `ler_upload_limitado()` conta os bytes efetivamente lidos. Todo endpoint de upload usa o helper em vez de `await arquivo.read()`.

**Contexto:** Nenhum dos 5 endpoints de upload (ConcilPro, extrato, notas, plano de contas, cartões) tinha limite, e todos os parsers carregam o arquivo inteiro em memória. Um único PDF grande derruba o worker — são 2 workers no uvicorn.

**Por quê esta decisão:**
- O middleware corta antes de o FastAPI montar o `UploadFile` — nenhum byte do corpo entra em memória
- O `Content-Length` é opcional: `Transfer-Encoding: chunked` não declara tamanho e passaria direto pelo middleware, daí a segunda contagem
- Um único ponto de configuração (`MAX_UPLOAD_MB`) em vez de limite por endpoint

**Trade-offs:**
- O mesmo limite serve o Razão anual em PDF e um CSV de fatura — dimensionado pelo maior
- O nginx do `contabil-front` já tem `client_max_body_size 50M` — deliberadamente mais frouxo, para que quem recuse seja a aplicação e o cliente receba o JSON tipado em vez do 413 em HTML do nginx. Subir `MAX_UPLOAD_MB` acima de 50 sem mexer no nginx inverte isso silenciosamente
- `ler_upload_limitado` levanta `AppError`, então handler que faça `except Exception` mascara o 413 como 500 (foi o caso do ConcilPro)

**Quando revisar:** Se os parsers passarem a processar em streaming — aí o limite pode subir bastante.

---

## ADR-012: Liveness e Readiness Separados

**Status:** Ativo
**Decisão:** `GET /api/health` executa `SELECT 1` com timeout de 2s e devolve 503 se o banco não responder. `GET /api/health/live` responde 200 sem tocar no banco e é o alvo do `HEALTHCHECK` do Docker. Ambos expõem `commit` (SHA injetado como build arg).

**Contexto:** O health check devolvia `{"status":"ok"}` estático. Não distinguia "container de pé" de "container sem banco" — exatamente a dúvida que apareceu ao validar o deploy de 31/07. E `version` vem do `APP_VERSION`, que não muda entre deploys, então não respondia "meu deploy subiu?".

**Por quê esta decisão:**
- Readiness com banco é o que o painel precisa para saber se a instância atende request
- Apontar o `HEALTHCHECK` do Docker para o readiness faria uma indisponibilidade momentânea do Postgres matar um container saudável — o problema estaria no banco, e reiniciar a aplicação não resolve
- O SHA do commit transforma "qual versão está no ar?" num `curl`

**Trade-offs:**
- Dois endpoints para manter
- Quem monitorar só o `/live` não percebe perda de banco — o alerta tem que apontar para `/api/health`
- O build precisa passar `--build-arg GIT_COMMIT=$(git rev-parse --short HEAD)`; sem isso o campo fica `unknown` (degrada, não quebra)

**Quando revisar:** Se entrarem outras dependências críticas (Redis, OpenAI) que mereçam entrar no readiness.

---

## ADR-013: O ConcilPro Não Fabrica Lançamento para Fechar Total

**Status:** Ativo
**Decisão:** Removida a função `_recuperar_lancamentos_ocultos` do parser. Quando os totais declarados no Razão não fecham com a soma dos lançamentos extraídos, o resultado fica incompleto e a divergência é sinalizada — nenhum lançamento é sintetizado para tapar o buraco.

**Contexto:** A função analisava saltos de saldo entre lançamentos consecutivos e criava entradas com histórico `"(RECUPERADO)"` para o valor faltante. Foi a origem do falso positivo de R$ 24.029,28 a pagar numa conta zerada (arquivo `id=5`).

**Por quê esta decisão:**
- O lançamento fabricado **não era distinguível no banco**. Não existe coluna `sintetico` em `cp_lancamento` — o único traço era o sufixo no texto livre do histórico, e `classificado_por_ia` ficava `True`, o que é falso: a entrada vinha de aritmética, não do modelo
- Uma vez persistido, ele entrava na conciliação FIFO como se fosse nota real (`valor_saldo = valor_credito` para COMPRA) e saía na exportação em Excel
- A sinalização de divergência **não dependia dela**: a comparação entre total declarado e soma dos lançamentos é independente, e a mensagem já informa exatamente quanto falta
- Só era alcançada pelo caminho da IA. O parser determinístico cobre 96% dos blocos do `Razão 2025.pdf` e a planilha XLSX fecha 208/208 sem IA — a fatia onde ela agia já era estreita e continua encolhendo
- O determinístico, quando os totais não fecham, devolve `None` e defere para a IA. Esse "não sei" explícito é o comportamento certo, e era o que a recuperação atropelava

**Trade-offs:**
- Fornecedor cujo bloco caiu na IA e cujos totais não fecham passa a ter `total_debito`/`total_credito` incompletos, em vez de completos-porém-inventados. Ambos estão errados; o incompleto é honestamente errado e vem marcado com `divergencia_calculo`
- Perde-se a tentativa de reconstruir número de NF a partir do texto do bloco. Na prática ela chutava: pegava NFs não atribuídas em ordem de aparição e casava com o gap mais próximo

**Quando revisar:** Se aparecer um formato de Razão em que a extração perca lançamentos de forma sistemática e a planilha XLSX não seja alternativa. Mesmo aí, o caminho é corrigir a extração — como foi feito no Formato 6 — não sintetizar dado contábil.
