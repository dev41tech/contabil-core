# Auditoria estática — Codex, 06/08/2026

Auditoria estática e exclusivamente de leitura, feita por uma sessão do Codex via
MCP dentro deste repositório em 06/08/2026 (19:29 UTC). Nenhum arquivo foi alterado,
teste executado ou migration aplicada durante a auditoria em si.

**Status: todos os 40 achados abaixo foram corrigidos e mergeados em `main`** em
07-08/08/2026, em 7 branches/clusters de correção paralela + integração serial.
Ver histórico de commits a partir de `fix(auth): isola tenant/empresa por padrao...`
até `fix(hardening): valores monetarios em Decimal...` para o código de cada correção.

## Crítico

### C1 — IDOR permite acesso e concessão de permissões entre tenants — confirmado

**Arquivo/trecho:** `src/api/deps.py:114-123`

```python
if ctx.role != "admin":
    select(Permissao).where(
        Permissao.usuario_id == ctx.user_id,
        Permissao.empresa_id == empresa_id,
    )
```

`src/domain/permissoes/service.py:42-48,68-89` consulta e cria permissões somente por
`empresa_id`, sem confirmar `Empresa.tenant_id`.

**Bug:** admins pulam toda validação da empresa; contadores são autorizados por uma
permissão que também não carrega tenant. Um admin pode ainda conceder a um usuário do
próprio tenant uma permissão sobre empresa de outro tenant.

**Cenário:** conhecendo o UUID de uma empresa do tenant B, um admin do tenant A acessa
qualquer endpoint `/empresas/{empresa_id}/...` ou concede esse acesso a um contador.
Notas, extratos, contas, relatórios e Open Banking ficam expostos ou alteráveis.

**Correção:** sempre carregar `Empresa` com `Empresa.id == empresa_id`,
`Empresa.tenant_id == ctx.tenant_id` e `Empresa.ativa == True`, validando também os
tenants nas operações de permissão.

### C2 — ConcilPro é global para todos os usuários autenticados — confirmado

**Arquivo/trecho:** `src/api/v1/concilpro.py:43-49`

```python
# as tabelas cp_* ainda não têm empresa_id
router = APIRouter(..., dependencies=[Depends(require_auth)])
```

Consultas como `select(ArquivoImportado)` e `select(Fornecedor)` aparecem sem qualquer
escopo em `src/api/v1/concilpro.py:274,389,406,427,434,498,662,684`. Os modelos em
`src/db/models.py:591-723` e a migration `0007_add_concilpro.py:21-123` não têm
`tenant_id` nem `empresa_id`.

**Bug:** autenticação substituiu anonimato, mas não implementou isolamento. O hash do
arquivo também é global e o fluxo de reprocessamento apaga fornecedores, lançamentos e
conciliações do registro encontrado em `concilpro.py:303-340`.

**Cenário:** qualquer usuário de qualquer escritório lista arquivos, nomes, CNPJs e
saldos de fornecedores de todos os demais; sabendo ou obtendo um ID, exporta os dados.
Um reprocessamento do mesmo hash pode apagar e reconstruir dados pertencentes a outro
escritório.

**Correção:** migrar todas as tabelas `cp_*` para um escopo obrigatório de
tenant/empresa, propagar esse filtro a todas as queries e tornar o hash único por
tenant/empresa.

### C3 — NEO pode gerar múltiplos registros contábeis para a mesma transação — confirmado

**Arquivo/trecho:** `src/domain/neo/engine.py:329-345`

```python
select(Transacao).where(
    Transacao.empresa_id == self._empresa_id,
    Transacao.status == "pendente",
)
```

Não há lock. `_registrar_match` insere o registro e só muda o objeto em memória em
`engine.py:145-177`. `RegistroContabil.transacao_id` não é único em
`src/db/models.py:279-299`.

Além disso, uma transação sem regra permanece pendente e recebe outra `NeoDecisao` a
cada execução (`engine.py:178-188`). A associação manual valida somente
`decisao.resultado` e cria outro registro sem verificar `transacao.status`, em
`src/api/v1/neo.py:124-173`.

**Bug:** há duas rotas independentes para duplicação: concorrência entre execuções do
NEO e múltiplas decisões `sem_regra` associadas manualmente.

**Cenário:** duas chamadas simultâneas a `/neo/processar` leem a mesma transação
pendente e inserem dois lançamentos. Alternativamente, executar o NEO três vezes sem
regra cria três decisões; associar cada uma manualmente contabiliza a mesma
movimentação três vezes. O endpoint manual também aceita `body.conta_id` sem confirmar
que a conta pertence à empresa (`neo.py:150-160`).

**Correção:** serializar a transição com `FOR UPDATE SKIP LOCKED`/update condicional,
tornar a decisão pendente idempotente e criar uma chave única de idempotência coerente
com os dois lados do lançamento.

### C4 — ConcilPro calcula saldo e contas a pagar ignorando o tipo e o saldo anterior — confirmado

**Arquivo/trecho:** `src/api/v1/concilpro.py:156-169,208-225`

```python
saldo_final = saldo_anterior + total_credito - total_debito
forn.valor_a_pagar = forn.total_credito - forn.total_debito
```

**Bug:** `saldo_anterior_tipo` é armazenado, mas não participa do sinal do saldo.
`valor_a_pagar` exclui completamente o saldo anterior, e contas sem movimento são
classificadas como `SEM_MOVIMENTO` mesmo que carreguem saldo de abertura.

**Cenário:** fornecedor com saldo credor anterior de R$ 10.000 e nenhum movimento
aparece com `valor_a_pagar = 0`. Um saldo anterior devedor também é somado como se
fosse credor.

**Correção:** normalizar o saldo anterior para valor assinado conforme D/C e derivar
saldo final, status e valor a pagar da mesma equação contábil.

### C5 — Endpoint público de bootstrap permite tomada do sistema — confirmado

**Arquivo/trecho:** `src/api/v1/setup.py:1-4,46-83`

```python
# Desabilitar em produção via ENABLE_SETUP_ENDPOINT=false.
total = select(func.count()).select_from(Tenant)
if total == 0:
    ... cria tenant e admin ...
```

Entretanto, não existe `ENABLE_SETUP_ENDPOINT` em `src/core/config.py`, e o router é
sempre incluído em `src/api/v1/__init__.py:27`.

**Bug:** numa instalação nova, qualquer pessoa pode ser o primeiro solicitante e criar
o admin. O `COUNT` e a criação também não são serializados; duas requisições
simultâneas podem criar dois tenants/admins.

**Cenário:** durante o primeiro deploy, um scanner chama `/api/v1/setup` antes do
operador e assume controle administrativo.

**Correção:** montar a rota somente quando uma flag explícita e um segredo de
bootstrap estiverem configurados, com garantia transacional de execução única.

### C6 — `migrate_export.py` funde todos os tenants em um único tenant de produção — confirmado

**Arquivo/trecho:** `migrate_export.py:24-37,46-61`

```python
SELECT * FROM empresas
SELECT * FROM plano_contas
d["tenant_id"] = TENANT_ID_PROD
```

**Bug:** o script descobre o primeiro tenant local, mas não usa esse ID como filtro.
Todas as empresas do banco são exportadas, e seus `tenant_id` são sobrescritos por um
único UUID.

**Cenário:** executar o fluxo em uma base local com dados de vários escritórios
importa todos os clientes para um único tenant de produção, causando vazamento e
mistura permanente.

**Correção:** exigir tenant de origem e destino por argumento, filtrar todas as
entidades pela origem e abortar se surgir qualquer entidade fora desse escopo.

## Alto

### A1 — Permissões por módulo não são aplicadas e rotas de empresas ignoram permissões — confirmado

**Arquivo/trecho:** `src/api/deps.py:114-123` apenas verifica a existência da linha
`Permissao`; nunca lê `Permissao.modulos`. Já `src/api/v1/empresas.py:60-94` usa
`Depends(require_auth)` até para criar, alterar e desativar empresas.

**Bug:** uma permissão `"extrato"` concede na prática acesso a notas, regras,
contabilidade, exportação, cartões, Open Banking e relatórios. Um contador pode ainda
listar todas as empresas do escritório e modificar qualquer uma.

**Cenário:** contador autorizado somente para extrato de uma empresa acessa seu Open
Banking ou desativa outra empresa do mesmo escritório.

**Correção:** tornar a dependência consciente do módulo/ação e restringir gestão de
empresas a admin, retornando aos contadores apenas empresas explicitamente permitidas.

### A2 — Tenants e empresas desativados continuam plenamente utilizáveis — confirmado

**Arquivo/trecho:** os campos existem em `src/db/models.py:62` (`Tenant.ativo`) e
`:139` (`Empresa.ativa`), mas login consulta apenas `Usuario.ativo` em
`src/domain/auth/service.py:42-46`; `get_company_context` nem sequer carrega a
empresa.

**Bug:** desativar uma empresa apenas muda um booleano em
`src/domain/empresas/service.py:139-142`; nenhum domínio impede novas importações ou
alterações. O mesmo ocorre com um tenant desativado.

**Cenário:** um escritório cancelado ou uma empresa encerrada continua entrando no
sistema e importando/expondo dados financeiros.

**Correção:** validar tenant e empresa ativos na autenticação/contexto central, não
individualmente em cada service.

### A3 — Login não possui rate limiting efetivo — confirmado

**Arquivo/trecho:** `src/core/config.py:77-78` declara limites, mas
`src/api/app.py:112-120` instala somente CORS, contexto e limite de upload. Não há
outro uso dos campos no repositório.

**Bug:** `/auth/login` é público e executa bcrypt sem limitação. Para usuário
inexistente, bcrypt nem é executado, permitindo também enumeração temporal de e-mails.

**Cenário:** atacante com tenant obtido pelo endpoint público `/auth/tenant` realiza
brute force ou satura os workers com tentativas de senha.

**Correção:** aplicar rate limit distribuído por IP, tenant e identidade, igualando o
custo temporal de usuários existentes e inexistentes.

### A4 — Refresh token de uso único pode ser reutilizado concorrentemente — confirmado

**Arquivo/trecho:** `src/domain/auth/service.py:79-108`

```python
select(RefreshToken).where(jti == jti, revogado == False)
...
update(RefreshToken).where(jti == jti).values(revogado=True)
```

**Bug:** a leitura e a revogação não usam lock nem update condicional. Duas
requisições podem validar o mesmo token antes de qualquer uma revogá-lo e ambas
emitirem novas sessões.

**Cenário:** token roubado e navegador legítimo fazem refresh simultâneo; ambos
recebem refresh tokens válidos.

**Correção:** consumir o token com `UPDATE ... WHERE revogado=false RETURNING`,
aceitando a rotação somente quando exatamente uma linha for alterada.

### A5 — Autoassociação do NEO pode reutilizar o mesmo comprovante/nota e ignora direção financeira — confirmado

**Arquivo/trecho:** `src/domain/neo/engine.py:218-247,270-299` filtra
`transacao_id IS NULL`, mas a sessão usa `autoflush=False`
(`src/db/session.py:38-44`) e só há flush após processar todas as transações
(`engine.py:91`).

**Bug:** uma associação feita na primeira iteração continua `NULL` no banco para a
query seguinte. O mesmo objeto pode ser escolhido novamente e terminar ligado somente
à última transação, embora o contador da resposta informe várias associações.
Comprovante de pagamento também pode ser ligado a uma transação de crédito porque `dc`
não é considerado.

**Cenário:** dois débitos de R$ 1.000 na janela de três dias disputam um comprovante;
ambos são contados como associados, mas o último sobrescreve o primeiro. Um crédito do
mesmo valor pode receber o comprovante indevidamente.

**Correção:** manter IDs consumidos em memória, fazer flush/lock por associação e
incluir direção e natureza do documento nos critérios.

### A6 — Registro contábil é unilateral; o balancete não pode fechar — confirmado

**Arquivo/trecho:** `src/domain/neo/engine.py:152-165` cria somente um
`RegistroContabil` com `dc=regra.dc`. O balancete soma apenas essa tabela em
`src/domain/relatorios/service.py:165-178,195-236`.

**Bug:** não existe a contrapartida da conta bancária nem entidade de lançamento com
lotes balanceados.

**Cenário:** despesa de R$ 100 produz débito na despesa, sem crédito no banco;
`total_debitos != total_creditos`, portanto o "Balancete de Verificação" não verifica
partidas dobradas.

**Correção:** modelar um lançamento com pelo menos duas partidas cuja soma de débitos
seja igual à de créditos, persistidas atomicamente.

### A7 — Edição/remoção do plano de contas reescreve ou oculta histórico — confirmado

**Arquivo/trecho:** apesar de o comentário declarar código imutável,
`src/domain/plano_contas/service.py:134-149` altera `codigo`, `tipo` e `tipo_sa` sem
revalidar pai/filhos. A remoção verifica somente filhos e regras ativas
(`:157-188`), não `RegistroContabil`. Relatórios excluem contas removidas
(`src/domain/relatorios/service.py:95-107,188-203`).

**Bug:** renomear código quebra a hierarquia; mudar `tipo` reclassifica
retroativamente todo o histórico; remover conta já movimentada faz seus registros
desaparecerem de DRE e balancete.

**Cenário:** conta de despesa com R$ 500 mil históricos é alterada para receita ou
removida após a regra ser desativada; relatórios de períodos já encerrados mudam.

**Correção:** tornar identidade/natureza imutáveis ou versionadas e proibir remoção
enquanto houver qualquer referência financeira.

### A8 — XML fiscal é aceito sem vínculo com a empresa, protocolo ou assinatura — confirmado

**Arquivo/trecho:** `src/domain/notas/xml_parser.py:192-255,274-324` procura tags e
converte valores; `parse_nota_xml` usa apenas `ET.fromstring` em `:329-346`.
`src/domain/notas/service.py:177-200` persiste o resultado sem carregar a empresa.

**Bug:** não há comparação do CNPJ emitente/destinatário com o CNPJ da empresa,
validação do `cStat`/protocolo de autorização, chave de 44 dígitos ou assinatura XML.

**Cenário:** usuário envia XML fabricado ou nota de empresa alheia, que entra nos
relatórios e exportações como documento fiscal válido.

**Correção:** exigir que a empresa seja parte da operação e validar chave,
protocolo/status e assinatura antes de considerar o XML fiscalmente confiável.

### A9 — Identidade de notas causa duplicação de NFS-e e bloqueio entre tenants — confirmado

**Arquivo/trecho:** a duplicidade só é testada quando há chave em
`src/domain/notas/service.py:87-95`; o parser sempre retorna `chave_acesso=None` para
NFS-e em `xml_parser.py:319-325`. Para NF-e, `chave_acesso` é globalmente única em
`src/db/models.py:327-330`.

**Bug:** reimportar a mesma NFS-e cria outra linha. Já uma NF-e legítima que pertence
a empresas de tenants diferentes só pode ser armazenada por uma delas.

**Cenário:** retry do mesmo ZIP duplica todas as NFS-e e infla exportações; um tenant
que importe primeiro a chave de uma NF-e impede o import legítimo por outro.

**Correção:** criar identidade por empresa/tenant — chave para NF-e e uma chave
composta robusta para NFS-e — com constraints correspondentes.

### A10 — ZIP/XLSX comprimidos contornam o limite de upload — confirmado

**Arquivo/trecho:** notas usam `zf.read(nome)` sem examinar tamanho descompactado em
`src/domain/notas/service.py:209-232`. Plano e ConcilPro materializam todas as linhas
com `list(ws.iter_rows(...))` em `src/api/v1/plano_contas.py:151-164` e
`src/domain/concilpro/planilha.py:181-189`.

**Bug:** o limite de 25 MB controla apenas bytes recebidos, não quantidade de
entradas, tamanho descompactado, células ou taxa de compressão.

**Cenário:** ZIP/XLSX pequeno expande para gigabytes e encerra ou bloqueia o worker.

**Correção:** limitar entradas, páginas/células, tamanho total descompactado e razão
de compressão antes de ler o conteúdo.

### A11 — Parser OFX perde transações silenciosamente e a deduplicação não é idempotente — confirmado

**Arquivo/trecho:** `src/domain/extrato/ofx_parser.py:59-65,104-120` descarta blocos
inválidos sem registrar erro. Datas com fração, como
`20240101120000.000[-3:BRT]`, falham em `:131-140`. O hash OFX inclui FITID e também
data, valor e histórico em `src/domain/extrato/service.py:263-274`.

**Bug:** o resultado informa apenas o número de itens que sobreviveram ao parser;
transações descartadas não entram em `erros`. Alterar o memo de um FITID estável gera
outro hash. Também não há `hashes_do_lote` no caminho OFX (`service.py:52-85`),
embora haja no caminho PDF.

**Cenário:** arquivo com dez movimentos, um deles com timestamp fracionário, é
importado como nove movimentos e `erros=0`. Reexportar o mesmo FITID com memo
atualizado duplica dinheiro; FITID repetido no próprio arquivo termina em violação da
unique constraint e rollback total.

**Correção:** usar `Decimal`, aceitar o formato completo de data, contabilizar
rejeições e deduplicar exclusivamente pela identidade bancária estável dentro do banco
e do lote.

### A12 — Import de PDF bloqueia o event loop e terceiriza/aceita dados sem controle de completude — confirmado

**Arquivo/trecho:** a rota async chama diretamente `parse_pdf` em
`src/api/v1/extrato.py:49-56`. O parser usa SDK síncrono da OpenAI em
`pdf_parser.py:310-325,353-377` e percorre todas as páginas em `:381-404`. Se o regex
encontrar qualquer transação, o resultado é aceito imediatamente em `:503-508`.

**Bug:** PDF grande bloqueia o worker durante CPU, renderização e chamadas de rede.
Texto/imagens completos do extrato são enviados à OpenAI, e os primeiros 500
caracteres da resposta financeira são logados (`:327-328,376-377`). Uma extração
parcial ou alucinada é persistida sem reconciliação com totais/saldos do documento.

**Cenário:** layout parcialmente reconhecido retorna uma única linha e ignora as
demais; PDF de muitas páginas ocupa o worker e gera dezenas de chamadas pagas.
Históricos e valores ficam em logs e no terceiro contratado.

**Correção:** mover processamento para job isolado com limites de páginas/tempo/custo,
política explícita de dados e validação de completude contra saldos/totais.

### A13 — Integridade de faturas de cartão pode ser falsificada ou perdida — confirmado

**Arquivo/trecho:** `src/domain/cartoes/service.py:228-265` permite definir
`status="paga"` diretamente ou associar qualquer transação da empresa, sem comparar
valor, direção ou uso anterior. `FaturaCartao.transacao_id` não é único
(`src/db/models.py:393-418`).

A soma denormalizada é lida e sobrescrita sem lock em
`cartoes/service.py:303-324,466-476`; CSVs são inseridos sem deduplicação em
`:326-406`. `conta_id` é persistido sem validar empresa em `:310-320`.

**Bug:** transação de R$ 1 pode quitar fatura de R$ 100 mil e a mesma transação pode
quitar várias faturas. Inclusões concorrentes podem deixar `valor_total` menor que a
soma real, e reupload do CSV duplica compras.

**Cenário:** duas compras adicionadas simultaneamente ficam nas linhas, mas o último
recálculo grava um total que contém apenas uma; uma conta contábil de outro cliente
pode ser vinculada pelo UUID.

**Correção:** validar valor/direção/uso exclusivo e conta, deduplicar imports e
recalcular sob lock ou derivar o total diretamente das linhas.

### A14 — Open Banking pode fabricar movimentos e omitir contas — parte confirmada; IDOR a confirmar

**Arquivo/trecho:** `src/domain/openbanking/service.py:48-59` retorna `MockProvider`
sempre que credenciais faltam, inclusive em produção. `salvar_conexao` usa apenas
`contas[0]` em `:101-131`. `SalvarConexaoRequest.item_id` é fornecido pelo cliente
(`src/schemas/openbanking.py:17-20`) e consultado com credenciais globais em
`providers/pluggy.py:84-113`.

**Bug confirmado:** erro de configuração em produção ativa dados fictícios; itens com
múltiplas contas ignoram todas exceto a primeira.

**A confirmar:** não há vínculo local entre connect token, tenant e `item_id`. Se a
Pluggy permitir consultar qualquer item da aplicação pelo ID, um ID vazado pode ser
anexado a outro tenant.

**Cenário:** secret ausente gera transações mock que parecem reais; uma empresa com
corrente e poupança importa apenas uma delas.

**Correção:** proibir mock fora de desenvolvimento, representar cada conta externa e
comprovar no callback que o item foi criado para aquela sessão/empresa.

### A15 — Falhas no ConcilPro deixam resultado parcial ou travado — confirmado

**Arquivo/trecho:** `_visao_batch` captura exceções e retorna `None`; o agregador
ignora o batch e aceita os demais em
`src/domain/concilpro/ai_classifier.py:379-405`. O background salva todos os
fornecedores e faz `db.commit()` antes da conciliação em
`src/api/v1/concilpro.py:202-208`.

No `except`, tenta consultar e atualizar o arquivo sem `db.rollback()` e depois engole
nova falha (`concilpro.py:231-241`).

**Bug:** páginas inteiras podem sumir enquanto o arquivo termina como concluído. Se
uma falha de banco deixar a sessão inválida, a tentativa de marcar `ERRO` também falha
silenciosamente; dados já commitados permanecem parciais.

**Cenário:** falha da IA na segunda de quatro partes elimina três páginas, mas as
restantes são persistidas; erro durante conciliação deixa fornecedores parciais e
arquivo em `PROCESSANDO`.

**Correção:** exigir sucesso/completude de todos os batches e usar uma transação
atômica ou staging, sempre fazendo rollback antes de registrar o erro.

### A16 — `migrate_apply.py` termina com migração parcial e sucesso aparente — confirmado

**Arquivo/trecho:** `migrate_apply.py:39-60`

```python
conn.commit()
...
except Exception:
    conn.rollback()
    erros += len(lote)
...
print("Concluído")
```

**Bug:** cada lote é commitado independentemente; um lote com erro é inteiramente
abandonado e o script continua, sem exit code de falha. Dependentes de linhas
perdidas podem falhar nos lotes seguintes.

**Cenário:** uma empresa do lote falho não é criada, suas contas falham depois, mas o
operador recebe "Concluído" e uma produção parcialmente migrada.

**Correção:** aplicar tudo atomicamente ou registrar/repetir cada statement com
dependências e terminar com código não zero quando houver qualquer falha.

### A17 — Seeds criam admins com senha conhecida na base configurada — confirmado

**Arquivo/trecho:** `scripts/seed.py:32-61` usa `settings.database_url` e cria
`admin@contabil.dev / Admin@1234`; `scripts/seed_dev.py:20-40` cria
`admin@41contabil.com.br / Admin@123`.

**Bug:** os scripts não verificam `environment == development`, hostname ou nome do
banco.

**Cenário:** execução acidental com `.env` de produção cria um admin conhecido. Em
conjunto com C1, esse admin consegue atravessar tenants se obtiver IDs de empresas.

**Correção:** abortar fora de development/test e exigir confirmação explícita do
destino e senha aleatória.

### A18 — Scripts de plano podem aplicar dados da empresa errada por fuzzy match — confirmado

**Arquivo/trecho:** `scripts/update_plano_contas.py:107-121` aceita o primeiro
Jaccard ≥ 0,75, não o melhor, e grava/commita em `:149-189`.
`scripts/import_plano_contas_full.py:147-163` escolhe o melhor ≥ 0,75, mas também não
exige CNPJ nem trata empate.

**Bug:** nomes parecidos bastam para reescrever/importar centenas de contas; o
primeiro script depende ainda da ordem do dicionário.

**Cenário:** duas empresas com nomes semelhantes acima do limiar recebem planos
trocados, alterando classificação e natureza contábil em massa.

**Correção:** casar por identificador forte, exigir unicidade/margem de confiança e
gerar dry-run obrigatório antes da escrita.

### A19 — Exportação de "conferência" marca conciliado apenas por presença — confirmado

**Arquivo/trecho:** `src/domain/exportacao/service.py:407-414`

```python
if tem_nota and tem_comp:
    status_conc = "Conciliado"
```

**Bug:** não há comparação entre valores, direção, datas ou soma de múltiplos
documentos.

**Cenário:** transação de R$ 10, nota de R$ 100 mil e comprovante de R$ 1 ligados ao
mesmo ID são exportados como "Conciliado".

**Correção:** calcular conciliação pela igualdade das somas, direção e tolerâncias
explícitas, expondo divergência residual.

### A20 — Regras equivalentes por caixa podem escolher contas diferentes — confirmado

**Arquivo/trecho:** o contrato diz case-insensitive em
`src/domain/regras/service.py:4-6`, mas a unicidade usa
`Regra.historico == historico` em `:162-169`. O NEO compara tudo em lowercase em
`src/domain/neo/engine.py:118-134`.

**Bug:** `"PIX ACME"` e `"pix acme"` podem coexistir ligadas a contas diferentes. O
NEO considera ambas iguais e escolhe pelo desempate de ID, não pela intenção do
usuário.

**Cenário:** regra criada posteriormente para corrigir a conta nunca vence; movimentos
continuam classificados na conta antiga.

**Correção:** normalizar uma coluna de histórico para unicidade/matching ou usar
índice funcional case-insensitive.

## Médio

### M1 — Valores monetários atravessam o sistema como `float` — confirmado

**Arquivo/trecho:** modelos anotam `Mapped[float]` sobre `Numeric` em
`src/db/models.py:260,292,320,355-359,404,434`; schemas monetários usam `float`, por
exemplo `src/schemas/notas.py:18`, `cartoes.py:24,135` e `comprovantes.py:16-20`.
Relatórios convertem somas `Decimal` para float em
`src/domain/relatorios/service.py:76-80,182-186,282-308`.

**Bug:** cálculos, parsing e totais acumulados passam por IEEE-754 antes ou depois do
`NUMERIC(15,2)`.

**Cenário:** grandes quantidades de lançamentos ou valores próximos ao limite de 15
dígitos acumulam erro suficiente para alterar o arredondamento de um centavo.

**Correção:** usar `Decimal` desde o schema/parser até a serialização, com regra única
de quantização e arredondamento.

### M2 — N+1 em listagens de alta frequência — confirmado

**Arquivo/trecho:** regras fazem dois `db.get` por item em
`src/domain/regras/service.py:55-63,176-192`; contabilidade repete o padrão em
`src/domain/contabil/service.py:60-68,91-109`; cartões fazem duas queries por cartão
e uma por fatura em `src/domain/cartoes/service.py:66-100,180-190`; livro-caixa
consulta separadamente cada agência em `src/domain/relatorios/service.py:264-300`.

**Cenário:** página de 200 regras pode executar aproximadamente 402 queries.

**Correção:** usar joins/selectinload e agregações em lote.

### M3 — Filtros `data_ate` excluem quase todo o último dia e entradas inválidas viram 500 — confirmado

**Arquivo/trecho:** `src/api/v1/contabil.py:33-48` e `src/api/v1/extrato.py:73-85`
usam `datetime.fromisoformat` manualmente; services aplicam `<= data_ate` em
`contabil/service.py:52-55` e `extrato/service.py:187-190`.

**Cenário:** `data_ate=2026-08-06` vira meia-noite e exclui transações após `00:00`;
texto inválido lança `ValueError` não convertido em 422.

**Correção:** receber `date/datetime` tipado pelo FastAPI e transformar fim de dia em
limite exclusivo do dia seguinte.

### M4 — Import do plano perde hierarquia e pode deixar a sessão quebrada — confirmado

**Arquivo/trecho:** `src/domain/plano_contas/service.py:191-237` cria
`PlanoContaCreate` sem `pai_id`, mesmo para códigos hierárquicos, e captura exceções
de `self.criar()` sem rollback/savepoint.

**Cenário:** plano completo `1`, `1.1`, `1.1.1` entra com todas as contas como raízes.
Uma `IntegrityError` durante `flush` deixa a sessão em estado inválido e todas as
linhas seguintes/commit falham.

**Correção:** importar em duas fases inferindo/validando pais e usar savepoint por
linha ou abortar atomicamente.

### M5 — `agencia_id` de comprovante pode apontar para outra empresa — confirmado

**Arquivo/trecho:** `src/domain/comprovantes/service.py:78-106` valida somente
`transacao_id` e grava diretamente `agencia_id=data.agencia_id`.

**Cenário:** usuário informa UUID de agência de outro cliente; o FK aceita e o
comprovante passa a combinar empresa e agência incompatíveis.

**Correção:** validar todas as FKs de domínio contra `empresa_id`, idealmente
reforçadas por constraints compostas.

### M6 — DRE inclui contas que não pertencem à demonstração e usa natureza errada — confirmado

**Arquivo/trecho:** `src/domain/relatorios/service.py:102-125` agrupa qualquer
`PlanoConta.tipo`; somente `receita` usa `C-D`, enquanto passivo e patrimônio líquido
caem em `D-C`.

**Cenário:** conta de passivo aparece na DRE e tem saldo com sinal invertido, ainda
que o resultado líquido use apenas receita/custo/despesa.

**Correção:** limitar a DRE a contas de resultado e definir natureza de saldo
explicitamente no plano de contas.

### M7 — Estatísticas contam transações soft-deleted — confirmado

**Arquivo/trecho:** `src/domain/stats/service.py:62-87,116-137,181-215` filtra
`deleted_at` em registros/notas/comprovantes, mas não em `Transacao`.

**Cenário:** transação removida continua em totais, percentuais mensais e contagem
por agência, podendo produzir `não conciliados` incoerente.

**Correção:** aplicar consistentemente `Transacao.deleted_at.is_(None)`.

### M8 — Migration e metadata divergem na unicidade de conta bancária — confirmado

**Arquivo/trecho:** `src/db/migrations/versions/0001_initial_schema.py:124-142` cria
`uq_agencia_empresa_banco_agencia_numero`; `AgenciaBancaria` em
`src/db/models.py:200-219` não possui `__table_args__` equivalente.

**Cenário:** produção via Alembic rejeita duplicatas, mas banco criado com
`Base.metadata.create_all()` e testes SQLite podem aceitá-las, mascarando
comportamento e corridas.

**Correção:** manter a constraint no model e adicionar teste de paridade
metadata/migrations.

### M9 — CSV/XLSX exportados permitem formula injection — confirmado

**Arquivo/trecho:** históricos importados são escritos sem neutralização em
`src/domain/exportacao/service.py:163-176,465-470`; nomes de fornecedor entram
diretamente em células em `src/api/v1/concilpro.py:696-704`.

**Cenário:** histórico OFX ou nome de fornecedor começando por `=`, `+`, `-` ou `@`
vira fórmula quando o arquivo é aberto no Excel, podendo disparar links/requisições ou
DDE em ambientes vulneráveis.

**Correção:** neutralizar prefixos de fórmula em todo campo textual destinado a
planilhas.

### M10 — Pequenos erros de parsing/conciliação podem alterar centavos — parte confirmada; agrupamento a confirmar

**Arquivo/trecho:** ConcilPro considera saldo exatamente de R$ 0,01 como quitado em
`src/domain/concilpro/conciliacao_intel.py:19-27,83-85,119-121`. `_decimal`
transforma texto `"1234.56"` em `"123456"` em
`src/domain/concilpro/planilha.py:71-84`.

O consolidador agrupa compra apenas por número da NF e data em
`src/domain/concilpro/consolidador.py:33-49`.

**Cenário confirmado:** um centavo efetivamente em aberto é zerado; célula textual em
formato inglês vira valor cem vezes maior.

**A confirmar:** notas de mesmo número/data, mas séries diferentes, serão fundidas se
esse formato puder aparecer na origem.

**Correção:** usar igualdade exata após quantização monetária, detectar separador
decimal pelo formato e incluir série/chave na identidade da nota.

### M11 — Exceções internas são devolvidas ao cliente — confirmado

**Arquivo/trecho:** `src/api/v1/concilpro.py:373-378` usa
`HTTPException(500, detail=str(exc))`; Open Banking incorpora `str(exc)` em respostas
de validação em `src/domain/openbanking/service.py:101-108,178-184`.

**Cenário:** erros de driver, schema, provedor ou parser expõem nomes internos,
endpoints e detalhes operacionais a usuários autenticados.

**Correção:** registrar a exceção com trace ID e responder somente com erro público
tipado.

### M12 — Tabela de auditoria existe, mas nenhuma mutação a utiliza — confirmado

**Arquivo/trecho:** `AuditLog` declara "registro imutável de toda ação de mutação" em
`src/db/models.py:552-572`, mas a única outra referência é a migration.

**Cenário:** alteração de regra, conta, associação ou status de fatura que muda
números históricos não deixa autor, estado anterior ou posterior verificável.

**Correção:** gravar o audit log na mesma transação de toda mutação
financeira/administrativa.

## Baixo

### B1 — Validações de entrada são inconsistentes — confirmado

**Arquivo/trecho:** `NotaFiscalCreate` e o setup só verificam comprimento do CNPJ em
`src/schemas/notas.py:31-39` e `src/api/v1/setup.py:30-36`; `AgenciaUpdate.numero`
não reutiliza o validador de criação em `src/schemas/agencias.py:80-103`; competência
aceita mês `00` ou `99` em `src/schemas/cartoes.py:76-83`.

**Cenário:** registros inválidos entram por caminhos manuais e falham posteriormente
em exportação, exibição ou integração.

**Correção:** centralizar CNPJ, conta bancária e competência em tipos/validadores
reutilizáveis.

### B2 — Paginação de usuários não tem limites — confirmado

**Arquivo/trecho:** `src/api/v1/usuarios.py:24-32` recebe `page` e `page_size` como
inteiros simples, sem `ge/le`.

**Cenário:** admin solicita `page_size` extremamente alto e força materialização de
toda a tabela.

**Correção:** usar `Query(ge=1, le=200)` como nas demais listagens.

## Cobertura

Revisados a fundo:

- Autenticação, JWT/cookies/CSRF, dependências de autorização e setup.
- Notas XML/ZIP, extrato OFX/PDF, NEO, regras, agências, contabilidade, plano de
  contas.
- Cartões, comprovantes, Open Banking e todos os componentes não-parser do ConcilPro.
- Empresas, permissões, exportação, relatórios e stats.
- `models.py`, sessão, config/context/logging, migrations `0001` a `0007`.
- `check_prod_concilpro.py`, `conciliar_cnpj.py`, `migrate_export.py`,
  `migrate_apply.py`.
- Todos os seis `scripts/*.py`.

Revisados mais rasamente/transversalmente:

- Módulos já corrigidos nesta sessão: middleware/uploads, CNPJ, `db/functions.py`,
  exportação e o parser principal do ConcilPro.
- Schemas e routers sem lógica própria.
- `__init__.py`, adapters e jobs, que estão vazios ou apenas agregam imports.

`listar_cnpj_para_conferencia.py` não existe no workspace, portanto não pôde ser
auditado.

Como exigido, não foram executados testes nem qualquer validação dinâmica; achados de
concorrência e integração foram determinados pelo fluxo transacional e constraints
presentes no código.
