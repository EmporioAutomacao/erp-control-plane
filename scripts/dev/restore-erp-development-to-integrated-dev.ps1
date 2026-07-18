param(
    [string]$ComposeFile = (Join-Path $PSScriptRoot '..\..\docker-compose.integrated-dev.yml'),
    [string]$ErpEnvFile = 'D:\GitHub\erp\core\.env',
    [string]$DumpDirectory = (Join-Path $PSScriptRoot '..\..\artifacts\integrated-dev'),
    [string]$PostgreSqlBin = 'D:\GitHub\pdv-local\artifacts\postgresql-17\extracted\pgsql\bin',
    [switch]$SkipDump
)

$ErrorActionPreference = 'Stop'

function Get-DotEnvValue {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Name
    )

    $line = Get-Content -LiteralPath $Path |
        Where-Object { $_ -match "^\s*$([regex]::Escape($Name))\s*=" } |
        Select-Object -First 1

    if (-not $line) {
        throw "Variavel $Name nao encontrada em $Path."
    }

    $value = ($line -split '=', 2)[1].Trim()
    $value = $value.Trim("'").Trim('"')
    return $value
}

function Invoke-Compose {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Args)
    & docker compose -f $ComposeFile @Args
    if ($LASTEXITCODE -ne 0) {
        throw "docker compose falhou: $($Args -join ' ')"
    }
}

function Wait-HttpOk {
    param(
        [Parameter(Mandatory = $true)][string]$Url,
        [int]$TimeoutSeconds = 180
    )

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    do {
        try {
            $response = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 5
            if ($response.StatusCode -ge 200 -and $response.StatusCode -lt 500) {
                return
            }
        } catch {
            Start-Sleep -Seconds 3
        }
    } while ((Get-Date) -lt $deadline)

    throw "Timeout aguardando $Url."
}

$ComposeFile = (Resolve-Path -LiteralPath $ComposeFile).Path
$DumpDirectory = [System.IO.Path]::GetFullPath($DumpDirectory)
New-Item -ItemType Directory -Path $DumpDirectory -Force | Out-Null

$pgDump = Join-Path $PostgreSqlBin 'pg_dump.exe'
$pgIsReady = Join-Path $PostgreSqlBin 'pg_isready.exe'
if (-not (Test-Path -LiteralPath $pgDump)) {
    throw "pg_dump.exe nao encontrado em $pgDump. Baixe/prepare os PostgreSQL 17 Client Tools antes de copiar a base."
}
if (-not (Test-Path -LiteralPath $pgIsReady)) {
    throw "pg_isready.exe nao encontrado em $pgIsReady. Baixe/prepare os PostgreSQL 17 Client Tools antes de copiar a base."
}

$sourceDb = Get-DotEnvValue -Path $ErpEnvFile -Name 'POSTGRES_DB'
$sourceUser = Get-DotEnvValue -Path $ErpEnvFile -Name 'POSTGRES_USER'
$sourcePassword = Get-DotEnvValue -Path $ErpEnvFile -Name 'POSTGRES_PASSWORD'
$sourceHost = Get-DotEnvValue -Path $ErpEnvFile -Name 'POSTGRES_HOST'
$sourcePort = Get-DotEnvValue -Path $ErpEnvFile -Name 'POSTGRES_PORT'

$dumpFile = Join-Path $DumpDirectory "$sourceDb.dump"

if (-not $SkipDump) {
    Write-Host "Validando acesso ao banco origem: ${sourceHost}:${sourcePort}/$sourceDb"
    & $pgIsReady -h $sourceHost -p $sourcePort -U $sourceUser -d $sourceDb -t 5
    if ($LASTEXITCODE -ne 0) {
        throw "Banco origem indisponivel: ${sourceHost}:${sourcePort}/$sourceDb. Verifique rede/VPN/firewall/PostgreSQL antes de copiar os dados."
    }
}

Write-Host "Subindo bancos e Redis do ambiente integrado..."
Invoke-Compose up -d cp_db cp_redis erp_cliente_db erp_cliente_redis

if (-not $SkipDump) {
    Write-Host "Gerando dump do ERP atual: ${sourceHost}:${sourcePort}/$sourceDb"
    if (Test-Path -LiteralPath $dumpFile) {
        Remove-Item -LiteralPath $dumpFile -Force
    }

    $env:PGPASSWORD = $sourcePassword
    & $pgDump `
        -h $sourceHost `
        -p $sourcePort `
        -U $sourceUser `
        -d $sourceDb `
        -Fc `
        --no-owner `
        --no-acl `
        -f $dumpFile

    if ($LASTEXITCODE -ne 0) {
        throw "Falha ao gerar dump do banco ERP atual."
    }
    Remove-Item Env:\PGPASSWORD -ErrorAction SilentlyContinue
}

if (-not (Test-Path -LiteralPath $dumpFile)) {
    throw "Dump nao encontrado: $dumpFile"
}

Write-Host "Copiando dump para o container PostgreSQL do cliente desenvolvimento..."
Invoke-Compose cp $dumpFile erp_cliente_db:/tmp/erp_desenvolvimento.dump

Write-Host "Resetando schema public do banco Docker do cliente..."
Invoke-Compose exec -T erp_cliente_db psql `
    -U erp_desenvolvimento `
    -d erp_desenvolvimento `
    -v ON_ERROR_STOP=1 `
    -c "DROP SCHEMA IF EXISTS public CASCADE; CREATE SCHEMA public; CREATE EXTENSION IF NOT EXISTS vector;"

Write-Host "Restaurando dump no banco Docker do cliente desenvolvimento..."
Invoke-Compose exec -T erp_cliente_db pg_restore `
    -U erp_desenvolvimento `
    -d erp_desenvolvimento `
    --no-owner `
    --no-acl `
    --role=erp_desenvolvimento `
    /tmp/erp_desenvolvimento.dump

Write-Host "Subindo CP e ERP do cliente desenvolvimento..."
Invoke-Compose up -d cp_web erp_cliente_web

Write-Host "Aguardando interfaces HTTP..."
Wait-HttpOk -Url 'http://127.0.0.1:8001/' -TimeoutSeconds 240
Wait-HttpOk -Url 'http://127.0.0.1:8002/health/' -TimeoutSeconds 240

Write-Host "Registrando cliente desenvolvimento no CP e sincronizando snapshot no ERP..."
Invoke-Compose exec -T cp_web python manage.py bootstrap_integrated_dev_cliente `
    --slug desenvolvimento `
    --nome Desenvolvimento `
    --plano pro `
    --email dev@localhost

Write-Host "Validando snapshot do PlanoCliente no ERP Docker..."
Invoke-Compose exec -T erp_cliente_web python manage.py shell -c "from core.models import PlanoCliente; p=PlanoCliente.obter(); print({'cp_cliente_id': str(p.cp_cliente_id), 'cp_cliente_nome': p.cp_cliente_nome, 'plano_nome': p.plano_nome, 'modulos': p.modulos_assinados})"

Write-Host "Ambiente integrado pronto."
Write-Host "CP:  http://127.0.0.1:8001/"
Write-Host "ERP: http://127.0.0.1:8002/"
Write-Host "DB cliente no host: 127.0.0.1:5434 / erp_desenvolvimento"
