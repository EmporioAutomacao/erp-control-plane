# Registra as skills compartilhadas (ERP / PDV / Sync) para o Claude Code
# descobrir neste repo, criando junctions em .claude/skills/ (nao versionado
# — ver .gitignore). Mesmo padrao usado no repo `erp`.
#
# Rode uma vez por maquina/clone:
#   powershell -ExecutionPolicy Bypass -File scripts/dev/setup_claude_skills.ps1

$ErrorActionPreference = "Stop"

$repo   = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$dst    = Join-Path $repo ".claude\skills"

# Catalogo de skills compartilhadas (fora deste repo).
$shared = "D:\GitHub\skills-shared\skills"

if (-not (Test-Path $shared)) {
    throw "Catalogo de skills compartilhadas nao encontrado em: $shared"
}

New-Item -ItemType Directory -Force -Path $dst | Out-Null

Get-ChildItem $shared -Directory |
    Where-Object { Test-Path (Join-Path $_.FullName "SKILL.md") } |
    ForEach-Object {
        $link = Join-Path $dst $_.Name
        if (Test-Path $link) { Remove-Item $link -Recurse -Force }
        New-Item -ItemType Junction -Path $link -Target $_.FullName | Out-Null
        Write-Host "registrada: $($_.Name)"
    }

Write-Host "`nReinicie o Claude Code para as skills aparecerem."
