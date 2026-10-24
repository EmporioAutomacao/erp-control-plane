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

# 4. Carregar dados iniciais (planos e módulos) — só cria os ausentes, nunca sobrescreve
#    (NÃO usar loaddata: ele reverte preços/edições do admin; o entrypoint.sh roda este comando a cada boot)
.\venv\Scripts\python.exe manage.py carregar_dados_iniciais

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
| `Modulo` | `registry_modulo` | Módulos ativáveis no ERP (ver tabela abaixo) |
| `HostInfraestrutura` | `registry_hostinfraestrutura` | Nós do Swarm por região |
| `ProvisionamentoLog` | `registry_provisionamentolog` | Log por etapa do provisionamento |
| `AtualizacaoVersao` | `registry_atualizacaoversao` | Histórico de upgrades de versão |
| `VerificacaoSaude` | `registry_verificacao_saude` | Snapshots de health check |
| `ConfiguracaoEmail` | `registry_configuracaoemail` | Singleton com credenciais SMTP (pk=1 sempre) |
| `ConfiguracaoAtualizacao` | `registry_configuracaoatualizacao` | Singleton (pk=1) da tela Verificar Atualizações: repositório Docker Hub do CP, usuário/token (criptografado, opcionais), status da conexão, versão disponível em cache e registro da última atualização iniciada pelo painel |
| `ConfiguracaoArmazenamento` | `registry_configuracaoarmazenamento` | Singleton (pk=1) com a conexão S3/RustFS dos backups (endpoint, bucket, access key, secret key **criptografada**) e o sinalizador `status_conexao` |
| `BackupCliente` | `registry_backupcliente` | Registro de cada backup por cliente (status, progresso, caminho do arquivo) |
| `InstalacaoAgente` | `registry_instalacaoagente` | Cache das `SyncInstallation` do ERP de um cliente (uma por loja/máquina) — versão do SyncAgent/PDV instalada. Reconciliado por `registry.cp_pull.ColetorVersoes` |

### Módulos disponíveis (`registry/fixtures/initial_data.json`)

| `slug` | Nome | Descrição |
|---|---|---|
| `assistente_comercial` | Assistente Comercial | IA para suporte e sugestões no processo de vendas |
| `caixa` | Caixa | Frente de caixa (PDV) para registro de vendas presenciais |
| `cobranca` | Cobrança | Gestão de cobranças, boletos e inadimplência |
| `compras` | Compras | Pedidos de compra e controle de fornecedores |
| `contratos` | Contratos | Emissão e acompanhamento de contratos com clientes |
| `dedetizacao` | Dedetização | Gestão de ordens de serviço de dedetização/controle de pragas |
| `ecommerce` | E-Commerce | Loja virtual integrada ao estoque do ERP |
| `financeiro` | Financeiro | Contas a pagar/receber, fluxo de caixa e conciliação |
| `flexo` | Flexo | Módulo flexível para fluxos customizados |
| `marketing` | Marketing | Campanhas, segmentação de clientes e automações |
| `mercadolivre` | Mercado Livre | Integração com anúncios e pedidos do Mercado Livre |
| `metas` | Metas Gamificadas | Definição e acompanhamento de metas com ranking |
| `notificacoes` | Notificações | Alertas internos e disparo de mensagens aos clientes |
| `ordens_servico` | Ordens de Serviço | Gestão de ordens de serviço genéricas, com fluxo simples ou por processo configurável |
| `radar_empresarial` | Radar Empresarial | Painel de indicadores estratégicos do negócio |
| `rh` | Recursos Humanos | Funcionários, folha de pagamento, benefícios e documentos de RH |
| `tarefas` | Tarefas | Gestão de tarefas e checklist por equipe |
| `yampi` | Yampi | Integração com a plataforma de e-commerce Yampi |

Todos os módulos têm `preco_mensal=0.00` — a cobrança é feita pelo plano. Planos definem quais módulos estão inclusos por padrão; o admin pode adicionar/remover módulos individualmente em cada cliente.

### Tasks Celery (`registry/tasks.py`)

| Função | Descrição |
|---|---|
| `task_provisionar_cliente` | Disparo automático via signal ao criar `Cliente` |
| `task_atualizar_versao` | Atualiza imagem Docker de uma instância ativa |
| `task_suspender_cliente` | Escala `{slug}_web` para 0 réplicas |
| `task_reativar_cliente` | Escala `{slug}_web` para 1 réplica |
| `task_verificar_saude_todas` | Dispara health check em todos os clientes ativos/trial |
| `task_verificar_saude_cliente` | Faz GET `/health/`, grava `VerificacaoSaude` e — de graça, do corpo `{"version": ...}` — atualiza `Cliente.versao_erp_detectada` |
| `task_coletar_versoes_cliente` | Consulta `GET /v1/cp/status` (fallback `/health/`) e grava a versão do ERP em execução + reconcilia `InstalacaoAgente`. Ver `registry.cp_pull.ColetorVersoes` |
| `task_coletar_versoes_todas` | Dispara `task_coletar_versoes_cliente` em todos os clientes ativos/trial. Agendar no django-celery-beat (ex.: a cada 30 min), como o health check |
| `task_enviar_email_boas_vindas` | Renderiza e envia o e-mail HTML de boas-vindas |
| `task_backup_clientes` | Gera `.tar.gz` do `CLIENTES_BASE_PATH` e mantém N versões |
| `task_iniciar_backup_cliente` | Etapa do manager: lê o config do cliente e despacha `task_backup_cliente` para a fila do nó do cliente (`registry/nos.py`). É o que o botão do admin chama. |
| `task_backup_cliente` | Roda **no nó do cliente** (fila `no.<hostname>`): pg_dump + mídia (`/app/media`) + config (recebido por argumento). Envia o `.tar.gz` ao bucket S3 (RustFS) e mantém 3 backups por cliente. |
| `task_restaurar_cliente` | Etapa do manager: escala web=0 e despacha `task_restaurar_no` para a fila do nó. |
| `task_restaurar_no` | Roda no nó: baixa o backup do bucket, restaura DB via psql e mídia via container alpine. Sempre aciona `task_finalizar_restauracao`, mesmo com erro. |
| `task_verificar_armazenamento` | Testa a conexão com o bucket e atualiza o sinalizador de `ConfiguracaoArmazenamento`. Agendar no django-celery-beat (ex.: a cada 15 min), como o health check |
| `task_verificar_atualizacao` | Consulta o Docker Hub e grava a versão disponível do CP (Configurações > Verificar Atualizações). Agendar no django-celery-beat (ex.: a cada 6 h) |
| `task_finalizar_restauracao` | Etapa do manager: reinicia web=1 e libera o backup. |

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
- **Página do Cliente em abas (Unfold):** Cadastro · Acesso e Infraestrutura · Plano e Faturamento · Versões · Status e Ações · Backups (fieldsets com `classes: ['tab']`; os campos das antigas 11 seções foram fundidos, sem perder nenhum) e os históricos como abas (`tab = True` nos inlines). `registry/static/registry/abas_cliente.js` lembra a aba e abre **Backups** ao acompanhar uma operação.
- **`lista_backups`** — seção "Backups" com botão 💾 Novo Backup, **console ao vivo** (ver "Backup completo por cliente"), tabela de histórico com barra de progresso, 📜 Log por backup, links ⬇ Baixar e ↩ Restaurar
- **`versao_atual`** — seção "Versão Atual": 3 linhas resumo (ERP em execução × alvo, SyncAgent, PDV Local) + tabela de todas as `InstalacaoAgente` (instalação, versão, visto por último, badge de conectividade) + botão 🔄 Verificar versões agora (`/<pk>/coletar-versoes/` → `task_coletar_versoes_cliente`, redireciona com `?coletando=1` e a página recarrega em ~5s). Dados vêm de `registry.cp_pull` — ver seção abaixo.
- **Ações em lote:** `acao_reprovisionar`, `acao_destruir`
- **URLs customizadas:** `/<pk>/reprovisionar/`, `/<pk>/destruir/`, `/<pk>/reenviar-boas-vindas/`, `/<pk>/coletar-versoes/`, `/<pk>/backup/`, `/<pk>/backups/<id>/download/`, `/<pk>/backups/<id>/restaurar/`, `/<pk>/backups/<id>/status/`

### ConfiguracaoAtualizacaoAdmin (Configurações > Verificar Atualizações)

- Singleton; o item da sidebar redireciona direto ao formulário. Mostra **versão atual** (`settings.VERSION`), **versão disponível** (maior tag `X.Y.Z` do repositório no Docker Hub, comparação numérica) e a situação (Atualizado / Atualização disponível / Não foi possível verificar / Não verificado), atualizadas ao vivo ao abrir a tela (`verificar/`, cache de 60 s; **Verificar agora** força).
- A lista de tags de `emporioautomacao/ararasuite-cp` é **pública**: usuário/token do Docker Hub são opcionais (token criptografado, nunca reexibido).
- **Botão Atualizar** (`atualizar/<versao>/`: GET confirma, POST executa; **só superusuário**): lógica em `registry/atualizacoes.py`. Um **container auxiliar destacado** (imagem atual do `web`, `docker.sock` + `/root/.docker`) roda `docker service update --with-registry-auth --update-failure-action rollback --detach=false --image <repo>:<versão>` em `celery` → `beat` → `agente-no` → `web` (web por último; `db`/`redis` nunca). Recusa downgrade/mesma versão, versão inexistente no Docker Hub, ambiente sem Swarm manager, stack não identificado e atualização em andamento. Em dev (sem Swarm) o botão não aparece.
- O log fica no container auxiliar (`cp-atualizador-<versão>-<ts>`, sem `--rm`) e a tela mostra Em andamento / Concluída / Falhou com as últimas linhas.
- **Depois de atualizar, ajustar `CP_VERSION` no stack do Portainer** — o painel altera os serviços direto no Swarm; um redeploy do stack com `CP_VERSION` antigo desfaria a atualização. **Exceção:** se o `web` roda em `latest` (`atualizacoes.stack_usa_latest`; guarda a tag anterior em `atualizacao_tag_anterior`), o aviso é trocado por "um redeploy com Re-pull image também traz a versão mais recente". Doc: `docs/registry/release-verificar-atualizacoes.md`.

### ConfiguracaoArmazenamentoAdmin (Configurações > Armazenamento)

- Singleton (Add oculto se já existe; Delete desativado). Campos: endpoint de acesso, endpoint público, bucket, access key, secret key, região.
- **Secret key criptografada** (`registry/cripto.py`: Fernet derivado de `SECRET_KEY`); o campo nunca reexibe o valor e, vazio, mantém o atual. Trocar a `SECRET_KEY` do CP exige salvar a secret de novo.
- **Sinalizador de status** (pill 🟢 Conectado / 🟡 Atenção / 🔴 Erro / ⚪ Não testado + mensagem): o change form consulta `verificar/` (JSON) ao abrir e atualiza ao vivo; também no `list_display`. Salvar roda o teste; botão **🔌 Testar conexão agora** (`testar-conexao/`).
- Teste (`storage_backup.testar_conexao`): `head_bucket` → grava/lê/apaga um objeto em `_cp-healthcheck/` → `GET <endpoint público>/health` (falha aqui = *Atenção*, não *Erro*). Envio de backup bem-sucedido chama `registrar_envio_ok` (não apaga um *Atenção*); falha de envio vira *Erro*.
- URLs: `/testar-conexao/`, `/verificar/`.

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
| Configurações | E-Mail, Cloudflare, Armazenamento, Verificar Atualizações, Ajuda |

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

**Multi-nó (Swarm):** `docker ps/exec/cp/run` só enxergam o daemon do nó local, e os serviços do cliente rodam em workers (`node.labels.region`), não no manager onde o Celery principal roda. Por isso o backup/restauração roda no **worker Celery do nó do cliente**:

- `registry/nos.py::fila_do_cliente(slug)` descobre o nó via `docker service ps {slug}_db` e devolve a fila `no.<hostname>`; sem Swarm (dev) ou se o serviço não for achado, cai em `celery`.
- `stack-prod.yml`: o serviço `agente-no` (`mode: global`, `node.role != manager`, **`node.platform.arch == x86_64`** — a imagem do CP é só `linux/amd64`; um nó ARM ficaria com a tarefa em `pending` para sempre, entrypoint `entrypoint-agente.sh`, sem `migrate`) consome só `no.<hostname>`. O `celery` do manager consome `celery,no.<hostname-do-manager>`. O `agente-no` **não monta pasta do host** (`CP_BACKUP_DIR=/tmp/cp-backups` dentro do container), então um worker novo não exige nenhuma preparação: só entrar no Swarm. No manager, `web`/`celery` continuam com o bind de `/opt/backups/cp`.
- O `.env`/`stack.yml` do cliente só existem no manager: `task_iniciar_backup_cliente` os lê e passa em `config_files` (base64) para a task do nó.
- **Bucket:** `registry/storage_backup.py` (boto3, path-style) lê a conexão de `ConfiguracaoArmazenamento` (Admin > Configurações > Armazenamento) — **não há variáveis de ambiente nem fallback**; sem cadastro completo, o backup falha cedo com "Armazenamento de backups não configurado". `BackupCliente.arquivo_path` guarda `s3://clientes/{slug}/arquivo.tar.gz` e o disco do nó é só temporário (backups antigos com caminho local continuam sendo baixados/restaurados). Download no admin = redirect para link temporário (presigned, usando o endpoint público). Upload manual também vai ao bucket. Retenção apaga os objetos excedentes.
- **RustFS em produção:** `stack-rustfs.yml` (colar no Portainer; serviço fixo em `servidorubuntuanptruenas`, volume local, Traefik `web`). O CP **não depende de rede compartilhada** com o RustFS: na tela de Armazenamento use o endereço público (`https://<S3_DOMAIN>`) como endpoint de acesso e como endpoint público. A rede overlay `storage-net` (criada pelo `stack-rustfs.yml`) é só um atalho interno **opcional** (`http://rustfs-s3:9000`) que exige anexá-la a `web`, `celery` e `agente-no`. Detalhes na seção 10 do guia.
- `BackupCliente.no` guarda o hostname do nó que gerou o backup.
- Limitação: se o worker do nó não estiver rodando, a task fica na fila e (na restauração) o web do cliente permanece em 0 réplicas até o agente voltar.
- **Console ao vivo** (tela do Cliente, aba Backups): `BackupCliente.log`/`log_restauracao` recebem uma linha por etapa (`registry/log_operacao.py::LogOperacao`, `.update()` + `Concat`, nunca `save(update_fields=)`) e `atividade_em` é o heartbeat. Comandos longos (pg_dump, docker cp, compactação, upload/download, psql) rodam via `rodar_com_pulso()` numa **thread** — **todo acesso ao banco fica na thread principal** (a thread só executa o comando; a `ConfiguracaoArmazenamento` é resolvida antes). O JS está em `registry/console_backup.py` (polling de `…/backups/<id>/status/`: `log`, `decorrido_s`, `sem_atividade_s`, `aguardando_worker`, `sem_inicio`, `ativo_operacao`). Estados: ● executando (<30 s sem atividade), ⚠ possível travamento (≥30 s), ✖ provavelmente travado + **Interromper** (≥120 s; `POST …/interromper/`, na restauração religa o web). Abre via `?acompanhar=<id>[&op=restauracao]` ou quando há operação ativa; ao terminar atualiza só a tabela (`#bk-tabela`). Dump compactado com gzip **nível 6** (o 9 levava dezenas de segundos). Doc: `docs/registry/release-console-backup-ao-vivo.md`.
- Guia completo (deploy, verificação, troubleshooting): `docs/registry/backup-multi-no.md`.

O `task_backup_cliente` cria um `.tar.gz` (em `CP_BACKUP_DIR/clientes/{slug}/` no nó, até o envio ao bucket) contendo três entradas:

| Entrada no arquivo | Origem | Conteúdo |
|---|---|---|
| `db.sql` | `docker exec {slug}_db pg_dump --clean --if-exists` | Dump completo do PostgreSQL (inclui pgvector/RAG) |
| `media/` | `docker cp {slug}_web:/app/media/.` | Arquivos enviados pelos usuários |
| `config/` | `CLIENTES_BASE_PATH/{slug}/` | `.env` e `stack.yml` do cliente |

**Retenção:** 3 backups por cliente (arquivos e registros no banco), **apenas entre backups `origem='automatico'`**. O excedente é removido automaticamente. Backups `origem='manual'` (ver "Importar backup" abaixo) nunca entram nessa contagem nem são apagados por ela.

**Progresso:** o campo `BackupCliente.progresso` (0–100) é atualizado em cada etapa. O admin faz polling a cada 2s no endpoint `/<pk>/backups/<id>/status/` e recarrega a página ao concluir.

**Backups parciais e vazios:** cada etapa (DB, mídia, config) que não encontra seu container/diretório, ou cujo comando (`pg_dump`/`docker cp`) falha, é registrada em uma lista de avisos. Se **nenhuma** etapa capturar conteúdo, o backup **não** é marcado como concluído — vai para `status='erro'` com `mensagem` explicando o motivo (arquivo `.tar.gz` é removido). Se ao menos uma etapa capturou algo, o backup fica `status='concluido'`, mas os avisos das etapas que falharam ficam em `mensagem` e aparecem como um aviso ⚠ na lista de backups do admin. Isso vale tanto para dev local sem containers ativos quanto para falhas reais em produção (ex: `pg_dump` com senha incorreta, container derrubado no meio do `docker cp`). Em dev local sem nenhum container do cliente e sem `CLIENTES_BASE_PATH/{slug}/`, o resultado esperado hoje é `status='erro'` (e não mais um arquivo vazio "concluído").

**Importar backup existente:** o botão "⬆ Importar Backup" (`_view_upload_backup`, `registry/admin.py`) permite enviar um `.tar.gz`/`.tgz` do PC do usuário. O arquivo é gravado via streaming (`request.FILES['arquivo'].chunks()`, sem storage/`FileField` do Django — o projeto não usa `MEDIA_ROOT`) em `CP_BACKUP_DIR/clientes/{slug}/manual_{slug}_{timestamp}.tar.gz`, e o `BackupCliente` criado com `origem='manual'`. A validação é só estrutural (`tarfile.is_tarfile` + checar se há `db.sql`/`media/` entre os membros) — não garante compatibilidade com a versão do ERP do cliente; ausências viram aviso em `mensagem`, sem bloquear o upload. Em produção, arquivos muito grandes podem esbarrar no timeout do gunicorn (`--timeout 120` em `entrypoint.sh`) — não tratado, é uma limitação conhecida.

**Restauração:** `task_restaurar_cliente` (manager) escala `{slug}_web=0` e `task_restaurar_no` (nó) restaura o banco via `psql -v ON_ERROR_STOP=1` e a mídia via container auxiliar (alpine) que monta o volume `{slug}_media` e recebe os arquivos por `docker cp` (`tasks._restaurar_midia`; **sem bind mount de pasta do host**), depois `task_finalizar_restauracao` (manager) reinicia `{slug}_web=1`. O serviço web é sempre reiniciado mesmo em caso de erro parcial. Assim como no backup, cada etapa que não encontra o container ou cujo comando falha vira um aviso — gravado em `BackupCliente.mensagem_restauracao` (campo separado de `mensagem`, que é sobre a *criação* do backup) e exibido como um segundo ⚠ na lista do admin. Mensagem vazia = última restauração sem erros.

**Caminhos absolutos nos comandos Docker (Windows):** `backup_base`/`restore_dir` são construídos com `.resolve()` sobre `Path(os.getenv('CP_BACKUP_DIR', '/opt/backups/cp'))`. Sem isso, no Windows, `Path('/opt/backups/cp')` produz um caminho "enraizado mas sem drive letter" (`\opt\backups\cp`, sem `D:`) — o Python consegue ler/escrever nele normalmente (resolve contra o drive do processo), mas `docker run -v <path>:...` rejeita esse formato com "not a valid Windows path". Isso já causou uma restauração de mídia silenciosamente quebrada em dev local (o `docker cp` do backup tolerou o caminho por sorte de CWD; o `docker run -v` da restauração, não).

### Auth do registry privado no Swarm

O `stack-prod.yml` monta `/root/.docker` do host manager nos containers `web` e `celery` com `read_only: true`. Isso permite que `docker stack deploy --with-registry-auth` embuta as credenciais do DockerHub e os workers Swarm consigam puxar a imagem privada.

### Label do Traefik e troca de domínio

O botão **"Aplicar Configurações"** (`_view_aplicar_modulos`) atualiza via `docker service update --env-add` as variáveis `ALLOWED_HOSTS`, `CSRF_TRUSTED_ORIGINS`, `SITE_URL`, `MODULOS_ATIVOS`, `TEMA_SITE`, `CP_CLIENTE_ID` e `CP_CLIENTE_NOME`. Ele também **sempre** atualiza a label do router principal do Traefik:

```python
'--label-add', f'traefik.http.routers.{slug}-erp.rule=Host(`{subdominio}`)',
```

**Por que isso importa:** se o `subdominio` do cliente for alterado no admin (ex.: migração de domínio de `ararasuite.com.br` para `jfnbrasil.com.br`), apenas atualizar o campo no banco não basta — a label do Traefik no serviço Docker ainda aponta para o domínio antigo. O wildcard DNS do domínio antigo continua roteando tráfego para o container, que agora rejeita o host com `DisallowedHost`. Clicar em "Aplicar Configurações" corrige ALLOWED_HOSTS e a label do Traefik de uma vez.

### `SITE_URL` da instância (URL pública do ERP)

`registry/provisioning.py::site_url_cliente(cliente)` = `https://<subdominio>` — **sempre o subdomínio**, nunca o domínio próprio (o domínio próprio depende de o cliente trocar os nameservers no Cloudflare; um link já enviado com DNS pendente quebraria). Gravado no `.env`/`environment` no provisionamento e reaplicado pelo **"Aplicar Configurações"** (clientes já provisionados só recebem ao clicar no botão). O ERP usa em tudo que sai sem request: link de peça de campanha no WhatsApp, imagens do HTML no e-mail, "abra no navegador", QR do portal de O.S., PDFs por cron. Doc: `docs/registry/site-url-instancias.md`.

### Vínculo CP → ERP para a ativação do SyncAgent (`CP_CLIENTE_ID`)

O `MotorProvisionamento` grava `CP_CLIENTE_ID` (= `str(cliente.id)`, o UUID do `Cliente` no CP) e `CP_CLIENTE_NOME` (= `cliente.nome`) no `.env`/`environment` do stack do cliente. O ERP lê essas variáveis em `core/settings.py` (`CP_CLIENTE_ID`/`CP_CLIENTE_NOME`) e `sync_api.cp_tenant.get_current_cp_tenant()` as usa como fonte primária do ID do cliente ao gerar códigos de ativação do SyncAgent (Configurações > Sincronização > Códigos de ativação).

Sem essas variáveis, o ERP cai no fallback `core.PlanoCliente.cp_cliente_id` — que **não** é populado em produção (só o comando de dev `bootstrap_integrated_dev_cliente` escreve nele) — e a tela de ativação bloqueia com *"Configure o ID do cliente no CP em Configurações > Plano..."*.

Clientes provisionados **antes** desta mudança não têm as variáveis: rodar **"Aplicar Configurações"** injeta `CP_CLIENTE_ID`/`CP_CLIENTE_NOME` no serviço `{slug}_web` existente.

**`erp_api_base_url` do `activation:complete` (ERP 0.0.96):** o ERP roda `daphne` atrás do Traefik (entrypoint `web`, porta 80; TLS terminado no Cloudflare). O Traefik reescreve `X-Forwarded-Proto=http`, então `request.build_absolute_uri()` devolvia `http://` — o SyncAgent recusa uma `erp_api_base_url` http de domínio público com `invalid_response`. `sync_api/views.py::_resolve_external_base_url` (ERP 0.0.96) força `https` para qualquer host que não seja localhost/IP privado. `SECURE_PROXY_SSL_HEADER` (0.0.95) sozinho não resolve porque o header chega `http`.

**mTLS:** o instalador do SyncAgent gravava `ErpSecurity:RequireMutualTls=true`; como o ERP não faz mTLS, a sync pós-ativação parava com *"Client certificate is required"*. Corrigir com `"RequireMutualTls": false` no `appsettings.json` do agente (instalador pdv-local ≥ 1.3.2 já usa `false`).

**Erros de ativação:** catálogo completo na seção "Sincronização — ativação do SyncAgent" da página de Ajuda do admin, e no guia canônico `docs/infra/sync-agent-ativacao-troubleshooting.md` do repo `erp`.

### Curadoria de versões do SyncAgent/PDV por cliente

Guia completo: `docs/registry/release-curadoria-versoes-syncagent.md`. Resumo:

- **`VersaoAgente`** (`registry/models.py`) — catálogo mestre global de versões do `pdv-local` (`versao`, `download_url`, `sha256`, `erp_minimo`, `ativo`). Populado pelo botão **"🔍 Verificar novas versões no GitHub"** (`registry/github_releases.py`) ou `python manage.py register_versao_agente`.
- **`Cliente.versoes_permitidas`** (M2M) — curadoria manual: quais versões aquela loja pode escolher no "Atualizar App" do Tray.
- **`Cliente.atualizacao_automatica_agente`** (bool) — quando ligado, **ignora** `versoes_permitidas` e manda o catálogo `ativo=True` inteiro pro ERP do cliente; releases novos entram sozinhos. O dono da loja continua confirmando no "Atualizar App" (a versão mais nova compatível vem pré-selecionada).
- **Push CP → ERP:** `registry/cp_push.py::SincronizadorVersoes` faz `POST https://{host}/v1/cp/agent-packages:sync` (Bearer `Cliente.integracao_secret`), full-sync. Disparado por: `m2m_changed` em `versoes_permitidas`; `pre_save`/`post_save` no `Cliente` (toggle do modo automático); `post_save` em `VersaoAgente` (mudança no catálogo → clientes que curam essa versão + todos os automáticos). Tudo (inclusive o botão manual) passa pela task Celery `task_sincronizar_versoes_agente(cliente_id, iniciada_por_id=None, com_retry=True)` — o botão manual chama com `com_retry=False` e `iniciada_por_id`.
- **Log da sincronização:** `SincronizadorVersoes` grava `SincronizacaoVersoesAgente.log` linha a linha (`_log()` faz `.update()` a cada linha → "ao vivo"). O admin mostra o log da última sincronização abaixo do botão e faz polling em `registry_cliente_sincronizacao_status` (`_view_status_sincronizacao`) enquanto `status='enviando'`, recarregando ao terminar — mesmo padrão do `lista_backups`. Corpo da resposta do ERP (`synced`/`retired`/`skipped`) e erros HTTP viram linhas em pt-BR no log. Ver `docs/registry/release-log-sincronizacao-versoes.md`.
- Lado ERP: `sync_api/cp_push.py` recebe e reflete em `SyncPackage.allowed`; `sync_api/services.py::get_available_packages` ordena por **semver** (não por `created_at`) e aplica as travas de downgrade/`erp_minimo`.

### Coleta de versões CP ← ERP (`GET /v1/cp/status`) — "Versão Atual" do Cliente

Doc: `docs/registry/versao-atual-clientes.md`. Contraparte de leitura do `cp_push` (que escreve).

- **`registry/cp_pull.py::ColetorVersoes`** — `GET https://{host}/v1/cp/status?cliente_id={id}` (Bearer `Cliente.integracao_secret`, mesmo segredo do push). Grava em `Cliente.versao_erp_detectada` (≠ `versao_erp`, que é a tag alvo do provisionamento), `versoes_detectadas_em`, `deteccao_versoes_erro`, e **reconcilia** (full-sync) as `InstalacaoAgente` do cliente.
- **Lado ERP:** `sync_api/cp_status.py::CpStatusAPIView` (repo `erp`) — devolve `erp_version` (`settings.VERSION`) + lista de `SyncInstallation` (`agent_version`, `last_seen_at`, `connectivity` leve por frescor). Rota fora do `/v1/sync/` (canal interno, não é o contrato produto).
- **ERP antigo (sem a rota):** 404 → `ColetorVersoes` faz fallback pro `GET /health/` público, que só tem a versão do ERP; as instalações ficam indisponíveis até o ERP ser atualizado.
- **SyncAgent × PDV:** o ERP conhece **um** número por instalação (`agent_version`). O painel mostra duas linhas (SyncAgent e PDV Local) com o mesmo valor — `pdv-local` é um artefato único. Uma versão separada do app PDV exigiria o agente reportá-la no heartbeat (mudança em `pdv-local` + `erp`).
- **Disparo:** botão 🔄 no admin (`task_coletar_versoes_cliente`) + task agendada `task_coletar_versoes_todas`. **Criar o `PeriodicTask` no admin do django-celery-beat** (ex.: crontab a cada 30 min) — não há schedule em código, igual ao health check. `task_verificar_saude_cliente` também atualiza `versao_erp_detectada` de graça (lê o corpo de `/health/`).

### Empresa e Estoque iniciais (`CP_CLIENTE_CNPJ` + `seed_cliente_inicial`)

Todo cliente novo precisa de pelo menos uma **Empresa** (Configurações > Empresas) e um **Estoque** (Produtos > Estoque, modelo `produtos.Loja`) para o ERP operar — cadastro de produtos, vendas e caixa dependem de ambos. O `MotorProvisionamento` grava `CP_CLIENTE_CNPJ` (= `cliente.cnpj`) no `.env`/`environment` do stack, junto de `CP_CLIENTE_ID`/`CP_CLIENTE_NOME`. O ERP lê em `core/settings.py` (`CP_CLIENTE_CNPJ`) e o `entrypoint.sh` roda `python manage.py seed_cliente_inicial` logo após as migrations e a criação do superuser:

- Se **não** houver Empresa, cria uma com `razao_social`/`nome_fantasia` = `CP_CLIENTE_NOME` e `cnpj` = `CP_CLIENTE_CNPJ`.
- Se **não** houver Estoque (`Loja`), cria um chamado `"Matriz"` vinculado a essa Empresa.
- Idempotente: com Empresa/Estoque já existentes, não faz nada (roda a cada boot sem efeito colateral).

Requer **ERP ≥ 0.0.104**. Clientes provisionados antes: cadastrar manualmente no ERP **ou** clicar em **"Aplicar Configurações"** (injeta `CP_CLIENTE_CNPJ` no serviço `{slug}_web`) e reiniciar o serviço — o seed roda no próximo boot.

> Ao mexer nisso foi corrigido de passagem um bug pré-existente no grafo de migrations do ERP: a criação de um banco do zero (todo provisionamento de cliente novo roda `migrate` num volume postgres vazio) falhava em `financeiro.0021` com *"column financeiro_condicaopagamento.quantidade_parcelas does not exist"* — nenhum cliente novo era provisionável desde ~jun/2026. Fix no repo `erp`: duas arestas de dependência em `financeiro/migrations/0018_rename_tables_condicao_especie.py` (`vendas.0024` e `dedetizacao.0006`), consistentes com a ordem histórica de aplicação. Detalhes na seção "Migration graph" do `CLAUDE.md` do `erp`.

### Dados iniciais (Planos e Módulos) no boot — nunca `loaddata`

O `entrypoint.sh` roda `manage.py carregar_dados_iniciais` a cada boot do `web`. O comando lê `registry/fixtures/initial_data.json` e **só cria** os `Modulo`/`Plano` ausentes; os existentes não são tocados. Um `Modulo` recém-criado é incluído nos `Plano` existentes que o listam na fixture (fluxo de release de módulo novo). Antes era `loaddata`, que sobrescrevia tudo a cada reinício — os preços editados no admin voltavam para 10/20/30. Consequências: mudar preço/limites de plano existente é pelo admin (a fixture só vale para banco novo); remover um módulo de um plano na fixture não o remove em bancos existentes. Doc: `docs/registry/release-fix-precos-planos-revertidos.md`.

### Relay do callback OAuth do Arpa (`/integracoes/arpa/oauth/callback/`)

`redirect_uri` único do app da Arara Suite no portal do desenvolvedor do Arpa (o portal só aceita uma URL por app; cada cliente tem o ERP num endereço próprio). `registry/arpa_oauth.py::arpa_oauth_callback` lê `state=<cliente>.<nonce>` (`<cliente>` = UUID do `Cliente`, que o ERP recebe como `CP_CLIENTE_ID`, ou o slug), acha o `Cliente` ativo/trial/atualizando e responde 302 para `site_url_cliente(cliente)` + `/arpa/oauth/retorno/` repassando só `code`, `state`, `error`, `error_description`. O CP **não** troca o `code`, não conhece `client_secret` nem tokens e não loga o `code`. Nunca redirecionar para URL vinda da querystring: `state` inválido → 400, slug desconhecido/inativo → 404, ambos com página de erro (`registry/arpa_oauth_erro.html`). Rota pública (sem login), registrada direto em `core/urls.py`. Doc: `docs/registry/arpa-oauth-relay.md`; plano do lado ERP em `docs/modulos/arpa/` do repo `erp`.

### Página de Ajuda do admin

O item "Ajuda" da sidebar (`registry/views.py::admin_ajuda`) é uma view de função simples, registrada em `core/urls.py` por `path()` direta (não é um `ModelAdmin`) e renderiza `registry/templates/registry/admin_ajuda.html` (HTML estático, sem model/DB por trás). O conteúdo é organizado em seções demarcadas por comentários `<!-- ═══ NOME ═══ -->`, usando classes CSS próprias definidas no topo do template (`.ajuda-card`, `.ajuda-table`, `.ajuda-step`, `.ajuda-alert`/`.ajuda-alert-warn`, `.tag-ok`/`.tag-no`/`.tag-warn`). Para documentar uma funcionalidade nova, edite o template diretamente na seção correspondente (ou crie uma nova seção seguindo o mesmo padrão) — não é necessário criar model nem admin.

**Ordem das URLs importa:** `path('admin/ajuda/', ...)` precisa vir **antes** de `path('admin/', admin.site.urls)` em `core/urls.py`. Se ficar depois, o `catch_all_view` do `django.contrib.admin` intercepta `ajuda/` como um sub-path desconhecido do admin e responde 404 antes que a rota específica seja tentada — mesmo com o usuário logado. Esse é um erro fácil de reintroduzir ao reordenar `urlpatterns`.

## Variáveis de ambiente relevantes

| Variável | Padrão | Descrição |
|---|---|---|
| `PROVISIONING_CHECK_HTTP` | `true` | Setar `false` em dev local |
| `CLIENTES_BASE_PATH` | `/opt/clientes` | Diretório base dos stacks por cliente |
| `ERP_DOMAIN` | `ararasuite.com.br` | Domínio base dos subdominios |
| `SAAS_DOMAIN` | `ararasuite.com.br` | Usado na landing para preview de subdomínio |
| `ERP_LATEST_VERSION` | `0.0.22` | Versão do ERP para novos clientes (seed de Empresa/Estoque exige ≥ 0.0.104) |
| `CP_BACKUP_DIR` | `/opt/backups/cp` | Destino dos backups do CLIENTES_BASE_PATH |
| `CP_BACKUP_MANTER` | `7` | Quantos backups do CLIENTES_BASE_PATH retener (task_backup_clientes) |
