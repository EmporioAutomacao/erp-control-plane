# Release: migracao de servidor cria os volumes de arquivos no destino, log ao vivo sem recarregar e limpeza de residuos pelo painel

- data: 2026-10-30
- responsavel: James Flavio
- status: implementado; suite `registry` (sem playwright) verde
- versao CP: `0.0.54`
- app: `registry`

---

## 1. Problema
Migracao do `demo` (anapolis → hostinger) voltou para a origem com *"Restauracao do volume media falhou: volume demo_media não existe neste nó"* (idem `demo_backups`). O destino sobe com web/cron em 0 replicas, e o Docker so cria um volume local quando um container que o monta inicia no no — la so o `demo_db` iniciou. A restauracao recusa volume ausente (protecao contra mascarar perda de dados). A origem conferiu intacta com o manifesto.

Alem disso, a tela da migracao fazia `location.reload()` a cada 5 s, e os volumes residuais tinham de ser removidos a mao no no.

## 2. O que mudou
- `registry/volumes.py`: `restaurar_pasta_no_volume(..., criar_se_ausente=False)` e `criar_volume()` (rotulo `com.docker.stack.namespace=<slug>`). `task_restaurar_no` passa `criar_se_ausente=True` so com `migracao_id`; a restauracao comum continua recusando volume ausente.
- Tela da migracao: polling de `…/migracaocliente/<pk>/andamento/` (JSON com `log` cru e HTML de `cabecalho`/`verificacoes`/`acoes`) a cada 3 s, trocando so a parte que mudou. O log so rola ate o fim se ja estava no fim.
- Botao **🧹 Remover agora** nos volumes residuais (so superusuario, migracao terminada) → `migracao.limpar_residuais` → `task_migracao_limpar_residuais` na fila do no dos residuos. Travas: operacao ativa no cliente, `{slug}_db` rodando no no dos residuos ou local desconhecido, task no no errado, container do stack rodando no no. `volumes.remover_volume` remove as tarefas paradas que referenciam o volume e recusa se algum container em execucao o usa. O que nao sair fica em `volumes_residuais`. O gate G9 aponta para o botao.
- Logs de provisionamento voltam a aparecer sem limite e sem o rotulo "(ultimos 30)" (uma linha por etapa; o limite segue nas Sincronizacoes e Atualizacoes).
- Hosts de infraestrutura: CPUs, memoria e disco deixam de ser digitados e vem da verificacao de status (migration `0032_hosts_recursos_automaticos` remove `cpu_total`/`ram_gb_total`).
- Docs: `docs/registry/migracao-cliente-entre-servidores.md` (secao 6), Ajuda do admin, `CLAUDE.md`.

## 3. Deploy
Script `.claude/skills/cp-release-docker/scripts/release_docker.ps1` (imagem `emporioautomacao/ararasuite-cp:0.0.54` e `:latest`). Atualizar o CP pelo painel (Configuracoes > Verificar Atualizacoes). Migration `0032` roda no boot. O `agente-no` dos workers tambem e atualizado (a restauracao e a limpeza rodam nele).

## 4. Depois do deploy
- `demo`: na pagina da migracao revertida, **🧹 Remover agora** (volumes em `ServidorUbuntu8vCpu32gb400gb`) e migrar de novo.
