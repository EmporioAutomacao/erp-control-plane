# Release: cadastro de Regiões + migração de cliente entre servidores

- data: 2026-10-29
- responsavel: James Flavio
- status: implementado e coberto por testes — **pendente de homologação em Swarm de 2 nós na VM** antes de tag/release
- versao CP: `0.0.52`
- app: `registry`
- repo: `erp-control-plane` (nada muda no `erp` nem no `pdv-local`)

---

## 1. Objetivo

Levar clientes de um servidor para outro (primeiro caso: Anápolis → Hostinger) sem perder dados e sem obrigar
as lojas a reativar o SyncAgent. Especificação completa, achados e runbook:
[`migracao-cliente-entre-servidores.md`](migracao-cliente-entre-servidores.md).

## 2. O que mudou

### Regiões
- **`Regiao`** (migrations `0024`/`0025`): cadastro em *Infraestrutura → Regiões*. PK `slug` = valor do label
  `node.labels.region` (só minúsculas, números e hífen; imutável depois de salvo). `anapolis`/`brasilia` e qualquer
  valor já gravado nos hosts são criados pela migration. `HostInfraestrutura.regiao` virou FK (mesma coluna).
  A tela da região mostra o comando `docker node update --label-add region=<slug> <node-id>`. Região com hosts não
  pode ser apagada; inativa some do cadastro de hosts.

### Status dos hosts
- **`HostInfraestrutura`** ganhou o sinalizador de saúde (migration `0028`) e a verificação periódica a cada 5 min
  (migration `0029`). `registry/hosts.py` combina o Swarm (`docker node inspect`: Ready, Active, x86_64, label da
  região, endereço, CPU/RAM) com o ping do worker do CP no nó (`task_pingar_host`: prova a fila `no.<hostname>`,
  mede o disco do Docker e conta os clientes). Online / Atenção / Offline / Não verificado, com o motivo.
- Admin: coluna **Status** e filtro na lista; no cadastro, painel atualizado ao abrir e botão **🔌 Verificar agora**.
  O `swarm_node_id` é preenchido sozinho quando a região tem um único nó.

### Verificações periódicas nascem agendadas
- Migration `0030`: cria, se ainda não existirem, as tarefas periódicas de saúde dos clientes (10 min), versões e
  instalações dos clientes (30 min), conexão com o armazenamento (15 min) e atualizações do CP (6 h). Antes a
  documentação mandava criá-las à mão em cada ambiente — produção estava sem a coleta, por isso o status/versão
  dos clientes funcionava no dev (botão) e não atualizava em produção. Tarefas já existentes, mesmo desativadas,
  não são duplicadas nem reativadas.
- Ajuda e `versao-atual-clientes.md` ganharam o diagnóstico "funciona num ambiente e não no outro".

### Migração de servidor
- **`MigracaoCliente`** (migration `0026`) e o vigia agendado (migration `0027`, `django_celery_beat`).
- **`registry/migracao.py`**: máquina de estados, gates G1–G13, volta para a origem, ponto sem volta, vigia.
- **`registry/swarm.py`** (manager) e **`registry/volumes.py`** (nó): utilitários de Swarm, volumes e Postgres.
- **Admin:** botão 🚚 Migrar servidor (aba Acesso e Infraestrutura), tela *Infraestrutura → Migrações de servidor*
  com log, gates e ⏹ Interromper. Com migração ativa o cadastro do cliente fica somente leitura e as ações que
  mexem no stack são recusadas.
- **`MotorProvisionamento.executar(modo_migracao=True)`**: app em 0 réplicas e fora do Traefik, exige os segredos
  do `.env`, não altera status/`data_ativacao`. `montar_env()`/`ler_env_existente()` extraídos de `executar`.

### Correções no backup e na restauração comuns
- Mídia lida **do volume** (funcionava só com o web ligado — cliente suspenso saía sem mídia).
- Volume `{slug}_backups` incluído no backup e na restauração.
- SHA-256 gravado no backup e conferido antes de restaurar.
- Restauração para e religa também o **cron** e devolve as réplicas de antes (antes religava `web=1` sempre,
  inclusive em cliente suspenso, e deixava o cron escrevendo durante o `psql`). Interromper usa as réplicas salvas.
- Restauração espera o Postgres aceitar conexão TCP antes do `psql`.

## 3. Migrations

`0024_regiao`, `0025_hostinfraestrutura_regiao_fk`, `0026_migracao_cliente`, `0027_agenda_vigia_migracoes`,
`0028_status_hosts`, `0029_agenda_verificacao_hosts`, `0030_agenda_verificacoes_periodicas`.
Rodam no `entrypoint-worker.sh` do `celery`. Testadas no banco local com hosts existentes (inclusive valor de região
fora da lista antiga).

## 4. Testes

- `registry/tests/test_hosts.py` (24): cada situação de status (Down, Drain, ARM, label/IP divergente, 2 nós sem ID,
  disco baixo, worker atrasado ou nunca visto, sem Swarm), ping na fila certa, agenda e admin.
- `registry/tests/test_regioes.py` (9) e `registry/tests/test_migracao.py` (63): caminho feliz, cada gate
  reprovando isoladamente, sondas, volta em cada etapa, ponto sem volta, vigia, unicidade, admin, tasks adiadas,
  modo migração do provisionamento e tasks dos nós.
- Suítes de backup/console/Playwright atualizadas para o comportamento novo.
- `registry/volumes.py` exercitado contra Docker real (volume com arquivo oculto, nome com espaço; cópia →
  restauração → manifesto idêntico) e `manifesto_banco` contra o Postgres do ERP de desenvolvimento.

## 5. Homologação obrigatória antes do release

Ver seção 9 da especificação: Swarm de 2 nós na VM, regiões `homolog-a`/`homolog-b`, cliente de teste com
dados, mídia e PDV ativado. Ida, volta forçada, falha pós-abertura, cliente suspenso e migração de retorno.

## 6. Deploy

1. Rebuild/push da imagem `0.0.52`; redeploy do stack do CP (o `beat` precisa reiniciar para ver o vigia).
2. Conferir em *Agendamentos → Tarefas Periódicas*: **"Vigia das migrações de servidor"**, **"Verificação dos
   hosts de infraestrutura"**, **"Saúde dos clientes (/health/)"** e **"Versões e instalações dos clientes"** ativas
   (se produção já tinha alguma criada à mão e desativada, a migration a mantém desativada — reative se for o caso).
3. Cadastrar a região `hostinger`, rodar o label no nó e cadastrar o host.
