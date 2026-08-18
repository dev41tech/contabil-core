# Plano de reformulação estrutural progressiva do `contabil-core`

**Status:** Proposto
**Escopo:** Backend/API
**Stack:** FastAPI, Python 3.12, PostgreSQL, asyncpg, SQLAlchemy async e Alembic
**Objetivo:** Introduzir uma superfície de API orientada ao fechamento contábil sem reescrever os módulos existentes nem quebrar integrações em produção.

## 1. Contexto

O `contabil-core` é a API de um sistema contábil multi-tenant em que escritórios administram várias empresas-clientes. O backend FastAPI está substituindo progressivamente um legado JHipster, compartilhando parcialmente o mesmo banco PostgreSQL durante a transição.

As decisões arquiteturais vigentes exigem:

- autenticação por JWT em cookie HttpOnly e proteção CSRF;
- API versionada em `/api/v1`;
- compatibilidade aditiva dentro da versão;
- coexistência temporária com o JHipster;
- isolamento por tenant e empresa;
- auditoria de mutações;
- processamento assíncrono futuro para importações e NEO;
- feature flags por tenant;
- limites de upload;
- preservação das integrações atuais.

Este repositório contém somente backend. "Reformulação visual" significa fornecer contratos de API que permitam ao `contabil-front` construir uma experiência integrada: visão de carteira, fechamento por competência, etapas, pendências, progresso, bloqueios e ações orientadas a fluxo.

Não faz parte deste plano implementar telas.

## 2. Diagnóstico do código atual

### 2.1 Organização predominante por módulo

`src/api/v1/__init__.py` registra 19 routers. A organização principal é:

- cadastros: empresas, agências, aplicações financeiras, plano de contas e regras;
- entradas: extrato, notas, comprovantes, cartões e Open Banking;
- processamento: NEO e ConcilPro;
- saídas: registros contábeis, relatórios, stats e exportação;
- administração: autenticação, usuários e permissões.

Em grande parte, cada router corresponde a um service e a um schema. Essa estrutura é adequada para invariantes locais e operações CRUD, mas não oferece uma representação explícita do processo contábil de uma competência.

### 2.2 Exceções relevantes à separação de camadas

A separação não é uniforme:

- `src/api/v1/neo.py` contém SQL e regras da associação manual;
- `src/api/v1/concilpro.py` concentra upload, background processing, persistência, queries e exportação;
- o ConcilPro não possui `schemas/concilpro.py` nem um service equivalente;
- `usuarios.py` acessa o banco diretamente;
- schemas de exportação estão em `schemas/contabil.py`;
- services de domínio recebem `AsyncSession` e fazem SQLAlchemy, apesar de o onboarding descrever domínio sem banco direto.

O plano não tentará normalizar toda essa arquitetura de uma vez. A lógica será extraída apenas quando necessário para ser reutilizada pela nova fachada.

### 2.3 Fluxos reais

Não existe hoje uma única cadeia linear "extrato → ConcilPro → NEO". Existem trilhas relacionadas.

#### Fluxo bancário-contábil

```text
OFX/PDF ou Pluggy
→ Transacao pendente
→ NEO por regra ou classificação manual
→ duas partidas em RegistroContabil
→ relatórios e exportação
```

O NEO já constitui um pequeno orquestrador local: cria partidas, atualiza a transação, registra a decisão e tenta associar notas e comprovantes.

#### Fluxo documental

```text
XML/OCR de nota ou PDF de comprovante
→ documento pendente
→ associação automática pelo NEO ou manual
→ conferência documental
```

#### Fluxo de cartões

```text
cadastro de cartão
→ fatura da competência
→ importação de lançamentos
→ classificação
→ fechamento da fatura
→ associação com transação bancária
→ fatura paga
```

#### Fluxo ConcilPro

```text
Razão de fornecedores em PDF/XLS
→ parser determinístico
→ classificação por IA dos casos incertos
→ fornecedores e lançamentos
→ conciliação FIFO
→ divergências e exportação
```

O ConcilPro concilia razão de fornecedores. Ele não consome as transações bancárias nem alimenta diretamente o NEO. No fechamento será uma etapa configurável e paralela.

### 2.4 Agregação existente

`GET /api/v1/empresas/{empresa_id}/stats` já agrega dados de transações, registros, notas, comprovantes e agências.

Ele deve ser preservado, mas não representa fechamento porque:

- os totais são principalmente históricos;
- não existe uma instância por competência;
- não existem fontes esperadas;
- ausência de movimento é indistinguível de ausência de dados;
- não existem bloqueios, responsabilidade, aprovação, fechamento ou reabertura;
- "conciliada" é inferida pela existência de `RegistroContabil`;
- consultas por agência são feitas em loop.

### 2.5 Fragmentação client-side atual

Para acompanhar uma competência, o frontend precisa combinar chamadas a: extrato, Open Banking, NEO, notas, comprovantes, cartões, ConcilPro, contábil, relatórios e exportação.

Isso transfere ao frontend regras como: o que significa "pronto", qual pendência bloqueia, qual etapa é aplicável, como calcular progresso, como correlacionar estados heterogêneos e como identificar o próximo passo. Essas regras pertencem ao backend e precisam de uma política versionada.

### 2.6 Estados heterogêneos

Os módulos possuem máquinas de estado independentes:

- transação: `pendente`, `processada`, `erro`;
- nota: `pendente`, `associada`, `cancelada`;
- fatura: `aberta`, `fechada`, `paga`;
- conexão bancária: `pendente`, `ativa`, `expirada`, `erro`;
- NEO: `associada`, `sem_regra`, `erro`;
- ConcilPro: estados em maiúsculas;
- export job: `pendente`, `processando`, `concluido`, `erro`.

O fechamento não reutilizará diretamente nenhum desses enums. Eles serão fatos de entrada para uma política canônica.

### 2.7 Processamento assíncrono incompleto

O ConcilPro retorna antes de processar, mas usa `FastAPI BackgroundTasks` e sessão síncrona: não há fila durável, restart pode abandonar processamento, há commits explícitos, o arquivo permanece em memória, retries não são centralizados, e o estado `PROCESSANDO` preso é detectado por heurística.

O NEO ainda processa no request. A exportação possui uma entidade chamada `ExportJob`, mas gera o arquivo sincronamente e já grava o job como concluído. `src/jobs` está vazio — ainda não existe uma infraestrutura genérica de jobs.

## 3. Decisões de produto e arquitetura

As seguintes decisões estão aprovadas.

### 3.1 Fechamento bloqueia mutações

Uma competência fechada bloqueia importações, classificações e outras mutações que alterem seus dados. Uma alteração exige reabertura explícita por usuário autorizado.

A reabertura deve registrar: motivo obrigatório, usuário solicitante, responsável, instante, status anterior e posterior, trace/request e metadados relevantes.

Uma competência pode ser fechada e reaberta várias vezes. O histórico deve ser append-only.

### 3.2 ConcilPro configurável por empresa

A configuração é por empresa, não global.

Quando desabilitado: etapa `concilpro` fica `nao_aplicavel`, não entra no denominador de etapas aplicáveis, não bloqueia fechamento.

Quando habilitado: ausência de evidência da competência bloqueia, processamento em andamento bloqueia, erro ou divergência bloqueante impede fechamento, divergências informativas podem apenas gerar atenção, segundo a política.

A configuração ficará em tabela nova sob ownership do FastAPI, evitando alterar prematuramente tabelas compartilhadas.

### 3.3 Fontes esperadas e "sem movimento"

Cada empresa terá fontes esperadas com vigência (conta bancária, cartão, conjunto de notas, comprovantes, ConcilPro, fonte manual ou externa).

Para cada competência, a fonte terá um estado: `aguardando`, `recebida`, `sem_movimento`, `dispensada`, `erro`.

Ausência de registros não equivale a `sem_movimento`. `sem_movimento` exige confirmação manual auditada. `dispensada` também exige autorização e motivo.

### 3.4 Competência e timezone

A competência será representada no banco pelo primeiro dia do mês (competência 2026-07 → `DATE 2026-07-01`).

Limites de timestamps serão calculados em `America/Sao_Paulo` e convertidos para UTC nas consultas, com intervalo semiaberto:

```text
[2026-07-01 00:00:00 America/Sao_Paulo,
 2026-08-01 00:00:00 America/Sao_Paulo)
```

## 4. Objetivos

### 4.1 Objetivos funcionais

- mostrar onde cada empresa está no fechamento;
- mostrar a carteira inteira do escritório;
- identificar bloqueios e pendências;
- apontar o próximo passo;
- permitir confirmação auditada de sem movimento;
- fechar e reabrir competências;
- coordenar processamentos longos;
- manter links para detalhes dos módulos atuais.

### 4.2 Objetivos técnicos

- evolução aditiva da API v1;
- nenhuma reescrita big-bang;
- reutilização dos services e engines atuais;
- preservação de Pluggy, parsers, IA, NEO e exportações;
- tabelas novas sob ownership exclusivo do FastAPI;
- isolamento por tenant e empresa;
- auditoria;
- rollout por feature flag;
- contratos testados antes de refatorações.

### 4.3 Não objetivos iniciais

- substituir todos os endpoints CRUD;
- implementar frontend;
- migrar todas as camadas para arquitetura hexagonal;
- criar event sourcing completo;
- materializar todos os indicadores desde a primeira versão;
- unificar fisicamente todas as tabelas dos módulos;
- alterar contratos externos do Pluggy ou ERPs.

## 5. Arquitetura proposta

```text
src/
  api/v1/
    fechamentos.py
    carteira_contabil.py

  domain/
    fechamento/
      service.py
      read_model.py
      policy.py
      permissions.py

  schemas/
    fechamento.py
    carteira_contabil.py
```

**Router** — path/query/body, dependências de autenticação/CSRF/capability, serialização. Nenhum SQL direto, nenhum cálculo de estado.

**`FechamentoService`** — coordena casos de uso, consulta o read model, aplica política, chama services existentes, cria comandos/jobs, registra transições. Não chama routers.

**`FechamentoReadModel`** — executa agregações entre módulos, filtra tenant/empresa/competência, calcula contagens em lote, retorna fatos (não decisões de UI), evita N+1.

**`FechamentoPolicy`** — recebe fatos e configuração, determina aplicabilidade e estados de etapa, produz bloqueios, determina status global, valida pré-condições de fechamento. É pura e versionada.

**Services existentes** — continuam responsáveis por parsing, deduplicação, invariantes locais, persistência dos módulos, integrações externas, matching NEO, conciliação FIFO e exportação.

## 6. Política canônica `fechamento-v1`

### 6.1 Estados globais

| Estado | Significado |
|---|---|
| `nao_iniciado` | Nenhuma evidência ou confirmação foi registrada |
| `em_andamento` | Há trabalho iniciado e nenhuma condição de erro dominante |
| `requer_atencao` | Há pendências não bloqueantes ou revisão humana |
| `bloqueado` | Existe bloqueio que impede fechamento |
| `pronto_para_fechar` | Todas as pré-condições foram atendidas |
| `fechado` | Fechamento confirmado e mutações bloqueadas |
| `reaberto` | Fechamento anterior foi reaberto e voltou a aceitar trabalho |

### 6.2 Estados de etapa

| Estado | Significado |
|---|---|
| `nao_aplicavel` | Etapa desabilitada pela configuração |
| `sem_evidencia` | Não há evidência suficiente para afirmar movimento ou ausência |
| `pendente` | Existem entradas ou ações esperadas ainda não concluídas |
| `em_processamento` | Há job ativo |
| `requer_atencao` | Revisão humana necessária, mas não necessariamente bloqueante |
| `concluida` | Critérios da política atendidos |
| `erro` | Falha técnica ou de integridade |

### 6.3 Etapas iniciais

- **`configuracao`** — plano de contas, agências/contas, fontes esperadas, configuração ConcilPro, contas bancárias contábeis necessárias ao NEO.
- **`entradas_bancarias`** — por fonte: OFX/PDF recebido, sincronização Open Banking, confirmação de sem movimento, erros de conexão, cobertura temporal.
- **`documentos`** — notas recebidas/pendentes, comprovantes, associações, erros de importação. Não associado começa como `requer_atencao`; torná-lo bloqueante exige regra de produto adicional.
- **`cartoes`** — cartões aplicáveis, fatura criada, lançamentos recebidos/sem conta, fatura aberta/fechada/paga, fonte confirmada sem movimento.
- **`classificacao`** — transações pendentes/erro, decisões NEO sem regra/erro, partidas criadas, associações manuais pendentes.
- **`concilpro`** — aplicável apenas quando habilitado: fonte esperada, arquivo cobrindo a competência, processamento, fornecedores, divergências (resolvidas), confirmação de sem movimento quando válida.
- **`validacao_contabil`** — total de débitos e créditos, partidas órfãs, registros sem conta válida, relatório gerável, alterações posteriores a fechamento anterior.
- **`exportacao`** — necessidade, última execução, erro, download, formato/destino configurado. Não precisa ser bloqueante para todas as empresas.

### 6.4 Progresso

```json
{
  "concluidas": 5,
  "aplicaveis": 7,
  "percentual": 71,
  "policy_version": "fechamento-v1"
}
```

`nao_aplicavel` não entra no denominador. No início, todas as etapas aplicáveis têm peso igual; pesos diferentes exigem nova versão de política.

## 7. Superfície de API

### 7.1 Visão da empresa

```http
GET /api/v1/empresas/{empresa_id}/fechamentos/{competencia}
```

```json
{
  "empresa_id": "uuid",
  "competencia": "2026-07",
  "status": "bloqueado",
  "policy_version": "fechamento-v1",
  "version": 3,
  "progresso": { "concluidas": 4, "aplicaveis": 7, "percentual": 57 },
  "responsavel": { "id": "uuid", "nome": "Contador responsável" },
  "bloqueios": [
    {
      "codigo": "TRANSACOES_SEM_CLASSIFICACAO",
      "severidade": "bloqueante",
      "quantidade": 18,
      "valor_total": "24350.90",
      "etapa": "classificacao"
    }
  ],
  "etapas": [
    {
      "codigo": "entradas_bancarias",
      "status": "concluida",
      "aplicavel": true,
      "pendencias": 0,
      "bloqueios": 0,
      "totais": { "fontes_esperadas": 3, "recebidas": 2, "sem_movimento": 1, "transacoes": 412 },
      "ultima_atualizacao": "2026-08-03T13:00:00Z",
      "acoes": []
    },
    {
      "codigo": "classificacao",
      "status": "requer_atencao",
      "aplicavel": true,
      "pendencias": 18,
      "bloqueios": 18,
      "totais": { "transacoes": 412, "processadas": 394, "sem_regra": 18, "erros": 0 },
      "acoes": [
        { "codigo": "REVISAR_SEM_REGRA", "method": "GET", "href": "/api/v1/empresas/uuid/fechamentos/2026-07/pendencias?tipo=neo_sem_regra" }
      ]
    },
    {
      "codigo": "concilpro",
      "status": "nao_aplicavel",
      "aplicavel": false,
      "pendencias": 0,
      "bloqueios": 0,
      "totais": {},
      "acoes": []
    }
  ],
  "jobs_ativos": [],
  "calculado_em": "2026-08-03T13:05:00Z"
}
```

### 7.2 Pendências

```http
GET /api/v1/empresas/{empresa_id}/fechamentos/{competencia}/pendencias
```

Filtros: `tipo`, `etapa`, `severidade`, `agencia_id`, `responsavel_id`, paginação.

Tipos iniciais: `fonte_aguardando`, `transacao_pendente`, `transacao_erro`, `neo_sem_regra`, `neo_erro`, `nota_nao_associada`, `comprovante_nao_associado`, `fatura_aberta`, `lancamento_cartao_sem_conta`, `conexao_bancaria_erro`, `concilpro_ausente`, `concilpro_divergencia`, `partida_orfa`, `balancete_desbalanceado`, `job_erro`.

Cada pendência contém referência à entidade original e link para detalhe. O fechamento não duplica integralmente os recursos dos módulos.

### 7.3 Fontes esperadas

```http
GET   /api/v1/empresas/{empresa_id}/fontes-esperadas
POST  /api/v1/empresas/{empresa_id}/fontes-esperadas
PATCH /api/v1/empresas/{empresa_id}/fontes-esperadas/{fonte_id}
```

Confirmação na competência:

```http
POST /api/v1/empresas/{empresa_id}/fechamentos/{competencia}/fontes/{source_key}/confirmar-sem-movimento
```

```json
{ "motivo": "Conta bancária sem movimentação no período", "version": 2 }
```

Desfazer confirmação é uma mutação auditada separada.

### 7.4 Carteira do escritório

```http
GET /api/v1/carteira-contabil/fechamentos?competencia=2026-07
```

```json
{
  "competencia": "2026-07",
  "resumo": { "total_empresas": 84, "fechadas": 31, "prontas": 8, "em_andamento": 32, "bloqueadas": 13 },
  "empresas": [
    {
      "empresa_id": "uuid",
      "nome": "Empresa Exemplo",
      "status": "bloqueado",
      "percentual": 42,
      "bloqueios": 3,
      "pendencias": 27,
      "responsavel_id": "uuid",
      "ultima_atividade_em": "2026-08-03T12:00:00Z"
    }
  ]
}
```

A consulta respeita permissões por empresa, inclusive para usuários do mesmo tenant.

### 7.5 Comandos (após estabilização da leitura)

```http
POST /empresas/{id}/fechamentos/{competencia}/acoes/processar-classificacao
POST /empresas/{id}/fechamentos/{competencia}/acoes/validar
POST /empresas/{id}/fechamentos/{competencia}/acoes/exportar
POST /empresas/{id}/fechamentos/{competencia}/acoes/fechar
POST /empresas/{id}/fechamentos/{competencia}/acoes/reabrir
```

Fechar: `{ "version": 7 }`
Reabrir: `{ "version": 8, "motivo": "Extrato complementar recebido após o fechamento", "responsavel_id": "uuid" }`

Requisitos: CSRF, capability adequada, `Idempotency-Key` para jobs, optimistic locking, erro de domínio tipado, auditoria, nenhuma transação distribuída entre módulos.

## 8. Modelo de dados planejado

**`fechamento_configuracoes`** — tenant, empresa (unique), ConcilPro habilitado, versão da política, timezone, auditoria de atualização.

**`fechamentos`** — tenant, empresa, competência, status, versão, política, responsável, fechamento atual, última reabertura. Unique `(tenant_id, empresa_id, competencia)`.

**`fechamento_etapas`** — fechamento, código, aplicabilidade, status, contagens, avaliação, conclusão, versão. Unique `(fechamento_id, codigo)`.

**`fechamento_fontes_esperadas`** — empresa, chave estável, tipo, referência interna opcional, obrigatoriedade, vigência, ativo, descrição.

**`fechamento_fontes`** — fechamento, fonte, cópia dos metadados, status, evidência, confirmação, usuário, motivo, versão.

**`fechamento_transicoes`** — histórico append-only: fechamento, status anterior/posterior, ação, motivo, responsável, usuário, trace, metadata, instante. Toda transição relevante também é registrada em `audit_logs`.

Detalhamento completo dos campos e constraints: ver backlog operacional da Fase 0 (`docs/fase-0-backlog-fechamento.md`, item F0-10).

## 9. Fases de execução

### Fase 0 — Contratos, semântica e blueprint

ADRs 014–021, catálogo dos endpoints v1, contract tests dos fluxos atuais, matriz de autenticação/CSRF/tenant/permissão, política `fechamento-v1`, blueprint do modelo de dados, schemas OpenAPI da Fase 1, baseline de desempenho, alinhamento com `contabil-front`, revisão de migrations com o time do legado. Nenhum endpoint ou schema de banco é implantado nesta fase. Backlog operacional completo: `docs/fase-0-backlog-fechamento.md`.

A Fase 0 termina quando o time consegue responder, de forma testável: o que é uma etapa concluída, o que bloqueia, quais contratos não podem mudar, quais dados alimentam cada indicador, como uma fonte é considerada recebida, como funciona sem movimento, como ConcilPro se torna aplicável, quais mutações serão bloqueadas após fechamento.

### Fase 1 — Projeção somente leitura

Implementar visão da competência, pendências, carteira do escritório, política calculada, links para endpoints atuais. Nenhuma tabela de fechamento obrigatória — leitura das tabelas atuais, feature flag por tenant, `policy_version`, `calculado_em`, queries agregadas em lote, endpoints existentes preservados.

Limitação deliberada: sem persistência de fontes esperadas, a primeira versão pode identificar dados observados, mas não declarar completude real. Para pilotos, fontes podem ser fornecidas por configuração temporária.

Riscos: N+1 na carteira, permissões compostas, ausência de `tenant_id` em várias tabelas, estados semânticos incompatíveis, grandes volumes. Mitigações: read model dedicado, joins por empresa/tenant, queries agrupadas, paginação, índices medidos, testes cross-tenant.

### Fase 2 — Estado persistido

Criar configurações, fechamentos, etapas, fontes esperadas, fontes da competência, transições. Rollout: migrations aditivas → ownership FastAPI → backfill mínimo → shadow mode → comparação calculado vs. persistido → feature flag → ativação gradual.

Introduz responsáveis, confirmação de sem movimento, ConcilPro por empresa, histórico de estado, optimistic locking, fechamento/reabertura em modo piloto.

Riscos: conflito Alembic/Liquibase, drift de contagens, mudança de configuração no meio da competência, reaberturas múltiplas, alterações tardias. Mitigações: snapshot de fontes, transições append-only, projeções recalculáveis, política versionada, nenhuma alteração silenciosa em tabelas legadas, revisão conjunta com o legado.

### Fase 3 — Jobs duráveis

Infraestrutura comum: tabela/repositório de jobs, fila durável, workers, heartbeat, retry, idempotência, progresso, erro seguro, resultado/download, observabilidade.

Ordem sugerida: NEO → importação OFX/PDF → ConcilPro → exportação → sincronizações Pluggy longas.

```json
{
  "job_id": "uuid",
  "tipo": "neo",
  "status": "processando",
  "etapa_atual": "classificando_transacoes",
  "progresso_atual": 280,
  "progresso_total": 412,
  "percentual": 67,
  "tentativa": 1,
  "erro_codigo": null,
  "erro_mensagem": null,
  "resultado": null,
  "criado_em": "...",
  "iniciado_em": "...",
  "finalizado_em": null
}
```

Compatibilidade: endpoints antigos permanecem, novos comandos retornam `202`, adaptadores mantêm responses legados, depreciação segue ADR-002.

Riscos: retries duplicarem partidas, ConcilPro com commits próprios, jobs abandonados, arquivo em memória, dois workers na mesma competência. Mitigações: `Idempotency-Key`, unique constraints, leases, heartbeat, locking por recurso, checkpoints, payload em object storage quando necessário, limites transacionais pequenos.

### Fase 4 — Orquestração e bloqueio real

Ativar comandos de fluxo, fechamento formal, reabertura, bloqueio de mutações, validação central, auditoria integral.

Mutações a proteger: importação de extrato da competência, sincronização Open Banking, processamento/associação manual NEO, criação/associação/cancelamento de documentos, importação/alteração de fatura, ConcilPro cobrindo a competência, alteração de registros contábeis, reprocessamentos, exclusões que mudem indicadores.

Implementação recomendada: `FechamentoGuard` reutilizável nos services (não depender só de middleware HTTP, pois jobs também escrevem); toda operação identifica competências afetadas; competência fechada gera erro de domínio tipado; reabertura ocorre antes da mutação; nenhuma reabertura automática.

Riscos: uma operação tocar várias competências, arquivo ConcilPro cobrindo período amplo, sync Pluggy trazendo transações retroativas, alteração de configuração retroagindo, relatórios vivos mudando após fechamento. Mitigações: calcular todas as competências afetadas, rejeitar a operação inteira ou exigir reabertura de todas, snapshots/versionamento quando necessário, política explícita para relatórios fechados, auditoria com IDs de evidência.

### Fase 5 — Adoção, otimização e deprecação

Materializar carteira se necessário, outbox/eventos para projeções, extrair SQL dos routers, decompor ConcilPro, extrair associação manual do NEO, consolidar schemas, remover duplicações, anunciar deprecações, manter coexistência prevista no ADR-002, desligar caminhos antigos somente após telemetria e migração do frontend.

## 10. Riscos específicos

1. **Permissões inferidas pela URL** — `get_company_context` deriva o módulo do segmento após `/empresas/{id}`; um novo prefixo `fechamentos` pode negar acesso a usuários não-admin. → capabilities explícitas, testes para não-admin, filtro de empresas autorizadas na carteira, backfill de permissões.
2. **ADR de tenant não refletido integralmente no schema** — várias tabelas têm `empresa_id` mas não `tenant_id`. → tabelas novas com ambos, consultas compostas validam empresa/tenant, testes cross-tenant.
3. **Semântica de "processada"** — não significa competência fechada. → estados canônicos independentes, política documentada, frontend não interpreta enums dos módulos como progresso global.
4. **ConcilPro não durável** — `BackgroundTasks` não garante execução após restart. → preservar contrato atual, caracterizar com testes, migrar para jobs na Fase 3.
5. **ExportJob síncrono** — nome sugere fila, comportamento é síncrono. → documentar comportamento atual, manter headers/arquivo, migrar sem alterar contrato silenciosamente.
6. **Limites transacionais** — NEO, ConcilPro e imports têm fronteiras diferentes; uma transação única do fechamento seria longa e frágil. → coordenação por estados, etapas atômicas, idempotência, retry, outbox/eventos.
7. **Dados vivos após fechamento** — relatórios consultam registros vivos. → bloqueio de mutações, reabertura explícita, detectar sync/import retroativo, decidir depois se documentos oficiais exigem snapshot imutável.
8. **Completude falsa** — nenhuma transação encontrada pode significar ausência de importação, não ausência de movimento. → fontes esperadas, evidência de recebimento, confirmação manual, `sem_evidencia` distinto de `concluida`.
9. **Performance da carteira** — rodar o dashboard individual por empresa gera N+1. → read model tenant-wide, agregações em lote, índices, baseline, projeção materializada só depois de medir.
10. **Banco compartilhado** — migrations podem conflitar com o legado. → tabelas novas com prefixo claro, ownership FastAPI, revisão conjunta, migrations aditivas, feature flags, nenhum rename/drop inicial.

## 11. Coordenação com `contabil-front`

**Estados e apresentação** — o backend fornece códigos, severidades, contagens, bloqueios, ações, links, timestamps. O frontend decide labels, cores, layout, navegação, componentes visuais. O frontend não deve inferir estado a partir de textos livres.

**Deep links** — a nova visão navega para detalhes existentes com empresa estável na URL (ADR-009).

**Feature flags (rollout conjunto)** — contrato disponível → visão somente leitura → pilotos → confirmação de fontes → fechamento → jobs → deprecação de chamadas client-side redundantes.

**Polling** — a Fase 3 exige um componente comum para polling com backoff, retry, erro parcial, expiração, download, retomada após reload.

## 12. Estratégia de testes

- **Contract tests** — status HTTP, cookies/CSRF, required fields, content types, headers, enums legados necessários, compatibilidade aditiva.
- **Unit tests** — política: combinações de fonte, ConcilPro aplicável/não aplicável, sem movimento, bloqueios, progresso, fechamento, reabertura, versão da política.
- **Integration tests** — persistência, unique constraints, optimistic locking, tenant/empresa, permissões, auditoria, queries por competência, shadow mode.
- **End-to-end de backend** — fluxo completo (fontes → importar → NEO → resolver pendências → confirmar sem movimento → validar → fechar → mutação bloqueada → reabrir com motivo → mutação permitida → fechar novamente); ConcilPro desabilitado (não aplicável, fechamento possível); ConcilPro habilitado (ausência bloqueia, processamento conclui, divergência bloqueia, resolução remove bloqueio).

## 13. Critérios de sucesso

- o frontend obtém o estado de uma competência em uma chamada principal;
- a carteira não faz chamadas por empresa;
- toda porcentagem tem política e denominador explícitos;
- ausência de dados não é interpretada como sem movimento;
- ConcilPro é aplicável por empresa;
- fechamento impede mutações;
- reabertura sempre é auditada;
- jobs sobrevivem a restart;
- nenhuma integração existente quebra;
- endpoints antigos coexistem durante a migração;
- isolamento por tenant e empresa coberto por testes;
- mudanças tardias são detectadas e tratadas.

## 14. Sequência recomendada imediata

1. Aprovar e registrar ADRs 014–021.
2. Criar catálogo de contratos v1.
3. Preencher `tests/contract`.
4. Aprovar `fechamento-policy-v1`.
5. Aprovar blueprint das tabelas da Fase 2.
6. Aprovar schemas da Fase 1 com `contabil-front`.
7. Medir queries e latência.
8. Implementar read model somente leitura.
9. Pilotar com feature flag.
10. Só então criar persistência e comandos de fechamento.

---
*Plano elaborado em conjunto com Codex (GPT-5.6 Sol) a partir da exploração do código atual do `contabil-core`. Backlog operacional detalhado da Fase 0 em [`fase-0-backlog-fechamento.md`](./fase-0-backlog-fechamento.md).*
