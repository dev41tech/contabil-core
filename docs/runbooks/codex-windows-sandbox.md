# Runbook — `codex sandbox` falhando no Windows (helper não encontrado)

Diagnosticado em 07/08/2026 com `codex-cli 0.146.1` → `0.147.0` no Windows 11.

---

## Sintoma

```
codex sandbox -- powershell -Command "Write-Output HELLO"
```

Falha de duas formas, dependendo da versão:

- `codex-cli 0.146.1`: `windows sandbox failed: CreateProcessWithLogonW failed: 2`
- `codex-cli 0.147.0`: `windows sandbox failed: orchestrator_helper_launch_failed: setup refresh failed to launch helper: helper=codex-windows-sandbox-setup.exe, ..., error=program not found`

## Causa raiz

`codex.exe` em `%LOCALAPPDATA%\Programs\OpenAI\Codex\bin\codex.exe` é um **symlink** para
a release real em `%USERPROFILE%\.codex\packages\standalone\releases\<versao>\bin\codex.exe`.

O `codex sandbox` precisa rodar dois binários auxiliares que só existem na pasta
`codex-resources\` da release (não em `bin\`):

- `codex-windows-sandbox-setup.exe`
- `codex-command-runner.exe`

A lógica que localiza/copia esses helpers "para perto do executável atual" resolve o
caminho pelo **symlink** (`AppData\Local\...\bin\`), não pelo alvo real da release —
por isso não encontra `codex-resources\` (que só existe do lado do binário de verdade,
dentro de `.codex\packages\standalone\releases\<versao>\`). Isso é um bug de
empacotamento do Codex CLI no Windows, não um problema de conta/permissão local.

**Importante:** o mecanismo real do sandbox no Windows usa um modo `read-acl-only`
(concede ACEs de leitura a "sandbox users" nos arquivos necessários). Ele **não** usa
contas de logon separadas (`CodexSandboxOffline`/`CodexSandboxOnline`, DPAPI, etc.) —
essa foi uma tentativa manual de replicar algo que não é como o mecanismo real
funciona. Não vale a pena seguir por esse caminho; é um beco sem saída.

## Diagnóstico

Log detalhado de cada tentativa fica em:

```
%USERPROFILE%\.codex\.sandbox\sandbox.<AAAA-MM-DD>.log
```

Procure por linhas `setup refresh: spawning ...` e `helper copy failed for ...` —
elas mostram exatamente qual caminho o Codex tentou usar para achar o helper.

`RUST_LOG=debug` **não** produz saída adicional para esse fluxo — não perca tempo com isso.

## Workaround

Copiar os dois helpers da release atual para dentro da pasta `bin\` do symlink:

```powershell
$bin = "$env:LOCALAPPDATA\Programs\OpenAI\Codex\bin"
$versao = (codex --version) -replace 'codex-cli ', ''
$resources = "$env:USERPROFILE\.codex\packages\standalone\releases\$versao-x86_64-pc-windows-msvc\codex-resources"

Copy-Item "$resources\codex-windows-sandbox-setup.exe" $bin -Force
Copy-Item "$resources\codex-command-runner.exe" $bin -Force
```

Depois teste:

```powershell
codex sandbox -- powershell -Command "Write-Output HELLO-FROM-SANDBOX"
```

Deve imprimir `HELLO-FROM-SANDBOX`.

**⚠️ Esse workaround se perde a cada `codex update`** — a atualização reescreve/realoca
a pasta da release, então os helpers copiados manualmente somem de `bin\`. Reaplique o
comando acima depois de cada `codex update` até a OpenAI corrigir a resolução do
symlink no instalador.

## Limpeza de uma tentativa anterior (contas manuais)

Se em algum momento foram criadas contas locais `CodexSandboxOffline`/`CodexSandboxOnline`
tentando replicar manualmente o sandbox (não é necessário, ver acima), remova-as como
Administrador:

```powershell
Remove-LocalUser -Name CodexSandboxOffline
Remove-LocalUser -Name CodexSandboxOnline
Remove-Item "$env:USERPROFILE\.codex\.sandbox-secrets\sandbox_users.json" -Force
```

Isso também remove os perfis associados a essas contas (verifique em
`C:\Users\CodexSandboxOffline` / `Online` se sobrou alguma pasta órfã e apague-as
manualmente se `Remove-LocalUser` não limpar o perfil sozinho).

## Diagnóstico rápido

| Sintoma | Causa provável | Correção |
|---|---|---|
| `CreateProcessWithLogonW failed: 2` (0.146.1) | Mesmo bug de symlink, mensagem de erro antiga/genérica | Aplicar o workaround acima; ou `codex update` para 0.147.0 primeiro |
| `orchestrator_helper_launch_failed: ... error=program not found` (0.147.0+) | Helpers não copiados para `bin\` por causa do bug do symlink | Aplicar o workaround acima |
| Workaround funciona e depois volta a falhar | `codex update` rodou e resetou `bin\` | Reaplicar o comando de cópia dos helpers |
