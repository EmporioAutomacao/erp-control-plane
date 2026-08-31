# Domínios Custom — Suporte a www

## Problema

Ao configurar `neodistribuidora.com.br` como domínio custom de um cliente, apenas
o apex (`neodistribuidora.com.br`) funcionava. Acessar `www.neodistribuidora.com.br`
retornava erro porque nenhuma das três camadas (DNS, Tunnel, Traefik) estava
configurada para o subdomínio `www`.

## Solução

Toda vez que um domínio custom é configurado, o sistema agora **automaticamente**:

1. Cria CNAME `www` no Cloudflare DNS → tunnel
2. Adiciona `www.{dominio}` como ingress rule no Cloudflare Tunnel
3. Cria router Traefik para `www.{dominio}` com middleware de redirect 301 → apex

O Django **nunca** recebe requests com `Host: www.{dominio}` — o redirect acontece
no Traefik antes de chegar à aplicação.

---

## Fluxo completo

```
Browser: https://www.neodistribuidora.com.br
    │
    ▼ (CNAME www → {tunnel_id}.cfargotunnel.com)
Cloudflare CDN
    │
    ▼ (ingress rule: www.neodistribuidora.com.br → http://traefik_traefik:80)
Cloudflare Tunnel
    │
    ▼ (router {slug}-erp-custom-www matches Host(`www.neodistribuidora.com.br`))
Traefik
    │
    ▼ middleware {slug}-www-redirect: redirectregex 301
    │   regex:       ^https?://www\.(.+)
    │   replacement: https://${1}
    ▼
Browser recebe: 301 → https://neodistribuidora.com.br
    │
    ▼ (segue normalmente pelo router apex {slug}-erp-custom)
Django ERP
```

---

## Arquivos modificados

### `registry/provisioning.py` — `_gerar_stack_yaml()`

Quando o stack é gerado (provisionamento inicial ou re-provisionamento), o bloco
`labels_custom` agora inclui além do router apex:

```yaml
# Router www → redirect 301 para apex
- "traefik.http.routers.{slug}-erp-custom-www.rule=Host(`www.{dominio}`)"
- "traefik.http.routers.{slug}-erp-custom-www.entrypoints=web"
- "traefik.http.routers.{slug}-erp-custom-www.middlewares={slug}-www-redirect"
- "traefik.http.middlewares.{slug}-www-redirect.redirectregex.regex=^https?://www\\.(.+)"
- "traefik.http.middlewares.{slug}-www-redirect.redirectregex.replacement=https://$${1}"
- "traefik.http.middlewares.{slug}-www-redirect.redirectregex.permanent=true"
```

> **Escaping no stack YAML:** `\\.` no YAML double-quoted → `\.` para Traefik (dot literal).
> `$${1}` → Docker interpola `$$` como `$` → Traefik recebe `${1}` (backreference Go regexp).

### `registry/admin.py` — `_view_aplicar_modulos()`

`docker service update --label-add` agora também aplica os labels www.
Não passa por interpolação Docker, então usa `${1}` direto.

### `registry/admin.py` — `_configurar_dominio_cloudflare()`

**Tunnel ingress:** ao atualizar as regras do tunnel, filtra `dominio` *e* `www.{dominio}`
dos existentes para evitar duplicatas, depois insere ambos.

**CNAME www:** após criar/atualizar o CNAME apex (`@`), também cria/atualiza o CNAME
`www` apontando para o mesmo tunnel CNAME. Apex e www passam pelo helper `_upsert_cname()`,
que **sempre busca registros existentes pelo FQDN** (`www.{dominio}`, não a label `www`) e
remove tanto registros `A` quanto `AAAA` conflitantes antes de criar o CNAME.

> **Bug corrigido:** a busca do CNAME `www` filtrava por `name='www'`, que a API do
> Cloudflare ignora — o registro existente nunca era encontrado, o código caía no `POST` e
> o Cloudflare respondia `400: An A, AAAA, or CNAME record with that host already exists`
> quando o cliente já tinha um `www` no DNS. Agora a busca usa o FQDN e o registro é
> atualizado no lugar.

---

## Como aplicar para clientes existentes

**Não é necessário re-provisionar.** Basta:

1. Acessar o cliente no admin do CP
2. Clicar em **⚙ Aplicar Configurações**

Isso executa `_view_aplicar_modulos()` + `_configurar_dominio_cloudflare()` com
as novas regras — o serviço web reinicia em ~30s e o DNS propaga em seguida.

---

## Verificação

```bash
# Confirmar CNAME www no Cloudflare Dashboard:
#   @    → {tunnel_id}.cfargotunnel.com  (proxied)
#   www  → {tunnel_id}.cfargotunnel.com  (proxied)

# Testar redirect após propagação DNS (~1-2 min):
curl -I https://www.neodistribuidora.com.br
# HTTP/1.1 301 Moved Permanently
# Location: https://neodistribuidora.com.br/
```

---

## Observações

- **ALLOWED_HOSTS:** não inclui `www.{dominio}` — desnecessário pois Django não recebe esses requests.
- **HTTPS:** o redirect já aponta para `https://` — mesmo que o cliente acesse via `http://www.`, sai como `https://apex`.
- **Idempotente:** clicar "Aplicar Configurações" múltiplas vezes é seguro — o CNAME é atualizado apenas se o conteúdo mudou.
