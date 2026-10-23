# Relay do callback OAuth do Arpa (`/integracoes/arpa/oauth/callback/`)

- criado: 2026-10-23
- codigo: `registry/arpa_oauth.py`, rota em `core/urls.py`, template `registry/templates/registry/arpa_oauth_erro.html`, testes `registry/tests/test_arpa_oauth.py`
- plano completo (lado ERP): `docs/modulos/arpa/` no repo `erp` (`escopo-v1.md`, decisao D5)

## Por que existe
O ERP vai enviar ao Arpa Control os pedidos do e-commerce pela API de Integracao da Arpa (OAuth2 *authorization code*). O portal do desenvolvedor do Arpa aceita **um unico** `redirect_uri` por aplicativo, e ele precisa ser identico em toda autorizacao. Como cada cliente tem o ERP num endereco proprio, o `redirect_uri` cadastrado no app da Arara Suite aponta para o CP:

```
https://ararasuite.com.br/integracoes/arpa/oauth/callback/
```

## Fluxo
```
ERP do cliente  --(Conectar ao Arpa)-->  Arpa /auth2/oauth/authorize?...&state=<slug>.<nonce>&redirect_uri=<URL acima>
Arpa            --302-->  CP  /integracoes/arpa/oauth/callback/?code=...&state=<slug>.<nonce>
CP              --302-->  https://<subdominio do cliente>/arpa/oauth/retorno/?code=...&state=...
ERP do cliente  confere o nonce da sessao e troca o code no Arpa (/oauth/token, com o mesmo redirect_uri)
```

O CP so repassa: nao conhece `client_secret`, nao troca o `code`, nao guarda tokens e nao grava o `code` em log.

## Regras
| Situacao | Resposta |
|---|---|
| `state` = `<slug>.<nonce>` valido, cliente com status `ativo`, `trial` ou `atualizando`, com `code` | 302 para `site_url_cliente(cliente)` + `/arpa/oauth/retorno/` |
| Usuario cancelou no Arpa (`error`/`error_description` + `state`) | 302 repassando `error`, `error_description` e `state` (o ERP mostra a mensagem) |
| `state` fora do formato (slug `[-a-zA-Z0-9_]{1,50}`, ponto, nonce `[A-Za-z0-9_-]{8,128}`) | 400, pagina de erro, sem redirecionar |
| Sem `code` e sem `error` | 400 |
| Algum parametro repassado com mais de 2048 caracteres | 400 |
| Slug inexistente ou cliente em outro status (suspenso, cancelado, trial expirado, provisionando...) | 404, pagina de erro |
| Metodo diferente de GET | 405 |

- O destino e sempre `https://<subdominio>` (`site_url_cliente`), nunca o dominio proprio nem uma URL vinda da querystring (sem open redirect). Parametros desconhecidos sao descartados.
- Respostas com `Cache-Control: no-store` e `Referrer-Policy: no-referrer`.

## Dependencias
- Lado ERP (repo `erp`, app `arpa_pedidos`): rota `GET /arpa/oauth/retorno/` — **ainda nao implementada**. Ate existir, o repasse chega ao ERP e da 404 la.
- Se a Arpa responder que o `state` so aceita numeros (pergunta P1 em `docs/modulos/arpa/perguntas-arpa.md` do `erp`), trocar o formato por um mapa `nonce numerico → cliente` com validade curta guardado no CP.
- A rota so fica no ar depois de publicar uma versao do CP com este codigo.
