# Release: salvar o Cliente com historico grande, sincronizacao de versoes so com novidade e andamento da atualizacao ao vivo

- data: 2026-10-30
- responsavel: James Flavio
- status: implementado; suite `registry` (sem playwright) verde — 379 testes
- versao CP: `0.0.53`
- app: `registry`

---

## 1. Problema
Salvar um Cliente no admin dava `TooManyFieldsSent` (`DATA_UPLOAD_MAX_NUMBER_FIELDS`, padrao 1000 do Django). Cada linha dos historicos da pagina (Logs de provisionamento, Atualizacoes, Sincronizacoes de Versoes) manda `id` + FK ocultos no POST, mesmo somente leitura. O historico de Sincronizacoes crescia sem parar porque todo `save()` de `VersaoAgente` reenviava o mesmo pacote a todos os clientes em modo automatico.

## 2. O que mudou
- `registry/admin.py`: `_UltimosRegistrosFormSet` — Sincronizacoes e Atualizacoes de Versao mostram so os 30 mais recentes. O POST fica com tamanho fixo. (Na 0.0.53 o limite e o rotulo "(ultimos 30)" tambem foram para os Logs de provisionamento; removidos na 0.0.54 — `MotorProvisionamento._log` usa `get_or_create` por etapa, entao sao no maximo 4 linhas.)
- `registry/cp_push.py`: `SincronizadorVersoes.ha_novidade()` compara o pacote com o ultimo envio `concluida` (versao nova, aposentada/removida ou metadado corrigido contam como novidade). `task_sincronizar_versoes_agente` ignora o push automatico sem novidade; o botao manual sempre envia.
- Retencao: `cp_push.podar_sincronizacoes` mantem 30 `SincronizacaoVersoesAgente` por cliente a cada envio. Migration `0031_poda_sincronizacoes_versoes` poda o acumulado.
- Logs de provisionamento **nao** sao podados: nao acumulam (uma linha por etapa) e o Painel de acesso le a senha inicial da etapa `criar_superuser`.
- Verificar Atualizacoes: o bloco Andamento mostra inicio/termino (data, hora, duracao), o log com a hora local de cada linha e se atualiza sozinho (`andamento/`, a cada 3 s) enquanto a atualizacao roda.

## 3. Deploy
Script `.claude/skills/cp-release-docker/scripts/release_docker.ps1` (imagem `emporioautomacao/ararasuite-cp:0.0.53` e `:latest`). Atualizar o CP pelo painel (Configuracoes > Verificar Atualizacoes). Migration `0031` roda no boot (poda o historico de sincronizacoes).

## 4. Depois do deploy
Nenhuma acao por cliente.
