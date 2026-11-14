"""Domínios próprios de um cliente (`DominioCliente`): o que vira env, label do Traefik e DNS.

Regras (doc: docs/registry/dominios-multiplos.md):

- O subdomínio (`Cliente.subdominio`) sempre serve o ERP; os domínios próprios são extras.
- Cada domínio próprio serve o ERP (`servir`) ou redireciona 301 para o endereço principal
  (`redirecionar`). O principal é o domínio marcado `principal` (sempre `servir`) ou, sem ele,
  o subdomínio.
- Só o subdomínio e os domínios `servir` entram no `ALLOWED_HOSTS`/`CSRF_TRUSTED_ORIGINS`:
  redirect e `www.` são resolvidos no Traefik e nunca chegam ao Django.
- Routers/middlewares dos domínios próprios têm prefixos fixos, para o "Aplicar Configurações"
  conseguir remover os que saíram da lista (`args_reconciliar`).
"""
import os
import re

MODO_SERVIR = 'servir'
MODO_REDIRECIONAR = 'redirecionar'

_HOSTNAME = re.compile(r'^(?=.{1,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z][a-z0-9-]{0,61}[a-z0-9]$')
REDIRECT_REGEX = r'^https?://[^/]+(.*)'


def dominios_da_plataforma():
    """Domínios da própria AraraSuite: nenhum domínio de cliente pode estar dentro deles."""
    return {d.strip().lower() for d in (
        os.getenv('ERP_DOMAIN', 'ararasuite.com.br'), os.getenv('SAAS_DOMAIN', 'ararasuite.com.br'),
    ) if d and d.strip()}


def normalizar(valor):
    """`https://WWW.Loja.com.br:443/x/` → `www.loja.com.br`; com acento, punycode. Não valida."""
    v = (valor or '').strip().lower()
    v = re.sub(r'^[a-z][a-z0-9+.-]*://', '', v)
    v = v.split('/', 1)[0].split('?', 1)[0].split('#', 1)[0]
    v = v.rsplit('@', 1)[-1]
    v = v.split(':', 1)[0].rstrip('.')
    if not v.isascii():
        # domínio com acento (lojá.com.br): Traefik, Django e Cloudflare usam a forma punycode
        try:
            v = v.encode('idna').decode('ascii')
        except UnicodeError:
            pass  # fica não-ASCII e o formato_valido recusa
    return v


def formato_valido(dominio):
    return bool(_HOSTNAME.match(dominio or ''))


def da_plataforma(dominio):
    return any(dominio == d or dominio.endswith('.' + d) for d in dominios_da_plataforma())


def chave(dominio):
    """Parte do nome do router/middleware. `.` → `_` (não existe `_` em hostname: injetivo)."""
    return dominio.replace('.', '_')


def hostnames_do_dominio(dominio_obj):
    """Hostnames que um DominioCliente ocupa: ele e, com `incluir_www`, o `www.`."""
    nomes = [dominio_obj.dominio]
    if dominio_obj.incluir_www and not dominio_obj.dominio.startswith('www.'):
        nomes.append(f'www.{dominio_obj.dominio}')
    return nomes


def do_cliente(cliente):
    """Domínios do cliente, principal primeiro (lista vazia para cliente ainda não salvo)."""
    if getattr(cliente, 'pk', None) is None:
        return []
    return sorted(cliente.dominios.all(), key=lambda d: (not d.principal, d.dominio))


def host_publico(cliente, dominios=None):
    dominios = do_cliente(cliente) if dominios is None else dominios
    principal = next((d for d in dominios if d.principal), None)
    return principal.dominio if principal else cliente.subdominio


def hosts_servidos(cliente, dominios=None):
    dominios = do_cliente(cliente) if dominios is None else dominios
    hosts = [cliente.subdominio]
    for d in dominios:
        if d.modo == MODO_SERVIR and d.dominio not in hosts:
            hosts.append(d.dominio)
    return hosts


def allowed_hosts(cliente, dominios=None):
    return ','.join(hosts_servidos(cliente, dominios))


def csrf_origins(cliente, dominios=None):
    return ','.join(f'https://{h}' for h in hosts_servidos(cliente, dominios))


def hostnames_cloudflare(cliente, dominios=None):
    """Todos os hostnames que precisam de DNS + ingress no tunnel (servir e redirecionar)."""
    dominios = do_cliente(cliente) if dominios is None else dominios
    nomes = []
    for d in dominios:
        for n in hostnames_do_dominio(d):
            if n not in nomes:
                nomes.append(n)
    return nomes


def prefixos_gerenciados(slug):
    return (
        f'traefik.http.routers.{slug}-erp-custom',
        f'traefik.http.middlewares.{slug}-redir-',
        f'traefik.http.middlewares.{slug}-www-redirect',  # legado (domínio único, até 0.0.60)
    )


def labels_traefik(cliente, dominios=None):
    """Labels (valor bruto, sem escape de compose) dos domínios próprios do `{slug}_web`.

    Não inclui o router do subdomínio (`{slug}-erp`), que é sempre gerado à parte.
    """
    dominios = do_cliente(cliente) if dominios is None else dominios
    slug = cliente.slug
    principal = host_publico(cliente, dominios)
    labels = {}

    def _redirect(alvo):
        nome = f'{slug}-redir-{chave(alvo)}'
        base = f'traefik.http.middlewares.{nome}.redirectregex'
        labels[f'{base}.regex'] = REDIRECT_REGEX
        labels[f'{base}.replacement'] = f'https://{alvo}${{1}}'
        labels[f'{base}.permanent'] = 'true'
        return nome

    def _router(nome, host, middleware=None):
        base = f'traefik.http.routers.{nome}'
        labels[f'{base}.rule'] = f'Host(`{host}`)'
        labels[f'{base}.entrypoints'] = 'web'
        labels[f'{base}.service'] = f'{slug}-erp'
        if middleware:
            labels[f'{base}.middlewares'] = middleware

    for d in dominios:
        nome = f'{slug}-erp-custom-{chave(d.dominio)}'
        www = [n for n in hostnames_do_dominio(d) if n != d.dominio]
        if d.modo == MODO_SERVIR:
            _router(nome, d.dominio)
            if www:
                _router(f'{nome}-www', www[0], _redirect(d.dominio))
        else:
            mw = _redirect(principal)
            _router(nome, d.dominio, mw)
            if www:
                _router(f'{nome}-www', www[0], mw)
    return labels


def args_reconciliar(slug, atuais, desejadas):
    """Argumentos `docker service update` que levam as labels gerenciadas de `atuais` a `desejadas`.

    Remove (`--label-rm`) toda label dos prefixos gerenciados que não está em `desejadas`;
    adiciona (`--label-add`) as desejadas. Labels fora dos prefixos nunca são tocadas.
    """
    prefixos = prefixos_gerenciados(slug)
    args = []
    for chave_label in sorted(atuais or {}):
        if chave_label.startswith(prefixos) and chave_label not in desejadas:
            args += ['--label-rm', chave_label]
    for chave_label, valor in desejadas.items():
        args += ['--label-add', f'{chave_label}={valor}']
    return args


def _escapar_yaml(texto):
    return texto.replace('\\', '\\\\').replace('"', '\\"').replace('$', '$$')


def labels_yaml(labels, indent=8):
    """Linhas de label para o `stack.yml` (escape de string YAML entre aspas + `$` do compose)."""
    pad = ' ' * indent
    return ''.join(f'\n{pad}- "{_escapar_yaml(f"{k}={v}")}"' for k, v in labels.items())
