# Plano técnico — Relatório Unificado de Melhorias (reedição)

Data da reedição: 2026-08-26  
Base conferida: `contabil-core` @ `583d15d`  
Base anterior: plano de 2026-08-21, feito sobre `574025d`  
Documento de origem: `2026-08-20-relatorio-unificado-melhorias.txt` (14 páginas, atualizado em 20/08/2026)

Método: releitura integral do relatório e do plano anterior; revisão estática do código atual, sem executar a suíte. O frontend continua fora deste repositório. As mudanças de frontend informadas para esta reedição foram registradas como entregues, mas não puderam ser conferidas neste código.

As classes mantêm o sentido do plano anterior:

| Classe | Significado |
|---|---|
| **JÁ EXISTE** | O backend necessário já existe; quando a entrega de frontend foi informada, a necessidade está fechada. Sem essa informação, pode restar consumo no front. |
| **FRONT** | Mudança apenas de interface; nenhum trabalho em `contabil-core`. |
| **AMBOS** | Ainda há complemento no backend e trabalho de interface. |
| **BACK** | O trabalho restante é de backend ou de modelagem neste repositório. |

O plano anterior dizia haver 37 classificações, mas a tabela dele tinha 34 linhas. Esta reedição conserva as mesmas 34 linhas de decomposição para permitir comparação direta; não infla a contagem para reproduzir o total incorreto.

---

## Resumo executivo

| Classe | Qtd em 26/08 | Leitura prática |
|---|---:|---|
| **JÁ EXISTE** | 15 | Inclui Sicredi, quarentena valor/saldo, visualização e operação manual do NEO, contrapartes, desativação de regra e cancelamento de lote de extrato. |
| **FRONT** | 2 | Colunas redimensionáveis e visual tipo Mister. |
| **AMBOS** | 9 | Principalmente filtros ainda incompletos e fluxos de documentos/exportação. |
| **BACK** | 8 | Plano de Contas, ConcilPro banco × razão, parsers/lifecycle e cartão. |

O saldo mudou: 2.1 está atendido no seletor por um caminho diferente do pedido; 3.2 tem caminho determinístico para o layout Sicredi; 3.3 tem prevenção, quarentena e ferramentas operacionais; 4.2, 4.3 e 4.4 deixaram de ser pendências estruturais; e o item 10 sobre exclusão de extrato ganhou lote rastreável e cancelamento auditado.

O que não mudou de natureza: o ConcilPro continua sem ler `Transacao`; comprovantes ainda não têm deduplicação configurável, lote nem edição; NF não guarda o original; cartão não tem exclusão de fatura, matching com fornecedor/NF nem exportador próprio; aplicações continuam sem ingestão de extrato.

Não foi verificado:

- se o arquivo Sicredi exato citado no relatório foi reimportado com sucesso; o repositório contém teste sanitizado que reproduz o layout, não o arquivo original;
- se `diagnostico_valor_saldo.py` e `limpar_extratos.py --executar` foram executados em produção;
- o estado atual do `contabil-front`, além das entregas descritas para esta reedição;
- qualquer comportamento de runtime ou migration, porque a suíte não foi executada.

---

## Mudanças que alteram a leitura do relatório

### 2.1 — Busca pela conta contábil: fechada por outro caminho

O relatório pede localizar a conta pelo número/código e manter classificação e descrição. O backend já entrega `conta_numero`, `codigo` e `descricao` ([plano_contas.py:142](../../src/schemas/plano_contas.py:142)). Segundo a entrega informada para esta reedição, o seletor passou a usar o **número da conta** como rótulo principal e a classificação como segunda linha; os dois são pesquisáveis em NEO, Regras, Agências e Contrapartes.

Isso não é uma reprodução literal do campo antigo: a necessidade foi atendida tornando o número visível e pesquisável no próprio rótulo, sem retirar a classificação. O frontend não está neste repositório; essa parte não foi conferida no código local.

### 3.1 — Extrato: quase todos os filtros chegaram; saldo agora existe

`GET /extrato` aceita status, agência, intervalo inclusivo de datas, histórico parcial, D/C e valor mínimo/máximo ([extrato.py:83](../../src/api/v1/extrato.py:83)). A consulta aplica esses predicados antes da paginação ([service.py:246](../../src/domain/extrato/service.py:246)). `saldo_apos` passou a ser persistido e devolvido ([models.py:508](../../src/db/models.py:508); [extrato.py:12](../../src/schemas/extrato.py:12)).

Ainda falta o **filtro por saldo** pedido no relatório: não há `saldo_min`, `saldo_max` ou `saldo` na rota nem em `TransacaoFiltro` ([extrato.py:83](../../src/api/v1/extrato.py:83); [extrato.py:53](../../src/schemas/extrato.py:53)). OFX e camadas de IA podem deixar o saldo nulo; a camada determinística de PDF o captura ([pdf_parser.py:283](../../src/domain/extrato/pdf_parser.py:283)). A classe permanece **AMBOS** por esse filtro e pela interface não conferida.

A quantidade por página já é parametrizável de 1 a 200 ([extrato.py:84](../../src/api/v1/extrato.py:84)). Se a tela ainda fixa 20, o restante é frontend.

### 3.2 — Sicredi: implementação existente, arquivo exato não verificado

O parser determinístico agora aceita saldo negativo, separa valor e saldo e remove os cabeçalhos do Sicredi ([pdf_parser.py:136](../../src/domain/extrato/pdf_parser.py:136); [pdf_parser.py:156](../../src/domain/extrato/pdf_parser.py:156); [pdf_parser.py:240](../../src/domain/extrato/pdf_parser.py:240)). A completude é conferida pela cadeia `saldo[n] - saldo[n-1] == movimento[n]`, validação explicitamente aplicável ao Sicredi ([pdf_parser.py:650](../../src/domain/extrato/pdf_parser.py:650)). Há teste sanitizado que reproduz o layout e o caso de conta no vermelho ([test_extrato_pdf_sicredi.py:27](../../tests/unit/test_extrato_pdf_sicredi.py:27); [test_extrato_pdf_sicredi.py:58](../../tests/unit/test_extrato_pdf_sicredi.py:58)).

Contraponto: o arquivo real `J:\...\07.2026` não está no repositório e não foi executado nesta revisão. A detecção do parser de extrato ainda é pela extensão `.pdf`; os demais nomes caem no caminho OFX ([jobs.py:184](../../src/domain/jobs.py:184)). Portanto, o código da demanda existe, mas a aceitação deve usar o arquivo original sanitizado.

### 3.3 — Valor × saldo: fechado no código, saneamento de produção não comprovado

Há quatro barreiras complementares:

1. o parser determinístico separa a primeira coluna monetária (movimento) da última (saldo) ([pdf_parser.py:136](../../src/domain/extrato/pdf_parser.py:136));
2. a importação rejeita linha crua cujo valor coincide com o saldo ou não coincide com nenhum valor da linha ([validacao.py:55](../../src/domain/extrato/validacao.py:55); [service.py:172](../../src/domain/extrato/service.py:172));
3. o NEO aplica a mesma régua e não contabiliza por regra, contraparte nem classificação manual ([engine.py:210](../../src/domain/neo/engine.py:210); [engine.py:274](../../src/domain/neo/engine.py:274); [engine.py:857](../../src/domain/neo/engine.py:857));
4. a cadeia de saldos recusa extração incompleta ou com valor trocado ([pdf_parser.py:650](../../src/domain/extrato/pdf_parser.py:650)).

Para os dados antigos, `diagnostico_valor_saldo.py` varre todas as empresas em modo somente leitura e separa transações contabilizadas das reparáveis ([diagnostico_valor_saldo.py:1](../../diagnostico_valor_saldo.py:1); [diagnostico_valor_saldo.py:101](../../diagnostico_valor_saldo.py:101)). `limpar_extratos.py` oferece dry-run, backup e soft delete coordenado de transações e partidas antes da reimportação ([limpar_extratos.py:1](../../limpar_extratos.py:1); [limpar_extratos.py:127](../../limpar_extratos.py:127)).

Conclusão: a recorrência e a contabilização acidental estão fechadas no código. Não há evidência neste repo de que a limpeza foi executada; por isso a aceitação do item exige diagnóstico em produção e reimportação controlada. A classe passa de **BACK** para **JÁ EXISTE**, com risco operacional ainda alto até essa confirmação.

### Ordenação — uma ordem única para as telas principais

O relatório não define crescente ou decrescente, mas pede facilitar a conferência do extrato e do lançamento classificado. O código adotou a ordem do papel, do mais antigo para o mais recente: data, lote de importação, posição no arquivo e id ([ordenacao.py:7](../../src/domain/extrato/ordenacao.py:7); [ordenacao.py:34](../../src/domain/extrato/ordenacao.py:34)). A mesma função é usada pela tela de extrato ([service.py:282](../../src/domain/extrato/service.py:282)), exportação ([service.py:365](../../src/domain/exportacao/service.py:365)), fila NEO ([consultas.py:170](../../src/domain/neo/consultas.py:170)) e log de decisões/classificadas ([consultas.py:347](../../src/domain/neo/consultas.py:347)).

Há uma exceção interna: o motor automático ainda carrega pendências por `data, id`, não por `data, lote, ordem, id` ([engine.py:1297](../../src/domain/neo/engine.py:1297)). Isso não muda a ordem exibida, mas pode mudar qual transação consome primeiro um comprovante/nota disputado. É uma inconsistência de backend a corrigir antes de confiar na ordem como critério de desempate automático.

---

## NEO — revisão dos itens 4.1 a 4.5

### 4.1 — Filtros: avançou, mas não fechou

Classificadas (`GET /neo/decisoes`) aceitam data, termo, valor mínimo/máximo, D/C, motivo, resultado, estratégia, agência, conta e competência ([neo.py:106](../../src/api/v1/neo.py:106)). Pendências (`GET /neo/pendencias`) aceitam data, histórico bancário, valor mínimo/máximo, D/C, agência e competência ([neo.py:194](../../src/api/v1/neo.py:194)).

Faltas reais:

- o termo das classificadas pesquisa `Transacao.historico` e `Regra.descricao`, não `RegistroContabil.historico`; portanto não é ainda o filtro de **Histórico contábil** pedido ([consultas.py:282](../../src/domain/neo/consultas.py:282); [consultas.py:329](../../src/domain/neo/consultas.py:329));
- pendências devolvem o motivo da decisão, mas não aceitam filtro por motivo ([consultas.py:170](../../src/domain/neo/consultas.py:170); [consultas.py:241](../../src/domain/neo/consultas.py:241));
- “Ação/Editar” é controle de interface, não predicado de backend.

Classe: **AMBOS**. A maior parte do backend caiu, mas histórico contábil, motivo nas pendências e consumo no front permanecem.

### 4.2 — Uma linha por transação, sem depender de agrupamento

A fila agora parte de `Transacao`, inclusive quando o motor ainda não criou decisão, e devolve data, histórico bancário, valor, D/C, motivo e estratégia ([consultas.py:170](../../src/domain/neo/consultas.py:170); [neo.py:132](../../src/schemas/neo.py:132)). As classificadas devolvem também data, histórico bancário e histórico contábil vigente ([neo.py:43](../../src/schemas/neo.py:43); [consultas.py:417](../../src/domain/neo/consultas.py:417)).

Segundo a entrega de frontend informada, a tela usa o layout do Extrato, uma linha por transação, e expõe os botões **Associar** e **Alterar**. Isso atende “Data | Histórico | Valor | Ação” de forma mais direta que o hover pedido: o histórico bancário fica na própria linha e o contábil fica separado. Essa interface não foi verificada neste repositório.

Classe: **JÁ EXISTE**.

### 4.3 — Manual tem prioridade real; o agrupamento pedido foi abandonado na tela

A classificação manual recebe conta e histórico ([neo.py:176](../../src/schemas/neo.py:176)); o texto digitado é gravado como `RegistroContabil.historico`, preservando a linha bancária em `historico_extrato` ([engine.py:832](../../src/domain/neo/engine.py:832); [engine.py:905](../../src/domain/neo/engine.py:905)). Há lote de até 200 transações ([neo.py:265](../../src/api/v1/neo.py:265)).

Para alterar uma classificação, o backend cancela o par de partidas, devolve a transação à fila e marca a recusa automática; o motor não a toca até decisão humana ([neo.py:542](../../src/api/v1/neo.py:542); [cancelamento.py:139](../../src/domain/neo/cancelamento.py:139); [engine.py:1309](../../src/domain/neo/engine.py:1309)). A tela pode então reclassificar pela associação manual. Isso fecha prioridade, alteração de histórico e alteração de conta.

O relatório também pede manter agrupamento de históricos semelhantes. A implementação atual fez outra escolha: a tela agrupada foi removida e a fila principal virou uma linha por transação. O backend legado de agrupamento ainda existe em `GET /neo/pendencias/agrupadas` ([neo.py:241](../../src/api/v1/neo.py:241)), mas não é a fila principal. Portanto “agrupamento” não deve ser marcado como entregue; é uma decisão de produto a validar com os contadores. O tratamento em lote continua possível por seleção de linhas, sem depender do agrupamento.

Classe: **JÁ EXISTE** para a necessidade operacional de conciliar/reclassificar manualmente. Se os contadores exigirem de volta o agrupamento visual, o restante é **FRONT**, pois o endpoint ainda existe.

### 4.4 — Contraparte automática: fechada com três níveis de evidência

Sem regra aplicável, o NEO tenta, nesta ordem:

1. CPF/CNPJ de NF ou comprovante candidato;
2. CPF/CNPJ impresso no histórico bancário;
3. razão social ou nome fantasia no histórico.

O fluxo está em [engine.py:635](../../src/domain/neo/engine.py:635). Documento na linha só é aceito como corrida máxima de 11/14 dígitos ([documento_no_historico.py:36](../../src/domain/neo/documento_no_historico.py:36)); o CNPJ da própria empresa é descartado e múltiplas contrapartes diferentes geram recusa explicada ([engine.py:708](../../src/domain/neo/engine.py:708)). O matching por nome remove sufixos societários, recusa núcleo curto e recusa empate ([contraparte_por_nome.py:61](../../src/domain/neo/contraparte_por_nome.py:61); [contraparte_por_nome.py:88](../../src/domain/neo/contraparte_por_nome.py:88)). Quando resolve, usa a conta vinculada à contraparte e gera `PGTO REF` ou `REC REF` ([engine.py:169](../../src/domain/neo/engine.py:169); [engine.py:567](../../src/domain/neo/engine.py:567)).

Diferença importante: uma regra existente continua vencendo; a contraparte vira medição em shadow mode e não substitui a conta da regra ([engine.py:486](../../src/domain/neo/engine.py:486)). Criar ou editar contraparte também não dispara reprocessamento por si só: as rotas apenas persistem o cadastro ([contrapartes.py:56](../../src/api/v1/contrapartes.py:56); [contrapartes.py:72](../../src/api/v1/contrapartes.py:72)). A pendência será tentada na próxima execução do NEO.

Classe: **JÁ EXISTE**. O teste de aceitação deve cobrir: sem regra, contraparte ativa, conta vinculada, documento/nome não ambíguo e nova execução do NEO.

### 4.5 — Fonte da conciliação

A resposta anterior permanece correta:

- o **NEO** lê `Transacao` bancária pendente e produz `RegistroContabil`; razão é saída, não entrada ([engine.py:1297](../../src/domain/neo/engine.py:1297); [engine.py:905](../../src/domain/neo/engine.py:905));
- o **ConcilPro** opera nas tabelas `cp_*` e concilia compras/pagamentos do razão de fornecedores, sem consultar `Transacao` ([models.py:1019](../../src/db/models.py:1019); [conciliacao_intel.py:48](../../src/domain/concilpro/conciliacao_intel.py:48)).

Nenhum dos dois cruza hoje banco com razão preexistente. Essa lacuna continua sendo o item 5.2.

---

## Tabela completa de classificação

| # | Item | Classe em 26/08 | Situação conferida | Mudou desde 21/08 | Esforço restante | Risco |
|---|---|---|---|---|:--:|---|
| 2.1 | Busca por número/código da conta | **JÁ EXISTE** | Backend expõe número, classificação e descrição ([plano_contas.py:142](../../src/schemas/plano_contas.py:142)); front informado usa número como rótulo e classificação na segunda linha, ambos pesquisáveis | **Fechado**, por interface diferente da frase do relatório | P, validação | Baixo |
| 2.2 | Reimportação do Plano de Contas | **BACK** | Já atualiza número/descrição e, sem movimentação, tipo/S-A; porém identifica a conta existente por `codigo`, não por `conta_numero` ([service.py:399](../../src/domain/plano_contas/service.py:399); [service.py:538](../../src/domain/plano_contas/service.py:538)) | **Igual — ainda BACK**, mas o plano anterior subestimou o upsert existente | M/G | Alto |
| 2.2 | Excluir conta com lançamentos/regras | **BACK** | Bloqueio é deliberado para regra ativa e movimentação ([service.py:231](../../src/domain/plano_contas/service.py:231); [service.py:246](../../src/domain/plano_contas/service.py:246)); falta encerramento/substituição | Igual | G | Alto |
| 2.2 | Filtro do Plano de Contas | **JÁ EXISTE** | API devolve todas as contas, sem `q` ([plano_contas.py:51](../../src/api/v1/plano_contas.py:51)); backend já fornece os campos para filtro client-side | Igual | P (front) | Baixo |
| 2.3 | Colunas redimensionáveis | **FRONT** | UI pura; frontend não conferido | Igual | P/M | Baixo |
| 3.1 | Filtros de transação, inclusive saldo | **AMBOS** | Data, histórico, valor, D/C, status e agência existem; saldo é persistido/exposto, mas não é filtrável ([extrato.py:83](../../src/api/v1/extrato.py:83); [extrato.py:53](../../src/schemas/extrato.py:53)) | Igual na classe; backend avançou muito | M | Médio |
| 3.1 | Escolher registros por página | **JÁ EXISTE** | `page_size` 1–200 ([extrato.py:84](../../src/api/v1/extrato.py:84)); se a tela fixa 20, falta só consumir | Igual | P (front) | Baixo |
| 3.2 | Leitura/importação Sicredi | **JÁ EXISTE** | Parser determinístico separa movimento/saldo e valida cadeia; fixture sanitizada cobre Sicredi ([pdf_parser.py:136](../../src/domain/extrato/pdf_parser.py:136); [test_extrato_pdf_sicredi.py:58](../../tests/unit/test_extrato_pdf_sicredi.py:58)) | **Mudou de classe: BACK → JÁ EXISTE** | P, aceitação real | Alto até testar o arquivo citado |
| 3.3 | Divergência valor × saldo | **JÁ EXISTE** | Prevenção na importação, quarentena no NEO, diagnóstico e limpeza/reimportação disponíveis ([validacao.py:55](../../src/domain/extrato/validacao.py:55); [engine.py:210](../../src/domain/neo/engine.py:210)) | **Mudou de classe: BACK → JÁ EXISTE** | M operacional | Alto até confirmar produção |
| 4.1 | Filtros NEO | **AMBOS** | Quase todos existem; falta buscar histórico contábil e filtrar motivo nas pendências ([neo.py:106](../../src/api/v1/neo.py:106); [neo.py:194](../../src/api/v1/neo.py:194)) | Igual na classe; escopo backend caiu | M | Médio |
| 4.2 | Visualização do lançamento | **JÁ EXISTE** | Respostas trazem data, histórico bancário, histórico contábil, valor e D/C; front informado usa uma linha por transação ([neo.py:43](../../src/schemas/neo.py:43); [neo.py:132](../../src/schemas/neo.py:132)) | **Mudou de classe: AMBOS → JÁ EXISTE** | P, aceitação | Baixo |
| 4.3 | Conciliação manual prioritária | **JÁ EXISTE** | Desfazer/reclassificar impede retorno automático e manual grava conta/histórico; lote segue disponível ([cancelamento.py:139](../../src/domain/neo/cancelamento.py:139); [engine.py:832](../../src/domain/neo/engine.py:832); [neo.py:265](../../src/api/v1/neo.py:265)) | **Mudou de classe: AMBOS → JÁ EXISTE**; agrupamento saiu da tela | P, decisão de produto | Médio |
| 4.4 | Contrapartes automáticas | **JÁ EXISTE** | Casa por documento de anexo, documento da linha e nome, com guardas de ambiguidade ([engine.py:635](../../src/domain/neo/engine.py:635); [contraparte_por_nome.py:88](../../src/domain/neo/contraparte_por_nome.py:88)) | **Mudou de classe: BACK → JÁ EXISTE** | P/M, aceitação | Médio/alto |
| 4.5 | Fonte da conciliação | **JÁ EXISTE** | Respondido: NEO é banco → razão; ConcilPro é razão de fornecedores isolado | Igual | — | — |
| 5.1 | Navegação na lista de empresas | **JÁ EXISTE** | Backend pagina até 200 ([empresas.py:33](../../src/api/v1/empresas.py:33)); restante é paginação/rolagem no front | Igual | M (front) | Baixo |
| 5.1 | Visual estilo Mister | **FRONT** | UI pura; frontend não conferido | Igual | M/G | Baixo |
| 5.2 | Reconhecer/vincular pagamentos bancários | **BACK** | ConcilPro concilia `CpLancamento` COMPRA × PAGAMENTO, não `Transacao` ([conciliacao_intel.py:32](../../src/domain/concilpro/conciliacao_intel.py:32); [models.py:1082](../../src/db/models.py:1082)) | Igual | G | Alto |
| 6 | Leitura incompleta de comprovantes | **BACK** | Parser ainda encerra a camada assim que encontra `valor_pago`, mesmo se outros campos faltarem ([pdf_parser.py:523](../../src/domain/comprovantes/pdf_parser.py:523); [pdf_parser.py:568](../../src/domain/comprovantes/pdf_parser.py:568)) | Igual | M/G | Médio |
| 6 | Impedir duplicidade, configurável | **AMBOS** | Model não tem hash/dedup key nem flag de política ([models.py:663](../../src/db/models.py:663)) | Igual | G | Alto |
| 6 | Processar/salvar em lote | **AMBOS** | Fluxo continua `extrair-pdf` sem persistir + POST unitário ([comprovantes.py:83](../../src/api/v1/comprovantes.py:83); [comprovantes.py:68](../../src/api/v1/comprovantes.py:68)) | Igual | M/G | Médio |
| 6 | Editar comprovante | **AMBOS** | Não há PATCH; rotas são GET/POST/DELETE ([comprovantes.py:36](../../src/api/v1/comprovantes.py:36)) | Igual | M | Médio |
| 6 | Excluir comprovante | **JÁ EXISTE** | `DELETE /comprovantes/{id}` com soft delete ([comprovantes.py:192](../../src/api/v1/comprovantes.py:192)) | Igual | P (front) | Baixo |
| 6 | Associação manual pesquisável | **JÁ EXISTE** | Associação/desassociação existem e o extrato agora oferece busca por histórico/valor/data/D-C ([comprovantes.py:161](../../src/api/v1/comprovantes.py:161); [extrato.py:83](../../src/api/v1/extrato.py:83)) | **Mudou de classe: AMBOS → JÁ EXISTE** | M (front) | Médio |
| 7 | Filtro por valor em NF | **BACK** | Rota filtra status/tipo/agência/CNPJ/emitente/data, não valor ([notas.py:33](../../src/api/v1/notas.py:33)) | Igual | P | Baixo |
| 7 | Visualizar a NF | **AMBOS** | `NotaFiscal` guarda dados extraídos, mas não arquivo original/object key ([models.py:618](../../src/db/models.py:618)) | Igual | G | Médio |
| 7 | Entrada × saída | **AMBOS** | Direção é inferida na exportação pelo CNPJ da empresa, mas não aparece em `NotaFiscalResponse` ([service.py:454](../../src/domain/exportacao/service.py:454); [notas.py:61](../../src/schemas/notas.py:61)) | Igual | M | Baixo |
| 8 | Excluir fatura | **BACK** | Há DELETE de cartão e lançamento, não de fatura ([cartoes.py:75](../../src/api/v1/cartoes.py:75); [cartoes.py:208](../../src/api/v1/cartoes.py:208)) | Igual | M | Médio |
| 8 | Vinculação automática do cartão | **BACK** | Fatura associa manualmente uma transação; lançamento só tem conta, sem FK para contraparte/NF ([service.py:293](../../src/domain/cartoes/service.py:293); [models.py:760](../../src/db/models.py:760)) | Igual | G | Alto |
| 8 | Exportar cartão/documentos | **AMBOS** | Tipos do exportador não incluem cartão ([service.py:84](../../src/domain/exportacao/service.py:84)) | Igual | M | Médio |
| 9 | Leitura de aplicações | **AMBOS** | Model declara apenas posição estática, explicitamente sem extrato/conciliação ([models.py:317](../../src/db/models.py:317)); API é CRUD ([aplicacoes.py:30](../../src/api/v1/aplicacoes.py:30)) | Igual | G | Alto |
| 10 | Excluir regras | **JÁ EXISTE** | O DELETE foi removido, mas `PATCH /regras/{id}` aceita `ativa=false` e desativa a regra preservando histórico ([regras.py:75](../../src/api/v1/regras.py:75); [regras.py:52](../../src/schemas/regras.py:52); [service.py:126](../../src/domain/regras/service.py:126)) | Igual na classe; implementação mudou de DELETE para PATCH | P (front) | Baixo |
| 10 | Excluir agências | **JÁ EXISTE** | `DELETE /agencias/{id}` desativa ([agencias.py:88](../../src/api/v1/agencias.py:88)) | Igual | P (front) | Baixo |
| 10 | Excluir razão | **BACK** | `/contabil` continua apenas GET; cancelamento auditado existe só pelo lançamento NEO ([contabil.py:26](../../src/api/v1/contabil.py:26); [neo.py:542](../../src/api/v1/neo.py:542)) | Igual; ganhou caminho parcial seguro | G | Alto |
| 10 | Excluir extratos | **JÁ EXISTE** | Upload virou lote rastreável; cancelamento desfaz partidas antes do soft delete das transações ([extrato.py:117](../../src/api/v1/extrato.py:117); [extrato.py:140](../../src/api/v1/extrato.py:140); [importacoes.py:84](../../src/domain/extrato/importacoes.py:84)) | **Mudou de classe: BACK → JÁ EXISTE** | P, aceitação | Médio |

---

## O que sobrou de verdade

### Backend, dados e decisões de produto

| Prioridade | Escopo restante | Esforço | Risco | Observação |
|---|---|:--:|---|---|
| 1 | **3.3** executar diagnóstico em produção, decidir limpeza e reimportar | M | **Alto** | Código preventivo pronto; falta evidência operacional. Não corrigir valores antigos no escuro. |
| 1 | **3.2** aceitar o PDF Sicredi exato de 07/2026 | P/M | **Alto** | Se falhar, corrigir com fixture sanitizada do arquivo real. |
| 1 | **2.2** reproduzir contas 16, 17, 21, 22, 27, 49 e 64 | M/G | **Alto** | O upsert atual já sobrescreve dados quando o `codigo` é igual. É preciso descobrir se o defeito é identidade por `conta_numero`, coluna lida ou regra de tipo; não reescrever o importador por hipótese. |
| 2 | **5.2** decidir se ConcilPro passa a cruzar banco × razão | G | **Alto** | É mudança arquitetural, não correção pontual de matching da Unimed. |
| 2 | **6** comprovantes: completude, dedup configurável, lote e PATCH | G | Alto | Dedup e lote exigem identidade persistente e política explícita por empresa/importação. |
| 2 | **10** cancelamento/reversão geral do razão | G | Alto | Reusar o conceito de `lancamento_id`; nunca apagar uma partida isolada. |
| 2 | **8** lifecycle e matching de cartão | G | Alto | Excluir fatura, ligar compra a contraparte/NF e exportar com documentos/histórico. |
| 3 | **7** valor, original e direção de NF | M/G | Médio | Filtro é pequeno; guardar/servir o original é a parte grande. |
| 3 | **9** movimentos de aplicações e ingestão de extrato | G | Alto | Hoje só existe posição estática. |
| 4 | **3.1/4.1** filtros restantes | M | Médio | Saldo no extrato; histórico contábil nas classificadas; motivo nas pendências. |
| 4 | Alinhar ordem interna do motor à ordem única | P/M | Médio | Evita que ordem invisível `data,id` decida disputa de documento. |

### Só frontend ou consumo de API

- colunas redimensionáveis no estilo Excel (**2.3**);
- seletor de quantidade por página no Extrato, usando `page_size` (**3.1**);
- paginação/rolagem da lista de empresas do ConcilPro (**5.1**);
- visual de conciliação inspirado no Mister (**5.1**);
- tela de associação manual pesquisável de comprovante, combinando filtros do extrato com o POST de associação (**6**);
- botões para DELETE já disponível de comprovante e agência (**6/10**);
- decisão com os contadores sobre restaurar o agrupamento visual do NEO (**4.3**).

A busca de conta do item 2.1 não entra nesta lista: foi informada como entregue no frontend em todos os seletores citados.

---

## Onda 0 anterior — o que caiu e o que ainda bloqueia

| Pré-requisito citado em 21/08 | Estado em 26/08 | Consequência |
|---|---|---|
| “Corrigir a CI para PostgreSQL” | **Continua, mas o diagnóstico correto é: criar CI ativa.** `.github/workflows` não existe. `pipelines/ci.yml` tem sintaxe de GitHub Actions, mas está fora do diretório descoberto pelo GitHub. Localmente, `tests/conftest.py` procura `TEST_DATABASE_URL_REAL`, cai em SQLite e usa `Base.metadata.create_all()` ([conftest.py:38](../../tests/conftest.py:38)). | Migration e comportamento específico de Postgres não têm gate automático. A suíte só roda quando alguém executa localmente. |
| Bloquear OpenAI sem consentimento no ConcilPro | **Continua bloqueando.** `_get_client()` verifica apenas a chave, não `allow_financial_data_to_openai` ([ai_classifier.py:222](../../src/domain/concilpro/ai_classifier.py:222)). | Não ampliar ConcilPro antes de fechar o envio de dados. |
| Remover dump de produção versionado | **Continua bloqueando por segurança.** `migrate_prod.sql` segue rastreado e tem cerca de 28 MB. | Tratar como incidente separado; não copiar dado de produção para fixtures. |
| Definir política monetária e arredondamento | **Continua.** Models usam `NUMERIC(15,2)`, mas não há contrato único de validação/arredondamento nos schemas e serviços ([models.py:508](../../src/db/models.py:508); [models.py:633](../../src/db/models.py:633)). | Bloqueia mudanças amplas de dedup, cartão, aplicações e ConcilPro. |
| Tornar jobs duráveis e idempotentes | **Parcial.** NEO e importação de extrato ganharam registro persistente, heartbeat e progresso ([jobs.py:45](../../src/domain/jobs.py:45); [jobs.py:115](../../src/domain/jobs.py:115)), mas continuam em `BackgroundTasks`; upload fica em bytes na memória e job perdido é marcado como falho, não retomado ([jobs.py:157](../../src/domain/jobs.py:157); [jobs.py:230](../../src/domain/jobs.py:230)). ConcilPro continua em background local ([concilpro.py:430](../../src/api/v1/concilpro.py:430)). | Caiu o problema de visibilidade do progresso; não caiu durabilidade/reexecução. Continua bloqueando ingestões pesadas. |

Outros bloqueios do plano anterior:

- **Filtro de último dia:** caiu para Extrato, NEO e Livro Caixa porque `Transacao.data` virou data de calendário e os limites são inclusivos ([service.py:263](../../src/domain/extrato/service.py:263); [consultas.py:149](../../src/domain/neo/consultas.py:149); [service.py:252](../../src/domain/relatorios/service.py:252)). Não caiu de forma geral: notas, DRE, balancete e partes da exportação ainda comparam `datetime <= data_ate` diretamente ([service.py:104](../../src/domain/notas/service.py:104); [service.py:79](../../src/domain/relatorios/service.py:79); [service.py:198](../../src/domain/exportacao/service.py:198)).
- **Lotes de extrato e reversão NEO:** caíram. Migrations 0028/0029 e as rotas de cancelamento sustentam rastreabilidade e reversão do par.
- **Prioridade manual do NEO:** caiu. A marca `auto_recusado_em` impede reclassificação automática até liberação/decisão humana ([models.py:532](../../src/db/models.py:532)).
- **ConcilPro isolado e não durável:** continua.

---

## Plano de execução em ondas

### Onda 0 — Segurança e gate mínimo

1. Criar workflow real em `.github/workflows` com PostgreSQL e falha explícita se a integração estiver em SQLite.
2. Rodar `alembic upgrade head` em banco descartável no workflow; `create_all()` não valida migrations.
3. Fechar consentimento OpenAI do ConcilPro e remover o dump versionado.
4. Definir tipo monetário, limites e arredondamento únicos.
5. Definir fila durável/idempotência para ConcilPro e uploads; o registro `Job` atual é observabilidade, não retomada.

### Onda 1 — Aceitação contábil e dados existentes

1. **3.3** rodar diagnóstico valor/saldo; inventariar partidas; limpar/reimportar com backup e aceite do contador.
2. **3.2** validar o Sicredi de 07/2026 ponta a ponta: quantidade, D/C, valores, saldos e ordem.
3. **2.2** reproduzir a reimportação com o arquivo que contém as contas apontadas; fechar a identidade correta antes de alterar o upsert.

### Onda 2 — Conciliação e lifecycle de alto risco

1. **5.2** decisão de produto e, se aprovada, modelagem banco × razão no ConcilPro.
2. **6** dedup/lote/edição de comprovantes e completude do parser.
3. **10** reversão geral do razão por lançamento, com auditoria.
4. **8** exclusão de fatura, vínculo com contraparte/NF e quitação automática controlada.

### Onda 3 — Documentos e aplicações

1. **7** original da NF, direção na consulta e filtro por valor.
2. **8** exportação de cartão com documentos e histórico padronizado.
3. **9** importação de movimentos de aplicações e vínculo com transação bancária.

### Onda 4 — Consultas e consistência

1. Completar saldo no Extrato e histórico contábil/motivo no NEO.
2. Usar `ordenar_como_o_extrato` também na ordem de processamento automático.
3. Corrigir de forma comum os limites de data ainda baseados em `datetime`.

### Onda 5 — Frontend, em paralelo desde a Onda 1

Colunas redimensionáveis; page size; paginação/rolagem de empresas; visual Mister; associação manual pesquisável; ações de exclusão disponíveis; validação com os contadores da fila NEO sem agrupamento.

---

## Migrations: entregues e ainda prováveis

Entregues desde o plano anterior:

- `0025`: `Transacao.saldo_apos`;
- `0026`: data da transação como data de calendário;
- `0027`: posição no arquivo;
- `0028`: lote de importação de extrato;
- `0029`: cancelamento auditado de lançamento;
- `0030`: recusa da automação/prioridade manual;
- `0031`: enum D/C único e alinhamento declarativo de índices/colunas nos models.

Ainda devem exigir migration, se as soluções forem aprovadas:

- **2.2** identidade/estado de encerramento de conta contábil;
- **5.2** vínculo persistente entre `Transacao` e entidades `cp_*`;
- **6** hash/dedup key, política de dedup e lote de comprovantes;
- **7** metadata/object key do arquivo original da NF;
- **8** vínculos de lançamento de cartão com contraparte/NF;
- **9** movimentos de aplicações;
- **10** origem/lote e estado de reversão para razões não gerados pelo NEO.

Não exigem migration: filtro por saldo; filtros NEO; desativação de regra; filtro por valor de NF; direção computada; DELETE de fatura usando `deleted_at`; PATCH de comprovante; exportação de cartão; alinhamento da ordenação interna.
