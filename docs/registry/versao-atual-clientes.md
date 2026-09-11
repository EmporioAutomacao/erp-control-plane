# Versão Atual do Cliente (ERP / SyncAgent / PDV)

- data: 2026-09-10
- responsavel: James Flavio
- status: implementado — pendente de deploy (endpoint no `erp` + coleta no CP)
- versao CP: `0.0.37`
- app: `registry`
- repos: `cp` (este) + `erp` (novo endpoint `GET /v1/cp/status`)
- plano: `em-cadastro-clientes-rosy-kahn.md`

---

## 1. Objetivo

Em **Cadastro > Clientes** só existia `versao_erp` — a *tag de imagem pedida no
provisionamento*, não a versão em execução. Nada sobre o SyncAgent/PDV instalado
em cada loja.

Agora a página do `Cliente` tem a seção **"Versão Atual"**: versão do ERP em
execução, versão do SyncAgent e do PDV Local, e uma tabela com **todas as
instalações** (uma por loja/máquina).

## 2. Fonte dos dados

| Versão | Fonte de verdade | Como o CP obtém |
|---|---|---|
| ERP em execução | `erp/core/settings.py` `VERSION` | `GET /health/` (público) **ou** `GET /v1/cp/status` |
| SyncAgent / PDV instalado | `erp/sync_api/models.py` `SyncInstallation.agent_version` (por instalação, atualizado a cada heartbeat ~30s) | `GET /v1/cp/status` (autenticado) |

**SyncAgent × PDV:** `pdv-local` é um artefato único — SyncAgent e app PDV saem
juntos na mesma release e o ERP conhece **um** número (`agent_version`). O painel
mostra as duas linhas com o mesmo valor; a linha "PDV Local" fica anotada
*"mesma release do SyncAgent (pdv-local)"*.

## 3. Endpoint no ERP — `GET /v1/cp/status`

`erp/sync_api/cp_status.py::CpStatusAPIView`. Canal interno CP → ERP, **fora** do
prefixo `/v1/sync/` (não é o contrato produto do repo `sync`). Mesma auth do
`/v1/cp/agent-packages:sync`:

1. `Authorization: Bearer <CP_SHARED_SECRET>` (`hmac.compare_digest`) → 401 `unauthorized`.
2. `settings.CP_CLIENTE_ID` vazio → 409 `not_linked_to_cp`.
3. `?cliente_id=` (opcional) ≠ `CP_CLIENTE_ID` → 409 `cliente_id_mismatch`.

Resposta 200:

```json
{
  "status": "ok",
  "erp_version": "0.0.110",
  "cliente_id": "<uuid>",
  "instalacoes": [
    {"instance_id": "...", "installation_label": "Loja Centro",
     "agent_version": "1.6.12", "last_seen_at": "2026-09-10T12:00:00Z",
     "active": true, "connectivity": "online"}
  ]
}
```

`connectivity` é calculado leve, só por frescor de `last_seen_at`
(`<90s` online · `<1h` instável · senão offline · sem heartbeat → unknown) — **não**
reusa `services.get_agent_status`, que faz várias queries de eventos 24h por
instalação.

## 4. Lado CP

- **`registry/models.py`**
  - `Cliente.versao_erp_detectada` — versão em execução (≠ `versao_erp`, a tag alvo).
  - `Cliente.versoes_detectadas_em`, `Cliente.deteccao_versoes_erro`.
  - **`InstalacaoAgente`** (`registry_instalacaoagente`) — cache das `SyncInstallation`
    do ERP, `unique_together (cliente, instance_id)`.
  - Migração `0017`.
- **`registry/cp_pull.py::ColetorVersoes`** — `coletar()`:
  - com `integracao_secret`: `GET /v1/cp/status` → grava os campos do `Cliente` e
    **reconcilia** (full-sync) as `InstalacaoAgente` (upsert das presentes, apaga
    as ausentes — mesma filosofia do `cp_push`).
  - **404** (ERP antigo): fallback `GET /health/` → só `versao_erp_detectada`;
    `deteccao_versoes_erro` explica.
  - 401 / 409 / timeout / ConnectionError: mensagem em pt-BR em `deteccao_versoes_erro`,
    sem crash.
  - sem `integracao_secret`: tenta `/health/`; erro pede "⚙ Aplicar Configurações".
- **`registry/tasks.py`**
  - `task_coletar_versoes_cliente(cliente_id)` — chama `ColetorVersoes.coletar()`.
  - `task_coletar_versoes_todas()` — varre clientes `ativo`/`trial`.
  - `task_verificar_saude_cliente` passou a ler o corpo de `/health/` e atualizar
    `versao_erp_detectada` de graça.
- **`registry/admin.py`**
  - `versao_atual` (readonly, fieldset "Versão Atual"): 3 linhas resumo + tabela de
    `InstalacaoAgente` + botão **🔄 Verificar versões agora**.
  - URL `/<pk>/coletar-versoes/` → `_view_coletar_versoes` → dispara a task,
    redireciona com `?coletando=1`; a página recarrega em ~5s.
  - `list_display`: `col_versao_erp` (detectada × alvo, destaque se divergir).

## 5. Agendamento

Não há schedule em código (igual ao health check). **Criar um `PeriodicTask` no
admin do django-celery-beat** apontando para `registry.tasks.task_coletar_versoes_todas`
com um crontab (ex.: a cada 30 min), em dev e em produção.

## 6. Ordem de deploy

1. `erp`: subir imagem com `GET /v1/cp/status` (bump de `VERSION` + build). Deploy nos clientes.
2. `cp`: `migrate` (0017) + deploy do código + criar o `PeriodicTask`.

Enquanto (1) não chega nos clientes, o painel mostra só a versão do ERP (via
`/health/`) e "instalações indisponíveis (ERP desatualizado)".

Runbook completo passo a passo (comandos, ordem, verificação, rollback):
`docs/registry/manual-deploy-versao-atual-clientes.md`.

## 7. Verificação

**ERP:** `python manage.py test sync_api.tests.CpStatusTests`

**CP:** `.\venv\Scripts\python.exe manage.py test registry.tests.test_cp_pull`

**Manual (CP dev):**

```python
from registry.models import Cliente
from registry.cp_pull import ColetorVersoes
c = Cliente.objects.get(slug='<algum>')
print(ColetorVersoes(c).coletar())
c.refresh_from_db()
print(c.versao_erp_detectada, c.deteccao_versoes_erro)
print(list(c.instalacoes_agente.values('installation_label', 'agent_version', 'connectivity')))
```
