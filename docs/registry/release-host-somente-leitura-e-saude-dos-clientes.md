# Release: botao Migrar servidor de volta, Host somente leitura e verificacao de saude confiavel

- data: 2026-10-30
- responsavel: James Flavio
- status: implementado; suite `registry` verde (o Playwright `test_pagina_do_cliente_e_dividida_em_abas` falhou uma vez na suite completa por instabilidade e passa sozinho)
- versao CP: `0.0.55`
- app: `registry`

---

## 1. Problemas
- Depois da migracao revertida do `demo`, a pagina do Cliente parou de mostrar o botao **🚚 Migrar servidor**. Causa: o historico de migracoes usava `{:%d/%m/%Y %H:%M}` no `format_html`, que converte os argumentos em `SafeString`; o formato levanta `ValueError`, o admin engole o erro de campo readonly e mostra "-" no lugar do campo inteiro. Afetava todo cliente com ao menos uma migracao no historico. O mesmo erro estava na data do ponto sem volta do painel da migracao.
- O campo **Host** do Cliente ficava editavel depois da implantacao. Troca-lo a mao e re-provisionar subiria o banco vazio no no da regiao nova, e a migracao partiria da regiao errada.
- Verificacoes de saude: o Cloudflare respondia 403 ao User-Agent padrao do `urllib`, entao todo cliente aparecia offline.

## 2. O que mudou
- `registry/admin.py`: datas formatadas antes do `format_html` (`timezone.localtime(...).strftime`, agora no horario local) em `format_html_join_migracoes` e no painel da migracao.
- `_host_editavel`: Host editavel so no cadastro novo, em cliente nunca ativado (`aguardando_provisao`/`erro_provisao` sem `data_ativacao`) ou legado sem host. Nos demais casos fica somente leitura, com aviso apontando para o 🚚 Migrar servidor (a migracao troca o host sozinha).
- Saude dos clientes: User-Agent proprio (`AraraSuite-CP/<versao> (health-check)`), codigo HTTP real em respostas de erro (502/503/500) e latencia vazia quando nao houve resposta; agendamento passa de 10 para 15 min (migration `0033_saude_a_cada_15_min`, so altera a tarefa se ela ainda estiver no intervalo da `0030`); retencao de 30 dias (`DIAS_RETENCAO_SAUDE`); admin de Verificacoes de Saude somente leitura com botao Verificar agora.
- Testes: regressao do botao sumido, data do ponto sem volta, regras do Host e `registry/tests/test_saude.py`.
- Docs: `CLAUDE.md`, Ajuda do admin, `docs/registry/migracao-cliente-entre-servidores.md`, `docs/registry/visao-geral.md`.

## 3. Deploy
Script `.claude/skills/cp-release-docker/scripts/release_docker.ps1` (imagem `emporioautomacao/ararasuite-cp:0.0.55` e `:latest`). Atualizar o CP pelo painel (Configuracoes > Verificar Atualizacoes). Migration `0033` roda no boot.

## 4. Depois do deploy
- `demo`: na pagina da migracao revertida, **🧹 Remover agora** (volumes em `ServidorUbuntu8vCpu32gb400gb`); depois **🚚 Migrar servidor** de novo.
