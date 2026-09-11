# Manual de deploy — "Versão Atual" (ERP / SyncAgent / PDV)

Runbook operacional para colocar em produção o que está descrito em
`docs/registry/versao-atual-clientes.md`. Execute na ordem — cada passo depende
do anterior.

---

## Pré-requisitos

- `docker login` autenticado (push nas duas imagens: `emporioautomacao/ararasuite-erp` e `emporioautomacao/ararasuite-cp`).
- Acesso SSH ao manager do Swarm de produção.
- Working tree dos dois repos (`erp`, `cp`) com as mudanças desta feature — **nada foi commitado ainda**, os passos 1 e 3 cobrem isso.

---

## Passo 1 — Release do ERP (endpoint `GET /v1/cp/status`)

Repo `d:\GitHub\ararasuite\erp`. Usa a skill `erp-release-docker` (commit + build + push automáticos):

```powershell
powershell -ExecutionPolicy Bypass -File skills/erp-release-docker/scripts/release_docker.ps1 `
  -CreateNewVersion `
  -CommitMessage "Endpoint GET /v1/cp/status: versao do ERP + instalacoes do SyncAgent/PDV pro CP"
```

Isso builda e publica `emporioautomacao/ararasuite-erp:<nova versão>` **e** retagueia `:latest`.

**Verificação:** `python manage.py test sync_api.tests.CpStatusTests` (já rodado nesta
sessão — 7/7 OK).

---

## Passo 2 — Levar a nova imagem do ERP aos clientes já provisionados

**Não existe hoje um botão no admin do CP que force essa atualização** —
`task_atualizar_versao` está no código mas sem nenhum caller (nem admin, nem
signal). Duas situações:

- **Cliente roda `versao_erp = ''` ou `'latest'`** (maioria — é o padrão do
  provisionamento): precisa forçar o Swarm a repuxar a imagem, já que a tag
  `:latest` não muda de nome:
  ```bash
  docker service update --with-registry-auth --force --image emporioautomacao/ararasuite-erp:latest {slug}_web
  ```
- **Cliente com `versao_erp` fixa** (ex.: `0.0.94`): precisa apontar pra nova tag:
  ```bash
  docker service update --with-registry-auth --image emporioautomacao/ararasuite-erp:<nova versão> {slug}_web
  ```
  (e depois atualizar o campo `versao_erp` no admin do CP pra refletir a realidade —
  hoje é manual, o campo não se auto-atualiza a partir do deploy).

Repita para cada cliente ativo/trial que deve expor `/v1/cp/status` desde já.
**Não é bloqueante** para o restante do rollout: enquanto um cliente não tiver a
imagem nova, o painel "Versão Atual" dele mostra só a versão do ERP (via
`/health/`, que sempre existiu) e o aviso "ERP sem `/v1/cp/status`".

**Clientes novos** (provisionados depois deste passo) já saem com a imagem nova,
sem ação extra — `ERP_LATEST_VERSION`/`:latest` de `core/settings.py`.

---

## Passo 3 — Deploy do CP

Repo `d:\GitHub\ararasuite\cp` (este). `VERSION` já foi bumpado para `0.0.37`
nesta sessão.

```bash
# 1. Commit
git add -A
git commit -m "feat(registry): painel 'Versao Atual' do cliente (ERP/SyncAgent/PDV) + coleta via GET /v1/cp/status"

# 2. Build + push da imagem
docker build --pull -t emporioautomacao/ararasuite-cp:0.0.37 -t emporioautomacao/ararasuite-cp:latest .
docker push emporioautomacao/ararasuite-cp:0.0.37
docker push emporioautomacao/ararasuite-cp:latest

# 3. No servidor: re-deployar o stack do CP
export $(cat /opt/cp/.env | xargs) && CP_VERSION=0.0.37 \
  docker stack deploy --with-registry-auth -c stack-prod.yml ararasuite-cp
docker service ps ararasuite-cp_web

# 4. Migração (roda automaticamente no entrypoint, ou manualmente se preciso)
docker exec -it $(docker ps -q -f name=ararasuite-cp_web) python manage.py migrate registry
```

---

## Passo 4 — Criar o `PeriodicTask` no django-celery-beat

Não existe schedule em código — igual ao health check. No admin de produção:

1. **Agendamentos > Agendamentos Cron** (`CrontabSchedule`) → **Adicionar**:
   - Minuto: `*/30`, Hora: `*`, Dia do mês/mês/dia da semana: `*` (a cada 30 min —
     ajuste se quiser outra cadência) → Salvar.
2. **Agendamentos > Tarefas Periódicas** (`PeriodicTask`) → **Adicionar**:
   - Nome: `Coletar versões (ERP/SyncAgent/PDV) de todos os clientes`
   - Tarefa (registrada): `registry.tasks.task_coletar_versoes_todas`
   - Agendamento Cron: o criado no passo anterior
   - Habilitada: ✓
   - Salvar.

Confirme que o worker Celery está no ar (`docker service ps ararasuite-cp_worker`
ou o processo `--pool=solo` em dev) — sem worker o beat enfileira e nada roda.

---

## Passo 5 — Verificação end-to-end

1. Abrir **Cadastro > Clientes** → um cliente já com a imagem nova do ERP
   (Passo 2) → seção **"Versão Atual"** → clicar **"🔄 Verificar versões agora"**.
2. Em ~5s a página recarrega com:
   - linha **ERP** com a versão detectada = a nova (sem destaque laranja se bater
     com o campo "alvo").
   - linhas **SyncAgent**/**PDV Local** com a(s) versão(ões) reportada(s) pelas
     lojas, ou "(sem instalações)"/"(nenhuma versão reportada)" se não houver
     nenhuma loja ativada ainda.
   - tabela de instalações abaixo (se houver).
3. Num cliente **ainda sem** a imagem nova: deve aparecer o aviso "ERP sem
   `/v1/cp/status` (versão antiga)" e só a versão do ERP preenchida.
4. Esperar o próximo disparo do `PeriodicTask` (ou rodar
   `task_coletar_versoes_todas.delay()` no shell) e conferir no log do worker
   que ele varreu só clientes `ativo`/`trial`.

---

## Rollback

- **CP:** `docker service update --image emporioautomacao/ararasuite-cp:<versão anterior> ararasuite-cp_web` (a migração `0017` é aditiva — não precisa reverter para voltar o código).
- **ERP:** reverter a imagem do(s) cliente(s) afetado(s) do mesmo jeito do Passo 2, apontando pra tag anterior. O endpoint novo é só leitura e aditivo — não há dado a reverter no ERP.
- **PeriodicTask:** desabilitar (checkbox "Habilitada") em vez de apagar, se for só pausar a coleta.
