# Release: provisionamento normal volta a concluir (NameError em cp_shared_secret)

- data: 2026-10-30
- responsavel: James Flavio
- status: implementado; suite `registry` verde (414 testes)
- versao CP: `0.0.56`
- app: `registry`

---

## 1. Problema
Desde a `0.0.52` (refatoracao que criou `MotorProvisionamento.montar_env` para a migracao de servidor), `MotorProvisionamento.executar()` usava a variavel `cp_shared_secret` ao gravar `Cliente.integracao_secret`, mas ela so existia dentro de `montar_env()`. Todo provisionamento **normal** (cliente novo e Re-provisionar) terminava com `NameError` depois do `docker stack deploy`: o stack subia, mas o cliente nao ia para `ativo`, nao recebia `data_ativacao` nem `integracao_secret`, e o Celery retentava ate `erro_provisao`. O modo migracao nao era afetado (retorna antes desse trecho).

## 2. O que mudou
- `registry/provisioning.py`: `cp_shared_secret = env_vars['CP_SHARED_SECRET']`, lido do mesmo `.env` que acabou de ser gravado.
- Teste `test_provisionamento_normal_deixa_o_cliente_ativo` (`registry/tests/test_migracao.py`): provisionamento normal deixa o cliente `ativo`, com `data_ativacao` e `integracao_secret` igual ao `CP_SHARED_SECRET` do `.env`.

## 3. Deploy
Script `.claude/skills/cp-release-docker/scripts/release_docker.ps1` (imagem `emporioautomacao/ararasuite-cp:0.0.56` e `:latest`). Atualizar o CP pelo painel (Configuracoes > Verificar Atualizacoes). Sem migration.

## 4. Depois do deploy
Clientes provisionados entre a `0.0.52` e a `0.0.55` que ficaram em `erro_provisao` (ou presos em `provisionando`) com o stack de pe: **Re-provisionar** reaproveita as senhas do `.env` existente (o volume `pgdata` continua valido) e conclui a ativacao. Depois, **Aplicar Configuracoes** para garantir o segredo de integracao no ERP.
