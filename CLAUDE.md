# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Visão geral

Este repositório é o **painel de controle SaaS** da plataforma AraraSuite — ele **não é o ERP em si**, mas o sistema que gerencia os clientes (tenants) e provisiona instâncias isoladas do ERP em Docker Swarm para cada um. Todo o código, labels e verbose_names estão em **português brasileiro**.

## Comandos de desenvolvimento

**Pré-requisito:** Docker Desktop em execução.

```powershell
# 1. Subir banco e Redis (necessário antes do runserver)
docker compose -f docker-compose.dev.yml up -d db redis

# 2. Servidor de desenvolvimento
.\venv\Scripts\python.exe manage.py runserver

# 3. Migrations
.\venv\Scripts\python.exe manage.py migrate

# 4. Carregar dados iniciais (planos e módulos)
.\venv\Scripts\python.exe manage.py loaddata registry/fixtures/initial_data.json

# 5. Shell Django
.\venv\Scripts\python.exe manage.py shell
```

O banco PostgreSQL sobe na porta `5433` (mapeada de `5432` no container) e o Redis na `6380`. Configure no `.env` conforme `.env.example`.

**Variável crítica para dev local:** defina `PROVISIONING_CHECK_HTTP=false` no `.env` — sem isso, o provisionamento tenta acessar `{slug}.ararasuite.com.br` que não resolve localmente.

## Arquitetura

### Apps Django

- **`registry`** — núcleo do sistema. Modelos de negócio, lógica de provisionamento, tasks Celery, admin.
- **`landing`** — site público com formulário de cadastro que dispara o provisionamento.
- **`core`** — settings, URLs raiz, configuração do Celery (`core/celery.py`).

### Fluxo principal: cadastro → provisionamento

```
POST /comecar/  →  view cadastro()
    └── Cliente.objects.create(status='aguardando_provisao')
            └── signal post_save  (registry/signals.py)
                    └── task_provisionar_cliente.delay(cliente.pk)  [Celery]
                            └── MotorProvisionamento.executar()
                                    ├── gera .env e stack.yml em CLIENTES_BASE_PATH/{slug}/
                                    ├── docker stack deploy ... {slug}
                                    ├── aguarda {slug}_web ficar Running (180s)
                                    ├── aguarda GET /health/ → 200 (se CHECK_HTTP=true)
                                    └── Cliente.status → 'ativo'
```

Cada etapa é registrada em `ProvisionamentoLog` via `MotorProvisionamento._log()`. Em falha, Celery retenta até 3x (delay 60s) e o status vai para `erro_provisao`.

### Modelos principais (`registry/models.py`)

| Modelo | Tabela | Descrição |
|---|---|---|
| `Cliente` | `registry_cliente` | Representa um cliente da plataforma (ex-`Tenant`) |
| `Plano` | `registry_plano` | Planos comerciais com limites de recursos |
| `Modulo` | `registry_modulo` | Módulos ativáveis no ERP |
| `HostInfraestrutura` | `registry_hostinfraestrutura` | Nós do Swarm por região |
| `ProvisionamentoLog` | `registry_provisionamentolog` | Log por etapa do provisionamento |
| `AtualizacaoVersao` | `registry_atualizacaoversao` | Histórico de upgrades de versão |
| `VerificacaoSaude` | `registry_verificacao_saude` | Snapshots de health check |

### Tasks Celery (`registry/tasks.py`)

| Função | Descrição |
|---|---|
| `task_provisionar_cliente` | Disparo automático via signal ao criar `Cliente` |
| `task_atualizar_versao` | Atualiza imagem Docker de uma instância ativa |
| `task_suspender_cliente` | Escala `{slug}_web` para 0 réplicas |
| `task_reativar_cliente` | Escala `{slug}_web` para 1 réplica |
| `task_verificar_saude_todas` | Dispara health check em todos os clientes ativos/trial |
| `task_verificar_saude_cliente` | Faz GET `/health/` e grava `VerificacaoSaude` |

### Infraestrutura por cliente

O `MotorProvisionamento` (`registry/provisioning.py`) gera um Docker Swarm stack com três serviços: `{slug}_web` (ERP Django), `{slug}_db` (PostgreSQL+pgvector), `{slug}_redis`. O placement usa `node.labels.region == {regiao}` do `HostInfraestrutura` associado. Os arquivos ficam em `CLIENTES_BASE_PATH/{slug}/`.

### Admin do django-celery-beat

O admin do `django_celery_beat` é **completamente sobrescrito** em `registry/celery_beat_pt.py` e importado no final de `registry/admin.py`. Os verbose_names dos modelos são monkey-patched (ex: `PeriodicTask` → "Tarefa Periódica"). Não edite o admin do celery beat fora desse arquivo.

## Padrões importantes

### Encoding no Windows

Ao gerar fixtures no Windows, **nunca** use redirecionamento de shell (`>` ou `Out-File`) — isso usa `cp1252` e corrompe caracteres acentuados. Use sempre o script via `manage.py shell`:

```python
import io
from django.core.management import call_command

buf = io.StringIO()
call_command('dumpdata', 'registry.Modulo', 'registry.Plano', '--indent', '2', stdout=buf)
with open('registry/fixtures/initial_data.json', 'w', encoding='utf-8', newline='\n') as f:
    f.write(buf.getvalue())
```

### Updates no Celery

Dentro de tasks Celery, nunca use `instance.save(update_fields=[...])` — use sempre `Modelo.objects.filter(pk=id).update(...)`. O objeto em memória fica obsoleto entre retentativas e `save(update_fields=)` levanta `NotUpdated` se nenhuma linha for afetada.

### `docker stack deploy` em retentativas

O deploy é idempotente mas retorna erro se algum serviço já existe. O `MotorProvisionamento` trata isso ignorando erros que contenham `'AlreadyExists'` no stderr.

### FKs e related_names

- `ProvisionamentoLog.cliente` → `related_name='logs'`
- `AtualizacaoVersao.cliente` → `related_name='atualizacoes'`
- `VerificacaoSaude.cliente` → `related_name='verificacoes_saude'`

## Variáveis de ambiente relevantes

| Variável | Padrão | Descrição |
|---|---|---|
| `PROVISIONING_CHECK_HTTP` | `true` | Setar `false` em dev local |
| `CLIENTES_BASE_PATH` | `/opt/clientes` | Diretório base dos stacks por cliente |
| `ERP_DOMAIN` | `ararasuite.com.br` | Domínio base dos subdominios |
| `SAAS_DOMAIN` | `ararasuite.com.br` | Usado na landing para preview de subdomínio |
| `ERP_LATEST_VERSION` | `0.0.22` | Versão do ERP para novos clientes |
