# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Visão geral

Este repositório é o **painel de controle SaaS** da plataforma AraraSuite — ele **não é o ERP em si**, mas o sistema que gerencia os clientes (tenants) e provisiona instâncias isoladas do ERP em Docker Swarm para cada um. Todo o código, labels e verbose_names estão em **português brasileiro**.

## Comandos de desenvolvimento

**Pré-requisito:** Docker Desktop em execução.

```powershell
# 1. Subir banco e Redis (necessário antes do runserver)
docker compose -f docker-compose.dev.yml up -d db redis

# 2. Servidor de desenvolvimento (porta padrão 8000 ou escolha outra)
.\venv\Scripts\python.exe manage.py runserver 8001

# 3. Migrations
.\venv\Scripts\python.exe manage.py migrate

# 4. Carregar dados iniciais (planos e módulos)
.\venv\Scripts\python.exe manage.py loaddata registry/fixtures/initial_data.json

# 5. Shell Django
.\venv\Scripts\python.exe manage.py shell

# 6. Worker Celery (em terminal separado, para dev local sem Docker)
# IMPORTANTE: no Windows usar --pool=solo (o prefork padrão falha com WinError 5/6)
.\venv\Scripts\python.exe -m celery -A core worker --loglevel=info --pool=solo

# 7. Rebuild da imagem dev do Celery (após mudanças em requirements.txt)
docker compose -f docker-compose.dev.yml build celery
docker compose -f docker-compose.dev.yml up -d celery
```

O banco PostgreSQL sobe na porta `5433` (mapeada de `5432` no container) e o Redis na `6380`. Configure no `.env` conforme `.env.example`.

**Variável crítica para dev local:** defina `PROVISIONING_CHECK_HTTP=false` no `.env` — sem isso, o provisionamento tenta acessar `{slug}.ararasuite.com.br` que não resolve localmente.

## Arquitetura

### Apps Django

- **`registry`** — núcleo do sistema. Modelos de negócio, lógica de provisionamento, tasks Celery, admin.
- **`landing`** — site público com formulário de cadastro que dispara o provisionamento.
- **`core`** — settings, URLs raiz (`core/urls.py`).

### Fluxo principal: cadastro → provisionamento → e-mail

```
POST /comecar/  →  view cadastro()
    └── Cliente.objects.create(status='aguardando_provisao')
            ├── signal post_save  (registry/signals.py)
            │       └── task_provisionar_cliente.delay(cliente.pk)  [Celery]
            │               └── MotorProvisionamento.executar()
            │                       ├── gera .env e stack.yml em CLIENTES_BASE_PATH/{slug}/
            │                       ├── docker stack deploy ... {slug}
            │                       ├── aguarda {slug}_web ficar Running (180s)
            │                       ├── aguarda GET /health/ → 200 (se CHECK_HTTP=true)
            │                       └── Cliente.status → 'ativo'
            └── task_enviar_email_boas_vindas.delay(cliente.pk)  [Celery]
                    └── enviar_email_boas_vindas()  (registry/email.py)
                            └── renderiza registry/email_boas_vindas.html → envia HTML
```

Cada etapa de provisionamento é registrada em `ProvisionamentoLog` via `MotorProvisionamento._log()`. Em falha, Celery retenta até 3x (delay 60s) e o status vai para `erro_provisao`.

### Modelos principais (`registry/models.py`)

| Modelo | Tabela | Descrição |
|---|---|---|
| `Cliente` | `registry_cliente` | Representa um cliente da plataforma |
| `Plano` | `registry_plano` | Planos comerciais com limites de recursos |
| `Modulo` | `registry_modulo` | Módulos ativáveis no ERP |
| `HostInfraestrutura` | `registry_hostinfraestrutura` | Nós do Swarm por região |
| `ProvisionamentoLog` | `registry_provisionamentolog` | Log por etapa do provisionamento |
| `AtualizacaoVersao` | `registry_atualizacaoversao` | Histórico de upgrades de versão |
| `VerificacaoSaude` | `registry_verificacao_saude` | Snapshots de health check |
| `ConfiguracaoEmail` | `registry_configuracaoemail` | Singleton com credenciais SMTP (pk=1 sempre) |
| `BackupCliente` | `registry_backupcliente` | Registro de cada backup por cliente (status, progresso, caminho do arquivo) |

### Tasks Celery (`registry/tasks.py`)

| Função | Descrição |
|---|---|
| `task_provisionar_cliente` | Disparo automático via signal ao criar `Cliente` |
| `task_atualizar_versao` | Atualiza imagem Docker de uma instância ativa |
| `task_suspender_cliente` | Escala `{slug}_web` para 0 réplicas |
| `task_reativar_cliente` | Escala `{slug}_web` para 1 réplica |
| `task_verificar_saude_todas` | Dispara health check em todos os clientes ativos/trial |
| `task_verificar_saude_cliente` | Faz GET `/health/` e grava `VerificacaoSaude` |
| `task_enviar_email_boas_vindas` | Renderiza e envia o e-mail HTML de boas-vindas |
| `task_backup_clientes` | Gera `.tar.gz` do `CLIENTES_BASE_PATH` e mantém N versões |
| `task_backup_cliente` | Backup completo de um cliente: pg_dump + mídia (`/app/media`) + config. Salvo em `CP_BACKUP_DIR/clientes/{slug}/`. Mantém 3 backups por cliente. |
| `task_restaurar_cliente` | Restaura um backup: escala web=0, restaura DB via psql, restaura mídia via container alpine, reinicia web=1. Sempre reinicia o web mesmo em caso de erro. |

### Infraestrutura por cliente

O `MotorProvisionamento` (`registry/provisioning.py`) gera um Docker Swarm stack com três serviços: `{slug}_web` (ERP Django), `{slug}_db` (PostgreSQL+pgvector), `{slug}_redis`. O placement usa `node.labels.region == {regiao}` do `HostInfraestrutura` associado. Os arquivos ficam em `CLIENTES_BASE_PATH/{slug}/`.

**Método `destruir()`:** remove o stack, aguarda 15s, remove volumes (`pgdata`, `media`, `backups`), apaga o diretório e deleta o registro do banco.

**Reuso de credenciais no re-provisionamento:** ao re-provisionar, o motor lê o `.env` existente e reaproveita `POSTGRES_PASSWORD` e `DJANGO_SUPERUSER_PASSWORD` para não conflitar com volumes pgdata já existentes.

### Admin do django-celery-beat

O admin do `django_celery_beat` é **completamente sobrescrito** em `registry/celery_beat_pt.py` e importado no final de `registry/admin.py`. Os verbose_names dos modelos são monkey-patched (ex: `PeriodicTask` → "Tarefa Periódica"). Não edite o admin do celery beat fora desse arquivo.

## Sistema de e-mail (`registry/email.py`)

**Regra crítica:** a configuração de e-mail vem **exclusivamente do banco** (`ConfiguracaoEmail`). Não há fallback para `.env` ou `settings.py`. Se não houver registro no banco, as funções lançam `RuntimeError`.

### Funções

| Função | Descrição |
|---|---|
| `obter_conexao_email()` | Retorna uma conexão SMTP configurada via DB |
| `enviar_email(assunto, corpo, destinatarios, *, html=False)` | Envia e-mail simples ou HTML |
| `enviar_email_boas_vindas(cliente)` | Renderiza `email_boas_vindas.html` e envia |

### Backend sem verificação SSL

`_SmtpSemVerificacaoSSL` — subclasse de `EmailBackend` que sobrescreve `ssl_context` com `check_hostname=False` e `CERT_NONE`. Usado quando `ConfiguracaoEmail.email_verificar_ssl=False`. Necessário para servidores com certificado self-signed ou hostname mismatch.

### Template do e-mail (`registry/templates/registry/email_boas_vindas.html`)

HTML puro com estilos inline (compatível com clientes de e-mail). Visual dark mode inspirado na landing page: fundo `#0f172a`, gradiente índigo→violeta, card de dados de acesso (URL, usuário `admin`, senha do `ProvisionamentoLog`), passos numerados e botão CTA.

## Admin (`registry/admin.py`)

### ClienteAdmin

- **`status_badge`** — pill colorida por status usando `_STATUS_CORES`
- **`painel_acesso`** — tabela com URL, usuário `admin` e senha inicial extraída do `ProvisionamentoLog` etapa `criar_superuser`
- **`badge_isencao`** — badge visual de situação de cobrança
- **`acoes_provisionamento`** — botões: ⚙ Aplicar Configurações, ☁ Configurar Cloudflare, Re-provisionar, Destruir (com confirm), ✉ Reenviar Boas-vindas
- **`lista_backups`** — seção "Backups" com botão 💾 Novo Backup, tabela de histórico com barra de progresso em tempo real (polling JSON a cada 2s), links ⬇ Baixar e ↩ Restaurar
- **Ações em lote:** `acao_reprovisionar`, `acao_destruir`
- **URLs customizadas:** `/<pk>/reprovisionar/`, `/<pk>/destruir/`, `/<pk>/reenviar-boas-vindas/`, `/<pk>/backup/`, `/<pk>/backups/<id>/download/`, `/<pk>/backups/<id>/restaurar/`, `/<pk>/backups/<id>/status/`

### ConfiguracaoEmailAdmin

- Singleton — botão "Add" oculto se já existe registro; Delete desativado
- Campo senha usa `PasswordInput(render_value=True)`
- Botão **"Enviar e-mail de teste para mim"** chama `/_view_testar_email/` de forma síncrona
- URL customizada: `/testar-email/`

## Sidebar (UNFOLD)

Definida em `core/settings.py` via `UNFOLD["SIDEBAR"]["navigation"]`. Grupos:

| Grupo | Itens |
|---|---|
| Clientes | Clientes, Planos, Módulos, Hosts de Infraestrutura, Verificações de Saúde |
| Agendamentos | Tarefas Periódicas, Intervalos, Crontabs |
| Configurações | E-Mail, Ajuda |

CSS customizado em `registry/static/registry/admin_custom.css` — melhora visibilidade de inputs no dark mode (bordas e fundo distintos do background).

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

Pelo mesmo motivo, evite `print()` com caracteres especiais (`→`, `✓`, etc.) em shells Windows — use strings ASCII no console.

### Updates no Celery

Dentro de tasks Celery, nunca use `instance.save(update_fields=[...])` — use sempre `Modelo.objects.filter(pk=id).update(...)`. O objeto em memória fica obsoleto entre retentativas e `save(update_fields=)` levanta `NotUpdated` se nenhuma linha for afetada.

### `docker stack deploy` em retentativas

O deploy é idempotente mas retorna erro se algum serviço já existe. O `MotorProvisionamento` trata isso ignorando erros que contenham `'AlreadyExists'` no stderr.

### Worker Celery e dependências

O worker inicializa `django.setup()` completo, o que importa todos os `INSTALLED_APPS` inclusive `unfold`. Se a imagem Docker do worker for antiga (antes de `unfold` ser adicionado), ela vai falhar na inicialização. Solução: `docker compose -f docker-compose.dev.yml build celery`.

### Worker Celery no Windows (dev local)

O pool padrão `prefork` do Celery falha no Windows com `WinError 5 (Acesso negado)` e `WinError 6 (Identificador inválido)` nos processos filhos (`billiard`). Sempre usar `--pool=solo` em dev no Windows:

```powershell
.\venv\Scripts\python.exe -m celery -A core worker --loglevel=info --pool=solo
```

Em produção (Linux/Docker) o prefork padrão funciona normalmente — não adicionar `--pool=solo` na imagem de produção.

### Backup completo por cliente

O `task_backup_cliente` cria um `.tar.gz` em `CP_BACKUP_DIR/clientes/{slug}/` contendo três entradas:

| Entrada no arquivo | Origem | Conteúdo |
|---|---|---|
| `db.sql` | `docker exec {slug}_db pg_dump --clean --if-exists` | Dump completo do PostgreSQL (inclui pgvector/RAG) |
| `media/` | `docker cp {slug}_web:/app/media/.` | Arquivos enviados pelos usuários |
| `config/` | `CLIENTES_BASE_PATH/{slug}/` | `.env` e `stack.yml` do cliente |

**Retenção:** 3 backups por cliente (arquivos e registros no banco). O excedente é removido automaticamente.

**Progresso:** o campo `BackupCliente.progresso` (0–100) é atualizado em cada etapa. O admin faz polling a cada 2s no endpoint `/<pk>/backups/<id>/status/` e recarrega a página ao concluir.

**Restauração:** `task_restaurar_cliente` escala `{slug}_web=0`, restaura o banco via `psql` e a mídia via container alpine temporário com acesso direto ao volume `{slug}_media`, depois reinicia `{slug}_web=1`. O serviço web é sempre reiniciado mesmo em caso de erro parcial.

**Comportamento em dev local:** sem containers Docker ativos, as etapas de DB e mídia são puladas silenciosamente. O backup conclui em < 1s e a barra de progresso vai direto ao 100% — isso é esperado. Em produção com dados reais, o progresso é visível entre os checkpoints.

### Auth do registry privado no Swarm

O `stack-prod.yml` monta `/root/.docker` do host manager nos containers `web` e `celery` com `read_only: true`. Isso permite que `docker stack deploy --with-registry-auth` embuta as credenciais do DockerHub e os workers Swarm consigam puxar a imagem privada.

### Label do Traefik e troca de domínio

O botão **"Aplicar Configurações"** (`_view_aplicar_modulos`) atualiza via `docker service update --env-add` as variáveis `ALLOWED_HOSTS`, `CSRF_TRUSTED_ORIGINS`, `MODULOS_ATIVOS` e `TEMA_SITE`. Ele também **sempre** atualiza a label do router principal do Traefik:

```python
'--label-add', f'traefik.http.routers.{slug}-erp.rule=Host(`{subdominio}`)',
```

**Por que isso importa:** se o `subdominio` do cliente for alterado no admin (ex.: migração de domínio de `ararasuite.com.br` para `jfnbrasil.com.br`), apenas atualizar o campo no banco não basta — a label do Traefik no serviço Docker ainda aponta para o domínio antigo. O wildcard DNS do domínio antigo continua roteando tráfego para o container, que agora rejeita o host com `DisallowedHost`. Clicar em "Aplicar Configurações" corrige ALLOWED_HOSTS e a label do Traefik de uma vez.

## Variáveis de ambiente relevantes

| Variável | Padrão | Descrição |
|---|---|---|
| `PROVISIONING_CHECK_HTTP` | `true` | Setar `false` em dev local |
| `CLIENTES_BASE_PATH` | `/opt/clientes` | Diretório base dos stacks por cliente |
| `ERP_DOMAIN` | `ararasuite.com.br` | Domínio base dos subdominios |
| `SAAS_DOMAIN` | `ararasuite.com.br` | Usado na landing para preview de subdomínio |
| `ERP_LATEST_VERSION` | `0.0.22` | Versão do ERP para novos clientes |
| `CP_BACKUP_DIR` | `/opt/backups/cp` | Destino dos backups do CLIENTES_BASE_PATH |
| `CP_BACKUP_MANTER` | `7` | Quantos backups do CLIENTES_BASE_PATH retener (task_backup_clientes) |
