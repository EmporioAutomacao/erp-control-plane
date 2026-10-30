# Release: User-Agent do CP em todo acesso publico aos ERPs e limpeza de residuos de migracao em intervencao

- data: 2026-10-30
- responsavel: James Flavio
- status: implementado; suite `registry` verde (420 testes)
- versao CP: `0.0.57`
- app: `registry`

---

## 1. Problema
A migracao do `demo` (anapolis → hostinger) voltou para a origem e religou o ERP, mas terminou em **Exige intervencao**: o ultimo passo (`migracao.health_publico`) consultava `https://<subdominio>/health/` com o User-Agent padrao do urllib, que o Cloudflare responde com 403. A espera durou 300 s e virou intervencao. O mesmo passo roda em `abrir()` depois do ponto sem volta, entao uma migracao bem-sucedida tambem terminaria em intervencao. O `_aguardar_http` do provisionamento e o fallback `/health/` do `cp_pull` tinham o mesmo problema (a verificacao de saude ja tinha sido corrigida na `0.0.55`).

Em `exige_intervencao` o CP nao grava `volumes_residuais`, entao o botao 🧹 Remover agora nao aparecia e os volumes do destino ficavam sem limpeza pelo painel.

## 2. O que mudou
- `registry/http_publico.py`: `abrir(url, timeout)` com User-Agent `AraraSuite-CP/<versao> (health-check)`. Usado por `migracao.health_publico`, `MotorProvisionamento._aguardar_http`, `ColetorVersoes._tentar_health` e `task_verificar_saude_cliente`.
- `migracao.residuais_da_migracao`: os volumes registrados ou, numa migracao em `exige_intervencao` com `host_trocado` e sem `ponto_sem_volta_em` (destino nunca exposto), os do destino (presumidos). O painel da migracao mostra o botao com uma nota; `limpar_residuais` exige, nesse caso, o `{slug}_db` rodando no no de origem e grava os volumes em `volumes_residuais` antes de despachar a task do no.
- Testes em `registry/tests/test_migracao.py` (residuos presumidos, travas e User-Agent da migracao e do provisionamento).
- Docs: `CLAUDE.md`, `docs/registry/migracao-cliente-entre-servidores.md` (secao 6).

## 3. Deploy
Script `.claude/skills/cp-release-docker/scripts/release_docker.ps1` (imagem `emporioautomacao/ararasuite-cp:0.0.57` e `:latest`). Atualizar o CP pelo painel (Configuracoes > Verificar Atualizacoes). Sem migration. O `agente-no` dos workers tambem e atualizado (a limpeza roda nele).

## 4. Depois do deploy
- `demo`: abrir a migracao (status Exige intervencao), **🧹 Remover agora** (volumes em `ServidorUbuntu8vCpu32gb400gb`) e migrar de novo.
