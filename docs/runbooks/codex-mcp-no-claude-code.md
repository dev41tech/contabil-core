# Runbook — Conectar o Codex como MCP no Claude Code (Windows)

Validado em 05/08/2026 com `codex-cli 0.146.1` e `@anthropic-ai/claude-code 2.1.222`.
Serve para replicar em outras máquinas, contas e instalações do Claude Code.

---

## Passo 1 — Instalar o Codex CLI

No PowerShell (usuário normal, não precisa de admin):

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://chatgpt.com/codex/install.ps1 | iex"
```

O instalador coloca o binário em:

```
%LOCALAPPDATA%\Programs\OpenAI\Codex\bin\codex.exe
```

Esse `codex.exe` é um **symlink** para `%USERPROFILE%\.codex\packages\standalone\releases\<versao>\bin\codex.exe`.
O symlink sobrevive a atualizações — o caminho acima continua válido depois de upgrades.

O instalador também adiciona a pasta `bin` ao **PATH do usuário** (chave de registro `HKCU:\Environment`).

## Passo 2 — Fechar TODOS os terminais e apps abertos

Este passo é o que mais causa confusão. Processos já em execução herdaram o PATH
antigo e **não enxergam** o `codex` recém-instalado, por mais que o registro esteja correto.

Feche: Claude Code (o app inteiro, não só a aba), VS Code, PowerShell, Windows Terminal.

## Passo 3 — Validar a instalação

Abra um PowerShell **novo**:

```powershell
codex --version
```

Esperado: `codex-cli 0.146.1` (ou superior).

Se der "não reconhecido", o PATH não propagou. Confira com:

```powershell
(Get-ItemProperty -Path 'HKCU:\Environment' -Name Path).Path -split ';' | Where-Object { $_ -like '*Codex*' }
```

Se a linha aparecer mas o comando falhar, faça logoff/logon do Windows (ou reinicie).
Se a linha não aparecer, pule para a variante de caminho absoluto no Passo 5.

## Passo 4 — Autenticar o Codex

```powershell
codex login
```

Abre o navegador para login na conta ChatGPT. As credenciais ficam em
`%USERPROFILE%\.codex\auth.json`.

> **Nunca copie o `auth.json` entre máquinas ou contas.** Ele contém tokens de
> sessão vinculados àquela conta. Em cada máquina/conta nova, rode `codex login`
> de novo.

## Passo 5 — Registrar o MCP no Claude Code

O ponto crítico é o **escopo**. Use `--scope user` para valer em qualquer pasta:

```bash
claude mcp add codex --scope user -- codex mcp-server
```

Diferença entre os escopos:

| Escopo | Onde grava | Vale para |
|---|---|---|
| `local` (padrão) | `.claude.json` → `projects[<pasta>]` | **só** a pasta em que você rodou o comando |
| `user` | `.claude.json` → `mcpServers` (raiz) | todas as pastas daquele usuário |
| `project` | `.mcp.json` no repositório | todo mundo que clonar o repo |

O erro clássico: rodar `claude mcp add` sem `--scope` dentro de `C:\Users\<voce>`
e depois estranhar que o MCP não aparece nos projetos.

**Variante com caminho absoluto** — use se o PATH não propagou ou se preferir não
depender dele:

```bash
claude mcp add codex --scope user -- "C:\Users\SEU_USUARIO\AppData\Local\Programs\OpenAI\Codex\bin\codex.exe" mcp-server
```

Para corrigir um registro feito no escopo errado, remova antes de re-adicionar:

```bash
claude mcp remove codex --scope local
```

## Passo 6 — Reiniciar o Claude Code (de novo)

MCPs são carregados **na inicialização da sessão**. Um servidor registrado no meio
de uma sessão em andamento não é carregado nela.

Feche e reabra o Claude Code.

## Passo 7 — Verificar

Numa sessão interativa do Claude Code:

```
/mcp
```

O `codex` deve aparecer como **connected**. As ferramentas expostas são:

- `mcp__codex__codex` — inicia uma sessão Codex
- `mcp__codex__codex-reply` — continua uma sessão existente

---

## Checklist de replicação

Por **máquina**:
- [ ] Passo 1 (instalar), Passo 2 (fechar tudo), Passo 3 (validar `codex --version`)

Por **conta de usuário do Windows**:
- [ ] Passo 4 (`codex login`) — cada conta tem seu próprio `~/.codex`
- [ ] Passo 5 (`claude mcp add --scope user`) — o `.claude.json` é por usuário

Por **instalação do Claude Code** (CLI, desktop, extensão de IDE):
- [ ] Todas compartilham o mesmo `~/.claude.json`, então o Passo 5 vale para todas
- [ ] Mas cada uma precisa ser **reiniciada** (Passo 6) para carregar o MCP

---

## Configuração opcional do Codex

`%USERPROFILE%\.codex\config.toml` controla modelo e sandbox. Exemplo em uso:

```toml
model = "gpt-5.3-codex"
model_reasoning_effort = "xhigh"

[windows]
sandbox = "elevated"

[projects.'C:\caminho\do\projeto']
trust_level = "trusted"
```

Esse arquivo **pode** ser copiado entre máquinas (não tem segredo), mas ajuste os
caminhos em `[projects.*]`, que são específicos de cada máquina.

---

## Diagnóstico rápido

| Sintoma | Causa provável | Correção |
|---|---|---|
| `codex` não reconhecido em terminal novo | PATH não propagou | Logoff/logon, ou use caminho absoluto no Passo 5 |
| `/mcp` não lista o `codex` | Registro em escopo `local` na pasta errada | `claude mcp remove codex --scope local` e refazer com `--scope user` |
| `/mcp` lista mas dá "failed to connect" | Binário inexistente ou PATH stale no processo do Claude | Reinicie o app; valide com `codex --version` |
| Conectou mas as chamadas dão erro de auth | Falta `codex login` naquela conta | Rode `codex login` |
| Funciona no terminal, não no app desktop | App aberto antes da instalação | Feche o app inteiro e reabra |
