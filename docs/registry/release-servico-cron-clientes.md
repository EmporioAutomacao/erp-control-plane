# Release: serviço de crons (`<slug>_cron`) nas instâncias do ERP

- data: 2026-10-13
- responsavel: James Flavio
- status: implementado — testes de provisionamento OK (9; suíte com banco não rodada nesta sessão); comandos Docker testados só com mock
- versao CP: `0.0.48`
- app: `registry`
- depende do ERP: qualquer versão (o endurecimento do executor vem no ERP `0.0.135`)

---

## 1. Objetivo
Nenhum cliente tinha processo rodando as crons do ERP (Reposição Automática, Mercado Livre, Yampi, filas de WhatsApp/Comunicações, Tarefas, NF-e): o web só sobe o Daphne e o stack só tinha `web`, `db` e `redis`. Um loop iniciado à mão morria a cada atualização de versão, que só mexe no `web`.

## 2. O que mudou
- `registry/provisioning.py`:
  - `CRON_COMANDO` = `python manage.py executar_crons --loop --sleep 60`.
  - `_gerar_stack_yaml`: novo serviço `cron` (mesma imagem, env e volumes do web; só rede `internal`, sem Traefik; `entrypoint` trocado — migrations ficam só com o web; `restart_policy: any`).
  - `sincronizar_servico_cron(versao=None)`: se `<slug>_cron` não existe, cria a partir da spec atual do `<slug>_web` (env, mounts, placement — o `.env` não reflete o `--env-add` do "Aplicar Configurações"); se existe, atualiza imagem e env.
  - `atualizar_versao`: sincroniza o cron **depois** do web subir (migrations já aplicadas). Falha marca a atualização como erro (retry da task é idempotente).
  - `suspender`: zera também o cron (cliente suspenso não dispara WhatsApp/e-mail). `reativar`: sobe o cron (cria se faltar).
- `registry/admin.py` ("Aplicar Configurações"): chama `sincronizar_servico_cron()` depois do update do web; falha só gera aviso.
- Testes: `registry/tests/test_provisioning.py::ServicoCronTests`.

## 3. Deploy
```bash
docker build --pull -t emporioautomacao/ararasuite-cp:0.0.48 -t emporioautomacao/ararasuite-cp:latest .
docker push emporioautomacao/ararasuite-cp:0.0.48
docker push emporioautomacao/ararasuite-cp:latest
```
Atualizar o CP pelo painel (Configurações > Verificar Atualizações) ou redeploy do stack com `CP_VERSION=0.0.48`. Sem migration.

## 4. Depois do deploy (um cliente por vez)
1. Cancelar no ERP as mensagens pendentes antigas (Configurações › Comunicações › Mensagens de Saída) — a fila sai toda de uma vez quando o cron sobe.
2. Remover qualquer cron criado à mão (Portainer/serviço com outro nome).
3. Abrir o Cliente e clicar em **"Aplicar Configurações"** → cria `<slug>_cron`.
4. Conferir: `docker service logs -f <slug>_cron` (em até 1 min aparece a linha da Reposição).

Desfazer: `docker service rm <slug>_cron` (não afeta web nem banco). Atenção à RAM do nó: +150–200 MB por cliente.
