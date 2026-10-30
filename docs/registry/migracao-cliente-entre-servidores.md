# Migração de cliente entre servidores (especificação)

- data: 2026-10-29
- responsavel: James Flavio
- status: implementado e coberto por testes (`registry/tests/test_migracao.py`) — pendente de homologação em Swarm de 2 nós
- versao CP: `0.0.52` (junto com o cadastro de Regiões)
- app: `registry`
- repo: `erp-control-plane` (nada muda no `erp` nem no `pdv-local`)

---

## 1. Objetivo

Botão **🚚 Migrar servidor** na página do cliente que leva o ERP do cliente de um nó do Swarm para outro
(ex.: Anápolis → Hostinger) **sem perder dados**, com integridade comprovada e volta automática ao servidor
antigo enquanto ela for segura.

Decisões:

| Tema | Decisão |
|---|---|
| Volumes migrados | `{slug}_pgdata` (via dump), `{slug}_media` e `{slug}_backups` |
| Status do cliente | Qualquer status pode ser migrado. Status, `data_ativacao` e o estado ligado/parado de `web`/`cron` são preservados |
| Homologação | Swarm de 2 nós na VM, com regiões distintas |
| Release | Uma versão só (Regiões + migração) |

## 2. Por que o fluxo manual atual não é seguro

Fluxo manual de hoje: Novo Backup → trocar Host → Re-provisionar → Restaurar.

| # | Achado | Onde | Consequência |
|---|---|---|---|
| A1 | O SyncAgent trata `401 refresh_token_invalid` como definitivo: grava `RefreshRejectedAtUtc` e passa a exigir **novo código de ativação** | `pdv-local/src/sync-agent/Provisioning/ErpActivationClient.cs` (`ClassifyRefreshFailure`), `ProvisionedAgentCredentials.NeedsReactivation` | O Re-provisionar sobe o web e espera `/health/` com o banco **vazio**: um agente que renovar o token nesse intervalo fica bloqueado. O mesmo vale para voltar ao banco antigo **depois** que o novo atendeu agentes (o ERP rotaciona o refresh token a cada uso). Respostas 404/502/timeout são `Transient` e não causam dano |
| A2 | O serviço `cron` escreve no banco e na mídia, mas backup e restauração só param o `web` | stack (`_gerar_stack_yaml`), `task_restaurar_cliente` | Escritas durante o dump e durante o `psql` |
| A3 | A mídia do backup é copiada do **container web** | `task_backup_cliente` | Com o web parado (congelamento ou cliente suspenso) o backup sai sem mídia |
| A4 | `task_finalizar_restauracao` religa `web=1` sempre | `tasks.py` | Restaurar um cliente suspenso o religa |
| A5 | `MotorProvisionamento.executar()` grava `status='ativo'` e `data_ativacao=hoje` | `provisioning.py` | Re-provisionar altera status e data de clientes suspensos/trial |
| A6 | Sem `.env`, `executar()` gera `POSTGRES_PASSWORD`, `DJANGO_SECRET_KEY` e `EMPRESAS_CREDENCIAL_MASTER_KEY` novos | `provisioning.py` | Credenciais de empresas cifradas no ERP ficam ilegíveis |
| A7 | O placement é por label de região e os volumes são locais do nó | `node.labels.region` | Com 2+ nós na mesma região o Swarm pode subir o banco num nó com volume vazio; a volta só é segura com um nó por região |
| A8 | `docker create -v {slug}_media:...` cria o volume vazio se ele não existir | `_restaurar_midia` | Ler um volume exige checar a existência antes |
| A9 | O backup não tem checksum nem conferência de conteúdo | backup/restauração | Corrupção ou perda parcial passa sem aviso |
| A10 | Timeouts fixos (pg_dump 300 s, psql 300 s, mídia 120/300 s) e dump inteiro em memória | `tasks.py` | Cliente grande falha no meio |
| A11 | O volume `{slug}_backups` não entra no backup | `task_backup_cliente` | Perda dos backups internos do ERP |
| A12 | A imagem em execução pode divergir de `cliente.versao_erp`, que é o que o redeploy usa | `atualizar_versao` (`service update`) | Dump de uma versão restaurado em outra |

## 3. Regras invioláveis

1. **O ERP do destino nunca atende tráfego antes de restaurado e conferido.** O destino sobe com `web=0`,
   `cron=0` e `traefik.enable=false` (A1).
2. **Ponto sem volta = habilitar o Traefik no destino.** Antes dele, qualquer falha leva à volta automática.
   Depois dele, nunca há volta automática: estado `exige_intervencao` e alerta.
3. **Origem intocada.** Nada é apagado no servidor antigo; os volumes antigos ficam registrados para limpeza manual.
4. **Integridade comprovada.** Contagem exata de linhas por tabela, conteúdo de `django_migrations`, número e
   bytes de arquivos de cada volume e SHA-256 do pacote. Origem e destino precisam bater exatamente.
5. **Uma operação por cliente.** Trava no banco (constraint) e bloqueio no admin de backup, restauração,
   re-provisionar, destruir, atualizar versão, aplicar configurações e troca de host enquanto houver migração ativa.
6. **Zero avisos.** Qualquer aviso no backup ou na restauração aborta.
7. **Preservação.** Status do cliente, `data_ativacao` e réplicas originais de `web`/`cron` (A4, A5).
   O status não bloqueia a migração; os gates técnicos sim.

## 4. Máquina de estados

Modelo `MigracaoCliente`. Cada etapa é uma task idempotente que só avança com
`filter(pk=..., status=esperado).update(status=proximo)`; reentrada é no-op. Tarefas que rodam num nó devolvem o
resultado despachando `task_migracao_avancar` na fila `celery` (manager). Comandos do Swarm (`stack deploy`,
`service scale/update`, `node ls/inspect`) só no manager; Postgres e volumes só na fila `no.<hostname>`.

```
verificando → congelando → manifestando → backup → implantando_destino → restaurando
            → conferindo → testando → abrindo ──(ponto sem volta)──> concluida
   │                 (falha em qualquer etapa antes de "abrindo")
   │                              └──> revertendo → revertida | exige_intervencao
   └──> falhou_sem_alteracao                        abrindo (falha) → exige_intervencao
```

| Etapa | Onde | O que faz | Se falhar |
|---|---|---|---|
| Verificações | manager + nós | Gates G1–G12 (seção 5) | `falhou_sem_alteracao` |
| Congelar | manager | Grava réplicas originais; `web=0`, `cron=0`; espera nenhuma task rodando | volta |
| Manifesto da origem | nó origem | Contagem por tabela, `django_migrations`, arquivos/bytes de `media` e `backups` | volta |
| Backup | nó origem | `BackupCliente(origem='migracao')`: dump + volumes lidos **do volume**, SHA-256, zero avisos | volta |
| Implantar destino | manager | Troca o host; `executar(modo_migracao=True)`; confere segredos do `.env` inalterados; `{slug}_db` rodando no nó destino | volta |
| Restaurar | nó destino | Confere SHA-256, `psql`, volumes; zero avisos; **não religa** | volta |
| Conferir | nó destino | Manifesto do destino == manifesto da origem | volta |
| Teste interno | manager + nó destino | `web` com as réplicas originais e Traefik ainda desligado; `/health/` por `docker exec`: 200 e versão = `versao_erp` | volta |
| **Abrir** | manager | Habilita o Traefik; `cron` com as réplicas originais; `/health/` público | `exige_intervencao` |
| Concluir | manager | Registra volumes residuais da origem e `concluida_em` | — |

Cliente com réplicas 0 (suspenso): o teste interno e a abertura conferem só o roteamento; o cliente termina parado.

### Volta (só antes do ponto sem volta)

1. `web=0`, `cron=0` no destino (se existirem).
2. Devolve o host original e executa `executar(modo_migracao=True)` na região de origem.
3. Confere `{slug}_db` de volta no nó de origem e o manifesto == manifesto da origem (prova de que os dados antigos
   estão intactos).
4. Restaura réplicas e Traefik; `/health/`.
5. Registra os volumes criados no destino em `volumes_residuais`.

Falha na volta → `exige_intervencao`.

### Vigia

`task_vigiar_migracoes` (django-celery-beat, a cada 1 min, criado por data migration). Etapa sem atividade além do
limite → antes do ponto sem volta dispara a volta; depois, `exige_intervencao`.

## 5. Gates (etapa Verificações)

Todos são gravados com evidência em `MigracaoCliente.verificacoes`.

| Gate | Condição |
|---|---|
| G1 | Nenhuma migração, backup ou restauração ativa no cliente (trava adquirida) |
| G2 | Host de destino ativo, região ativa e diferente da origem |
| G3 | Exatamente 1 nó `Ready`/`active`/x86_64 com o label da região de destino |
| G4 | Exatamente 1 nó com o label da região de origem, e é o nó onde `{slug}_db` roda |
| G5 | `agente-no` rodando no nó de destino **e** uma task de ping na fila do destino responde no prazo |
| G6 | Armazenamento de backups com teste de conexão OK |
| G7 | Imagem de `{slug}_web` == `cliente.versao_erp` |
| G8 | `.env` presente com `POSTGRES_PASSWORD`, `DJANGO_SECRET_KEY` e `EMPRESAS_CREDENCIAL_MASTER_KEY` |
| G9 | Nó de destino **sem** volumes `{slug}_pgdata`, `{slug}_media`, `{slug}_backups` |
| G10 | Espaço livre no destino ≥ 3 × (banco + volumes), tamanhos medidos na origem |
| G11 | Tamanhos dentro dos limites suportados pelos timeouts da migração |
| G12 | Vigia agendado e ativo |

## 6. Correções nos fluxos existentes

Necessárias para a migração e aproveitadas pelo backup/restauração comuns:

- Mídia e `backups` lidos **do volume** por container auxiliar, checando a existência antes (A3, A8, A11).
- Restauração comum para e religa também o `cron` e devolve as réplicas originais (A2, A4).
- SHA-256 gravado no backup e conferido no download (A9).
- `executar(modo_migracao=...)`: réplicas 0, Traefik desligado, preserva status/data e exige o `.env` (A5, A6).

## 7. Pré-requisitos de um servidor novo

- Entrar no Swarm; arquitetura **x86_64**.
- Label exclusivo da região: `docker node update --label-add region=<codigo> <node-id>` (a tela da Região mostra o comando).
- `agente-no` rodando nele (`docker service ps <stack>_agente-no`).
- **MTU do overlay** compatível quando o nó está atrás de WireGuard (VXLAN sobre WireGuard exige MTU menor;
  sem isso, respostas grandes travam mesmo com `/health/` respondendo).
- Host cadastrado no CP com a região correspondente.

## 8. Runbook

### `exige_intervencao`

O cliente pode estar no destino já aberto. **Não reverta para o banco antigo** sem decidir conscientemente: lojas
cujo agente renovou o token no destino precisarão de novo código de ativação.

1. Ler o log e `verificacoes` da migração.
2. `docker service ps {slug}_web {slug}_cron {slug}_db` no manager.
3. Corrigir no destino (roteamento, MTU, serviço) sempre que possível.

### Volumes residuais

Após `concluida` (na origem) ou `revertida` (no destino), os volumes ficam no nó. Conferir e remover no nó
indicado em `volumes_residuais`:

```bash
docker volume ls --filter name=^{slug}_
docker volume rm {slug}_pgdata {slug}_media {slug}_backups
```

Uma nova migração para um nó com volumes residuais é recusada (G9).

## 9. Homologação

Swarm de 2 nós na VM (`docs/development-structure-and-vm-runbook.md` do `pdv-local`), regiões `homolog-a` e
`homolog-b`, cliente de teste com dados, mídia e SyncAgent/PDV ativado.

| Cenário | Esperado |
|---|---|
| Ida A → B | Manifestos iguais, login, imagens, PDV sincroniza **sem reativação** |
| Volta forçada (agente do destino parado na restauração) | Cliente de volta em A, manifesto igual, PDV ok |
| Falha simulada depois da abertura | `exige_intervencao`, sem volta |
| Cliente suspenso | Migra e termina parado e suspenso |
| B → A em seguida | Recusada até remover os volumes residuais (G9); depois conclui |
