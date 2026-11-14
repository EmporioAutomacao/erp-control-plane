# Vários domínios próprios por cliente (0.0.61)

Substitui o campo único `Cliente.dominio_custom` (docs antigos: `release-dominio-custom.md`,
`dominio-custom-cloudflare.md`, `../dominios-custom-www.md`).

Passo a passo de operação (zona, e-mail no provedor externo, nameservers, armadilhas):
[dominio-proprio-email-externo.md](dominio-proprio-email-externo.md).

## Modelo

`DominioCliente` (`registry_dominiocliente`), inline **Domínios** (aba) na página do Cliente:

| Campo | Regra |
|---|---|
| `dominio` | Único no banco. Normalizado (minúsculas, sem esquema/caminho/porta/ponto final). Não pode estar dentro de `ERP_DOMAIN`/`SAAS_DOMAIN`, ser o subdomínio de um cliente nem colidir (inclusive pelo `www.`) com hostname de outro cliente |
| `modo` | `servir` (abre o ERP) ou `redirecionar` (301 para o principal) |
| `principal` | No máximo um por cliente (constraint `dominio_principal_unico_por_cliente`), sempre `servir` |
| `incluir_www` | Publica `www.<dominio>` com 301. Desligado sozinho se o domínio já começa com `www.` |

`Cliente.host_publico` = principal ou, sem ele, o subdomínio. `Cliente.url`, health check,
`cp_pull`, `cp_push` e e-mail de boas-vindas usam esse host. **`SITE_URL` continua sempre o
subdomínio** (`site_url_cliente`).

`Cliente.dominios_aplicados` (JSON, não editável): hostnames publicados no Cloudflare pelo
último Aplicar/Configurar Cloudflare. É a referência para remover o que saiu da lista.

A migration `0035` converte cada `dominio_custom` em um `DominioCliente` principal, `servir`,
com www — o mesmo que estava publicado — e grava `dominios_aplicados = [d, www.d]`.

## O que cada camada recebe (`registry/dominios.py`)

| Camada | Conteúdo |
|---|---|
| `ALLOWED_HOSTS` / `CSRF_TRUSTED_ORIGINS` | subdomínio + domínios `servir` (sem `www.` e sem os `redirecionar`, que nunca chegam ao Django) |
| Traefik | router `{slug}-erp-custom-{chave}` por domínio e `{slug}-erp-custom-{chave}-www`; middleware `{slug}-redir-{chave_do_alvo}` (`redirectregex ^https?://[^/]+(.*)` → `https://<alvo>${1}`, permanente). `chave = dominio.replace('.', '_')` |
| Cloudflare | CNAME proxied → `<tunnel>.cfargotunnel.com` e ingress → `http://traefik_traefik:80` para cada hostname (domínio + `www.`) |

Alvo dos redirects: o `www.` de um domínio `servir` vai para o próprio domínio; um domínio
`redirecionar` (e o `www.` dele) vai para o `host_publico`.

## Aplicar Configurações

1. Grava env no `.env` e monta o `docker service update` do `{slug}_web`.
2. Lê as labels atuais (`swarm.labels_do_servico`) e `dominios.args_reconciliar` gera
   `--label-rm` para toda label dos prefixos gerenciados que não está no desejado:
   `traefik.http.routers.{slug}-erp-custom*`, `traefik.http.middlewares.{slug}-redir-*` e o
   legado `traefik.http.middlewares.{slug}-www-redirect*`. Os routers legados `{slug}-erp-custom`
   e `{slug}-erp-custom-www` saem assim na primeira aplicação depois da 0.0.61. Labels fora desses
   prefixos (`{slug}-erp`, `traefik.enable`, serviço) nunca são tocadas.
3. Com sucesso, `cloudflare.sincronizar(cliente)` (também chamado pelo botão ☁ Configurar Cloudflare).

Re-provisionar/migrar geram o `stack.yml` com as mesmas labels (`dominios.labels_yaml`, que
escapa `$` → `$$` para o compose); o `docker stack deploy` substitui as labels inteiras.

## Cloudflare (`registry/cloudflare.py`)

- Zona: sobe os níveis do domínio até achar uma zona da conta (`erp.loja.com.br` → `loja.com.br`).
  Sem zona, tenta criar a do próprio domínio; o Cloudflare recusa zona de subdomínio e a mensagem
  pede para cadastrar a zona raiz. Zona criada → mensagem com os nameservers.
- Ingress: hostnames desejados no topo, removidos fora, demais regras mantidas, catch-all por último.
- Remoção: `dominios_aplicados − desejados`, exceto hostnames que hoje pertencem a outro cliente.
  O CNAME só é apagado se apontar para o nosso tunnel; senão fica e a tela avisa. A zona nunca é apagada.
- Certificado: o universal do Cloudflare cobre só `zona` e `*.zona`. Hostname dois níveis abaixo
  (`www.erp.loja.com.br`) é publicado com aviso de que vai dar erro de certificado.
- `dominios_aplicados` depois: desejados ∪ remoções que falharam (refeitas no próximo clique).
- `sincronizar` nunca lança: devolve `{'ok', 'mensagens': [(nivel, texto)], 'erro'}`.

## Outras regras

- Migração de servidor ativa: o inline fica somente leitura.
- Salvar a lista de domínios não publica nada: o admin avisa para clicar em Aplicar Configurações.
- Trocar o principal num salvamento só funciona: `DominioCliente.get_constraints` tira a
  constraint do principal da validação do `full_clean` (o formset confere entre as linhas) e
  `_DominiosFormSet.save` desmarca o antigo antes de gravar. A constraint do banco continua valendo.

## Comportamento verificado no Traefik real

Traefik v2.11 e v3.6 (provider docker e swarm) com as labels geradas: domínio `servir` → 200;
`www.` → 301 para o domínio com caminho e querystring; domínio `redirecionar` → 301 em GET e
**308** em POST/PUT (método preservado) para o principal; sem www → 404 no `www.`.

**Não pôr em "Redireciona" um domínio onde um SyncAgent/PDV foi ativado:** o cliente HTTP do
.NET descarta o `Authorization` ao seguir redirect e a sincronização para com 401.

Domínio com acento é gravado em punycode (`lojá.com.br` → `xn--loj-gla.com.br`).

## Teste E2E com Swarm local (opcional)

`registry/tests/test_dominios_swarm_e2e.py` roda o "Aplicar Configurações" com `docker service
update` real (Cloudflare simulado). Só com `CP_E2E_SWARM=1` e um Swarm local descartável:

```bash
docker swarm init --advertise-addr 127.0.0.1
docker network create -d overlay --attachable cpe2e-net
docker service create -d --name acme_web --network cpe2e-net   --label traefik.enable=true --label 'traefik.http.routers.acme-erp.rule=Host(`acme.ararasuite.com.br`)'   --label traefik.http.services.acme-erp.loadbalancer.server.port=80   --label 'traefik.http.routers.acme-erp-custom.rule=Host(`velho.com.br`)' traefik/whoami:v1.10
CP_E2E_SWARM=1 python manage.py test registry.tests.test_dominios_swarm_e2e
docker service rm acme_web; docker network rm cpe2e-net; docker swarm leave --force
```

## Limitações

- **Destruir** cliente não limpa o Cloudflare (já não limpava): remova os domínios e aplique antes.
- Sem lista de sufixos públicos: a busca da zona testa até o penúltimo nível (`com.br`), o que é
  inofensivo (não existe zona `com.br` na conta).

## Testes

`registry/tests/test_dominios.py`.
