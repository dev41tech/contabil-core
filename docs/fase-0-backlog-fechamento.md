# Backlog operacional — Fase 0 do plano de fechamento contábil

Ver contexto completo em [`plano-fechamento-contabil.md`](./plano-fechamento-contabil.md). A Fase 0 não cria endpoints nem migrations — ela encerra as decisões arquiteturais, congela os contratos legados e produz a especificação executável das Fases 1/2. Cada item abaixo pode virar uma issue/tarefa independente.

## F0-01 — Escrever os ADRs de fechamento

Criar os seguintes ADRs em `DECISIONS.md`, continuando a numeração atual:

- **ADR-014 — Fechamento mensal como agregado de processo.** Cada combinação `(tenant_id, empresa_id, competencia)` terá uma instância de fechamento que coordena etapas e fontes, sem substituir as entidades dos módulos existentes.
- **ADR-015 — Competência fechada bloqueia mutações e exige reabertura auditada.** Mutações que afetem uma competência fechada serão rejeitadas até que um usuário autorizado a reabra, registrando obrigatoriamente motivo, responsável e instante.
- **ADR-016 — Completude de entradas por fontes esperadas.** Uma etapa de entrada só será considerada concluída quando cada fonte esperada da competência tiver evidência de recebimento ou confirmação manual auditada de "sem movimento".
- **ADR-017 — ConcilPro configurável por empresa.** A participação do ConcilPro no fechamento é controlada por configuração da empresa; desabilitado → etapa `nao_aplicavel`; habilitado → pode bloquear o fechamento.
- **ADR-018 — Estado canônico e política versionada de fechamento.** Estados globais, estados de etapa, severidades e critérios de bloqueio serão definidos por uma política versionada, independente dos enums internos de NEO, transação, nota, cartão e ConcilPro.
- **ADR-019 — Jobs duráveis para processamento longo.** NEO, importações longas, ConcilPro e exportações pesadas migram progressivamente para jobs duráveis, mantendo adaptadores compatíveis com os endpoints v1 atuais durante a transição.
- **ADR-020 — Autorização explícita para APIs compostas.** Endpoints de fechamento e carteira usam capabilities explícitas, sem depender apenas da inferência do módulo pelo segmento da URL feita hoje por `get_company_context`.
- **ADR-021 — Competência contábil e timezone.** A competência é persistida como o primeiro dia do mês; limites temporais calculados em `America/Sao_Paulo`, convertidos para UTC somente nas consultas a timestamps.

**Critérios de aceite:** cada ADR tem contexto, decisão, consequências, estratégia progressiva e condição de revisão; indicam que endpoints existentes permanecem válidos; ADR-015 enumera quais mutações serão bloqueadas após a Fase 4; ADR-016 diferencia "sem registros encontrados" de "sem movimento confirmado"; ADR-020 define capabilities iniciais (`fechamento:read`, `fechamento:operate`, `fechamento:close`).

## F0-02 — Catálogo de contratos v1 existentes

Criar `docs/api-v1-contract-baseline.md`, uma linha por endpoint relevante, com colunas: módulo; método e path; autenticação; CSRF; capability/permissão atual; request; status HTTP de sucesso; response/content type; efeitos no banco; idempotência; síncrono ou background; tabelas tocadas; competência afetada; consumidor conhecido; teste existente; teste de contrato a criar; candidato a depreciação futura.

**Escopo mínimo:** extrato, Open Banking, NEO, notas, comprovantes, cartões, ConcilPro, contábil, relatórios, stats, exportação.

**Critérios de aceite:** todos os endpoints desses módulos aparecem na matriz; fluxos que escrevem em mais de uma entidade estão identificados; endpoints com lógica no router estão assinalados; nenhum endpoint é marcado para remoção sem consumidor conhecido e plano ADR-002.

## F0-03 — Infraestrutura de contract tests

Preencher `tests/contract/` (hoje vazio):

```text
tests/contract/
  conftest.py
  assertions.py
  test_auth_contract.py
  test_extrato_contract.py
  test_openbanking_contract.py
  test_neo_contract.py
  test_documentos_contract.py
  test_cartoes_contract.py
  test_concilpro_contract.py
  test_contabil_relatorios_contract.py
  test_exportacao_contract.py
  test_stats_contract.py
  test_company_scope_contract.py
```

**Regras:** validar status HTTP, content type, campos obrigatórios e tipos; aceitar campos adicionais (compatibilidade aditiva do ADR-002); não usar snapshots rígidos de JSON completo; testar enums/estados apenas onde clientes já dependem deles; separar teste de contrato HTTP de teste interno do service; rodar em CI junto aos testes de integração.

**Critérios de aceite:** `pytest tests/contract/` roda isoladamente; há helpers comuns para login, cookie, CSRF, tenant e empresa; falha de contrato informa claramente método/path/campo incompatível.

## F0-04 — Congelar contrato de autenticação, CSRF e isolamento

Testes parametrizados para: request sem autenticação; empresa de outro tenant; empresa do mesmo tenant sem permissão; usuário com permissão adequada; mutação sem CSRF; mutação com CSRF válido; ID de entidade pertencente a outra empresa. Cobrir ao menos um GET e uma mutação de cada módulo composto.

**Endpoints prioritários:** `POST /api/v1/auth/login`, `POST /api/v1/auth/refresh`, `GET /api/v1/auth/me`, todos os prefixes `/api/v1/empresas/{empresa_id}/...`.

**Verificação específica:** registrar o comportamento atual de `get_company_context`; provar que IDs de NEO, notas, comprovantes, cartões, conexões e ConcilPro não cruzam empresas; incluir teste de usuário não-admin (admin ignora a lista de módulos); não assumir que `empresa_id` sozinho equivale a isolamento de tenant.

**Critérios de aceite:** matriz de autorização documentada; testes de cross-tenant e cross-company para todos os módulos que alimentarão o fechamento; decisão explícita sobre `403` vs `404` sem vazar existência.

## F0-05 — Congelar o fluxo bancário e NEO

**Extrato** — `POST /empresas/{id}/extrato/importar`, `GET /empresas/{id}/extrato`, `GET /empresas/{id}/extrato/{transacao_id}`. Casos: OFX/PDF válido, reimportação idempotente, filtros, upload inválido/acima do limite, empresa/agência incompatíveis, preservação dos estados `pendente/processada/erro`.

**Open Banking** — connect token, salvamento de conexão, sincronização, reconexão, remoção, listagem. Casos: sincronização cria/vincula agência; transações entram `pendente`; duas sincronizações não duplicam; erro do Pluggy não vaza detalhes internos; item/session de uma empresa não pode ser usado em outra; resposta atual de sincronização permanece estável.

**NEO** — `POST /neo/processar`, `GET /neo/decisoes`, `POST /neo/decisoes/{id}/associar-manual`. Casos: filtro por competência, idempotência, match cria duas partidas balanceadas, sem regra permanece pendente, associação manual não recontabiliza, associação automática de notas/comprovantes, decisão e entidades associadas pertencem à mesma empresa, corpo do `NeoResultado` permanece compatível.

**Critério de aceite:** teste de caracterização cobrindo `importar OFX → processar NEO → consultar decisão → consultar registros contábeis → verificar duas partidas e status da transação`.

## F0-06 — Congelar contratos documentais e de cartões

**Notas** — criação, importação XML/ZIP/visual-OCR, associação/desassociação, cancelamento, deduplicação e escopo por empresa.

**Comprovantes** — criação, extração PDF, associação/desassociação, download, limites e content types, não reutilização indevida pelo NEO.

**Cartões** — CRUD, criação/atualização de fatura, importação CSV/PDF, lançamentos, associação fatura↔transação, transições `aberta → fechada → paga`, imutabilidade da fatura paga, unicidade de competência por cartão.

**Critério de aceite:** testes identificam, para cada mutação, qual competência seria bloqueada depois da Fase 4.

## F0-07 — Congelar o contrato e a semântica do ConcilPro

Contratos: upload, status por arquivo, listagem, resumo, fornecedores, conciliação FIFO, divergências, exportações.

**Casos obrigatórios:** upload novo retorna imediatamente `arquivo_id` e `PROCESSANDO`; polling retorna campos/estados atuais; conclusão e erro; arquivo duplicado; reprocessamento; restart/job abandonado (caracterizando a heurística atual); isolamento entre empresas; divergência não fabrica lançamento (ADR-013); exports atuais continuam válidos; parsing pesado e IA substituídos por doubles nos contract tests HTTP.

**Entregável adicional:** `docs/concilpro-boundary.md` deixando explícito que o ConcilPro concilia razão de fornecedores e não transações bancárias/NEO; matriz mapeando `data_inicio/data_fim` do arquivo para competências atingidas; decisão sobre arquivo ConcilPro cobrindo mais de um mês.

**Critérios de aceite:** estados legados em maiúsculas ficam congelados somente nos endpoints antigos; a política de fechamento mapeia esses estados para estados canônicos sem mudar o ConcilPro.

## F0-08 — Congelar contratos de leitura e saída

Cobrir: `GET /contabil`, `GET /contabil/{registro_id}`, DRE, balancete, livro-caixa, stats, `POST /exportacao/gerar`.

**Casos:** período/competência, empresa sem movimento, isolamento entre empresas, registros soft-deleted, totais D/C, exportação CSV/XLSX, headers (`Content-Disposition`, `X-Total-Registros`, `X-Job-Id`), exportação vazia, neutralização de fórmulas, caracterização de que `ExportJob` termina sincronamente como `concluido`.

**Critério de aceite:** o contrato deixa claro que relatórios atuais são "dados vivos", não snapshots de fechamento.

## F0-09 — Especificar a política `fechamento-v1`

Criar `docs/fechamento-policy-v1.md`. Para cada etapa (`configuracao`, `entradas_bancarias`, `documentos`, `cartoes`, `classificacao`, `concilpro`, `validacao_contabil`, `exportacao`), documentar: aplicabilidade, fontes, métrica, critério de conclusão, pendências, bloqueios, ações possíveis, endpoint detalhado existente.

**Critérios bloqueantes iniciais:** fonte obrigatória sem recebimento nem confirmação de sem movimento; transação `pendente`/`erro`; decisão NEO `sem_regra`/`erro` não resolvida; lançamento de cartão sem conta (se aplicável); fatura da competência ainda aberta; ConcilPro habilitado sem arquivo/evidência da competência; divergência ConcilPro bloqueante não resolvida; total de débitos ≠ créditos; job obrigatório em processamento ou erro.

**Observação:** nota/comprovante não associado começa como `requer_atencao` — bloqueante exige confirmação de regra contábil.

**Critérios de aceite:** toda métrica aponta para tabela/campo real; regras sem evidência suficiente marcadas como decisão pendente; percentual informa numerador, denominador e `policy_version`; `nao_aplicavel` não reduz artificialmente o progresso.

## F0-10 — Aprovar o blueprint de dados da Fase 2

Nenhuma migration nesta tarefa — entregável é `docs/fechamento-data-model.md`.

**`fechamento_configuracoes`** — `id UUID PK`, `tenant_id UUID NOT NULL`, `empresa_id UUID NOT NULL UNIQUE`, `concilpro_habilitado BOOLEAN NOT NULL DEFAULT FALSE`, `policy_version VARCHAR NOT NULL`, `timezone VARCHAR NOT NULL DEFAULT 'America/Sao_Paulo'`, `created_at`, `updated_at`, `updated_by`.

**`fechamentos`** — `id UUID PK`, `tenant_id`, `empresa_id`, `competencia DATE NOT NULL` (sempre dia 1), `status`, `policy_version`, `version INTEGER NOT NULL` (optimistic locking), `responsavel_id NULL`, `fechado_at/fechado_por NULL`, `ultima_reabertura_at/ultima_reabertura_por NULL`, `created_at`, `updated_at`. Unique `(tenant_id, empresa_id, competencia)`; competência com `day=1`; `fechado_at/fechado_por` obrigatórios quando `status=fechado`. O motivo de reabertura não fica só aqui, pois pode haver múltiplas reaberturas — ver `fechamento_transicoes`.

**`fechamento_etapas`** — `id UUID PK`, `tenant_id`, `fechamento_id`, `codigo`, `status`, `aplicavel BOOLEAN`, `policy_version`, `bloqueios_count`, `pendencias_count`, `avaliado_at`, `concluido_at`, `concluido_por`, `version`, `created_at`, `updated_at`. Unique `(fechamento_id, codigo)`. Contagens são projeções/cache — as entidades originais continuam sendo fonte de verdade.

**`fechamento_fontes_esperadas`** — `id UUID PK`, `tenant_id`, `empresa_id`, `source_key VARCHAR NOT NULL`, `tipo` (`conta_bancaria`, `cartao`, `notas`, `comprovantes`, `concilpro`, ...), `referencia_id UUID NULL`, `descricao`, `obrigatoria BOOLEAN`, `ativa BOOLEAN`, `vigencia_inicio DATE`, `vigencia_fim DATE NULL`, `created_at`, `updated_at`, `updated_by`. Unique `(empresa_id, source_key)`; vigência final não pode anteceder a inicial. `source_key` estável mesmo sem UUID interno (permite fontes externas/manuais).

**`fechamento_fontes`** — `id UUID PK`, `tenant_id`, `fechamento_id`, `fonte_esperada_id`, cópia de `source_key/tipo/descricao/obrigatoria`, `status` (`aguardando`, `recebida`, `sem_movimento`, `dispensada`, `erro`), `evidencia_tipo NULL`, `evidencia_id NULL`, `recebida_at NULL`, `confirmada_at/confirmada_por/confirmacao_motivo NULL`, `version`, `created_at`, `updated_at`. Unique `(fechamento_id, source_key)`; `sem_movimento` exige `confirmada_at/confirmada_por/motivo`; `recebida` exige evidência ou regra documentada de detecção automática; `dispensada` exige autorização e motivo. Cópia dos metadados preserva o snapshot caso a configuração futura mude.

**`fechamento_transicoes`** — append-only: `id UUID PK`, `tenant_id`, `fechamento_id`, `de_status`, `para_status`, `acao` (`fechar`, `reabrir`, `atribuir`, `confirmar_sem_movimento`), `motivo NULL`, `responsavel_id`, `request_user_id`, `trace_id`, `metadata JSONB`, `created_at`. `reabrir` exige motivo não vazio; registros nunca são atualizados/deletados; toda transição também gera `audit_logs`.

**Fora da Fase 2:** tabelas genéricas `jobs` e `job_tentativas/eventos` pertencem à Fase 3 — não misturar na primeira migration do fechamento.

**Critérios de aceite:** revisão com responsável pelo JHipster/Liquibase; ownership das tabelas atribuído exclusivamente ao FastAPI; índices propostos para carteira e competência; política de retenção definida para transições; plano de backfill/shadow mode documentado; nenhuma coluna adicionada a tabela legada sem coordenação explícita.

## F0-11 — Definir a API da Fase 1 antes de implementá-la

Produzir schemas OpenAPI de exemplo para os 3 endpoints de leitura (visão de empresa, pendências, carteira), com exemplos para: empresa sem configuração; sem movimento não confirmado; em andamento; ConcilPro não aplicável; ConcilPro obrigatório e faltante; pronto para fechar; fechado; reaberto; job em andamento.

**Critérios de aceite:** frontend aprova nomes e enumerações; responses permitem campos adicionais; ações contêm links para detalhes existentes; dados calculados informam `calculado_em` e `policy_version`; carteira retorna somente empresas autorizadas.

## F0-12 — Baseline de desempenho e observabilidade

Medir com massa representativa: fechamento de uma empresa; carteira com 10/50/100/500 empresas; stats com várias agências; contagem de pendências NEO/documentais; ConcilPro com arquivo grande; relatório e exportação de 12 meses.

Registrar: número de queries, p50/p95, memória, tempo de parser, tamanho das respostas, índices utilizados (`EXPLAIN`).

**Metas iniciais sugeridas:** visão de empresa p95 < 500ms; carteira de 100 empresas p95 < 1s; nenhuma query por empresa/agência em loop; paginação obrigatória para pendências; logs com `tenant_id`, `empresa_id`, `competencia`, `policy_version`, `trace_id`.

## F0-13 — Alinhamento formal com `contabil-front`

Revisão de contrato com: mapa endpoint atual → nova etapa; enums canônicos; exemplos OpenAPI; política de polling futuro; capability de fechamento; deep links; feature flags; comportamento de fechado/reaberto; fallback para endpoints atuais.

**Critérios de aceite:** responsável de frontend aprova o contrato; telas atuais reutilizadas como detalhe estão identificadas; nenhuma tela depende de interpretar texto livre para determinar estado; estratégia de rollout por tenant acordada.
