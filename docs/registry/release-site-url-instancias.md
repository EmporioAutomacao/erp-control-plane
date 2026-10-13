# Release: SITE_URL nas instâncias do ERP

- data: 2026-10-12
- responsavel: James Flavio
- status: implementado — testes de provisionamento OK; "Aplicar Configurações" conferido no diff (suíte com banco não rodada nesta sessão)
- versao CP: `0.0.47`
- app: `registry`
- depende do ERP: `0.0.134` (criativos de campanha, modo link, imagens do HTML no e-mail)

---

## 1. Objetivo
O ERP passou a montar links públicos sem request (criativo de campanha no WhatsApp, "abra no navegador", imagens do HTML no e-mail). Para isso precisa de `SITE_URL` absoluto, que nenhuma instância recebia do CP.

## 2. O que mudou
- `registry/provisioning.py::site_url_cliente(cliente)` → `https://<subdominio>` (sempre o subdomínio; motivo em `docs/registry/site-url-instancias.md`).
- `MotorProvisionamento.provisionar`: `SITE_URL` no `.env` e no `environment` do stack.
- `ClienteAdmin._view_aplicar_modulos` ("Aplicar Configurações"): grava `SITE_URL` no `.env` e aplica com `docker service update --env-add`.
- Testes: `registry/tests/test_provisioning.py` (`test_site_url_usa_sempre_o_subdominio`, `test_stack_propaga_site_url`).

## 3. Deploy
```bash
docker build --pull -t emporioautomacao/ararasuite-cp:0.0.47 -t emporioautomacao/ararasuite-cp:latest .
docker push emporioautomacao/ararasuite-cp:0.0.47
docker push emporioautomacao/ararasuite-cp:latest
```
Atualizar o CP pelo painel (Configurações > Verificar Atualizações) ou redeploy do stack com `CP_VERSION=0.0.47`. Sem migration.

## 4. Depois do deploy
Clientes já provisionados: abrir cada Cliente e clicar em **"Aplicar Configurações"** para injetar `SITE_URL` (o serviço web reinicia, ~30 s).
