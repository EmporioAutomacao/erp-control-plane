# Release: Domínio Custom com Automação Cloudflare

- data: 2026-05-23
- responsavel: James Flavio
- status: implementado
- app: `registry`
- arquivos modificados:
  - `registry/admin.py` — `_view_aplicar_modulos`, `_view_configurar_cloudflare`, `_configurar_dominio_cloudflare`, `acoes_provisionamento`
  - `registry/provisioning.py` — `executar()`, `_gerar_stack_yaml()`
  - `registry/templates/registry/admin_ajuda.html` — seções Domínio Custom e Cloudflare
  - `.env.example` — `CF_API_TOKEN`, `SERVER_IP`
  - `requirements.txt` — `requests>=2.31`
  - `docs/registry/dominio-custom-cloudflare.md` — planejamento técnico detalhado

---

## 1. Objetivo

Permitir que um cliente use seu próprio domínio (ex: `neodistribuidora.com.br`) no ERP de forma simultânea ao subdomínio padrão (`neodistribuidora.ararasuite.com.br`), com automação máxima via CP e Cloudflare API.

---

## 2. O que foi implementado

### 2.1 Campo `dominio_custom` (já existia no model)

`Cliente.dominio_custom` — CharField(200), blank=True. Admin em **Infraestrutura → Domínio custom**.

`Cliente.url` já prioriza `dominio_custom` se preenchido.

### 2.2 `provisioning.py` — stack gerado com domínio custom

`executar()` agora:
- Calcula `allowed_hosts = f'{subdominio},{dominio_custom}'` se `dominio_custom` estiver preenchido
- Calcula `csrf_origins` equivalente
- Passa `dominio_custom` para `_gerar_stack_yaml()`

`_gerar_stack_yaml(slug, subdominio, versao, regiao, env_vars, dominio_custom='')` agora gera labels Traefik adicionais quando `dominio_custom` está presente:

```yaml
- "traefik.http.routers.{slug}-erp-custom.rule=Host(`{dominio_custom}`)"
- "traefik.http.routers.{slug}-erp-custom.entrypoints=web"
- "traefik.http.routers.{slug}-erp-custom.service={slug}-erp"
```

Os dois routers coexistem — o padrão (`{slug}-erp`) e o custom (`{slug}-erp-custom`) apontam para o mesmo serviço.

### 2.3 `_view_aplicar_modulos` — aplica domínio no serviço em execução

Além de `MODULOS_ATIVOS` e `TEMA_SITE`, agora também aplica via `docker service update`:
- `--env-add ALLOWED_HOSTS={subdominio},{dominio_custom}`
- `--env-add CSRF_TRUSTED_ORIGINS=https://{subdominio},https://{dominio_custom}`
- `--label-add traefik.http.routers.{slug}-erp-custom.*` (quando domínio custom presente)

Atualiza o `.env` do cliente em disco com os mesmos valores (campos `MODULOS_ATIVOS`, `TEMA_SITE`, `ALLOWED_HOSTS`, `CSRF_TRUSTED_ORIGINS`).

Se o `docker service update` for bem-sucedido e houver `dominio_custom`, chama automaticamente `_configurar_dominio_cloudflare()`.

### 2.4 `_view_configurar_cloudflare` — botão independente

Nova URL: `/<pk>/configurar-cloudflare/`

Novo botão **☁ Configurar Cloudflare** nas ações do cliente. Funciona de forma independente do Docker — pode ser usado sem reiniciar o serviço.

Lê o IP de `cliente.host.ip` (HostInfraestrutura) ou fallback para `os.getenv('SERVER_IP')`.

### 2.5 `_configurar_dominio_cloudflare(dominio, ip)` — API Cloudflare

Método privado em `ClienteAdmin`. Fluxo:

1. `GET /zones?name={dominio}` — verifica se a zona existe
2. Se não existe: `POST /zones` — cria zona, retorna nameservers
3. `GET /zones/{zone_id}/dns_records?type=A&name={dominio}` — verifica registro A
4. Se não existe: `POST /dns_records` com `proxied=true`
5. Se existe mas IP ou proxy errados: `PUT /dns_records/{id}`

Retorna `{'ok': bool, 'zona_criada': bool, 'nameservers': list, 'record': 'criado'|'atualizado'}` ou `{'ok': False, 'erro': str}`.

### 2.6 Página de Ajuda — seções novas

Adicionadas as seções:
- **Domínio custom** — funcionamento, o que é automático, o que é manual
- **Cloudflare** — 4 cards: pré-requisitos, o que o botão faz, fluxo completo de ativação, casos especiais

Tabela de ações atualizada com a nova coluna "☁ Configurar Cloudflare" e descrição expandida do "⚙ Aplicar Configurações".

---

## 3. Configuração necessária em produção

```env
# .env do CP
CF_API_TOKEN=<token com Zone:Read + DNS:Edit em All Zones>
SERVER_IP=104.248.189.43
```

Token criado em: Cloudflare → My Profile → API Tokens → Create Token → Custom Token.

---

## 4. Variáveis de ambiente geradas no .env do cliente

| Variável | Sem domínio custom | Com domínio custom |
|---|---|---|
| `ALLOWED_HOSTS` | `neodistribuidora.ararasuite.com.br` | `neodistribuidora.ararasuite.com.br,neodistribuidora.com.br` |
| `CSRF_TRUSTED_ORIGINS` | `https://neodistribuidora.ararasuite.com.br` | `https://neodistribuidora.ararasuite.com.br,https://neodistribuidora.com.br` |

---

## 5. O que NÃO é automatizado

- **Troca de nameservers no registrador** (Registro.br, GoDaddy etc.) — sempre manual, feita pelo cliente uma vez. O CP exibe os nameservers corretos após criar a zona.
- **Remoção do registro DNS** ao limpar `dominio_custom` — o registro A na Cloudflare deve ser removido manualmente se necessário.

---

## 6. Dependência adicionada

`requests>=2.31` em `requirements.txt` — a biblioteca `docker` (já presente) puxa `requests` como dependência transitiva, mas agora declaramos explicitamente para uso direto.
