# Ambiente integrado CP + ERP em Docker Desktop

## Objetivo

Rodar localmente o Control Plane e o primeiro ERP de cliente para
desenvolvimento, usando Docker Desktop.

## Arquivos

```text
docker-compose.integrated-dev.yml
scripts/dev/restore-erp-development-to-integrated-dev.ps1
registry/management/commands/bootstrap_integrated_dev_cliente.py
```

## Servicos

| Servico | Porta host | Uso |
| --- | --- | --- |
| `cp_web` | `8001` | Control Plane. |
| `cp_db` | `5433` | PostgreSQL do CP. |
| `cp_redis` | `6380` | Redis do CP. |
| `erp_cliente_web` | `8002` | ERP do cliente `desenvolvimento`. |
| `erp_cliente_db` | `5434` | PostgreSQL do cliente `desenvolvimento`. |
| `erp_cliente_redis` | `6381` | Redis do ERP cliente. |

## Banco do cliente desenvolvimento

```text
Host: 192.168.0.31
Porta: 5434
Database: erp_desenvolvimento
Usuario: erp_desenvolvimento
Senha dev: erp_desenvolvimento_dev
```

## Fluxo de restauracao

O script copia a base atual do ERP de desenvolvimento para o banco Docker do
cliente:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File D:\GitHub\erp-control-plane\scripts\dev\restore-erp-development-to-integrated-dev.ps1
```

Origem esperada pelo script:

```text
D:\GitHub\erp\core\.env
POSTGRES_HOST=192.168.0.8
POSTGRES_PORT=5432
POSTGRES_DB=erp_desenvolvimento
```

O script:

1. valida a origem com `pg_isready`;
2. gera dump custom com `pg_dump`;
3. reseta o schema `public` do destino;
4. restaura no banco Docker;
5. sobe `cp_web` e `erp_cliente_web`;
6. cria/atualiza o cliente `desenvolvimento` no CP;
7. sincroniza o snapshot do plano no ERP.

## Cliente CP inicial

```text
Slug: desenvolvimento
Nome: Desenvolvimento
Status: ativo
Plano: Pro
ERP: http://192.168.0.31:8002
```

URL externa do CP na rede local:

```text
http://192.168.0.31:8001
```

O snapshot do plano no ERP e escrito pelo CP em `core_planocliente`.
O ERP apenas exibe esse snapshot.

## Acesso externo

Para pgAdmin ou VM acessarem os servicos do Docker Desktop, liberar no Windows
da maquina `192.168.0.31`:

```powershell
New-NetFirewallRule `
  -DisplayName "Arara Integrated Dev PostgreSQL 5434" `
  -Direction Inbound `
  -Action Allow `
  -Protocol TCP `
  -LocalPort 5434

New-NetFirewallRule `
  -DisplayName "Arara Integrated Dev ERP 8002" `
  -Direction Inbound `
  -Action Allow `
  -Protocol TCP `
  -LocalPort 8002
```

## Validacoes

```powershell
docker compose -f D:\GitHub\erp-control-plane\docker-compose.integrated-dev.yml ps
Invoke-WebRequest http://192.168.0.31:8002/health/ -UseBasicParsing
Invoke-WebRequest http://127.0.0.1:8001/ -UseBasicParsing
```

Valide o SyncAgent da VM:

```powershell
$sec = ConvertTo-SecureString $env:SYNC_VM_PASSWORD -AsPlainText -Force
$cred = New-Object System.Management.Automation.PSCredential('codex_sync', $sec)

Invoke-Command -ComputerName 192.168.0.184 -Credential $cred -Authentication Basic -ScriptBlock {
    Invoke-RestMethod http://127.0.0.1:47891/status
}
```
