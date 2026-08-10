# Checklist de teste manual — correções da auditoria (06-08/08/2026)

Roteiro para validar manualmente as correções já em `main` antes de seguir para as
melhorias de produto do [levantamento de 10/08](2026-08-10-levantamento-produto-sincopecas.md).
Os testes automatizados já passam 100% (rodados a cada merge) — isso aqui é sobre
confirmar comportamento visível na aplicação de verdade.

Convenção: tenha à mão pelo menos **duas empresas de tenants diferentes** e, dentro de
um tenant, um usuário **admin** e um **contador** com permissão limitada — a maioria dos
testes críticos depende disso.

---

## 1. Isolamento entre empresas/escritórios (prioridade máxima)

- [ ] Logado como admin do tenant A, tentar acessar `/empresas/{id}/...` de uma
  empresa do tenant B (usando um UUID conhecido de outro escritório) → **esperado:
  403/404**, não deve retornar dados.
- [ ] Como admin do tenant A, tentar conceder permissão a um usuário do próprio
  tenant sobre uma empresa do tenant B → **esperado: erro**, não deve conseguir.
- [ ] No ConcilPro, subir um arquivo em duas empresas de tenants diferentes e
  conferir que a lista de fornecedores de uma **não** mostra nada da outra (era
  exatamente o bug relatado na SINCOPEÇAS).
- [ ] Desativar uma empresa e confirmar que login/importações para ela deixam de
  funcionar (antes continuava tudo liberado).
- [ ] Desativar um tenant inteiro e confirmar que nenhum usuário dele consegue mais
  logar.

## 2. Permissões por módulo

- [ ] Criar um contador com permissão só de "extrato" numa empresa e confirmar que
  ele **não** consegue acessar notas, regras, contabilidade, cartões, Open Banking
  nem relatórios dessa empresa.
- [ ] Confirmar que esse contador **não** consegue criar, editar ou desativar
  empresas (só admin pode).
- [ ] Confirmar que o contador só vê, na lista de empresas, as que têm permissão
  explícita.

## 3. Login e sessão

- [ ] Tentar login com senha errada várias vezes seguidas rápido → **esperado:**
  bloqueio/rate limit depois de algumas tentativas (antes não tinha limite).
- [ ] Conferir que login com e-mail inexistente e login com senha errada para e-mail
  existente demoram tempos parecidos (antes o inexistente respondia bem mais rápido,
  o que permitia descobrir e-mails válidos).
- [ ] Fazer refresh de sessão duas vezes rápido com o mesmo token (duas abas, por
  exemplo) → **esperado:** só uma deve funcionar, a outra deve falhar (antes ambas
  passavam).

## 4. Endpoint de setup inicial

- [ ] Confirmar que `/api/v1/setup` **não** está mais acessível publicamente sem a
  flag/segredo configurado (antes qualquer um podia virar o primeiro admin).

## 5. NEO / lançamentos contábeis

- [ ] Rodar "Processar NEO" duas vezes seguidas na mesma empresa → **esperado:** a
  segunda vez não duplica lançamentos das transações já associadas (`associadas`
  deve vir `0` na segunda chamada).
- [ ] Conferir um lançamento criado pelo NEO no razão/balancete: deve aparecer **duas
  partidas** (débito e crédito), não uma só — e o balancete deve fechar
  (`total_débitos == total_créditos`).
- [ ] Associar manualmente uma transação "sem regra" a uma conta e confirmar que não
  dá pra associar a mesma decisão duas vezes.
- [ ] Criar duas regras para o mesmo histórico em maiúsculo/minúsculo (ex.: "PIX
  ACME" e "pix acme") → **esperado:** deve dar erro de duplicidade, não deixar
  coexistir.

## 6. DRE e relatórios

- [ ] Gerar a DRE de uma empresa com contas de passivo/patrimônio líquido cadastradas
  e confirmar que **só** aparecem contas de receita/custo/despesa (antes contas de
  passivo apareciam com sinal invertido).
- [ ] Filtrar contabilidade/extrato por uma data final específica e conferir que
  transações do próprio último dia aparecem (antes o filtro cortava o dia inteiro).
- [ ] Excluir (soft-delete) uma transação e conferir que ela some das estatísticas
  (`/stats`) também, não só das listagens.

## 7. Plano de contas

- [ ] Tentar renomear o código de uma conta que já tem movimentação → **esperado:**
  bloqueado.
- [ ] Tentar excluir uma conta com lançamentos/registros contábeis vinculados →
  **esperado:** bloqueado (antes sumia dos relatórios silenciosamente).
- [ ] Importar um plano de contas com hierarquia (ex.: `1`, `1.1`, `1.1.1`) e conferir
  que a árvore vem correta, com pai/filho certos.

## 8. Notas fiscais

- [ ] Importar um XML de nota fiscal com CNPJ emitente/destinatário que **não**
  corresponde à empresa → **esperado:** rejeitado.
- [ ] Reimportar a mesma NFS-e duas vezes → **esperado:** não duplica.
- [ ] Confirmar que a mesma chave de NF-e pode ser usada por empresas de tenants
  diferentes sem bloqueio cruzado (antes travava indevidamente).
- [ ] Tentar subir um ZIP de notas artificialmente grande/muitos arquivos pequenos →
  **esperado:** rejeitado com erro claro, sem travar.

## 9. Extrato bancário (OFX/PDF)

- [ ] Importar um OFX com uma transação de data com fração de segundo/timezone →
  **esperado:** não perde a transação silenciosamente (antes sumia sem aparecer em
  `erros`).
- [ ] Importar o mesmo extrato PDF duas vezes → **esperado:** não duplica.
- [ ] Importar um PDF grande e confirmar que a aplicação continua respondendo a
  outras requisições enquanto processa (antes travava o worker).

## 10. Cartões de crédito

- [ ] Tentar quitar uma fatura com uma transação de valor **diferente** do total →
  **esperado:** bloqueado.
- [ ] Tentar usar a mesma transação para quitar duas faturas diferentes →
  **esperado:** bloqueado.
- [ ] Tentar marcar fatura como "paga" diretamente sem passar pela quitação normal →
  **esperado:** bloqueado.
- [ ] Reimportar o mesmo CSV de fatura duas vezes → **esperado:** não duplica os
  lançamentos.

## 11. Comprovantes

- [ ] Tentar vincular um comprovante a uma agência bancária de **outra** empresa →
  **esperado:** bloqueado.

## 12. Open Banking

- [ ] Em ambiente sem credenciais Pluggy configuradas, confirmar que a aplicação
  **não** finge sucesso com dados fictícios em produção (deve falhar/avisar
  claramente).
- [ ] Conectar uma conta que tem corrente **e** poupança → **esperado:** as duas
  contas aparecem, não só a primeira.

## 13. Exportações e conciliação

- [ ] Na exportação de "conferência", ligar uma transação pequena a uma nota/
  comprovante de valor bem diferente → **esperado:** não deve mais aparecer
  "Conciliado" só porque os três documentos existem — deve expor a divergência.
- [ ] Exportar um CSV/XLSX com um fornecedor cujo nome comece com `=`, `+`, `-` ou
  `@` → abrir no Excel e confirmar que **não** vira fórmula.
- [ ] Provocar um erro no ConcilPro (ex.: arquivo malformado) e conferir que a
  mensagem de erro devolvida ao usuário é genérica, sem detalhes internos
  (stacktrace, nome de driver, etc.).

## 14. Valores monetários (Decimal)

- [ ] Conferir alguns valores com centavos em diferentes telas (extrato, cartões,
  relatórios) e ver se batem exatamente com o esperado, sem diferença de
  arredondamento — mudança ampla, vale mais atenção aqui do que nos outros itens.

## 15. Auditoria (log de mutações)

- [ ] Depois de criar/alterar uma empresa, permissão, conta do plano ou associar uma
  transação manualmente no NEO, confirmar (via banco ou endpoint, se houver um) que
  ficou um registro em `AuditLog` com autor e ação. **Nota:** essa cobertura é
  intencionalmente parcial — só os pontos citados foram instrumentados, não é toda
  mutação do sistema.

## 16. Scripts (não dá pra testar pela UI)

Esses só se aplicam se/quando forem rodados manualmente por alguém do time:

- [ ] `migrate_export.py` agora exige `--tenant-origem`/`--tenant-destino` (ou
  equivalente) — confirmar que roda sem argumento não funciona mais.
- [ ] `migrate_apply.py` com um lote proposital com erro → **esperado:** termina com
  código de saída diferente de zero e não imprime "Concluído".
- [ ] `update_plano_contas.py`/`import_plano_contas_full.py` → **esperado:** rodam em
  modo dry-run por padrão, só escrevem no banco com as flags explícitas.

---

## Se algo falhar

Anota exatamente: usuário/empresa usados, passos, o que esperava vs. o que aconteceu.
Isso é o suficiente para eu voltar no achado correspondente (C1-C6, A1-A20, M1-M12,
B1-B2 — ver [relatório completo](2026-08-06-auditoria-codex.md)) e investigar.
