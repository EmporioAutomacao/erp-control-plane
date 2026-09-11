# Release: Log ao vivo do botão "⇪ Sincronizar Versões com o ERP"

- data: 2026-09-08
- responsavel: James Flavio
- status: implementado — pendente de deploy
- versao CP: `0.0.36`
- app: `registry`
- repo: `erp-control-plane` (nada muda no `erp`)
- plano: `dentro-do-cadastro-do-twinkling-sunrise.md`

---

## 1. Objetivo

O botão **⇪ Sincronizar Versões com o ERP** (página do `Cliente`) só mostrava um
toast de uma linha e voltava. Não dava pra ver o que foi enviado, o que o ERP
respondeu (`{"synced","retired","skipped"}` — **descartado**) nem por que falhou.

Agora, ao clicar, aparece **abaixo do botão** um log passo a passo que **atualiza
sozinho** (linha por linha) enquanto roda, e fica gravado no histórico.

## 2. O que mudou

- **`SincronizacaoVersoesAgente`** (`registry/models.py`): dois campos novos —
  `log` (TextField, preenchido linha a linha) e `iniciada_por` (FK `auth.User`,
  nulo = push automático). Migração `0016`.
- **`SincronizadorVersoes`** (`registry/cp_push.py`): `__init__(cliente, iniciada_por=None)`;
  helper `_log(linha)` que **persiste a cada linha** (`.filter(pk=).update(log=...)`)
  — é isso que faz o log ser "ao vivo". Passos logados em pt-BR: modo
  (curadoria/automático), versões montadas, `POST {url}`, `HTTP {status} em {t}s`,
  e **parse do corpo da resposta do ERP** (`N sincronizada(s), N aposentada(s),
  N ignorada(s)`), antes descartado. Erros HTTP do ERP (`401 unauthorized`,
  `409 not_linked_to_cp`/`cliente_id_mismatch`, `400 invalid_payload`) e exceções
  (timeout / conexão) viram linha amigável em pt-BR. `mensagem_erro` continua
  existindo (toast do admin + retry da task) — agora com a versão amigável.
- **Botão manual agora é assíncrono** (`registry/admin.py::_view_sincronizar_versoes`):
  enfileira `task_sincronizar_versoes_agente.delay(pk, iniciada_por_id=user.id,
  com_retry=False)` em vez de rodar síncrono. A task ganhou os params
  `iniciada_por_id` e `com_retry` (default `True` — os signals não mudam).
- **Endpoint de status**: `GET /admin/.../<pk>/sincronizacoes/<sync_id>/status/`
  (`_view_status_sincronizacao`, name `registry_cliente_sincronizacao_status`) →
  `{status, log, ativo, http}`.
- **`acoes_versoes`**: renderiza a última sincronização (badge de status, quem/
  quando, `<pre>` com o log). Se `status='enviando'`, `<script>` inline faz
  polling a cada 1,5s (molde do `lista_backups`) e recarrega ao terminar.
- **`SincronizacaoVersoesAgenteInline`**: repaginado — badge colorido,
  `versoes_fmt` (lista legível em vez do dump JSON) e `log_fmt` (`<pre>`).

## 3. Dependência nova: worker Celery

O botão manual **antes era síncrono**; agora depende do worker Celery estar
rodando (como backup/restauração/provisionamento já dependem). Em prod
(`stack-prod.yml`, `celery --concurrency=1`) um sync pode ficar em fila atrás de
um provisionamento/backup — o log fica em "Sincronização iniciada" até o worker
pegar. O `acoes_versoes` mostra o aviso "confirme que o worker Celery está
rodando" quando fica >30s em `enviando`.

Dev Windows: `.\venv\Scripts\python.exe -m celery -A core worker --loglevel=info --pool=solo`.

## 4. Testes

`registry/tests/test_cp_push.py` — classes `SincronizarLogTests` (13) e
`SincronizarTaskEViewTests` (6): sucesso/erro HTTP/timeout/conexão, parse de
`skipped`, modo no log, `iniciada_por`, log persistindo incremental,
`com_retry` (retenta / não retenta), a view enfileirando com `com_retry=False`,
guarda de `integracao_secret`, endpoint de status. Total do arquivo: 30 testes.

## 5. Deploy

Sem migração no ERP. No CP:

```bash
# git: bump 0.0.36, commit, push
docker build --pull -t emporioautomacao/ararasuite-cp:0.0.36 -t emporioautomacao/ararasuite-cp:latest .
docker push emporioautomacao/ararasuite-cp:0.0.36
docker push emporioautomacao/ararasuite-cp:latest
# no servidor:
export $(cat /opt/cp/.env | xargs) && CP_VERSION=0.0.36 \
  docker stack deploy --with-registry-auth -c stack-prod.yml ararasuite-cp
docker service ps ararasuite-cp_web
```
