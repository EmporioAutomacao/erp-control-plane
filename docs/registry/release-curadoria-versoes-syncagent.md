# Release: Curadoria de versões do SyncAgent/PDV por cliente (sem downgrade, com bloqueio de ERP incompatível)

- data: 2026-09-05
- responsavel: James Flavio
- status: implementado (fases 1-7 do plano), pendente de deploy em produção
- versao: `0.0.32`
- app: `registry` (CP) + `sync_api` (ERP) + `pdv-local` (SyncAgent/Tray) + `sync` (contrato)
- repos: `erp-control-plane`, `erp`, `pdv-local`, `sync`
- plano original: `C:\Users\JamesFlavio\.claude\plans\vamos-planejar-para-no-foamy-crown.md`

---

## 1. Objetivo

Antes desta feature, decidir qual versão do PDV Local um cliente recebia era
manual e por instância: alguém logava no admin do ERP **daquele cliente**
específico e marcava um `SyncPackage` como `is_current` (só 1 por vez, sem
escolha pro dono da loja). Não havia nenhuma trava contra downgrade nem
qualquer noção de compatibilidade entre a versão do ERP e a versão do
SyncAgent/PDV.

Agora:

1. A equipe cura, **aqui no painel de controle**, por cliente, um **conjunto**
   de versões do SyncAgent/PDV permitidas — sem precisar logar no admin do ERP
   de cada cliente.
2. O dono da loja escolhe, dentro desse conjunto, qual versão aplicar (tela
   "Atualizar App" da bandeja, que virou uma lista).
3. **Nunca é possível fazer downgrade** — trava técnica, não só cuidado de UI.
4. **Nunca é possível aplicar uma versão que exija um ERP mais novo** que o do
   cliente — a versão aparece na lista desabilitada, com o motivo.

## 2. Como funciona — visão geral

```
Clientes > Versões do Agente (catálogo mestre, global)
        │  versão, download_url, sha256, "versão mínima do ERP exigida"
        ▼
Cliente > "Versões do SyncAgent/PDV" > Versões permitidas (M2M, por cliente)
        │  editar dispara sozinho o push (m2m_changed) — ou botão manual
        ▼
POST https://{subdominio-do-cliente}/v1/cp/agent-packages:sync
        │  Authorization: Bearer {Cliente.integracao_secret}
        ▼
erp do cliente: SyncPackage.allowed / erp_minimo / synced_from_cp_at
        │  full-sync: quem está no payload vira allowed=True,
        │  quem não está e estava allowed vira allowed=False (nunca deletado)
        ▼
GET /v1/sync/agents/{instanceId}/available-packages  (SyncAgent consulta)
        │  nunca inclui versão <= instalada; marca blocked=True quando
        │  erp_minimo > versão real deste ERP (settings.VERSION, não confia
        │  no que o CP acha que é)
        ▼
Tray > "Atualizar App": lista de versões, downgrade nem aparece,
       incompatíveis aparecem desabilitadas com o motivo
```

### Defesa em profundidade (3 camadas, nenhuma confia sozinha na anterior)

| Camada | Downgrade | ERP incompatível |
|---|---|---|
| **CP** (curadoria) | Só UX — não bloqueia o push | Só UX — não bloqueia o push |
| **erp do cliente** (`sync_api.services.get_available_packages`) | **Trava real** — compara semver com `SyncInstallation.agent_version` | **Trava real** — compara `erp_minimo` com `settings.VERSION` local (nunca confia no CP) |
| **SyncAgent** (`SelfUpdater`) | Revalida de novo antes de baixar, mesmo pacote já filtrado | Revalida `blocked` recebido antes de baixar |

O **erp de cada cliente é a fonte de verdade** (é o único que sabe com
certeza a versão instalada e a versão real daquele ERP). O CP é só curadoria/
UX. O SyncAgent é a última linha de defesa.

## 3. Onde mexer no dia a dia

1. **Cadastrar uma versão nova no catálogo** (uma vez, vale pra todos os
   clientes): **Clientes → Versões do Agente** → Adicionar. Ver a "Ajuda" do
   admin (seção "Curadoria de versões do PDV Local por cliente") para o
   passo a passo com os campos.
2. **Permitir para um cliente específico**: página do `Cliente` → aba
   **"Versões do SyncAgent/PDV"** → adicionar em "Versões permitidas" →
   salvar (dispara o push sozinho) ou clicar em **"⇪ Sincronizar Versões com
   o ERP"** pra forçar na hora.
3. **Ver o histórico de pushes**: inline "Sincronização de Versões (CP → ERP)"
   na própria página do cliente — status, payload enviado, código HTTP,
   mensagem de erro se houver.

## 4. Modelos novos (`registry/models.py`)

- `VersaoAgente` — catálogo mestre global (`versao`, `erp_minimo`,
  `download_url`, `sha256` com o mesmo `RegexValidator` de 64 hex do `erp`,
  `release_notes`, `ativo`).
- `Cliente.versoes_permitidas` — M2M pro catálogo, curadoria por cliente.
- `Cliente.integracao_secret` — Bearer token usado pelo CP nas chamadas pra
  aquela instância. Gerado no provisionamento (`MotorProvisionamento`) ou no
  backfill do botão **"⚙ Aplicar Configurações"**, para clientes já
  provisionados antes desta feature.
- `SincronizacaoVersoesAgente` — log de cada push (mesmo padrão de
  `AtualizacaoVersao`), aparece como inline na página do cliente.

Migração: `registry/migrations/0014_versao_agente_curadoria.py`.

## 5. Push automático (`registry/signals.py` + `registry/tasks.py`)

- `m2m_changed` em `Cliente.versoes_permitidas.through` (conectado
  imperativamente em `RegistryConfig.ready()` — o through model auto-gerado
  não tem nome estável pra usar com `@receiver(sender='app.Model')`).
- `post_save` em `VersaoAgente` — se o catálogo mestre mudar (corrigir um
  sha256, aposentar uma versão com `ativo=False`, trocar `erp_minimo`),
  reenvia a curadoria de todo cliente que tenha essa versão marcada.
- `task_sincronizar_versoes_agente` (Celery, `bind=True, max_retries=3`) —
  mesma classe `SincronizadorVersoes` do botão manual, só que assíncrona.
- Clientes sem `integracao_secret` ainda são ignorados pelos signals (nada a
  sincronizar até o backfill acontecer).

## 6. Endpoint recebido pelo `erp` (não faz parte do contrato `sync`)

`POST /v1/cp/agent-packages:sync` (`sync_api/cp_push.py`, repo `erp`) —
autenticado por `CP_SHARED_SECRET` (não o bearer token de `SyncInstallation`),
valida `cliente_id == settings.CP_CLIENTE_ID`, faz upsert full-sync em
`SyncPackage` (quem sai do payload vira `allowed=False`, nunca é deletado).
Fica **fora** do prefixo `v1/sync/...` de propósito — é canal interno CP→erp,
não o contrato produto ERP↔SyncAgent (esse ganhou `GET /available-packages`,
contrato `sync` v2.6.0).

## 7. Validação feita (ambiente local, não produção)

Testado ao vivo com `docker-compose.integrated-dev.yml` (CP em `:8001` +
um erp de cliente de teste em `:8002`, imagens rebuildadas a partir do código
desta feature):

- Migrações `0011`→`0014` (`erp` e `registry`) aplicadas sozinhas no boot dos
  containers.
- Ciclo completo: curar 2 versões no CP → push HTTP real → `SyncPackage`
  reconciliado corretamente (`synced`/`retired`) → `GET /available-packages`
  devolvendo a lista certa pro SyncAgent.
- Downgrade nunca ofertado (testado bumping `agent_version` de teste).
- Versão com `erp_minimo` maior que o ERP local apareceu com
  `blocked: true, blocked_reason: erp_incompativel`.
- Autenticação do push: 401 sem/errado, 409 `cliente_id` divergente.
- Admin (login, página do `VersaoAgente`, página do `Cliente` com a aba nova,
  botão "Sincronizar Versões") renderizando sem erro.

### Problema de ambiente encontrado (não é bug da feature)

O Django recusa hostname com `_` (RFC 1034/1035) — e o Docker Compose nomeia
o serviço `erp_cliente_web` com underscore. **Nunca acontece em produção**
(subdomínio real nunca tem `_`), só apareceu porque o `Cliente.subdominio` de
teste apontava pro nome do serviço docker. Contorno usado no teste: falar
direto com o IP do container e forçar o header `Host` para um valor já
presente em `ALLOWED_HOSTS` (ex.: `localhost`). Nada foi alterado no código
por causa disso.

### Não verificado neste ambiente

Um **worker Celery de verdade** consumindo `task_sincronizar_versoes_agente`
— o `docker-compose.integrated-dev.yml` não tem um serviço de worker. `.delay()`
enfileira sem erro (Redis do CP está de pé), mas não vi a task ser executada
por um consumidor.

## 8. Pendências antes de valer em produção

1. **Migrações**: `erp` (`0011` + `0012`, por instância de cliente) e
   `erp-control-plane` (`0014`) — nenhuma aplicada em produção ainda.
2. **`integracao_secret`**: nenhum cliente real tem ainda. Nasce no próximo
   provisionamento, ou clicando **"⚙ Aplicar Configurações"** em cada
   cliente já existente.
3. **Celery/Redis do CP em produção**: confirmar que o worker está de pé
   (`docker service ls | grep celery`) para o push automático (seção 5)
   funcionar sem depender do botão manual.
4. **Publicar uma versão real no catálogo**: `PDV Local 1.6.0` já está
   publicado no GitHub (`github.com/EmporioAutomacao/pdv-local/releases/tag/v1.6.0`)
   — falta cadastrar em **Clientes → Versões do Agente** e permitir pros
   clientes que devem recebê-la.
5. **Repo `sync`** (contrato `2.6.0`, endpoint `available-packages`
   documentado) está commitado localmente mas **sem remote configurado**
   nesta máquina — não foi publicado em lugar nenhum ainda.

## 9. Decisão de produto registrada (fase 7 do plano)

O admin manual de `SyncPackage` (ações "Permitir"/"Bloquear") **não foi**
travado como somente-leitura. Continua sendo o fallback para instalações
ainda sem cliente vinculado ao CP, ou pra ajuste pontual — mas avisa
(`messages.WARNING`) quando o pacote editado já tem `synced_from_cp_at`
preenchido, já que o próximo push do CP reconcilia o conjunto inteiro de
novo e pode desfazer a edição manual.

## 10. Commits

| Repo | Commits |
|---|---|
| `erp` | `a366651` (SyncPackage.allowed + travas), `f36f93e` (endpoint cp_push), `5812945` (docs + aviso no admin) |
| `erp-control-plane` | `3e8df00` (catálogo + curadoria + push manual), `a7d14a2` (push automático), este commit (docs + Ajuda) |
| `pdv-local` | `71ae236` (Tray vira lista + travas client-side), tag `v1.6.0` publicada |
| `sync` | `1349da2` (contrato 2.6.0) — local, sem remote |
