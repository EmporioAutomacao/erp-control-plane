# Release: Correção do CNAME `www` na automação Cloudflare

- data: 2026-08-31
- responsavel: James Flavio
- status: implementado
- versao: `0.0.27`
- app: `registry`
- arquivos modificados:
  - `core/settings.py` — `VERSION` 0.0.26 → 0.0.27
  - `registry/admin.py` — `_configurar_dominio_cloudflare()`: bloco de DNS refatorado no helper `_upsert_cname()`
  - `registry/templates/registry/admin_ajuda.html` — seções "Configurar Cloudflare" e "Casos especiais"
  - `docs/dominios-custom-www.md` — nota do bug corrigido

---

## 1. Sintoma

Ao clicar em **☁ Configurar Cloudflare** (ou **⚙ Aplicar Configurações** com domínio custom) para um cliente cujo domínio **já tinha um registro `www` no DNS**:

```
Falha ao configurar Cloudflare: Cloudflare 400: An A, AAAA, or CNAME record
with that host already exists.
```

## 2. Causa

Em `_configurar_dominio_cloudflare()`, a busca pelo CNAME `www` existente era feita com:

```python
params={'type': 'CNAME', 'name': 'www'}
```

A API do Cloudflare espera o **FQDN** no parâmetro `name` (`www.dominio.com.br`), não a label curta `www`. A busca sempre retornava vazio, o código caía no ramo de `POST` (criar) e o Cloudflare recusava com `400` porque o registro `www` já existia — normalmente um CNAME apontando para outro host (registros `A` de `www` até eram removidos antes; CNAME não).

Efeito secundário: a limpeza de registros conflitantes só removia tipo `A`, nunca `AAAA`.

## 3. Correção

O bloco que criava/atualizava os CNAMEs de apex e `www` (duas cópias quase idênticas) foi unificado no helper interno `_upsert_cname(nome_label, nome_fqdn)`:

- **Sempre busca registros pelo FQDN** — corrige o `www`.
- Remove registros **`A` e `AAAA`** conflitantes no host (antes só `A`).
- Usa `_cf_raise()` também no `DELETE` (falhas de permissão deixavam de ser silenciosas).
- Se já existe CNAME, faz `PUT` (atualiza só se o conteúdo/proxy mudou); senão `POST`.

```python
record_acao = _upsert_cname('@', dominio)
_upsert_cname('www', f'www.{dominio}')
```

Comportamento externo idêntico ao anterior nos casos que já funcionavam — apenas passa a tratar o caso do `www` pré-existente e de registros `AAAA`.

## 4. Sem migração

Nenhuma mudança de modelo/banco. Não há fixture nova. É só deploy da imagem.

## 5. Deploy em produção

```bash
# 1. Build + push da imagem do CP com a nova tag
docker build -t emporioautomacao/ararasuite-cp:0.0.27 .
docker push emporioautomacao/ararasuite-cp:0.0.27

# 2. No servidor: apontar CP_VERSION e re-deployar o stack
#    (editar /opt/cp/.env → CP_VERSION=0.0.27, ou exportar na hora)
export $(cat /opt/cp/.env | xargs) && CP_VERSION=0.0.27 \
  docker stack deploy --with-registry-auth -c stack-prod.yml ararasuite-cp

# 3. Conferir rollout
docker service ps ararasuite-cp_web
```

O `entrypoint.sh` roda `migrate` + `loaddata` no start — sem efeito prático nesta versão (nada mudou nesses).

## 6. Como testar após o deploy

1. Cliente com domínio custom cujo apex/`www` já tenha registros no Cloudflare (ou criar um CNAME `www` de teste apontando para qualquer host).
2. Admin do CP → cliente → **☁ Configurar Cloudflare**.
3. Esperado: mensagem de sucesso `CNAME de "<dominio>" criado/atualizado no Cloudflare → tunnel "<nome>"`, **sem** o erro 400.
4. No Cloudflare Dashboard → DNS: `@` e `www` como CNAME → `{tunnel-id}.cfargotunnel.com` (proxied).
5. Após propagação: `curl -I https://www.<dominio>` → `301` para o apex.
