# Plano: Domínio Custom — Automação via Cloudflare API

- data: 2026-05-22
- responsavel: James Flavio
- status: planejado
- app: `registry`
- arquivos a modificar:
  - `registry/admin.py` — `_view_aplicar_modulos` + novo método `_view_configurar_dominio`
  - `registry/provisioning.py` — já atualizado (dominio_custom em labels + env vars)
  - `.env.example` — adicionar `CF_API_TOKEN`
  - `registry/models.py` — campo `dominio_custom_status` (opcional, para exibir estado no admin)

---

## 1. Objetivo

Quando o admin preencher `dominio_custom` no CP e clicar em **Aplicar Configurações**, o sistema deve:

1. Criar (ou verificar) a zona `neodistribuidora.com.br` na conta Cloudflare via API
2. Criar/atualizar o registro A: `@` → IP do servidor, com proxy ativo (laranja)
3. Aplicar as labels Traefik e variáveis Django no serviço Docker (já implementado)
4. Exibir os nameservers da Cloudflare para o admin informar ao cliente, caso a zona tenha sido recém-criada

---

## 2. O que não é automatizável

| Etapa | Motivo |
|---|---|
| Trocar NS no registrador (Registro.br, GoDaddy etc.) | Cada registrador tem painel próprio, sem API pública padronizada. Sempre manual — feito uma vez por domínio. |

---

## 3. Dependências de configuração

### 3.1 CF_API_TOKEN — token da Cloudflare

O token existente no **Traefik** (`CF_DNS_API_TOKEN`) pode ser o mesmo, **desde que** ele tenha as permissões abaixo.

Criar em: **Cloudflare → My Profile → API Tokens → Create Token**

| Permissão | Escopo |
|---|---|
| `Zone:Zone:Read` | All zones |
| `Zone:DNS:Edit` | All zones |

> ⚠️ Escopo "All zones" é necessário porque o domínio custom pertence ao cliente, não à conta principal. Um token com escopo apenas `ararasuite.com.br` **não funciona**.

Adicionar ao `.env` do CP:
```
CF_API_TOKEN=<token>
```

Adicionar ao `.env.example`:
```
# Cloudflare API — permissões: Zone:Read + DNS:Edit em All Zones
CF_API_TOKEN=
```

### 3.2 IP do servidor

Lido de `cliente.host.ip` (`HostInfraestrutura.ip`). Se o cliente não tiver host associado, fallback para variável de ambiente `SERVER_IP` (a definir no `.env` do CP).

Adicionar ao `.env.example`:
```
# IP público do servidor Swarm manager (fallback quando host não está definido no cliente)
SERVER_IP=104.248.189.43
```

---

## 4. Fluxo da automação

```
Admin salva dominio_custom → clica "Aplicar Configurações"
    │
    ├─ [já implementado] docker service update (labels Traefik + env Django)
    │
    └─ [a implementar] _configurar_dominio_cloudflare(cliente)
            │
            ├─ GET /zones?name=neodistribuidora.com.br
            │       ├─ zona existe → pega zone_id
            │       └─ zona não existe → POST /zones (cria)
            │               └─ salva nameservers retornados → exibe no admin
            │
            ├─ GET /zones/{zone_id}/dns_records?type=A&name=neodistribuidora.com.br
            │       ├─ registro existe → PUT (atualiza IP se diferente)
            │       └─ não existe → POST (cria A @, proxied=true)
            │
            └─ retorna: { status, nameservers, record_criado }
```

---

## 5. Implementação

### 5.1 Função `_configurar_dominio_cloudflare` em `registry/admin.py`

```python
def _configurar_dominio_cloudflare(self, dominio, ip):
    """
    Garante que `dominio` existe como zona ativa na Cloudflare com A record → ip (proxied).
    Retorna dict com status, nameservers (se zona recém-criada) e se o record foi criado/atualizado.
    """
    import os, requests

    token = os.getenv('CF_API_TOKEN', '')
    if not token:
        return {'ok': False, 'erro': 'CF_API_TOKEN não configurado no CP.'}

    headers = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}
    base = 'https://api.cloudflare.com/client/v4'

    # 1. Buscar zona
    r = requests.get(f'{base}/zones', params={'name': dominio}, headers=headers, timeout=15)
    r.raise_for_status()
    zonas = r.json()['result']

    nameservers = []
    zona_criada = False

    if zonas:
        zone_id = zonas[0]['id']
    else:
        # 2. Criar zona
        r = requests.post(f'{base}/zones', json={'name': dominio, 'jump_start': False}, headers=headers, timeout=15)
        r.raise_for_status()
        data = r.json()['result']
        zone_id = data['id']
        nameservers = data.get('name_servers', [])
        zona_criada = True

    # 3. Verificar registro A existente
    r = requests.get(
        f'{base}/zones/{zone_id}/dns_records',
        params={'type': 'A', 'name': dominio},
        headers=headers, timeout=15,
    )
    r.raise_for_status()
    records = r.json()['result']

    if records:
        record_id = records[0]['id']
        if records[0]['content'] != ip or not records[0].get('proxied'):
            requests.put(
                f'{base}/zones/{zone_id}/dns_records/{record_id}',
                json={'type': 'A', 'name': '@', 'content': ip, 'proxied': True, 'ttl': 1},
                headers=headers, timeout=15,
            ).raise_for_status()
        record_acao = 'atualizado'
    else:
        requests.post(
            f'{base}/zones/{zone_id}/dns_records',
            json={'type': 'A', 'name': '@', 'content': ip, 'proxied': True, 'ttl': 1},
            headers=headers, timeout=15,
        ).raise_for_status()
        record_acao = 'criado'

    return {
        'ok': True,
        'zona_criada': zona_criada,
        'nameservers': nameservers,
        'record': record_acao,
    }
```

### 5.2 Integrar em `_view_aplicar_modulos`

Após aplicar o Docker service update com sucesso, se `dominio_custom` estiver preenchido:

```python
if dominio_custom:
    ip = cliente.host.ip if cliente.host else os.getenv('SERVER_IP', '')
    if ip:
        try:
            cf = self._configurar_dominio_cloudflare(dominio_custom, ip)
            if cf['ok']:
                if cf['zona_criada']:
                    ns = ', '.join(cf['nameservers'])
                    messages.info(
                        request,
                        f'Zona "{dominio_custom}" criada no Cloudflare. '
                        f'Informe ao cliente para trocar os nameservers no registrador para: {ns}',
                    )
                else:
                    messages.success(request, f'Registro A de "{dominio_custom}" {cf["record"]} no Cloudflare.')
            else:
                messages.warning(request, f'Cloudflare: {cf["erro"]}')
        except Exception as exc:
            messages.warning(request, f'Falha ao configurar Cloudflare: {exc}')
    else:
        messages.warning(request, 'IP do servidor não encontrado — configure CF_API_TOKEN e SERVER_IP no .env do CP.')
```

### 5.3 Dependência Python

Adicionar ao `requirements.txt` do CP:
```
requests>=2.31
```

> Verificar se já está presente — o CP pode já usar `requests` para health checks.

---

## 6. Remoção de domínio custom

Quando o admin limpa o campo `dominio_custom` e aplica:

- **Django + Traefik:** já tratado — `ALLOWED_HOSTS` e `CSRF_TRUSTED_ORIGINS` voltam a ter só o subdomínio; labels `*-erp-custom` não são adicionadas
- **Cloudflare:** **não removemos a zona automaticamente** — a zona pode conter outros records do cliente. Apenas o registro A pode ser removido via API, mas isso é opcional e fora do escopo inicial

---

## 7. Limitações e edge cases

| Caso | Comportamento |
|---|---|
| `CF_API_TOKEN` ausente | Mensagem de aviso no admin; Traefik/Django aplicados normalmente |
| Zona já existe e A já aponta para o IP correto | Nenhuma alteração na Cloudflare — idempotente |
| Zona existe mas sob conta diferente | `GET /zones` retorna lista vazia → tenta criar → Cloudflare retorna erro "already taken" → mensagem de aviso |
| Cliente sem `host` associado e sem `SERVER_IP` | Aviso: "IP do servidor não encontrado" |
| Propagação de NS | Pode levar 1–24h após o cliente trocar no registrador — fora do controle do CP |

---

## 8. Checklist de implementação

- [ ] Criar `CF_API_TOKEN` na Cloudflare com permissões Zone:Read + DNS:Edit em All Zones
- [ ] Verificar se o token atual do Traefik (`CF_DNS_API_TOKEN`) tem essas permissões — se sim, pode ser reaproveitado
- [ ] Adicionar `CF_API_TOKEN` e `SERVER_IP` ao `.env` do CP em produção
- [ ] Atualizar `.env.example` do CP
- [ ] Implementar `_configurar_dominio_cloudflare()` em `registry/admin.py`
- [ ] Integrar chamada em `_view_aplicar_modulos()` após sucesso do Docker update
- [ ] Verificar se `requests` já está no `requirements.txt`; adicionar se não
- [ ] Testar com domínio real em staging
- [ ] Documentar mensagem de NS exibida ao admin para repassar ao cliente
