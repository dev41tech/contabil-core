# Plano de usuários e permissões

Data: 2026-08-26  
Estado analisado: commit `583d15d`  
Objetivo: substituir autorização implícita por path por um modelo explícito, auditável e migrável sem interromper a operação.

## 1. Diagnóstico confirmado

O levantamento está correto, com duas precisões operacionais:

- `Usuario.role` é `admin | contador`, no tenant: [src/db/models.py:90](../../src/db/models.py:90).
- `Permissao` é a junção `(usuario_id, empresa_id)` com CSV e `"*"`: [src/db/models.py:184](../../src/db/models.py:184).
- `get_company_context` valida empresa no tenant e deriva o módulo do primeiro segmento depois de `/empresas/{id}`: [src/api/deps.py:106](../../src/api/deps.py:106).
- A whitelist tem 15 módulos concedíveis mais `"*"`; os 19 prefixos incluem quatro ausentes: `aplicacoes_financeiras`, `auditoria`, `concilpro` e `permissoes`: [src/schemas/permissoes.py:9](../../src/schemas/permissoes.py:9), [src/api/v1/__init__.py:31](../../src/api/v1/__init__.py:31).
- O admin cria usuário já com uma senha escolhida no request e concede empresa em chamada separada: [src/api/v1/usuarios.py:62](../../src/api/v1/usuarios.py:62), [src/api/v1/permissoes.py:42](../../src/api/v1/permissoes.py:42).
- Já existem desativação e reativação: [src/api/v1/usuarios.py:96](../../src/api/v1/usuarios.py:96). Não existem convite, troca/reset de senha ou papel intermediário.
- A desativação já bloqueia access token e refresh na próxima chamada porque ambos relêem `Usuario.ativo`: [src/api/deps.py:54](../../src/api/deps.py:54), [src/domain/auth/service.py:107](../../src/domain/auth/service.py:107). Contudo, os refresh tokens não são revogados em lote nem há `session_version` para invalidar credenciais por troca de senha.
- Concessão/alteração/revogação de permissão já valida tenant e grava `audit_logs`: [src/domain/permissoes/service.py:77](../../src/domain/permissoes/service.py:77). Criação/desativação/reativação de usuário ainda não grava auditoria.

## 2. Decisão de granularidade

### 2.1 Recomendação

Passar de **módulo** para **recurso × ação**, com vocabulário pequeno e estável:

- `read`: consultar/listar/exportar uma representação já existente;
- `write`: criar ou alterar cadastro reversível;
- `execute`: iniciar operação que produz ou desfaz efeito financeiro/contábil;
- `manage`: administrar acesso, configuração estrutural ou identidade.

Não introduzir `approve` agora. “Aprovar” exige objeto pendente, autor diferente do aprovador, estados, rejeição e segregação de funções; hoje o backend executa a operação na própria request. Chamar `execute` de `approve` daria aparência de dupla conferência sem fornecê-la. Quando houver fechamento de período ou four-eyes real, adicionar `request`/`approve` como workflow, não como sinônimo de POST sensível.

### 2.2 O que precisa ser separado hoje

No mínimo, estas capacidades não podem continuar equivalentes a “tem acesso ao módulo”:

| Recurso | Ações mínimas | Operações sensíveis em `execute`/`manage` |
|---|---|---|
| `extrato` | `read`, `execute` | importar e cancelar lote; excluir transação |
| `neo` | `read`, `execute` | processar, classificar, reclassificar, cancelar/liberar lançamento |
| `contabil` | `read`, `write`, `execute` | criar lançamento manual; cancelar/estornar quando existir |
| `plano_contas` | `read`, `write`, `manage` | importação em massa e exclusão estrutural |
| `regras` | `read`, `write` | criar/alterar/desativar regra automática |
| `notas` / `comprovantes` | `read`, `write` | importar, associar/desassociar, cancelar/remover |
| `openbanking` | `read`, `manage`, `execute` | conectar/remover conta e sincronizar |
| `cartoes` | `read`, `write`, `execute` | fechar/pagar/reabrir fatura e importar lançamentos |
| `concilpro` | `read`, `execute` | upload/processamento e exportação de resultado |
| `exportacao` / `relatorios` / `stats` | `read` | exportar deve exigir `read`; se houver entrega oficial, criar `execute` específico |
| `agencias` / `contrapartes` / `aplicacoes_financeiras` | `read`, `write` | vínculo contábil fica em `write`; exclusão estrutural pode evoluir para `manage` |
| `auditoria` | `read` | leitura restrita por papel; nunca mutável |
| `permissoes` | `read`, `manage` | conceder, alterar e revogar acesso |
| `jobs` | `read` | visibilidade derivada do recurso que originou o job |

O primeiro ganho obrigatório é separar `read` de mutação e retirar `execute/manage` do contador comum. Não vale criar uma ação diferente para cada endpoint: `neo.execute` e `extrato.execute` são suficientes no estágio atual, desde que a auditoria registre a operação concreta.

## 3. Onde a autorização deve morar

### 3.1 Mecanismo recomendado

Criar um `AuthorizedAPIRouter`, subclasse/wrapper de `APIRouter`, cujos métodos exijam um argumento explícito, por exemplo:

```python
@router.post(
    "/pendencias/classificar-lote",
    permission="neo.execute",
    dependencies=[Depends(require_csrf)],
)
async def classificar_lote(...):
    ...
```

Ao registrar a rota, o router deve:

1. validar `permission` contra o catálogo tipado de permissões;
2. anexar `Depends(require_permission("neo.execute"))`;
3. guardar a permissão como metadata da `APIRoute`/OpenAPI;
4. recusar startup se uma rota não declarar uma entre: `permission=...`, `auth="authenticated"` ou `auth="public"`.

Rotas com `{empresa_id}` não podem usar `auth="authenticated"` como escape: precisam declarar permissão ou uma política administrativa explícita. Um teste percorre `app.routes` e falha se houver rota sem metadata, permissão inexistente ou método mutável mapeado apenas para `.read`.

Isso torna impossível uma rota nova “nascer aberta” ou depender do nome do path. A política fica no ponto em que o risco é criado — a declaração da rota — e a avaliação fica centralizada em uma dependência.

### 3.2 Avaliação da permissão

`require_permission` deve sempre:

1. autenticar usuário e reler `ativo`, `tenant_role` e `session_version`;
2. validar que a empresa pertence ao tenant e está ativa;
3. resolver membership da empresa;
4. calcular `permissões do papel + allows - denies`, ignorando override expirado;
5. negar por padrão e devolver a mesma resposta para recurso inexistente ou fora do escopo.

`owner` e `admin` podem ter política tenant-wide, mas nunca ignoram o filtro de tenant. O job exige tanto `jobs.read` quanto visibilidade do recurso de origem; não se deve reconstruir isso relendo CSV como ocorre hoje em [src/api/v1/jobs.py:18](../../src/api/v1/jobs.py:18).

### 3.3 Migração sem parada

1. Introduzir o router declarativo usando ainda um adaptador que traduz `recurso.ação` para o CSV atual; toda ação de um módulo equivale temporariamente ao módulo legado.
2. Declarar todas as rotas e ativar o teste/startup guard. Corrigir de imediato os quatro módulos ausentes.
3. Criar as tabelas novas e fazer backfill.
4. Fazer dual-write nas APIs de permissão e shadow-read: avaliar antigo e novo, autorizar pelo antigo e registrar divergência sem dados sensíveis.
5. Zerar divergências em produção; então autorizar pelo novo e manter fallback legado atrás de feature flag curta.
6. Remover o fallback; só depois tornar o CSV somente leitura e, em release posterior, removê-lo.

Se o corte para o avaliador novo vier antes do backfill, contadores recebem 403. Se o CSV deixar de ser escrito antes do dual-write, uma alteração feita durante a transição desaparece no corte.

## 4. Papéis recomendados

### 4.1 Modelo

Usar **papel fixo + overrides**, em dois níveis:

- papel no tenant: `owner`, `admin`, `member`, `client`;
- papel na empresa/membership: `contador_senior`, `contador`, `auditor`, `cliente_leitura`;
- overrides por usuário × empresa × permissão, com `allow` ou `deny`, motivo e expiração opcional.

Não usar apenas permissões: a operação perderia nomes compreensíveis, onboarding repetível e revisão de acesso. Não usar apenas papel: exceções reais forçariam proliferação de papéis (“contador sem Open Banking”, “cliente com relatórios”). Overrides devem ser exceção visível, não o caminho normal.

### 4.2 Semântica dos papéis

- `owner`: dono do escritório; gerencia admins, usuários, empresas e permissões. Não pode remover/desativar o último owner ativo.
- `admin`: administração delegada do escritório; gerencia operação e acessos, mas não promove/remove owner nem altera controles reservados.
- `member`: precisa de membership em cada empresa; não ganha acesso por estar no tenant.
- `client`: identidade de cliente final; também precisa de membership e nunca recebe papel operacional por padrão.
- `contador_senior`: leitura/escrita e `execute` nos módulos contábeis da empresa; pode cancelar/reclassificar, mas não gerencia usuários.
- `contador`: leitura/escrita de cadastros e classificação normal; operações de maior impacto ficam fora ou entram por override explícito.
- `auditor`: somente leitura de dados, relatórios e trilha, sem exportação sensível se a política comercial assim exigir.
- `cliente_leitura`: extrato, documentos e relatórios próprios em leitura; sem NEO, regras, plano estrutural, jobs internos ou auditoria do escritório.

O papel efetivo de empresa deve aparecer na UI junto com overrides e data da última revisão. Um mesmo usuário pode ser `contador_senior` numa empresa e `auditor` em outra.

## 5. Ciclo de vida do usuário

### 5.1 Criação e convite

1. Owner/admin informa nome, e-mail e papel; não define senha de outra pessoa.
2. O backend cria usuário `pending`, membership(s) e token de convite aleatório, guardando apenas `SHA-256` do token.
3. O link de uso único expira em 48 horas. Reenvio revoga todos os convites anteriores ainda abertos.
4. Ao aceitar, o usuário confirma identidade, define senha, recebe `password_changed_at`, passa a `active` e inicia sessão normal.

Enquanto o envio de e-mail não estiver pronto, o backend pode devolver o link **uma única vez** para entrega por canal seguro a owner/admin; nunca usar senha temporária conhecida pelo administrador nem registrar token em log/auditoria.

### 5.2 Senha

- Troca autenticada: exigir senha atual, aplicar política/checagem de vazamento se disponível, incrementar `session_version` e revogar todos os refresh tokens, inclusive o atual; o usuário autentica novamente.
- Reset: resposta pública uniforme para e-mail existente/inexistente; token hash de uso único, validade de 30 minutos, rate limit por IP/tenant/identidade; no consumo, incrementar `session_version` e revogar todos os refresh tokens.
- Não implementar expiração periódica de senha sem requisito regulatório. Ela incentiva padrões previsíveis; expirar convite/reset e invalidar por incidente é o necessário.
- Não reutilizar últimas senhas se houver política regulatória; caso contrário, priorizar comprimento, bloqueio de senhas vazadas e MFA futuro.

### 5.3 Desativação e revogação

Desativar usuário deve, na mesma transação:

1. mudar `status` para `suspended`/`disabled`;
2. incrementar `session_version` e `authz_version`;
3. revogar todos os `refresh_tokens` ativos;
4. revogar convites/resets abertos;
5. gravar auditoria com motivo.

O access token deixa de funcionar na próxima request porque a dependência relê o usuário. A versão de sessão protege também uma futura otimização que deixe de consultar todos os campos; o claim `session_version` do JWT deve igualar o valor atual no banco.

Revogar membership ou override entra em vigor na próxima request e incrementa `authz_version`. Uma operação já autorizada e em execução não pode ser “desexecutada”: jobs já iniciados continuam e ficam atribuídos ao ator original. Para incidente de segurança, oferecer ação separada “desativar e cancelar jobs ainda na fila”; job em processamento exige cancelamento cooperativo, nunca morte abrupta no meio de transação contábil.

### 5.4 Reativação

Reativar não restaura sessão nem token. O usuário volta a `active`, mantém memberships ainda ativos e entra por reset/novo convite se não tiver credencial válida. Reativação e restauração de acesso são decisões distintas e auditadas.

## 6. Modelagem e migration

### 6.1 DDL recomendado

Nomes podem seguir a convenção final do projeto, mas as constraints abaixo são parte da decisão, não detalhe opcional:

```sql
-- Integridade de tenant para FKs compostas.
ALTER TABLE usuarios ADD CONSTRAINT uq_usuarios_id_tenant UNIQUE (id, tenant_id);
ALTER TABLE empresas ADD CONSTRAINT uq_empresas_id_tenant UNIQUE (id, tenant_id);

ALTER TABLE usuarios
  ADD COLUMN tenant_role varchar(20),
  ADD COLUMN status varchar(20),
  ADD COLUMN session_version bigint NOT NULL DEFAULT 1,
  ADD COLUMN authz_version bigint NOT NULL DEFAULT 1,
  ADD COLUMN password_changed_at timestamptz,
  ADD CONSTRAINT ck_usuario_tenant_role
    CHECK (tenant_role IN ('owner','admin','member','client')),
  ADD CONSTRAINT ck_usuario_status
    CHECK (status IN ('pending','active','suspended','disabled'));

CREATE TABLE auth_permissions (
  code varchar(100) PRIMARY KEY,             -- ex.: neo.execute
  resource varchar(60) NOT NULL,
  action varchar(20) NOT NULL,
  risk varchar(10) NOT NULL CHECK (risk IN ('low','medium','high')),
  description varchar(300) NOT NULL,
  UNIQUE (resource, action)
);

CREATE TABLE auth_company_roles (
  code varchar(40) PRIMARY KEY,
  name varchar(100) NOT NULL,
  assignable boolean NOT NULL DEFAULT true,
  sort_order integer NOT NULL
);

CREATE TABLE auth_company_role_permissions (
  role_code varchar(40) NOT NULL REFERENCES auth_company_roles(code),
  permission_code varchar(100) NOT NULL REFERENCES auth_permissions(code),
  PRIMARY KEY (role_code, permission_code)
);

CREATE TABLE company_memberships (
  id uuid PRIMARY KEY,
  tenant_id uuid NOT NULL,
  usuario_id uuid NOT NULL,
  empresa_id uuid NOT NULL,
  role_code varchar(40) NOT NULL REFERENCES auth_company_roles(code),
  status varchar(20) NOT NULL DEFAULT 'active'
    CHECK (status IN ('active','revoked')),
  granted_by uuid,
  granted_at timestamptz NOT NULL DEFAULT now(),
  revoked_by uuid,
  revoked_at timestamptz,
  revocation_reason varchar(500),
  UNIQUE (usuario_id, empresa_id),
  FOREIGN KEY (usuario_id, tenant_id) REFERENCES usuarios(id, tenant_id),
  FOREIGN KEY (empresa_id, tenant_id) REFERENCES empresas(id, tenant_id),
  FOREIGN KEY (granted_by, tenant_id) REFERENCES usuarios(id, tenant_id),
  FOREIGN KEY (revoked_by, tenant_id) REFERENCES usuarios(id, tenant_id)
);
CREATE INDEX ix_membership_empresa_status
  ON company_memberships (empresa_id, status);
CREATE INDEX ix_membership_usuario_status
  ON company_memberships (usuario_id, status);

CREATE TABLE user_permission_overrides (
  id uuid PRIMARY KEY,
  membership_id uuid NOT NULL REFERENCES company_memberships(id),
  permission_code varchar(100) NOT NULL REFERENCES auth_permissions(code),
  effect varchar(10) NOT NULL CHECK (effect IN ('allow','deny')),
  reason varchar(500) NOT NULL,
  expires_at timestamptz,
  granted_by uuid NOT NULL REFERENCES usuarios(id),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (membership_id, permission_code)
);
CREATE INDEX ix_override_membership_expira
  ON user_permission_overrides (membership_id, expires_at);

CREATE TABLE user_action_tokens (
  id uuid PRIMARY KEY,
  tenant_id uuid NOT NULL,
  usuario_id uuid NOT NULL,
  purpose varchar(20) NOT NULL CHECK (purpose IN ('invite','password_reset')),
  token_hash char(64) NOT NULL UNIQUE,
  expires_at timestamptz NOT NULL,
  consumed_at timestamptz,
  revoked_at timestamptz,
  created_by uuid,
  created_at timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (usuario_id, tenant_id) REFERENCES usuarios(id, tenant_id),
  FOREIGN KEY (created_by, tenant_id) REFERENCES usuarios(id, tenant_id)
);
CREATE INDEX ix_user_action_token_aberto
  ON user_action_tokens (usuario_id, purpose, expires_at)
  WHERE consumed_at IS NULL AND revoked_at IS NULL;
```

Durante a transição, `Usuario.role`, `Usuario.ativo` e `Permissao.modulos` permanecem. Depois do corte, `senha_hash` deve aceitar `NULL` somente para `pending`, com `CHECK` exigindo hash em `active`; `ativo` é substituído por `status` e o enum `role_enum` pode ser removido em migration posterior.

`deny` vence `allow`; override expirado é ignorado. Papel não pode conceder acesso tenant-wide: a exceção é a política explícita de `owner/admin`, sempre limitada ao tenant.

### 6.2 Catálogo inicial

Semear permissões versionadas na migration e manter o mesmo catálogo tipado no código. Exemplos obrigatórios: `extrato.read`, `extrato.execute`, `neo.read`, `neo.execute`, `contabil.read`, `contabil.write`, `contabil.execute`, `permissoes.read`, `permissoes.manage`, `auditoria.read` e equivalentes da tabela da seção 2.

Semear papéis `contador_senior`, `contador`, `auditor`, `cliente_leitura` e dois papéis não atribuíveis pela UI: `legacy_full` e `legacy_custom`. Os dois últimos existem apenas para backfill sem perda e são eliminados após revisão manual.

### 6.3 Backfill sem perda

1. `Usuario.role='admin'` → `tenant_role='admin'`; `contador` → `member`; `ativo` → `status active/suspended`. Não escolher owner por heurística: promover explicitamente o dono de cada tenant depois do deploy aditivo.
2. Para cada linha de `permissoes`, criar membership ativa.
3. CSV `"*"` → papel `legacy_full`, contendo **todas as permissões correspondentes às rotas existentes no momento da migration**, inclusive os quatro módulos hoje alcançados apenas por `"*"`.
4. CSV explícito → papel `legacy_custom`, sem grants-base, e um override `allow` para todas as ações então existentes de cada módulo listado. Assim, o corte não concede módulo novo por pertencer a um papel amplo.
5. `jobs` legado é traduzido para `jobs.read`; concessões legadas de `neo`/`extrato` também recebem visibilidade apenas dos jobs de sua origem, preservando o comportamento atual.
6. Admin continua com política tenant-wide no primeiro corte. Depois que cada tenant indicar owner, aplicar as restrições novas de promoção/último owner.
7. Gerar relatório de reconciliação: para cada usuário × empresa × rota, resultado antigo e novo devem coincidir. Divergência bloqueia o corte.

### 6.4 Dual-write e remoção do legado

As APIs atuais de permissão passam a escrever membership/overrides e CSV na mesma transação. Após o avaliador novo ficar estável, congelar o CSV, observar pelo menos um ciclo operacional completo e só então remover tabela/coluna legada em migration separada.

## 7. Auditoria de identidade e acesso

Reutilizar `audit_logs` e `registrar_auditoria`, sempre na mesma transação da mudança. Registrar:

- criação, convite enviado/reenviado/aceito/expirado/revogado;
- ativação, suspensão, desativação e reativação;
- troca/reset de senha e revogação global de sessões — nunca senha, hash ou token;
- mudança de `tenant_role` e bloqueio de tentativa de remover o último owner;
- criação, mudança de papel e revogação de membership;
- criação, alteração, expiração e remoção de override;
- quem concedeu/revogou, usuário alvo, tenant, empresa, permissão/papel, antes/depois, motivo, expiração, IP, trace e correlação de operação em lote.

Eventos sugeridos: `usuario.convidado`, `usuario.ativado`, `usuario.desativado`, `usuario.sessoes_revogadas`, `usuario.tenant_role_alterado`, `membership.concedida`, `membership.role_alterado`, `membership.revogada`, `permissao.override_concedido` e `permissao.override_revogado`.

Negativas de alto risco (`permissoes.manage`, promoção a owner, reset repetido) devem gerar evento de segurança com rate limit; não gravar cada 403 comum em `audit_logs`, para não transformar a trilha contábil em ruído/DoS. O papel da aplicação deve ter `INSERT/SELECT`, mas não `UPDATE/DELETE`, sobre `audit_logs`; correção de log é evento compensatório, nunca edição.

## 8. Invariantes que os testes precisam travar

### 8.1 Autorização e negação

1. **Default deny:** permissão ausente, desconhecida, expirada ou membership revogada sempre nega.
2. **Rota declarada:** toda rota tem metadata explícita; toda rota com `{empresa_id}` tem `recurso.ação`; método mutável não pode declarar só `.read`.
3. **Catálogo fechado:** permissão de rota, papel ou override precisa existir em `auth_permissions`.
4. **Precedência:** grants do papel + `allow`, com `deny` vencendo; override expirado não altera o resultado.
5. **Sem escalada:** admin não promove owner nem concede permissão que sua política proíbe; contador nunca gerencia membership por conhecer o endpoint.
6. **Último owner:** duas requests concorrentes não conseguem desativar/rebaixar os últimos owners e deixar o tenant órfão; a checagem usa lock/constraint transacional.
7. **CSRF continua obrigatório:** autorização válida não substitui CSRF em mutações por cookie.
8. **Nega sem efeito colateral:** 401/403 não altera domínio, não cria job e não grava audit log de sucesso.

### 8.2 Isolamento de tenant/empresa

Para **cada** rota de empresa, parametrizar:

- usuário de outro tenant com UUID conhecido;
- admin/owner de outro tenant;
- usuário do mesmo tenant sem membership;
- membership na empresa A tentando path da empresa B;
- path da empresa A com `conta_id`, `transacao_id`, `lancamento_id`, `job_id`, `nota_id` ou outro filho da empresa B;
- usuário/tenant/empresa inativo;
- tentativa de criar membership cruzando tenant diretamente no serviço e no banco.

Todos os caminhos devem negar sem revelar a existência do alvo. As FKs compostas precisam rejeitar vínculo cross-tenant mesmo fora da aplicação. Em operações com vários IDs, **cada pai e filho** deve pertencer à mesma empresa.

### 8.3 Sessões e ciclo de vida

- convite/reset é hash-only, uso único, expira e é invalidado por reenvio;
- aceite concorrente do mesmo token tem um vencedor;
- usuário `pending/suspended/disabled` não faz login, refresh nem request autenticada;
- troca/reset/desativação incrementa versão e invalida access/refresh anteriores;
- reativação não ressuscita token antigo;
- revogar uma empresa preserva acesso às outras;
- alteração de papel/override passa a valer na request seguinte;
- token e senha nunca aparecem em resposta posterior, log ou auditoria.

### 8.4 Migração e compatibilidade

- snapshot de todas as permissões legadas antes do backfill deve produzir a mesma decisão depois, rota por rota;
- `"*"` preserva todas as capacidades atuais, inclusive os quatro módulos ausentes da whitelist;
- CSV explícito não ganha módulo/ação que não possuía;
- dual-write é atômico: falha de um lado reverte ambos;
- shadow-read registra divergência sem mudar decisão;
- rollback/feature flag volta ao avaliador legado enquanto a tabela ainda existe;
- migrations rodam em PostgreSQL real e `alembic check` fica limpo.

### 8.5 Auditoria

- mudança e audit log entram juntos; rollback remove ambos;
- antes/depois, ator, alvo, tenant/empresa, motivo e trace estão corretos;
- concessão em lote tem correlação e um detalhe por alvo;
- segredos não entram em `dados_antes`, `dados_depois` ou logs;
- aplicação não consegue atualizar/apagar `audit_logs` com a credencial normal.

## 9. Plano incremental e ordem de deploy

### 9.1 Sem migration

1. Criar catálogo tipado de `recurso.ação` no código e a matriz método/endpoint.
2. Implementar `AuthorizedAPIRouter` e `require_permission` com adaptador para o CSV atual.
3. Declarar todas as rotas; primeiro rodar o verificador em modo relatório, depois fazê-lo falhar em teste/startup.
4. Corrigir a whitelist legada dos quatro módulos para fechar o buraco durante a transição.
5. Adicionar a matriz de negação/isolamento e o teste de introspecção de rotas.
6. Auditar criação, desativação e reativação de usuário; ao desativar, revogar em lote os refresh tokens existentes.
7. Instalar CI em `.github/workflows` antes de tornar o guard de startup obrigatório.

**O que quebra se inverter:** ativar o guard antes de declarar todas as rotas impede a aplicação de subir; remover a inferência por path antes de o adaptador cobrir todas as rotas abre ou bloqueia módulos; confiar em testes sem instalar CI mantém o mesmo risco atual.

### 9.2 Exige migration aditiva

1. Adicionar constraints compostas, colunas de estado/versão e tabelas de catálogo, membership, overrides e tokens.
2. Semear permissões/papéis legados.
3. Fazer backfill de usuário e `Permissao`, gerar relatório de equivalência e promover owner explicitamente por tenant.
4. Só depois colocar `NOT NULL` nas colunas preenchidas e habilitar a regra do último owner.

**O que quebra se inverter:** código que consulta tabela antes da migration responde 500; `NOT NULL` antes do backfill falha o deploy; escolher owner automaticamente pode entregar poder à pessoa errada; aplicar papéis novos diretamente pode conceder mais do que o CSV antigo.

### 9.3 Dual-write e shadow-read

1. Deploy do backend que escreve legado e novo na mesma transação.
2. Frontend passa a mostrar papel + overrides, ainda usando endpoints compatíveis.
3. Avaliar os dois modelos em toda request; autorizar pelo legado e medir divergências.
4. Corrigir dados/mapeamento até divergência zero por um ciclo operacional completo.

**O que quebra se inverter:** backfill sem dual-write envelhece enquanto administradores mudam acesso; frontend novo antes do backend não salva a intenção; shadow autorizando pelo modelo novo antes de zerar divergência produz 403 ou escalada silenciosa.

### 9.4 Corte do avaliador

1. Autorizar pelo modelo novo e manter fallback legado por feature flag de emergência.
2. Ativar diferenças de ação: primeiro `read` × mutação, depois `execute/manage` nas operações sensíveis.
3. Normalizar memberships `legacy_*` para os quatro papéis reais, com revisão por owner/admin.
4. Monitorar 403, divergências, jobs e chamados; retirar fallback somente após estabilidade.

**O que quebra se inverter:** exigir `execute/manage` antes de semear grants e atualizar UI paralisa classificação/importação/cancelamento; retirar fallback no mesmo release elimina saída segura; converter `legacy_custom` automaticamente para papel amplo pode escalar acesso.

### 9.5 Convite, senha e sessão

1. Backend de convite/aceite/reset com tokens hash-only e rate limit.
2. Integração de e-mail e tela; fallback de link único apenas enquanto necessário.
3. Emitir `session_version` nos novos JWTs; durante uma janela, aceitar token antigo como versão 1.
4. Depois que os access tokens antigos expirarem, tornar o claim obrigatório e ativar invalidação global em troca/reset/desativação.

**O que quebra se inverter:** exigir claim antes de todos os tokens antigos expirarem desloga todos sem aviso; enviar convite antes de o consumo existir cria usuário preso em `pending`; habilitar reset sem rate limit facilita abuso e enumeração.

### 9.6 Migration de contração

Após pelo menos um ciclo operacional sem fallback:

1. tornar CSV somente leitura e comparar mais uma vez;
2. remover `Permissao.modulos`/tabela legada e código de inferência por path;
3. remover `Usuario.role`/`ativo` depois de todos os consumidores usarem `tenant_role`/`status`;
4. eliminar papéis `legacy_full`/`legacy_custom` somente quando não houver membership neles.

**O que quebra se inverter:** remover qualquer dado legado antes do fim do fallback torna rollback impossível; remover os papéis legados com memberships restantes viola FK ou perde acesso.

## 10. Decisão final

Adotar **papéis fixos por empresa + overrides**, com papel tenant separado, e autorização **declarativa por rota em recurso × ação**. Entregar em estratégia expand/dual-write/shadow/cut/contract; não introduzir workflow de aprovação fictício. A prioridade é tornar leitura, mutação e execução financeira distinguíveis, negar por padrão e provar isolamento em PostgreSQL/CI antes de remover o modelo legado.
