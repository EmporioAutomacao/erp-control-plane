# Release: `agente-no` sem pasta do host (worker novo não exige preparação)

- data: 2026-10-19
- responsavel: James Flavio
- status: implementado e validado localmente (restauração real com Docker) — pendente de deploy
- versao CP: `0.0.49`
- app: `registry`
- repo: `erp-control-plane` (nada muda no `erp`)

---

## 1. Problema

O `agente-no` (serviço global, um por worker) montava `/opt/backups/cp` do host. O Swarm **não cria a origem de um bind
mount**: se a pasta não existisse no nó, a tarefa do agente não subia (`agente-no` `0/1` naquele nó). Todo worker novo exigia um
`mkdir -p /opt/backups/cp` manual.

A pasta tinha de ser bind com o **mesmo caminho** no host e no container por um único motivo: a restauração da mídia rodava
`docker run -v <pasta-no-host>:/restore/media`, e quem interpreta esse caminho é o Docker do host.

## 2. O que mudou

- **`registry/tasks.py::_restaurar_midia`** (novo): em vez do bind, usa um container auxiliar só para montar o volume
  `{slug}_media`:
  1. `docker run --rm -v {slug}_media:/app/media alpine find /app/media -mindepth 1 -delete` (limpa, inclusive arquivos ocultos);
  2. `docker create -v {slug}_media:/app/media alpine true`;
  3. `docker cp <pasta-local>/. <id>:/app/media/` (lê da pasta do próprio agente, sem depender de caminho do host);
  4. `docker rm -f <id>` (sempre, mesmo se o `docker cp` falhar).
- **`stack-prod.yml` → `agente-no`**: removido o bind de `/opt/backups/cp`; `CP_BACKUP_DIR: "/tmp/cp-backups"` (temporário dentro
  do container). `web` e `celery` (manager) mantêm o bind.
- O backup em si já não dependia de pasta do host (`docker cp` e `pg_dump` gravam no container do agente).

## 3. Validação (Docker Desktop real)

| Verificação | Resultado |
|---|---|
| `_restaurar_midia` num volume com conteúdo antigo | o antigo sumiu (inclusive arquivo oculto); entraram os novos, com arquivo oculto, subpastas e nome com espaço |
| Auxiliar removido | nenhum container sobrando |
| Ciclo completo com temporário fora de qualquer bind (backup real → estrago no volume → restauração real) | volume ficou só com o conteúdo do backup; pasta temporária do agente terminou vazia; log da restauração com todas as etapas |
| Testes | `manage.py test registry` — todos OK (novos: sem `-v` de pasta do host; sequência run→create→cp→rm; `rm` mesmo com falha do `cp`; falha ao limpar não copia) |

## 4. Deploy

Imagem nova + atualizar o stack do CP. Em **workers que já têm a pasta** nada precisa ser removido (fica sem uso). Em workers novos
não é preciso criar nada.

## 5. Limites

- O temporário agora ocupa o disco do container (`/var/lib/docker` do nó, o mesmo disco de antes) e some quando o container é
  recriado. Dump de ~100 MB não muda nada; para backups de vários GB conferir o espaço do nó.
- `docker cp` para dentro do volume grava os arquivos como `root` (como a restauração anterior).
