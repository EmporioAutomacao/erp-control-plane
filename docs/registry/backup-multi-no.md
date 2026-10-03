# Backup e restauração multi-nó (worker por nó + bucket S3)

- data: 2026-10-02
- responsavel: James Flavio
- status: implementado e validado localmente com RustFS (sem Swarm) — pendente de deploy e de validação em Swarm real
- versao CP: `0.0.42`
- app: `registry`
- repo: `erp-control-plane` (nada muda no `erp`)
- plano: `vamoz-fazer-a-op-o-sunny-lollipop.md`

---

## 1. Problema

Em produção o botão **💾 Novo Backup** gerava um arquivo de ~1,6 KB, "Concluído",
com os avisos *"Container demo_db nao encontrado — dump do banco nao incluido"* e
*"Container demo_web nao encontrado — midia nao incluida"*.

Causa: `docker ps/exec/cp/run` só enxergam o daemon Docker **da máquina onde rodam**.
O Celery do CP está fixo no manager (`node.role == manager`), mas os serviços de cada
cliente rodam no nó com `node.labels.region == {regiao}` — normalmente um worker. O
backup só conseguia capturar o `config/` (que está em `/opt/clientes` do manager).
A restauração tinha o mesmo defeito (`psql` e o volume `{slug}_media` só existem no nó
do cliente).

## 2. Visão geral da solução

1. Cada nó do Swarm tem um **worker Celery** que consome uma fila própria, `no.<hostname>`.
2. O manager descobre em que nó o cliente roda e **despacha a task para a fila daquele nó**.
3. O arquivo de backup **não fica no disco do nó**: vai para um **bucket S3-compatível**
   (RustFS). O disco do nó só guarda o arquivo temporariamente.

```
Admin (web, manager)
  └─ task_iniciar_backup_cliente  [fila celery — manager]
        ├─ lê .env/stack.yml do cliente (/opt/clientes só existe aqui)
        ├─ fila_do_cliente(slug) → docker service ps {slug}_db → "no.worker-2"
        └─ task_backup_cliente.apply_async(queue="no.worker-2", config_files=...)
              [nó worker-2]  pg_dump + docker cp media + config → .tar.gz
                              └─ upload para o bucket → BackupCliente.arquivo_path = s3://...
```

```
Admin → task_restaurar_cliente  [manager]  scale {slug}_web=0 → despacha p/ fila do nó
          └─ task_restaurar_no  [nó]       baixa do bucket → psql → volume de mídia
                └─ (sempre, mesmo com erro) task_finalizar_restauracao [manager]
                                            scale {slug}_web=1, libera o backup
```

`docker service scale` só funciona em nó **manager**, enquanto o `psql` e o volume de
mídia só existem no nó do cliente — por isso a restauração é dividida em
manager → nó → manager.

## 3. Componentes

| Arquivo | Papel |
|---|---|
| `registry/nos.py` | `hostname_do_no`, `fila_do_cliente`, `container_local` (filtro ancorado `^{slug}_db(\.\|$)`) |
| `registry/storage_backup.py` | boto3 path-style: `enviar`, `baixar`, `apagar`, `url_download` (link temporário) |
| `registry/tasks.py` | `task_iniciar_backup_cliente`, `task_backup_cliente`, `task_restaurar_cliente`, `task_restaurar_no`, `task_finalizar_restauracao` |
| `registry/admin.py` | botão chama `task_iniciar_backup_cliente`; download redireciona p/ link temporário; upload manual envia ao bucket |
| `registry/models.py` | `BackupCliente.no` (hostname do nó que gerou o backup) — migration `0019` |
| `stack-prod.yml` | serviço `agente-no` (global) e filas do `celery` do manager |
| `entrypoint-agente.sh` | worker do nó: espera o Postgres do CP e sobe o Celery (sem `migrate`) |

### Filas

| Worker | Onde roda | Filas |
|---|---|---|
| `celery` | manager | `celery` (provisionamento, e-mail, health check, etc.) + `no.<hostname-do-manager>` |
| `agente-no` | todo nó **não** manager (`mode: global`) | `no.<hostname>` |

O hostname vem do Swarm: `NODE_HOSTNAME: "{{.Node.Hostname}}"` no `environment` do serviço.
Um nó novo que entra no Swarm ganha o agente automaticamente.

Sem Swarm (dev local) ou se `docker service ps` falhar, `fila_do_cliente` devolve `celery` —
o backup roda no próprio worker e o comportamento é o de antes.

### Formato de `BackupCliente.arquivo_path`

| Valor | Significado |
|---|---|
| `s3://clientes/{slug}/backup_{slug}_{ts}.tar.gz` | objeto no bucket (modo normal em produção) |
| caminho de disco | backup local — dev sem bucket, ou backups criados antes desta mudança |

Backups antigos (caminho local no manager) continuam listados; o download funciona
enquanto o arquivo existir no disco do container web, mas **a restauração só encontra
o arquivo se a task cair no nó que o tem** — na prática, no manager.

### Retenção

Mantém 3 backups `origem='automatico'` por cliente. Os excedentes têm o registro
removido e o objeto apagado do bucket. Backups `origem='manual'` nunca entram na contagem.

## 4. Configuração

Variáveis de ambiente (`.env` do CP, repassadas pelo `stack-prod.yml` ao `web`,
`celery` e `agente-no`):

| Variável | Padrão | Descrição |
|---|---|---|
| `BACKUP_S3_ENDPOINT` | — | URL do RustFS (alcançável **pelos nós**), ex.: `http://rustfs:9000` |
| `BACKUP_S3_BUCKET` | — | Nome do bucket (precisa existir) |
| `BACKUP_S3_ACCESS_KEY` / `BACKUP_S3_SECRET_KEY` | — | Credenciais com leitura, escrita e exclusão no bucket |
| `BACKUP_S3_REGION` | `us-east-1` | Região (RustFS aceita qualquer valor) |
| `BACKUP_S3_PUBLIC_ENDPOINT` | vazio | Endereço do bucket **alcançável pelo navegador**, usado só nos links de download. Vazio = usa `BACKUP_S3_ENDPOINT` |

Se qualquer uma das quatro primeiras estiver vazia, o backup fica no disco do nó
(`logger.warning` no worker). Isso só serve em dev de nó único: o painel não alcança o
disco de um worker remoto.

## 5. Deploy

1. **Bucket:** criar o bucket no RustFS e as credenciais. Testar do manager e de um worker
   (a porta do RustFS precisa estar acessível pela VPN).
2. **Cada worker do Swarm:** `mkdir -p /opt/backups/cp`. O diretório é bind com o **mesmo
   caminho** no host e no container, porque a restauração de mídia usa
   `docker run -v <caminho>:...` e o daemon interpreta o caminho do host.
3. **`.env` do CP:** preencher `BACKUP_S3_*`.
4. **Imagem:** rebuild e push (inclui `boto3` e `entrypoint-agente.sh`).
5. **Redeploy do stack** do CP (`docker stack deploy --with-registry-auth ...`). A
   migration `0019` roda no `entrypoint-worker.sh` do `celery`.

### Verificação pós-deploy

```bash
docker service ls                       # agente-no deve ter 1 réplica por worker
docker service logs <stack>_agente-no   # procurar "[ ... ] celery@... ready" e a fila no.<hostname>
docker service logs <stack>_celery      # filas: celery, no.<hostname-do-manager>
```

Depois: **💾 Novo Backup** num cliente que roda em um worker. Esperado: status
*Concluído*, tamanho de MB (não KB), **sem** avisos de container, objeto novo no bucket em
`clientes/{slug}/`, e `BackupCliente.no` com o hostname do worker. Teste ⬇ Baixar e
↩ Restaurar em um cliente de teste (o ERP fica indisponível durante a restauração).

## 6. Operação e solução de problemas

| Sintoma | Causa provável | O que fazer |
|---|---|---|
| Backup fica "Em andamento" com 0% | Não há worker consumindo a fila do nó (agente parado ou nó sem agente) | `docker service ps <stack>_agente-no`; ver logs; a task executa quando o agente voltar |
| Restauração travada e o ERP do cliente fora do ar | Agente do nó caiu entre o `scale web=0` e a restauração | Subir o agente (a task roda e religa o web). Emergência: `docker service scale {slug}_web=1` no manager |
| Aviso "Container {slug}_db nao encontrado" | A task rodou em nó errado (ex.: serviço migrou de nó depois do despacho) | Repetir o backup; conferir `docker service ps {slug}_db` |
| `Falha ao despachar o backup para a fila ...` | Redis do CP inacessível | Verificar o serviço `redis` e a rede `cp-internal` |
| Erro no envio ao bucket | Endpoint/credenciais/bucket errados, ou porta bloqueada na VPN | Testar `aws s3 ls --endpoint-url ...` no nó; revisar `BACKUP_S3_*` |
| ⬇ Baixar abre link que não carrega | Endpoint do bucket não é alcançável pelo navegador | Definir `BACKUP_S3_PUBLIC_ENDPOINT` |
| Disco do nó enchendo em `/opt/backups/cp` | Task morreu no meio e deixou temporários (`_media_tmp_*`, `_restore_*`, `.tar.gz`) | Apagar os resíduos; o fluxo normal remove tudo ao final |

## 7. Limitações conhecidas

- **Sem agente, a task espera na fila.** Não há timeout nem alerta. Na restauração isso
  deixa o web do cliente em 0 réplicas até o agente voltar.
- **Backups grandes na memória:** o dump (`pg_dump`) é lido inteiro em memória antes de
  entrar no tar — comportamento herdado, agora no worker do nó. Bancos muito grandes
  pedem streaming.
- **Concorrência do agente = 2:** no máximo 2 backups/restaurações simultâneos por nó.
- **Upload manual e timeout do gunicorn** (`--timeout 120`): arquivos grandes podem estourar
  o tempo da requisição antes de terminar o envio ao bucket (limitação já existente).
- **Migrar o cliente de nó** entre o despacho e a execução faz a task cair no nó antigo
  (ver tabela acima).

## 8. Escala

O desenho (agente por nó + bucket) escala horizontalmente até algumas dezenas de nós.
Para centenas de milhares de serviços o limite passa a ser o próprio Swarm (um stack por
cliente); a evolução natural é dividir a infraestrutura em **células** (vários Swarms, cada
um com seu manager/Redis) e trocar o `pg_dump` diário por backup físico contínuo
(pgBackRest/wal-g). `registry/nos.py` concentra a pergunta "qual Docker atende este
cliente", que é o ponto a estender para múltiplas células.

## 9. Teste local (sem Swarm) com RustFS no Docker Desktop

Valida bucket, backup, download por link, restauração e retenção. **Não** valida
`agente-no`, filas `no.<hostname>` nem os templates do stack (precisam de Swarm real).

1. Subir o RustFS: `docker compose -f docker-compose.rustfs-dev.yml up -d`
   (S3 em `http://localhost:9000`, console em `http://localhost:9001`, credenciais de dev
   `rustfsdev` / `rustfsdev-secret`).
2. Criar o bucket `cp-backups` (uma vez), com o mesmo cliente que o CP usa:
   `storage_backup._cliente().create_bucket(Bucket='cp-backups')` num `manage.py shell`
   com as variáveis do passo 3 definidas.
3. No `.env` local: `BACKUP_S3_ENDPOINT=http://localhost:9000`, `BACKUP_S3_BUCKET=cp-backups`,
   `BACKUP_S3_ACCESS_KEY`, `BACKUP_S3_SECRET_KEY`, `BACKUP_S3_REGION=us-east-1`.
4. Subir `runserver` e o worker (`celery -A core worker --pool=solo`, no Windows). Sem Swarm,
   `docker service ps` falha e o backup cai na fila `celery` — esperado.
5. O CP localiza os containers por nome (`{slug}_db`, `{slug}_web`) e faz
   `pg_dump -U erp_{slug} erp_{slug}`. Para usar o cliente do `docker-compose.integrated-dev.yml`
   (banco `erp_desenvolvimento`), crie um `Cliente` com slug `desenvolvimento` (status `ativo`) e
   renomeie temporariamente os containers: `docker rename arara-integrated-dev-erp_cliente_db-1 desenvolvimento_db`
   (idem `..._web-1` → `desenvolvimento_web`). Reverta ao final.

### Resultado da validação (2026-10-02, Docker Desktop, RustFS `latest`)

| Verificação | Resultado |
|---|---|
| Backup pelo botão do admin | `concluido`, 28 MB (`db.sql` 111 MB + mídia + `config/`), sem avisos, `arquivo_path=s3://...` |
| Objeto no bucket / resíduo local | tamanho confere; nenhum `.tar.gz` sobrando em `CP_BACKUP_DIR` |
| ⬇ Baixar | 302 → URL assinada → 200, tar válido, `Content-Disposition` com o nome do arquivo |
| ⬆ Importar backup | objeto `manual_*` no bucket, `origem=manual` |
| Retenção | 4 automáticos → 3 (registro e objeto do mais antigo removidos); manual preservado |
| ↩ Restaurar | linhas apagadas de `django_migrations` (465 → 460) voltaram a 465; mídia restaurada no volume |
| Falha do bucket | RustFS parado → `status=erro` com a mensagem do endpoint, sem arquivo temporário |

Observação: o `pg_dump --clean --if-exists` recria os objetos que estão no dump, mas **não apaga**
tabelas criadas depois do backup. Restaurar traz de volta o estado do que foi dumpado; não zera
o banco.

## 10. RustFS em produção (Portainer / Swarm)

Stack pronto para colar no Portainer: [`stack-rustfs.yml`](../../stack-rustfs.yml). Serviço único `rustfs`,
fixo no nó `servidorubuntuanptruenas`, dados no volume nomeado local `rustfs_data`, exposto pelo Traefik.

### Como o tráfego passa

| Quem | Endereço | Caminho |
|---|---|---|
| Nós do Swarm (upload no backup, download na restauração) | `BACKUP_S3_ENDPOINT=http://rustfs-s3:9000` | rede overlay `storage-net` (criada pelo stack do RustFS), direto ao RustFS |
| Navegador (⬇ Baixar) | `BACKUP_S3_PUBLIC_ENDPOINT=https://<S3_DOMAIN>` | Cloudflare → Traefik (`web`) → RustFS |
| Você (console) | `https://<CONSOLE_DOMAIN>` | Cloudflare → Traefik (`web`) → RustFS:9001 |

As labels seguem o padrão do Traefik do Swarm (`traefik.swarm.network=traefik-public`, entrypoint `web`, sem certresolver).
O upload **não** passa pelo Cloudflare de propósito: o proxy limita o tamanho do corpo das requisições e o
backup faria um desvio pela internet. O link assinado de download usa o domínio público, e a assinatura
SigV4 vale porque o Traefik preserva o `Host` recebido.

### Passo a passo

1. Cloudflare: criar os registros DNS (proxied) de `S3_DOMAIN` e `CONSOLE_DOMAIN` (padrões
   `s3.ararasuite.com.br` e `rustfs.ararasuite.com.br`), como os demais subdomínios.
2. Portainer → Stacks → Add stack → colar o `stack-rustfs.yml` e definir as variáveis:
   `RUSTFS_ACCESS_KEY`, `RUSTFS_SECRET_KEY` (mín. 8 caracteres, não use `rustfsadmin`) e, se quiser outros
   domínios, `S3_DOMAIN`/`CONSOLE_DOMAIN`; opcionalmente `RUSTFS_VERSION` (fixe uma tag em vez de `latest`).
   Este stack **cria a rede overlay `storage-net`** — faça o deploy dele antes do CP.
3. Abrir o console, criar o bucket `cp-backups` e uma **access key dedicada** ao CP (não use a conta root).
4. No `.env` do CP: `BACKUP_S3_BUCKET=cp-backups`, `BACKUP_S3_ACCESS_KEY`/`BACKUP_S3_SECRET_KEY` (a chave
   dedicada) e `BACKUP_S3_PUBLIC_ENDPOINT=https://<S3_DOMAIN>`. `BACKUP_S3_ENDPOINT` já vem como
   `http://rustfs-s3:9000` no `stack-prod.yml`.
5. Redeploy do stack do CP (o `stack-prod.yml` agora anexa `web`, `celery` e `agente-no` à `storage-net`).

### Verificação

```bash
docker service ps rustfs_rustfs                    # deve estar Running em servidorubuntuanptruenas
curl -I https://<S3_DOMAIN>/health                 # 200 pelo Traefik/Cloudflare
# de dentro da rede interna (qualquer nó), para validar o endpoint usado pelos agentes:
docker run --rm --network storage-net curlimages/curl -s -o /dev/null -w "%{http_code}
" http://rustfs-s3:9000/health
```

Depois, **💾 Novo Backup** num cliente: o registro deve apontar para `s3://clientes/{slug}/...` e o objeto
aparecer no bucket; ⬇ Baixar deve abrir o link no domínio público.

### Observações

- O volume é local ao nó: se o `servidorubuntuanptruenas` for perdido, os backups vão junto. Faça cópia do
  volume `rustfs_data` (ou replicação do bucket) para outro lugar.
- A constraint `node.hostname == servidorubuntuanptruenas` impede o Swarm de mover o serviço para um nó sem os dados.
- `RUSTFS_VERSION=latest` pode mudar a versão a cada redeploy; fixe uma tag testada.
