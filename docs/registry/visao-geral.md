# Registry: Visao Geral Tecnica

- data: 2026-05-14
- componente: registry
- responsavel: Time ERP
- status: entregue

## 1. Objetivo do componente

- manter o cadastro de tenants (clientes) da plataforma SaaS AraraSuite;
- provisionar automaticamente uma instancia Docker Swarm isolada para cada tenant apos o cadastro;
- registrar planos, modulos e hosts de infraestrutura disponiveis;
- monitorar disponibilidade das instancias via health check periodico;
- expor operacoes de ciclo de vida: atualizacao de versao, suspensao e reativacao.

## 2. Escopo funcional

1. CRUD de `Modulo` e `Plano` com relacao M2M entre eles.
2. CRUD de `HostInfraestrutura` para registro dos nos do Swarm por regiao.
3. CRUD de `Tenant` com fluxo de status controlado.
4. Provisionamento automatico via Celery: criacao do stack Docker, aguardo de servico e health check HTTP.
5. Registro de log de cada etapa do provisionamento em `ProvisionamentoLog`.
6. Atualizacao de versao do ERP em instancias ativas via `task_atualizar_versao`.
7. Suspensao e reativacao de instancias via `task_suspender_tenant` e `task_reativar_tenant`.
8. Health check periodico de todas as instancias ativas via `task_healthcheck_todas_instancias`.
9. Fixture inicial `registry/fixtures/initial_data.json` com os planos e modulos da plataforma.

## 3. Entidades principais

- `registry.Modulo`
  - catalogo de modulos disponibilizaveis no ERP;
  - PK e o `slug` (ex: `financeiro`, `notificacoes`);
  - relacionado a `Plano` via M2M `modulos_inclusos`.

- `registry.Plano`
  - planos comerciais oferecidos (Starter, Pro, Enterprise);
  - PK e o `slug` (ex: `starter`);
  - guarda limites de recursos: `max_usuarios`, `max_empresas`, `recursos_cpu`, `recursos_ram_gb`;
  - `destaque = True` ativa destaque visual na landing page;
  - `ordem` controla a sequencia de exibicao nos cards.

- `registry.HostInfraestrutura`
  - nos do Docker Swarm por regiao (`anapolis`, `brasilia`);
  - guarda `swarm_node_id`, `ip`, `tipo`, `cpu_total`, `ram_gb_total`;
  - associado ao `Tenant` via FK; o placement do stack usa `node.labels.region == regiao`.

- `registry.Tenant`
  - representa um cliente da plataforma;
  - PK e UUID gerado automaticamente;
  - campos principais: `slug`, `nome`, `cnpj`, `email_contato`, `telefone`;
  - campos de infraestrutura: `host`, `versao_erp`, `stack_path`, `subdominio`, `dominio_custom`;
  - campos comerciais: `plano` (FK), `modulos_ativos` (M2M de `Modulo`);
  - campos de pagamento: `asaas_customer_id`, `asaas_subscription_id`;
  - `status` com fluxo descrito na secao 5;
  - datas de ciclo de vida: `trial_ate`, `data_ativacao`, `data_suspensao`, `data_cancelamento`.

- `registry.ProvisionamentoLog`
  - uma linha por etapa do provisionamento;
  - campos: `etapa` (str), `status` (`pendente | executando | concluido | erro`), `mensagem`, `iniciado_em`, `concluido_em`;
  - criado via `ProvisioningEngine._log()`.

- `registry.AtualizacaoVersao`
  - registro de cada atualizacao de versao aplicada a um tenant;
  - guarda versao anterior, versao nova, status e log de execucao.

- `registry.HealthCheck`
  - snapshot de disponibilidade de uma instancia em um momento;
  - guarda `status_http`, `latencia_ms`, `online`.

## 4. Fluxo de provisionamento

O fluxo completo e executado pela task Celery `task_provisionar_tenant` chamando `ProvisioningEngine.executar()`.

```
signup form
    └── views.signup()
            └── Tenant.objects.create(status='aguardando_provisao')
                    └── signal post_save
                            └── task_provisionar_tenant.delay(tenant.pk)

task_provisionar_tenant
    ├── status → provisionando
    ├── ProvisioningEngine.executar()
    │       ├── [escolher_host]    leitura de Tenant.host.regiao
    │       ├── [gerar_stack]      gerar variaveis de ambiente e stack.yml
    │       ├── [criar_superuser]  log 'pendente' com senha_temp antes do deploy
    │       ├── [subir_stack]      docker stack deploy --with-registry-auth
    │       │       ├── aguardar servico {slug}_web ficar Running (timeout 180s)
    │       │       └── aguardar GET /health/ retornar 200 (se PROVISIONING_CHECK_HTTP=true)
    │       └── status → ativo  (via Tenant.objects.filter(pk=...).update(...))
    └── em caso de excecao: status → erro_provisao, Celery retry (max 3x, delay 60s)
```

### Etapas registradas em ProvisionamentoLog

| etapa | descricao |
|---|---|
| `escolher_host` | selecao da regiao do host |
| `gerar_stack` | geracao do stack YAML e arquivo .env |
| `criar_superuser` | log da senha_temp antes e confirmacao apos deploy |
| `subir_stack` | `docker stack deploy` + aguardo de servico e HTTP |

## 5. Fluxo de status do Tenant

```
aguardando_provisao
    └── provisionando
            ├── ativo           (provisionamento concluido)
            └── erro_provisao   (falha apos 3 tentativas Celery)

ativo
    ├── suspenso    (inadimplencia ou acao manual)
    └── cancelado   (cancelamento pelo cliente)

suspenso
    └── ativo       (regularizacao do pagamento)

trial
    └── trial_expirado
```

## 6. Stack Docker gerada por instancia

Cada instancia recebe um stack isolado com tres servicos:

| Servico | Imagem | Funcao |
|---|---|---|
| `{slug}_web` | `emporioautomacao/ararasuite-erp:{versao}` | Django + Daphne, porta 8000 |
| `{slug}_db` | `pgvector/pgvector:0.8.0-pg17` | PostgreSQL com extensao pgvector |
| `{slug}_redis` | `redis:7-alpine` | Broker Celery e cache |

Redes:
- `traefik-public` — rede externa compartilhada com o Traefik do Swarm (acesso HTTP/HTTPS)
- `{slug}_internal` — rede overlay privada entre os servicos da instancia

Volumes: `pgdata`, `media`, `backups` (nomeados pelo Docker por instancia).

### Placement por regiao

O stack usa `node.labels.region == {regiao}` para fixar todos os servicos no mesmo no. A regiao vem de `Tenant.host.regiao` (valores: `anapolis`, `brasilia`).

## 7. Variaveis de ambiente injetadas no container web

| Variavel | Descricao |
|---|---|
| `SLUG` | identificador do tenant |
| `POSTGRES_DB` / `USER` / `PASSWORD` | credenciais do banco dedicado |
| `POSTGRES_HOST` | `db` (DNS interno do Swarm dentro da rede `internal`) |
| `REDIS_URL` | `redis://redis:6379/0` |
| `DJANGO_SECRET_KEY` | chave secreta gerada com `secrets.token_hex(50)` |
| `ALLOWED_HOSTS` | subdominio do tenant |
| `CSRF_TRUSTED_ORIGINS` | `https://{subdominio}` |
| `MODULOS_ATIVOS` | slugs dos modulos separados por virgula |
| `EMPRESAS_CREDENCIAL_MASTER_KEY` | chave de criptografia de credenciais |
| `DJANGO_SUPERUSER_USERNAME` | `admin` |
| `DJANGO_SUPERUSER_EMAIL` | e-mail do responsavel do tenant |
| `DJANGO_SUPERUSER_PASSWORD` | senha temporaria gerada com `secrets.token_urlsafe(12)` |

O arquivo `.env` e gravado em `CLIENTES_BASE_PATH/{slug}/.env` com permissao `0600` como referencia; o entrypoint.sh do ERP le as variaveis diretamente do ambiente Docker.

## 8. Responsabilidades do entrypoint do ERP

O container web executa ao iniciar, via `entrypoint.sh`:
1. Aguarda PostgreSQL responder na porta 5432.
2. Instala extensao `pgvector` no banco.
3. Executa `python manage.py migrate`.
4. Cria o superuser com as variaveis `DJANGO_SUPERUSER_*` (idempotente — nao falha se ja existe).
5. Inicia o Daphne.

Nao ha `docker run --rm` externo para essas operacoes — elas ocorrem dentro do proprio container, onde o DNS `db` resolve corretamente na rede `internal`.

## 9. Variaveis de ambiente do Control Plane

| Variavel | Padrao | Descricao |
|---|---|---|
| `CLIENTES_BASE_PATH` | `/opt/clientes` | diretorio base onde ficam os stacks de cada tenant |
| `ERP_DOMAIN` | `ararasuite.com.br` | dominio base para os subdominios |
| `PROVISIONING_CHECK_HTTP` | `true` | `false` em desenvolvimento local (sem Traefik/subdominio roteavel) |
| `SAAS_DOMAIN` | `ararasuite.com.br` | usado na landing page para montar preview de subdominio |
| `ERP_LATEST_VERSION` | `0.0.22` | versao padrao do ERP para novos tenants |

## 10. Bugs corrigidos no ciclo atual (2026-05-14)

### 10.1 NotUpdated ao tentar salvar Tenant inexistente no retry do Celery

**Sintoma**: `django.db.models.base.Model.DoesNotExist` ou `NotUpdated` ao usar `tenant.save(update_fields=[...])` no retry da task.

**Causa**: o tenant era buscado antes do retry mas o objeto em memoria ficava obsoleto; `save(update_fields=)` levanta `NotUpdated` se nenhuma linha e afetada.

**Correcao**: todos os `tenant.save(update_fields=[...])` substituidos por `Tenant.objects.filter(pk=tenant_id).update(...)`, que e seguro mesmo se a linha nao existir (retorna 0 sem levantar excecao). Guard adicionado no inicio da task:

```python
try:
    tenant = Tenant.objects.get(pk=tenant_id)
except Tenant.DoesNotExist:
    return
```

### 10.2 AlreadyExists ao fazer docker stack deploy no retry do Celery

**Sintoma**: `subprocess.CalledProcessError` com `AlreadyExists` no stderr do `docker stack deploy` quando a task era retentada apos um stack parcialmente criado.

**Causa**: `docker stack deploy` retorna codigo de erro quando algum servico ja existe, mesmo que o deploy seja idempotente para os demais.

**Correcao**: o `subprocess.run` agora usa `capture_output=True` e so levanta excecao se o erro nao contiver `'AlreadyExists'` no stderr:

```python
result = subprocess.run(
    ['docker', 'stack', 'deploy', '--with-registry-auth', '-c', str(stack_file), slug],
    capture_output=True, text=True,
)
if result.returncode != 0 and 'AlreadyExists' not in result.stderr:
    raise subprocess.CalledProcessError(result.returncode, result.args, result.stderr)
```

### 10.3 Loop infinito no _run_manage por DNS errado fora do stack

**Sintoma**: `_run_manage(['shell', ...])` ficava em loop esperando o banco, mesmo com o stack ja rodando.

**Causa**: o container avulso (`docker run --rm --network {slug}_internal`) usava `POSTGRES_HOST=db`, mas fora do stack o DNS `db` nao resolve — o nome correto seria `{slug}_db`. O entrypoint.sh dentro do stack resolve `db` corretamente porque esta na rede `internal`.

**Correcao**: `_run_manage` foi eliminado do fluxo de provisionamento. A criacao do superuser passou a ser responsabilidade do `entrypoint.sh` via variaveis `DJANGO_SUPERUSER_*`.

## 11. Arquivos chave

| Arquivo | Funcao |
|---|---|
| `registry/models.py` | todos os modelos do componente |
| `registry/provisioning.py` | `ProvisioningEngine` com logica de deploy |
| `registry/tasks.py` | tasks Celery de provisionamento e ciclo de vida |
| `registry/admin.py` | interface admin para gestao de tenants e logs |
| `registry/fixtures/initial_data.json` | dados iniciais de `Modulo` e `Plano` |
| `.env` | variaveis do control plane (PROVISIONING_CHECK_HTTP, SAAS_DOMAIN etc.) |

## 12. Pontos de atencao

- O `ProvisioningEngine` depende do socket Docker do host (`docker.from_env()`); o control plane deve rodar no no manager do Swarm ou com acesso ao socket via volume.
- A senha temporaria do superuser fica gravada no `ProvisionamentoLog` da etapa `criar_superuser` como `senha_temp:{valor}`. Deve ser redefinida pelo cliente no primeiro acesso.
- O arquivo `.env` do tenant em `/opt/clientes/{slug}/.env` tem permissao `0600` e contem todas as credenciais da instancia — acesso restrito ao usuario do control plane.
- `PROVISIONING_CHECK_HTTP=false` deve ser definido em desenvolvimento local, pois o subdominio `{slug}.ararasuite.com.br` nao resolve para `localhost`.

## 13. Documentos relacionados

- [landing/visao-geral.md](../landing/visao-geral.md) — landing page que dispara o provisionamento
- [padroes/encoding-postgres.md](../padroes/encoding-postgres.md) — bug de encoding nos nomes de modulos
- [../../../../erp/docs/saas/plano-saas-v2.md](../../../../erp/docs/saas/plano-saas-v2.md) — plano arquitetural completo da plataforma SaaS
