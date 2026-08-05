# CHANGELOG — Contabil Core

Registro do que muda a cada entrega. As mudanças que alteram **comportamento
visível** vêm primeiro, porque são as que exigem aviso a quem usa o sistema.

Decisões arquiteturais ficam no [`DECISIONS.md`](DECISIONS.md). Aqui é o "o quê";
lá é o "por quê".

---

## 2026-08-04 — em produção

46 arquivos, +3.920 / −199. Suíte de testes: **234 → 371**.

### ⚠️ Mudanças de comportamento

Estas alteram o que o sistema já fazia. Vale avisar o escritório antes que
apareçam sozinhas.

| O que | Antes | Agora |
|---|---|---|
| **ConcilPro sem login** | Os 9 endpoints respondiam abertos na internet, incluindo o export em Excel com nome, CNPJ e saldo de todos os fornecedores | **401** |
| **Exportar notas de empresa com CNPJ inválido** | Arquivo **vazio, sem erro** — parecia "não teve nota no período" | **422** explicando que o CNPJ do cadastro não é válido |
| **Cadastrar empresa com CNPJ de dígito verificador errado** | Aceito | **422** |
| **Upload acima de 25 MB** | Aceito, carregado inteiro em memória | **413** |
| **`GET /api/health` com o banco fora** | `{"status":"ok"}` — dizia que estava tudo bem | **503** |

> A mudança da exportação de notas afeta **72 das 77 empresas**, que estão
> cadastradas com CNPJ sintético gerado na migração do MrContador. O arquivo
> vazio já estava errado; a diferença é que agora o erro aparece. Ver
> "Pendências conhecidas".

### ✨ Novidades

- **Exportação do extrato bancário** — tipo `extrato` em
  `POST /empresas/{id}/exportacao/gerar`, em CSV ou XLSX. Colunas: data, agência,
  histórico, D/C, valor e status. Respeita os filtros de agência, status e
  período da tela; **não** respeita a paginação, de propósito — o relatório traz
  o período inteiro, não só a página aberta.
- **`GET /empresas/cnpj-invalidos`** — lista as empresas cujo CNPJ não passa na
  validação de dígito verificador. Até então esse problema era invisível.
- **`GET /api/health/live`** — liveness separado, que não toca no banco. É o novo
  alvo do `HEALTHCHECK` do Docker.
- **Health check com SHA do commit** — campo `commit`, injetado como build arg,
  para "meu deploy subiu?" virar um `curl`.
- **`conciliar_cnpj.py`** — propõe o CNPJ real cruzando com os razões já
  processados pelo ConcilPro. Dry-run por padrão. Só propõe quando o nome casa
  com exatamente uma empresa e um CNPJ.
- **`listar_cnpj_para_conferencia.py`** — gera a planilha de trabalho para o
  escritório preencher. Somente leitura.
- **`scripts/criar_usuario_restrito.sql`** — cria o papel Postgres da aplicação,
  para tirar a API de cima do superusuário. Idempotente, com verificação
  embutida. Testado num banco descartável, não em produção.

### 🔒 Segurança

- **ConcilPro passou a exigir autenticação.** A proteção é declarada no
  **router**, não rota a rota: rota nova nasce protegida em vez de depender de
  alguém lembrar de anotá-la.
- **Limite de upload em duas barreiras** (ADR-011). Middleware corta pelo
  `Content-Length`; a contagem real cobre quem não declara tamanho
  (`Transfer-Encoding: chunked`). Todos os parsers carregam o arquivo inteiro em
  memória — o limite é o que separa um upload grande de derrubar o worker.
- **Levantamento do alcance da credencial de produção.** Medido: o
  `DATABASE_URL` do contabil-core é superusuário e alcança **11 bancos** do mesmo
  cluster. O SQL do papel restrito corta isso; falta aplicar.

### 🐛 Correções

- **NEO classificava a mesma transação em contas diferentes entre execuções.**
  `_carregar_regras` não tinha `ORDER BY`, e nas estratégias substring e prefixo
  a primeira regra que casasse vencia — a ordem era a que o Postgres devolvesse.
  Desempate agora: regra mais específica (histórico mais longo), depois `id`.
- **ConcilPro parou de fabricar lançamento** (ADR-013). `_recuperar_lancamentos_ocultos`
  sintetizava entradas por análise de salto de saldo quando os totais não
  fechavam. Não existe coluna `sintetico` em `cp_lancamento`: o lançamento
  inventado entrava na conciliação FIFO como nota real e saía na exportação,
  indistinguível de um verdadeiro. Foi a origem do falso positivo de
  R$ 24.029,28 a pagar numa conta zerada.
- **Nome do arquivo exportado** saía `registros_...` para qualquer tipo,
  inclusive notas e conferência. Agora usa o tipo da exportação.
- **Fixtures de teste usavam CNPJs inválidos** (`12.345.678/0001-90` e outros).
  Trocadas por CNPJs com dígito verificador correto.

### 🧰 Internos

- **`src/core/cnpj.py`** — validação de dígito verificador, com rejeição de
  sequências de dígito repetido.
- **`src/db/functions.py`** — expressão `mes_ano()` traduzida por dialeto. O
  domínio `stats` usava `func.to_char`, exclusivo do PostgreSQL, e por isso era
  **impossível** de testar na suíte, que roda em SQLite. O SQL de produção não
  mudou: há teste de compilação por dialeto garantindo.
- **Cobertura de testes** nos seis domínios que não tinham nenhum:

  | Domínio | Testes | Foco |
  |---|---|---|
  | `cartoes` | 23 | Fatura paga imutável, recálculo do total, CSV em formato BR/EN |
  | `permissoes` | 20 | Efeito real da permissão no acesso aos dados, não só o CRUD |
  | `comprovantes` | 16 | Vínculo com transação, soft delete, content-type do anexo |
  | `openbanking` | 15 | Dedup na re-sincronização, criação automática de agência |
  | `relatorios` | 12 | Sinal de saldo por natureza da conta, identidade do balancete |
  | `stats` | 9 | Percentual de conciliação, isolamento entre empresas |

- **ADR-011** (limite de upload), **ADR-012** (liveness vs readiness) e
  **ADR-013** (não fabricar lançamento) no `DECISIONS.md`.

### 📌 Exige ação no deploy

- Passar `--build-arg GIT_COMMIT=$(git rev-parse --short HEAD)` no build. Sem
  isso o campo `commit` fica `unknown` — **é o que está acontecendo hoje em
  produção**.
- Confirmar que o orquestrador monitora `/api/health/live`, e não `/api/health`.
  O segundo agora devolve 503 quando o banco cai, e um painel apontado para ele
  pode passar a reiniciar container por indisponibilidade de banco.

### 📋 Pendências conhecidas

- **72 empresas com CNPJ placeholder.** A origem é
  [`scripts/import_mrcont.py`](scripts/import_mrcont.py), que nunca leu CNPJ de
  fonte alguma: a razão social vem do nome da pasta e o CNPJ é sintetizado do
  índice. Só 2 são recuperáveis automaticamente; as outras 70 precisam vir de
  contrato, certificado A1 ou do sistema de origem. **Enquanto não forem
  corrigidas, a exportação de notas dessas empresas falha.**
- **ConcilPro fora do modelo multi-tenant.** As tabelas `cp_*` não têm FK para
  `empresas` nem `tenants`; qualquer usuário autenticado vê os arquivos de todos
  os clientes. Bloqueado pelos CNPJs — o backfill só casa 1 dos 6 arquivos.
- **Papel Postgres restrito não aplicado** em produção, e a porta 3308 segue
  aberta na internet.
- **Comprovantes gravados em base64** dentro do Postgres. Infla ~33%, entra em
  todo backup e impede servir por CDN.

### 🔎 Nota sobre versionamento

O repositório não tem tags e o `app_version` do código (`0.1.0`) não bate com o
que produção reporta (`1.0.0`, vindo da variável de ambiente). Enquanto isso não
for reconciliado, as entradas aqui são identificadas por data.

---

### Em conjunto, no `contabil-front`

Mudanças que dependem das acima para funcionar:

- **Import do ConcilPro aceita Excel.** O backend já aceitava `.xlsx`; o front é
  que travava em `.pdf`/`.zip`. O texto passou a recomendar Excel — a planilha
  declara o que o PDF obriga a inferir, então o parsing é determinístico e não
  usa IA.
- **Coluna "Conta" mostra o código reduzido** (`1667`) no lugar da classificação
  completa (`2.1.3.01.0002`). As duas continuam no export em Excel.
- **Status `SEM_MOVIMENTO`** ganhou rótulo, badge e filtro. Tem card próprio no
  resumo: somá-lo aos quitados inflaria a métrica com contas que nunca tiveram
  lançamento.
- **Botão "Exportar" no extrato bancário**, repassando os filtros da tela.
