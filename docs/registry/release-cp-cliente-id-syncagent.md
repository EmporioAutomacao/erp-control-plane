# Release: Vínculo CP → ERP para a ativação do SyncAgent

- data: 2026-08-31
- responsavel: James Flavio
- status: implementado
- versao: `0.0.28`
- app: `registry`
- repos: `erp-control-plane` (CP) + `erp` (ERP)

---

## 1. Sintoma

Em produção, ao acessar **Sincronização > Códigos de ativação** no admin do ERP
de um cliente, a geração falhava com:

```
Configure o ID do cliente no CP em Configurações > Plano antes de gerar
códigos de ativação.
```

## 2. Causa

O ERP resolve o ID do cliente via `sync_api.cp_tenant.get_current_cp_tenant()`,
nesta ordem:

1. `settings.CP_CLIENTE_ID` / `CP_CLIENTE_NOME` — **não existiam** em
   `core/settings.py` do ERP.
2. `core.PlanoCliente.cp_cliente_id` — nunca populado em produção (só o comando
   de dev `bootstrap_integrated_dev_cliente` escreve nele).

O `MotorProvisionamento` do CP nunca gravava esse vínculo no stack do cliente.

## 3. Correção

### CP (`erp-control-plane`)

- `registry/provisioning.py` — `MotorProvisionamento` grava `CP_CLIENTE_ID`
  (`str(cliente.id)`, o UUID do `Cliente`) e `CP_CLIENTE_NOME` (`cliente.nome`)
  no `.env` / bloco `environment` do stack.
- `registry/admin.py` — o botão **"Aplicar Configurações"**
  (`_view_aplicar_modulos`) injeta as duas variáveis via
  `docker service update --env-add` no serviço `{slug}_web` já existente.

### ERP (`erp`) — release `0.0.94`

- `core/settings.py` — `CP_CLIENTE_ID` e `CP_CLIENTE_NOME` lidos do ambiente.
- `get_current_cp_tenant()` passa a resolver pelo passo 1.

## 4. Sem migração

Nenhuma mudança de modelo/banco nos dois repos. É só deploy das imagens.

## 5. Deploy em produção

```bash
# 1. Build + push da imagem do CP
docker build -t emporioautomacao/ararasuite-cp:0.0.28 .
docker push emporioautomacao/ararasuite-cp:0.0.28

# 2. No servidor: apontar CP_VERSION e re-deployar o stack
export $(cat /opt/cp/.env | xargs) && CP_VERSION=0.0.28 \
  docker stack deploy --with-registry-auth -c stack-prod.yml ararasuite-cp

# 3. Conferir rollout
docker service ps ararasuite-cp_web
```

A imagem do ERP (`emporioautomacao/ararasuite-erp:0.0.94` / `:latest`) já foi
publicada pelo fluxo `erp-release-docker`.

## 6. Clientes já provisionados (com o erro)

A variável nova não entra sozinha num serviço Swarm já em execução. Para cada
cliente afetado, **depois** de subir a imagem `0.0.94` do ERP:

1. Admin do CP → cliente → **⚙ Aplicar Configurações**
   (injeta `CP_CLIENTE_ID` / `CP_CLIENTE_NOME` no `{slug}_web`).
2. Conferir em **Sincronização > Códigos de ativação** que a tela não bloqueia
   mais.

Alternativa sem esperar a imagem nova do ERP (grava direto no banco do cliente):

```powershell
docker exec -i <slug>_web python manage.py shell -c "from core.models import PlanoCliente; from django.utils import timezone; p = PlanoCliente.obter() or PlanoCliente(); p.cp_cliente_id='<uuid>'; p.cp_cliente_nome='<nome>'; p.ultima_sincronizacao_cp=timezone.now(); p.save()"
```

## 7. Pendência conhecida

A API de sincronização de plano CP → ERP (que preencheria `plano_nome`,
`modulos_assinados`, `limites_plano` e também o `cp_cliente_id` em
`core.PlanoCliente`) continua não implementada — ver
`../infra/configuracoes-plano-cliente.md` no repo `erp`.
