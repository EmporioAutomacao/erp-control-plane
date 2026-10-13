# SITE_URL das instâncias do ERP

Data: 12/10/2026

## Por quê
O ERP precisa da sua URL pública em tudo que monta **sem uma requisição em mãos** (cron, fila, envio para fora):
- link do criativo de campanha no WhatsApp (modo link) e página "abra no navegador" do e-mail;
- imagens usadas dentro do HTML do e-mail (precisam de URL absoluta);
- link de preview dos criativos, QR do portal de O.S., PDFs gerados por cron.

Sem `SITE_URL`, esses links saem relativos (`/m/p/...`) e não abrem fora do ERP. Ver no repo `erp`: `docs/modulos/marketing/campanha-criativos-midia-v1.md`.

## O que o CP faz
- `registry/provisioning.py::site_url_cliente(cliente)` → `https://<subdominio>`.
- **Provisionamento** (`MotorProvisionamento`): grava `SITE_URL` no `.env` e no `environment` do stack.
- **"Aplicar Configurações"** (`ClienteAdmin._view_aplicar_modulos`): atualiza `SITE_URL` no `.env` e via `docker service update --env-add` (o serviço reinicia, ~30 s).

## Decisão: subdomínio, não domínio próprio
O subdomínio (`<slug>.ararasuite.com.br`) responde desde o provisionamento. O domínio próprio só funciona depois que o cliente troca os nameservers no Cloudflare — se `SITE_URL` usasse o domínio próprio, links já enviados para clientes finais ficariam quebrados até o DNS propagar. O ERP continua aceitando os dois hosts (`ALLOWED_HOSTS`).

## Clientes já provisionados
Não recebem `SITE_URL` sozinhos: abrir o Cliente no CP e clicar em **"Aplicar Configurações"** depois do deploy desta versão do CP.

## Testes
`registry/tests/test_provisioning.py`: `test_site_url_usa_sempre_o_subdominio`, `test_stack_propaga_site_url`.
