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
