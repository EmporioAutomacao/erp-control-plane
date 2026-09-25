# Release: Canal de sincronização do plano do cliente (CP → ERP)

- data: 2026-09-25
- responsavel: James Flavio
- status: implementado
- versao: `0.0.39`
- app: `registry`
- arquivos modificados:
  - `core/settings.py` — `VERSION` 0.0.38 → 0.0.39
  - `registry/cp_push.py` — `SincronizadorPlano` (novo) + `_mensagem_erro_http_amigavel` (extraído de `SincronizadorVersoes._registrar_erro_http`, reaproveitado pelos dois)
  - `registry/tasks.py` — `task_sincronizar_plano_cliente` (novo)
  - `registry/signals.py` — `_cliente_plano_ou_status_alterado` (novo), `modulos_ativos_changed` (novo), `_cliente_captura_auto_update` estendido para capturar `plano_id`/`status` anteriores
  - `registry/apps.py` — conecta `modulos_ativos_changed` no `m2m_changed` de `Cliente.modulos_ativos.through`
  - `registry/admin.py` — botão "🔄 Sincronizar Plano com o ERP" (`acoes_plano_sync`, campo readonly no fieldset "Plano") + `_view_sincronizar_plano`
  - `registry/provisioning.py` — push inicial não-fatal em `MotorProvisionamento.executar()`

Contraparte no repositório `erp`: ver `docs/modulos/empresas/release-limite-empresas-plano.md` (motivação do lado ERP: bloquear/exibir limite de empresas por plano em Configurações > Empresas).

---

## 1. Objetivo

`registry.Cliente.plano` (FK para `Plano`, que já tem `max_usuarios`, `max_empresas`, `recursos_cpu`, `recursos_ram_gb`) nunca chegava à instância `erp` do cliente — `core.PlanoCliente.limites_plano` lá era um JSONField sempre vazio em produção, porque não existia nenhum canal HTTP escrevendo nele. Isso bloqueava qualquer feature do lado ERP que precisasse saber os limites reais do plano contratado (o gatilho imediato foi o limite de empresas cadastráveis).

## 2. `SincronizadorPlano` (`registry/cp_push.py`)

Mesmo padrão de `SincronizadorVersoes` (mesmo arquivo): monta payload, `POST https://{dominio_custom ou subdominio}/v1/cp/plano:sync` com `Authorization: Bearer {cliente.integracao_secret}`, timeout 15s. Diferença: **sempre síncrono**, sem model de auditoria dedicado — o payload é pequeno e rápido (ao contrário do catálogo de versões do SyncAgent, que pode ser grande e lento), e o próprio ERP já guarda `PlanoCliente.ultima_sincronizacao_cp`. `sincronizar()` nunca lança — sempre devolve `{'sucesso': bool, 'mensagem': str, 'http_status': int|None}`, seguro de chamar tanto direto (view, provisionamento) quanto de dentro de uma task.

Payload:
```json
{
  "cliente_id": "<uuid>",
  "plano_nome": "Essencial",
  "status_assinatura": "ativo",
  "modulos_assinados": ["financeiro", "empresas", "..."],
  "limites_plano": {"max_usuarios": 5, "max_empresas": 1, "recursos_cpu": 1, "recursos_ram_gb": 2}
}
```

`status_assinatura` passa por `_STATUS_ASSINATURA_MAP`: `Cliente.STATUS_CHOICES` tem 8 valores, `PlanoCliente.STATUS_CHOICES` (erp) só aceita 4 (`ativo`/`trial`/`suspenso`/`cancelado`) — `trial_expirado` vira `suspenso`; os três estados transitórios de provisionamento (`aguardando_provisao`/`provisionando`/`erro_provisao`) viram `ativo` como fallback defensivo (na prática só chegam aqui se algo chamar o sync antes do fluxo normal, já que o push depende de `integracao_secret`, que só existe depois do provisionamento).

## 3. Gatilhos automáticos

- **Plano ou status mudou** (`registry/signals.py::_cliente_plano_ou_status_alterado`, `post_save` em `Cliente`): mesmo padrão de `_cliente_auto_update_alterado` — `pre_save` captura `plano_id`/`status` anteriores, `post_save` compara e dispara `task_sincronizar_plano_cliente.delay(...)` se algum dos dois mudou. Só dispara com `integracao_secret` já gerado.
- **`Cliente.modulos_ativos` mudou** (`registry/signals.py::modulos_ativos_changed`, `m2m_changed`): mesmo padrão de `versoes_permitidas_changed`, conectado imperativamente em `RegistryConfig.ready()` (through model sem nome estável para `@receiver`).
- Nenhum dos dois dispara em `Cliente.objects.filter().update()` (provisionamento, tasks em lote) — só em `.save()` via admin.

## 4. Push inicial no provisionamento

`MotorProvisionamento.executar()`, logo após o serviço web confirmar saúde e o `Cliente` ser marcado `ativo`: chama `SincronizadorPlano(self.cliente).sincronizar()` direto, envolto em try/except não-fatal (nunca derruba o provisionamento). Garante que uma instância recém-provisionada já nasce com `limites_plano` real, sem depender do próximo signal.

## 5. Botão manual

Campo readonly `acoes_plano_sync` no fieldset "Plano" do `ClienteAdmin` (ao lado de `plano`/`modulos_ativos`/`tema_site`) — botão "🔄 Sincronizar Plano com o ERP", síncrono (sem task, sem log ao vivo — diferente do botão de versões, que usa task só para ter log em tempo real). Sem `integracao_secret`, avisa para rodar "⚙ Aplicar Configurações" primeiro.

## 6. Sem migração

`registry.Plano.max_empresas` e demais campos já existiam. Confirmado com `makemigrations --check --dry-run`.

## 7. Deploy em produção

```bash
docker build -t emporioautomacao/ararasuite-cp:0.0.39 .
docker push emporioautomacao/ararasuite-cp:0.0.39

export $(cat /opt/cp/.env | xargs) && CP_VERSION=0.0.39 \
  docker stack deploy --with-registry-auth -c stack-prod.yml ararasuite-cp

docker service ps ararasuite-cp_web
```

Depende da versão do `erp` com `CpPlanoSyncAPIView` (`/v1/cp/plano:sync`) já em produção nos clientes alvo — sem isso, o push falha com `404` (rota inexistente) e a task retenta 3x até desistir; não é destrutivo, só fica pendente até o cliente atualizar a imagem do erp.

## 8. Como testar após o deploy

1. Trocar `Cliente.plano` no admin do CP → task assíncrona dispara (checar log do worker Celery) → `PlanoCliente` no ERP alvo atualiza.
2. Alterar `Cliente.modulos_ativos` → mesmo efeito.
3. Clicar "🔄 Sincronizar Plano com o ERP" → mensagem de sucesso/erro síncrona.
4. Provisionar um cliente de teste novo → confirmar que `PlanoCliente.ultima_sincronizacao_cp` já vem preenchido no ERP recém-criado.
