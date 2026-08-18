# Handoff para o `contabil-front` — demandas do PDF de agosto/2026

**Este documento vive no `contabil-core` (backend) mas descreve trabalho a ser feito no `contabil-front`** (github.com/dev41tech/contabil-front), repositório separado. Nada aqui deve ser implementado neste repo — é a especificação para levar para uma sessão no `contabil-front`.

Contexto: as mudanças de backend que viabilizam isso já estão prontas e mergeadas via PR (branch `feat/demandas-pdf-neo-agosto-2026`). Este handoff cobre 3 frentes de UI, em ordem de prioridade sugerida.

---

## 1. Tela de cadastro de Contrapartes (nova — prioridade alta)

### Por quê
O backend já tem um cadastro completo de fornecedores/clientes (por CNPJ/CPF, com conta contábil padrão), mas **não existe nenhuma tela para usá-lo ainda**. Sem essa tela, o cadastro fica vazio e as próximas automações (identificação automática de fornecedor, histórico automático) não têm o que usar quando forem ativadas.

### Contrato da API

Base: `/api/v1/empresas/{empresa_id}/contrapartes`

| Método | Rota | Uso |
|---|---|---|
| GET | `` | Lista, com filtros |
| GET | `/{id}` | Detalhe |
| POST | `` | Criar (precisa header `X-CSRF-Token`) |
| PATCH | `/{id}` | Editar (precisa CSRF) |
| DELETE | `/{id}` | Remover — soft delete (precisa CSRF) |

**Filtros da listagem** (query params, todos opcionais): `termo` (busca por razão social, nome fantasia ou documento — com ou sem máscara), `tipo` (`fornecedor`/`cliente`/`ambos`), `apenas_ativas` (bool), `page`, `page_size`.

**Campos do formulário de criação/edição:**

| Campo | Tipo | Obrigatório | Observação |
|---|---|---|---|
| `tipo` | select: fornecedor / cliente / ambos | sim | |
| `documento` | texto | sim (só na criação) | Aceita CPF ou CNPJ, com ou sem máscara — o backend normaliza. Não pode editar depois de criado. |
| `razao_social` | texto | sim | |
| `nome_fantasia` | texto | não | |
| `conta_contabil_id` | select (buscar em `/plano-contas`) | sim | A conta precisa pertencer à mesma empresa — o backend valida e retorna 422 se não pertencer |

**Resposta** (list e detail) inclui também, só leitura: `conta_codigo`, `conta_descricao` (dados da conta já expandidos, não precisa buscar separado), `origem` (sempre `"manual"` por enquanto — pode até esconder esse campo da UI), `confirmado_em`, `ativa`.

### Comportamento esperado na tela
- Listagem com busca por texto (campo único, busca nome/CNPJ ao mesmo tempo) + filtro por tipo + toggle "só ativas".
- Criar: formulário simples. Se o documento já existir numa contraparte ativa, a API retorna `409` com mensagem — mostrar como erro de validação amigável ("Já existe uma contraparte ativa com este documento").
- Editar: mesmo formulário, sem o campo documento (é fixo após criado).
- Desativar: em vez de excluir, usar `PATCH { ativa: false }` — preferir isso a excluir de fato. O documento de uma contraparte desativada fica livre para recadastro.
- Excluir (DELETE): oferecer como ação secundária/menos visível — é soft delete no backend, mas do ponto de vista do usuário deve ser tratado como "remover mesmo".

---

## 2. Busca e filtro na tela do NEO (item 4 do PDF — prioridade alta)

### Por quê
Pedido direto dos contadores: "seria possível colocar no NEO uma opção de busca/filtro?"

### Contrato da API (já existente, agora estendido)

`GET /api/v1/empresas/{empresa_id}/neo/decisoes`

Novos query params, todos opcionais e combináveis:

| Param | Valores | Busca em |
|---|---|---|
| `termo` | texto livre | Histórico do extrato bancário OU descrição da regra aplicada |
| `resultado` | `associada` \| `sem_regra` \| `erro` | (já existia) |
| `estrategia` | `exato` \| `substring` \| `prefixo` \| `manual` | Como o NEO decidiu |
| `dc` | `D` \| `C` | Débito ou crédito |
| `agencia_id` | UUID | Agência bancária da transação |
| `conta_id` | UUID | Conta contábil usada na classificação |
| `mes` | `AAAA-MM` | Competência (mês da transação) |

A resposta agora inclui `page` e `page_size` junto com `items`/`total` (antes só tinha `items`/`total`).

### Comportamento esperado na tela
- Campo de busca por texto (`termo`) — o mais importante, é o que resolve a maior parte dos casos de uso ("achar aquele lançamento").
- Filtros secundários (podem ficar num painel expansível/"filtros avançados" para não poluir a tela): resultado, estratégia, débito/crédito, agência, conta, mês.
- Debounce no campo de busca (não disparar request a cada tecla).
- Ao aplicar filtro, resetar para `page=1`.
- Mostrar contagem total (`total`) e paginação.

---

## 3. Arrastar e soltar comprovantes (item 5 do PDF — prioridade média)

### Por quê
Pedido direto: permitir arrastar um ou vários arquivos de comprovante direto na tela, em vez de selecionar um por um.

### Importante: não há endpoint novo pra isso
O backend continua aceitando **um arquivo por vez**. O fluxo de upload de comprovante já é em duas etapas e continua assim:

1. `POST /api/v1/empresas/{empresa_id}/comprovantes/extrair-pdf` — multipart, campo `arquivo` (PDF, PNG ou JPG). Só extrai os dados (favorecido, CNPJ/CPF, valor, datas) para pré-preencher o formulário — **não salva nada**. Limite de tamanho: 25MB por arquivo (configurável no backend, mas esse é o padrão).
2. `POST /api/v1/empresas/{empresa_id}/comprovantes` — JSON com os campos (revisados pelo usuário ou não) + `arquivo_nome`/`arquivo_base64` — esse sim persiste o comprovante.

### Comportamento esperado na tela
- Área de drop que aceita múltiplos arquivos de uma vez (PDF/PNG/JPG).
- Para cada arquivo solto, disparar o fluxo de extração (`/extrair-pdf`) individualmente.
- **Limitar concorrência** — não disparar todos os uploads ao mesmo tempo se o usuário soltar muitos arquivos; usar uma fila com 2–4 uploads simultâneos no máximo, para não sobrecarregar o processamento de PDF (que já é pesado no backend, síncrono).
- Mostrar progresso/status por arquivo individualmente (extraindo → revisar → salvo / erro), não um progresso único para o lote.
- Falha em um arquivo não deve travar os outros — tratamento de erro por arquivo.
- Depois da extração, o usuário revisa os campos (fluxo que já existe hoje) e confirma o salvamento — isso não muda.
- Se o formulário de revisão hoje só suporta 1 comprovante por vez na tela, decidir: revisar um de cada vez em fila, ou um formulário por card/arquivo simultaneamente. Fica a critério de quem implementar, conforme o padrão de UI já usado no restante do app.

Não é necessário (nem recomendado agora) criar um endpoint de upload em lote no backend — isso só valeria a pena se, na prática, o padrão de fila com concorrência limitada se mostrar um gargalo real de UX.

---

## O que NÃO precisa de mudança de frontend agora

- **Identificação automática de fornecedor/conta e histórico automático** (itens 1 e 2 do PDF) — o backend calcula isso em segundo plano ("shadow mode"), mas não aplica nada ainda. Não há nada visível pra construir até isso ser ativado de fato (fase futura, decisão pendente do lado do backend).
- Nenhuma tela existente precisa ser removida ou ter contrato quebrado — todas as mudanças de API foram aditivas.
