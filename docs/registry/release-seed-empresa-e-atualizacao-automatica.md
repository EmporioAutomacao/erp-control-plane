# Release: Seed de Empresa/Estoque no cliente novo + modo "Atualização Automática" de versões

- data: 2026-09-08
- responsavel: James Flavio
- status: implementado
- versao CP: `0.0.35`
- versao ERP: `0.0.105`
- app: `registry` (CP) / `sync_api`, `core` (ERP)
- repos: `erp-control-plane` (CP) + `erp` (ERP)

---

## 1. Seed de Empresa e Estoque iniciais

### Sintoma

Cliente recém-provisionado sobe sem nenhuma **Empresa** (Configurações > Empresas)
nem **Estoque** (Produtos > Estoque = `produtos.Loja`) — cadastro de produtos,
vendas e caixa dependem dos dois, então a loja não roda até alguém cadastrar na mão.

### Correção

**CP (`erp-control-plane`)**

- `registry/provisioning.py` — `MotorProvisionamento` grava `CP_CLIENTE_CNPJ`
  (`= cliente.cnpj`) no `.env` / bloco `environment` do stack, junto de
  `CP_CLIENTE_ID` / `CP_CLIENTE_NOME`.
- `registry/admin.py` — o botão **"⚙ Aplicar Configurações"**
  (`_view_aplicar_modulos`) injeta `CP_CLIENTE_CNPJ` via `--env-add` no
  `{slug}_web` já existente (backfill para clientes antigos).

**ERP (`erp`) — release `0.0.105`**

- `core/settings.py` — novo `CP_CLIENTE_CNPJ` lido do ambiente.
- `core/management/commands/seed_cliente_inicial.py` (novo) — cria
  `empresas.Empresa` (razão social/CNPJ dos env vars do CP) e `produtos.Loja`
  "Matriz" se não existirem. Idempotente.
- `entrypoint.sh` — roda `manage.py seed_cliente_inicial` após `migrate` +
  superuser (não-fatal).
- **Fix de migration (pré-existente):** `migrate` num banco vazio (todo
  provisionamento de cliente novo) falhava em `financeiro.0021`
  (`column financeiro_condicaopagamento.quantidade_parcelas does not exist`) —
  o grafo de migrations agendava o rename da tabela antes de `vendas.0017`
  adicionar as colunas. Fix: arestas em `financeiro/migrations/0018` →
  `vendas.0024` / `dedetizacao.0006`, consistentes com a ordem histórica de
  aplicação (nenhum `InconsistentMigrationHistory` nos bancos já rodando).

### Clientes já provisionados

Não recebem o seed sozinhos (a imagem antiga não tem o comando). Depois de
subir a imagem `0.0.105` do ERP no cliente: **⚙ Aplicar Configurações** (injeta
`CP_CLIENTE_CNPJ` + recria o `{slug}_web`) → o seed roda nesse boot. Ou cadastrar
Empresa/Estoque manualmente.

---

## 2. Modo "Atualização Automática" de versões do SyncAgent/PDV

### Necessidade

Hoje, a cada release do `pdv-local`, alguém precisa entrar em **cada cliente** e
adicionar a versão nova em `Cliente.versoes_permitidas`. Repetitivo e fácil de esquecer.

### Correção — CP (`erp-control-plane`)

- `registry/models.py` — novo `Cliente.atualizacao_automatica_agente` (bool).
  Migração `0015_cliente_atualizacao_automatica_agente`.
- `registry/cp_push.py` — `SincronizadorVersoes._versoes_a_enviar()`: modo
  automático envia todo `VersaoAgente.objects.filter(ativo=True)`; senão a
  curadoria manual (`versoes_permitidas`, guardada, volta ao desligar).
- `registry/signals.py` —
  - `pre_save`/`post_save` no `Cliente` disparam o push ao ligar/desligar a caixa;
  - `versao_agente_atualizada` reescrito: versão **nova** no catálogo
    (descoberta no GitHub, `register_versao_agente`) propaga sozinha pros
    clientes automáticos.
- `registry/admin.py` — campo no fieldset "Versões do SyncAgent/PDV",
  `list_filter`, banner informativo em `acoes_versoes`.
- Testes: `registry/tests/test_cp_push.py` (novo, 10).

### Correção — ERP (`erp`) — release `0.0.105`

- `sync_api/services.py::get_available_packages` — ordena os `SyncPackage`
  permitidos por **semver decrescente** (`versioning.parse_version`), não por
  `-created_at`. Um push em lote do CP cria os pacotes na ordem inversa da
  versão, o que fazia o Tray pré-selecionar a **mais antiga** no "Atualizar App".
- Teste: `sync_api/tests.py::...test_lista_ordenada_por_semver_nao_por_data`.

### O que NÃO muda

Travas de downgrade e `erp_minimo` (aplicadas pelo ERP + re-checadas pelo
SyncAgent). O dono da loja continua abrindo **"Atualizar App"** e confirmando —
só que a versão mais nova compatível já vem pré-selecionada.

Guia completo: `docs/registry/release-curadoria-versoes-syncagent.md` (seção 3.2).

---

## 3. Migrações

- CP: `registry/migrations/0015_cliente_atualizacao_automatica_agente.py`
  (`AddField` bool, `default=False` — sem backfill).
- ERP: só arestas de dependência em migrations já existentes (`financeiro.0018`),
  sem operação nova de schema.

## 4. Deploy em produção

```bash
# 1. ERP — via skill erp-release-docker (repo d:\GitHub\erp), imagem 0.0.105 + latest
#    (commit e git push já feitos; roda com -SkipCommit -KeepCurrentVersion)

# 2. CP — build + push da imagem
cd d:\GitHub\erp-control-plane
docker build --pull -t emporioautomacao/ararasuite-cp:0.0.35 .
docker push emporioautomacao/ararasuite-cp:0.0.35
docker tag emporioautomacao/ararasuite-cp:0.0.35 emporioautomacao/ararasuite-cp:latest
docker push emporioautomacao/ararasuite-cp:latest

# 3. No servidor: re-deployar o stack do CP
export $(cat /opt/cp/.env | xargs) && CP_VERSION=0.0.35 \
  docker stack deploy --with-registry-auth -c stack-prod.yml ararasuite-cp
docker service ps ararasuite-cp_web

# 4. Atualizar a imagem do ERP dos clientes que forem receber o seed / a curadoria automática:
#    Admin do CP → cliente → "Atualizar versão" (0.0.105) → depois "⚙ Aplicar Configurações"
```
