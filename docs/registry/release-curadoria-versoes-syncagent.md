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

1. **Atualizar o catálogo** (uma vez, vale pra todos os clientes):
   **Clientes → Versões do Agente** → botão **"🔍 Verificar novas versões no
   GitHub"** — varre `github.com/EmporioAutomacao/pdv-local/releases`
   sozinho, sem precisar digitar nada (ver seção 3.1). Cadastro manual
   (Adicionar, ou o comando `register_versao_agente`) continua funcionando
   como alternativa.
2. **Permitir para um cliente específico**: página do `Cliente` → aba
   **"Versões do SyncAgent/PDV"** → adicionar em "Versões permitidas" →
   salvar (dispara o push sozinho) ou clicar em **"⇪ Sincronizar Versões com
   o ERP"** pra forçar na hora.
3. **Ver o histórico de pushes**: inline "Sincronização de Versões (CP → ERP)"
   na própria página do cliente — status, payload enviado, código HTTP,
   mensagem de erro se houver.

### 3.1 Descoberta automática de versões (`registry/github_releases.py`)

Em vez de alguém ter que cadastrar cada versão nova na mão (ou o CI do
`pdv-local` ter que avisar o CP — canal que existe como alternativa, seção 6,
mas não está ligado a nenhum workflow hoje), o próprio botão **"🔍 Verificar
novas versões no GitHub"** varre a API pública de Releases do repo
(`PDV_LOCAL_GITHUB_REPO`, padrão `EmporioAutomacao/pdv-local`) e, pra cada
Release publicado (ignora rascunho/pré-release) que tenha um
`pdv-local-vX.Y.Z.zip` + `.zip.sha256`, faz upsert do `VersaoAgente`
correspondente — versão, URL, SHA256 e as notas do Release. **Nunca** mexe em
`erp_minimo` de uma versão já cadastrada (o GitHub não tem essa informação;
fica só sob controle manual) e **nunca** permite a versão pra nenhum cliente
sozinho — isso continua decisão manual, por cliente (passo 2 acima).
Idempotente: clicar de novo não duplica nem apaga nada.

Opcional: `GITHUB_TOKEN` (settings) aumenta o limite de requisições da API do
GitHub — funciona sem ele, só com limite mais baixo (60/h por IP, suficiente
pra cliques manuais ocasionais).

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

### 6.1 Endpoint recebido pelo próprio CP (canal alternativo, não usado por nenhum CI)

`POST /v1/releases/pdv-local:register` (`registry/release_api.py`) — pensado
originalmente pra o workflow de release do `pdv-local` chamar depois de
publicar um Release, mas a **forma recomendada** de popular o catálogo virou
o botão de descoberta automática (seção 3.1), então este endpoint **não está
ligado a nenhum workflow hoje** — fica disponível como canal alternativo
(scripts, automações futuras) e como base da mesma lógica que o comando
`register_versao_agente` usa. Autenticado por `PDV_LOCAL_RELEASE_TOKEN`
(shared secret, não o `Cliente.integracao_secret` de ninguém). Só popula o
catálogo — nunca permite a versão pra nenhum cliente sozinho.

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
- Botão "🔍 Verificar novas versões no GitHub" testado contra a API **real**
  do GitHub (`EmporioAutomacao/pdv-local`) — encontrou e cadastrou as 4
  versões publicadas (`1.4.0`, `1.5.0`, `1.5.1`, `1.6.0`) com URL/SHA256
  corretos; clicar de novo não duplicou nada (idempotente).
- `python manage.py test registry.tests.test_release_api
  registry.tests.test_github_releases` — 11 testes, todos passando, contra
  banco de teste isolado (`test_erp_cp`) criado do zero (valida a migração
  `0014` funcionando limpa também).

### Bugs encontrados e corrigidos durante o teste ao vivo

- **Item novo invisível no menu**: `VersaoAgenteAdmin` foi registrado, mas o
  menu lateral deste admin usa uma lista fixa (`UNFOLD.SIDEBAR.navigation`
  em `core/settings.py`, `show_all_applications: False`) — o link não foi
  adicionado lá, então a tela existia (por URL direta) mas não aparecia pra
  ninguém achar. Foi assim que o usuário reportou "não estou achando
  Clientes → Versões do Agente". Corrigido adicionando o item na navegação.
- **`NameError: name 'messages' is not defined`** em `_view_verificar_github`
  — `messages` é importado localmente dentro de cada view neste arquivo
  (convenção do `admin.py`), esqueci o import na view nova. Só apareceu
  rodando de verdade (`manage.py check` não pega isso), corrigido depois de
  reproduzir o 500 ao vivo.
- **Testes shadowed**: este app tem um pacote `registry/tests/` (vários
  arquivos `test_*.py`), mas também existia um `registry/tests.py` (boilerplate
  do Django nunca removido) — o pacote sempre vence na resolução de import, e
  qualquer teste escrito em `tests.py` nunca rodava de verdade, silenciosamente.
  Os testes desta feature foram movidos pra `registry/tests/test_release_api.py`
  e `registry/tests/test_github_releases.py`; `tests.py` foi removido.

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
4. **Popular o catálogo em produção**: clicar **"🔍 Verificar novas versões no
   GitHub"** uma vez em **Clientes → Versões do Agente** — cadastra `1.4.0` a
   `1.6.0` sozinho. Falta só **permitir** pros clientes que devem receber
   cada versão (decisão manual, por cliente, de propósito).
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
| `erp-control-plane` | `3e8df00` (catálogo + curadoria + push manual), `a7d14a2` (push automático), `924aaeb` (docs + Ajuda v1), este commit (menu lateral do catálogo, descoberta automática via GitHub, endpoint alternativo `release_api.py`) |
| `pdv-local` | `71ae236` (Tray vira lista + travas client-side), tag `v1.6.0` publicada |
| `sync` | `1349da2` (contrato 2.6.0) — local, sem remote |
