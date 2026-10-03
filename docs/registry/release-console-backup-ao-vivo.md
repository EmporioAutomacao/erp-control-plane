# Release: Console ao vivo no Backup/Restauração + página do Cliente em abas

- data: 2026-10-03
- responsavel: James Flavio
- status: implementado e validado localmente (backup, restauração e worker parado, com RustFS) — pendente de deploy
- versao CP: `0.0.45`
- app: `registry`
- repo: `erp-control-plane` (nada muda no `erp`)

---

## 1. Objetivo

Ao clicar em **💾 Novo Backup** só aparecia uma barra de porcentagem: não dava para saber **o que** estava rodando nem se
o processo **travou** (o `pg_dump`, o `docker cp` e o upload são comandos longos e silenciosos; e, com o `agente-no`
parado, a tarefa ficava na fila sem sinal algum). Agora há um **console em tempo real** na tela do cliente.
Junto, a tela de edição do cliente (11 seções + 3 históricos numa página só) foi dividida em **abas**.

## 2. Console ao vivo

Abre sozinho na aba **Backups** ao clicar em Novo Backup / Restaurar (`?acompanhar=<id>[&op=restauracao]`) e sempre que há
uma operação em andamento. Mostra o log da operação, o tempo decorrido e o estado:

| Estado | Quando |
|---|---|
| ● Executando (verde, pulsando) | houve atividade nos últimos 30 s |
| ⚠ Sem atividade há N s — possível travamento | 30 s ou mais sem atividade |
| ✖ Sem atividade há mm:ss — provavelmente travado + botão **Interromper** | 120 s ou mais |
| ✔ Concluído / ✖ Erro | operação terminou (a tabela de backups se atualiza sozinha, sem recarregar) |

Botões: **Copiar** o log, **Pausar rolagem**, **Interromper** (só aparece quando travado), **Atualizar lista**.
Cada backup antigo da tabela ganha **📜 Log** (log do backup e da última restauração), para diagnosticar falhas passadas.

Avisos que explicam o travamento: *"Nenhuma tarefa começou ainda: nenhum worker pegou o pedido"* (conferir o serviço
`celery`) e *"Nenhum worker do nó iniciou a tarefa"* (conferir o `agente-no` do nó).

### O que aparece no log (exemplo real)

```
[16:00:28] Backup solicitado. Verificando a configuração do armazenamento…
[16:00:29] Tarefa enviada à fila "celery". Aguardando um worker do nó iniciar…
[16:00:29] Worker "NBJamesFlavio" iniciou o backup.
[16:00:29] [1/4] Banco de dados: procurando o container desenvolvimento_db…
[16:00:33] pg_dump concluído: 106.4 MB em 3s. Compactando db.sql…
[16:00:38] db.sql compactado em 5s.
[16:00:38] [2/4] Mídia: procurando o container desenvolvimento_web…
[16:00:39] Mídia copiada: 17.2 MB. Compactando…
[16:00:40] [3/4] Configuração do cliente (.env e stack.yml)…
[16:00:40] [4/4] Enviando ao armazenamento (bucket "cp-backups")…
[16:00:41] CONCLUÍDO: 45.2 MB em 12s.
[16:00:41] Retenção: 1 backup(s) antigo(s) removido(s).
```

Comandos longos registram *"<comando> em execução há 12s · 34 MB…"* a cada ~6 s (upload/download mostram % e bytes).
A restauração tem o próprio log (`[1/4] Baixando…` → `psql em execução…` → `Religando o serviço …_web`).

## 3. Como funciona

- **`BackupCliente`** (migration `0022`): `log`, `log_restauracao`, `atividade_em` (heartbeat) e `restauracao_iniciada_em`.
- **`registry/log_operacao.py`**: `LogOperacao.linha()` anexa `[HH:MM:SS] texto` e renova o heartbeat com `.update()`
  (regra do projeto dentro de tasks); `rodar_com_pulso()` executa o comando bloqueante numa **thread** enquanto a thread
  principal registra pulsos — todo acesso ao banco fica na thread principal.
- **`registry/console_backup.py`**: HTML + JS do console (polling a cada 1,5 s de `…/backups/<id>/status/`).
- **API de status** estendida (campos antigos mantidos): `log`, `operacao`, `ativo_operacao`, `decorrido_s`,
  `sem_atividade_s`, `aguardando_worker`, `sem_inicio`.
- **Interromper** (`POST …/backups/<id>/interromper/`): só com ≥ 120 s sem atividade. Marca o backup como erro; numa
  restauração também **religa o web** do cliente (`docker service scale …_web=1`). É a saída para worker morto/tarefa perdida,
  que antes deixava o registro "Em andamento" para sempre.
- Callbacks de progresso opcionais em `storage_backup.enviar/baixar` (`progresso=`, `cfg=`).

### Correção de desempenho achada na validação

O console revelou que o backup parecia "congelar" por 40–110 s depois do `pg_dump`: o dump (~100 MB) era compactado em
gzip **nível 9** dentro do `tar.addfile`, na thread principal e sem pulsos — e o "pg_dump concluído em Ns" somava essa
compactação. Agora a compactação roda com pulso e o gzip usa **nível 6**. No teste local o backup de ~106 MB caiu de
**~100 s para ~12 s**, e o log informa os tempos de cada etapa corretamente.

## 4. Página do Cliente em abas

Clientes → Cliente usa as abas nativas do Unfold (`classes: ['tab']`): **Cadastro** · **Acesso e Infraestrutura** ·
**Plano e Faturamento** · **Versões** · **Status e Ações** · **Backups**, e os históricos (**Logs de Provisionamento**,
**Atualizações de Versão**, **Sincronizações de Versões**) como abas no topo (`tab = True` nos inlines).
Todos os campos continuam no mesmo formulário; `registry/static/registry/abas_cliente.js` lembra a aba aberta (por
navegador) e abre **Backups** automaticamente ao acompanhar uma operação.

## 5. Validação

| Cenário | Resultado |
|---|---|
| Backup real pela tela (RustFS) | console abre sozinho, linhas chegam ao vivo, pulsos do `pg_dump`/`psql`, ✔ Concluído, tabela atualiza sem recarregar |
| Restauração real pela tela | console "Restauração" com as etapas; `django_migrations` 460 → 465 |
| Worker parado | ⚠ aos 30 s, ✖ + **Interromper** aos 120 s; ao clicar → Erro e a tabela mostra "Interrompida manualmente" |
| Testes | `manage.py test registry` — todos OK (novos: `test_console_backup.py`, Playwright de abas/console/travamento) |

## 6. Deploy

Imagem nova + redeploy do stack do CP (a migration `0022` roda no `celery`). Nenhuma variável nova. Os `agente-no` também
precisam da imagem nova para gravar o log das etapas que rodam no nó. Backups feitos antes desta versão não têm log
(o botão 📜 Log não aparece para eles).

## 7. Limites conhecidos

- O `pg_dump` não informa progresso: o console mostra tempo decorrido e "em execução" (não porcentagem).
- O dump continua em memória antes de compactar (limite já conhecido; streaming em disco ficou fora do escopo).
- "Interromper" não mata o processo no nó (o `pg_dump` pode terminar sozinho): apenas libera o registro e, na restauração, religa o web.
