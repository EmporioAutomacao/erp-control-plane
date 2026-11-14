"""Publicação dos domínios próprios de um cliente no Cloudflare: DNS (CNAME → tunnel) + ingress.

`sincronizar(cliente)` leva o Cloudflare ao estado da lista de `DominioCliente`:

- cada hostname (domínio e, com `incluir_www`, o `www.`) ganha um CNAME proxied para
  `<tunnel>.cfargotunnel.com` na zona encontrada subindo os níveis do domínio
  (`erp.loja.com.br` → `loja.com.br`); sem zona, tenta criar a do próprio domínio;
- o ingress do tunnel recebe os hostnames no topo (→ Traefik) e perde os removidos;
- hostnames que estavam em `Cliente.dominios_aplicados` e saíram da lista são removidos do
  ingress e têm o CNAME apagado **só se apontar para o nosso tunnel**. A zona nunca é apagada.
- hostname que hoje pertence a outro cliente nunca é removido (domínio trocado de cliente).

Doc: docs/registry/dominios-multiplos.md.
"""
import os

import requests

from . import dominios as dom

BASE = 'https://api.cloudflare.com/client/v4'
SERVICO_TRAEFIK = 'http://traefik_traefik:80'


class ErroCloudflare(Exception):
    pass


class Api:
    """Chamadas à API do Cloudflare usadas pelo CP (token/tunnel de `ConfiguracaoCloudflare`)."""

    def __init__(self, token, tunnel_nome=''):
        self.auth = {'Authorization': f'Bearer {token}'}
        self.json = {**self.auth, 'Content-Type': 'application/json'}
        self.tunnel_nome_cfg = tunnel_nome
        self.account_id = self.tunnel_id = self.tunnel_nome = None

    @classmethod
    def da_configuracao(cls):
        from .models import ConfiguracaoCloudflare
        cfg = ConfiguracaoCloudflare.obter()
        token = ((cfg.cf_api_token if cfg else None) or os.getenv('CF_API_TOKEN', '')).strip()
        if not token:
            raise ErroCloudflare('Token Cloudflare não configurado. Acesse Configurações → Cloudflare no admin do CP.')
        api = cls(token, (cfg.tunnel_name if cfg else None) or os.getenv('CF_TUNNEL_NAME', ''))
        api.conectar()
        return api

    @staticmethod
    def _ok(r):
        if not r.ok:
            try:
                erros = r.json().get('errors', [])
                msg = erros[0].get('message', r.text) if erros else r.text
            except Exception:
                msg = r.text
            raise ErroCloudflare(f'Cloudflare {r.status_code}: {msg}')
        return r

    def _get(self, caminho, **params):
        return self._ok(requests.get(f'{BASE}{caminho}', params=params or None, headers=self.auth, timeout=15)).json()

    def _enviar(self, metodo, caminho, payload=None):
        r = requests.request(metodo, f'{BASE}{caminho}', json=payload, headers=self.json, timeout=15)
        return self._ok(r).json()

    def conectar(self):
        r = requests.get(f'{BASE}/user/tokens/verify', headers=self.auth, timeout=10)
        if not r.ok:
            raise ErroCloudflare(
                'CF_API_TOKEN inválido. Verifique: (1) é um API Token, não a Global API Key; '
                '(2) tem permissões Zone:Read + Zone:Edit + DNS:Edit + Account:Cloudflare Tunnel:Edit. '
                f'Cloudflare retornou {r.status_code}.'
            )
        # Account ID a partir de uma zona existente (não requer Account:Read)
        saas = os.getenv('SAAS_DOMAIN', 'ararasuite.com.br')
        r_zona = requests.get(f'{BASE}/zones', params={'name': saas}, headers=self.auth, timeout=15)
        if r_zona.ok and r_zona.json().get('result'):
            self.account_id = r_zona.json()['result'][0]['account']['id']
        else:
            contas = self._get('/accounts')['result']
            if not contas:
                raise ErroCloudflare(
                    f'Não foi possível obter o Account ID. Adicione "Account Settings:Read" ao token '
                    f'ou certifique que SAAS_DOMAIN ({saas}) está na conta Cloudflare.'
                )
            self.account_id = contas[0]['id']
        params = {'is_deleted': 'false'}
        if self.tunnel_nome_cfg:
            params['name'] = self.tunnel_nome_cfg
        tunnels = self._get(f'/accounts/{self.account_id}/cfd_tunnel', **params)['result']
        if not tunnels:
            raise ErroCloudflare('Nenhum Cloudflare Tunnel encontrado. Configure o nome do tunnel em Configurações → Cloudflare.')
        self.tunnel_id, self.tunnel_nome = tunnels[0]['id'], tunnels[0]['name']

    @property
    def tunnel_cname(self):
        return f'{self.tunnel_id}.cfargotunnel.com'

    # ── tunnel ────────────────────────────────────────────────────────────
    def ler_ingress(self):
        caminho = f'/accounts/{self.account_id}/cfd_tunnel/{self.tunnel_id}/configurations'
        config = self._get(caminho)['result'].get('config', {}) or {}
        return config

    def gravar_ingress(self, config):
        caminho = f'/accounts/{self.account_id}/cfd_tunnel/{self.tunnel_id}/configurations'
        self._enviar('PUT', caminho, {'config': config})

    # ── zonas / DNS ───────────────────────────────────────────────────────
    def zona(self, nome):
        result = self._get('/zones', name=nome)['result']
        return result[0] if result else None

    def criar_zona(self, nome):
        return self._enviar('POST', '/zones', {'name': nome, 'jump_start': False})['result']

    def registros(self, zona_id, tipo, nome):
        return self._get(f'/zones/{zona_id}/dns_records', type=tipo, name=nome)['result']

    def apagar_registro(self, zona_id, registro_id):
        self._enviar('DELETE', f'/zones/{zona_id}/dns_records/{registro_id}')

    def gravar_registro(self, zona_id, payload, registro_id=None):
        if registro_id:
            self._enviar('PUT', f'/zones/{zona_id}/dns_records/{registro_id}', payload)
        else:
            self._enviar('POST', f'/zones/{zona_id}/dns_records', payload)


def novo_ingress(regras, desejados, removidos):
    """Ingress com `desejados` no topo, sem `removidos`, as demais regras e o catch-all no fim."""
    tirar = set(desejados) | set(removidos)
    outras = [r for r in regras if r.get('hostname') and r['hostname'] not in tirar]
    catch_all = next((r for r in regras if not r.get('hostname')), {'service': 'http_status:404'})
    return [{'hostname': h, 'service': SERVICO_TRAEFIK} for h in desejados] + outras + [catch_all]


def candidatos_de_zona(hostname):
    """`a.erp.loja.com.br` → ['a.erp.loja.com.br', 'erp.loja.com.br', 'loja.com.br', 'com.br']."""
    partes = hostname.split('.')
    return ['.'.join(partes[i:]) for i in range(len(partes) - 1)]


def coberto_pelo_certificado(hostname, zona):
    """Certificado universal do Cloudflare: `zona` e `*.zona` (um nível só)."""
    if hostname == zona:
        return True
    return hostname.endswith('.' + zona) and '.' not in hostname[:-len(zona) - 1]


def achar_zona(api, hostname):
    for nome in candidatos_de_zona(hostname):
        zona = api.zona(nome)
        if zona:
            return zona
    return None


def publicar_cname(api, zona_id, hostname):
    """CNAME proxied → tunnel, apagando A/AAAA no mesmo nome. Devolve 'criado'/'atualizado'/'ok'."""
    for tipo in ('A', 'AAAA'):
        for rec in api.registros(zona_id, tipo, hostname):
            api.apagar_registro(zona_id, rec['id'])
    payload = {'type': 'CNAME', 'name': hostname, 'content': api.tunnel_cname, 'proxied': True, 'ttl': 1}
    recs = api.registros(zona_id, 'CNAME', hostname)
    if recs:
        if recs[0]['content'] != api.tunnel_cname or not recs[0].get('proxied'):
            api.gravar_registro(zona_id, payload, recs[0]['id'])
            return 'atualizado'
        return 'ok'
    api.gravar_registro(zona_id, payload)
    return 'criado'


def remover_cname_nosso(api, hostname):
    """Apaga o CNAME de `hostname` se apontar para o nosso tunnel. Devolve (removido, aviso)."""
    zona = achar_zona(api, hostname)
    if not zona:
        return False, ''
    recs = api.registros(zona['id'], 'CNAME', hostname)
    removido = False
    for rec in recs:
        if rec.get('content') == api.tunnel_cname:
            api.apagar_registro(zona['id'], rec['id'])
            removido = True
    if recs and not removido:
        return False, f'{hostname}: o CNAME aponta para {recs[0].get("content")}, não para o tunnel — mantido.'
    return removido, ''


def _em_uso_por_outro(cliente, hostname):
    from .models import DominioCliente
    outros = DominioCliente.objects.exclude(cliente_id=cliente.pk)
    return any(hostname in dom.hostnames_do_dominio(d) for d in outros)


def sincronizar(cliente, api=None):
    """Publica os domínios do cliente e remove os que saíram. Nunca lança.

    Devolve {'ok': bool, 'mensagens': [(nivel, texto)], 'erro': str}; `nivel` é
    'success' | 'info' | 'warning' | 'error' (níveis do `django.contrib.messages`).
    """
    from .models import Cliente

    lista = dom.do_cliente(cliente)
    desejados = dom.hostnames_cloudflare(cliente, lista)
    aplicados = list(cliente.dominios_aplicados or [])
    removidos = [h for h in aplicados if h not in desejados and not _em_uso_por_outro(cliente, h)]
    mensagens = []
    if not desejados and not removidos:
        Cliente.objects.filter(pk=cliente.pk).update(dominios_aplicados=[])
        return {'ok': True, 'mensagens': [], 'erro': ''}

    pendentes = list(removidos)
    falhou = False
    try:
        api = api or Api.da_configuracao()
        config = api.ler_ingress()
        config['ingress'] = novo_ingress(config.get('ingress', []), desejados, removidos)
        api.gravar_ingress(config)

        for d in lista:
            try:
                zona = achar_zona(api, d.dominio)
                if not zona:
                    try:
                        zona = api.criar_zona(d.dominio)
                    except ErroCloudflare as exc:
                        raise ErroCloudflare(
                            f'nenhuma zona do Cloudflare contém {d.dominio} e não foi possível criá-la ({exc}). '
                            'Para subdomínio (ex.: erp.loja.com.br), cadastre antes a zona do domínio raiz no Cloudflare.'
                        )
                    ns = ', '.join(zona.get('name_servers', []))
                    mensagens.append(('info', f'Zona "{zona["name"]}" criada no Cloudflare. '
                                              f'Informe ao cliente para trocar os nameservers no registrador para: {ns}'))
                acoes = [f'{h} {publicar_cname(api, zona["id"], h)}' for h in dom.hostnames_do_dominio(d)]
                mensagens.append(('success', f'Cloudflare: {", ".join(acoes)} (zona {zona["name"]}) → tunnel "{api.tunnel_nome}".'))
                fora = [h for h in dom.hostnames_do_dominio(d) if not coberto_pelo_certificado(h, zona['name'])]
                if fora:
                    mensagens.append(('warning', (
                        f'{", ".join(fora)}: o certificado universal do Cloudflare só cobre {zona["name"]} e '
                        f'*.{zona["name"]} — o navegador vai dar erro de certificado. Desligue "Incluir www" '
                        'ou contrate um Advanced Certificate para esse nome.'
                    )))
            except ErroCloudflare as exc:
                falhou = True
                mensagens.append(('warning', f'Cloudflare, {d.dominio}: {exc}'))

        for h in removidos:
            try:
                removido, aviso = remover_cname_nosso(api, h)
                pendentes.remove(h)
                if aviso:
                    mensagens.append(('warning', aviso))
                mensagens.append(('info', f'Cloudflare: {h} removido do tunnel{" e do DNS" if removido else ""}.'))
            except ErroCloudflare as exc:
                falhou = True
                mensagens.append(('warning', f'Cloudflare, remoção de {h}: {exc}'))
    except Exception as exc:
        Cliente.objects.filter(pk=cliente.pk).update(dominios_aplicados=_unir(desejados, pendentes))
        return {'ok': False, 'mensagens': mensagens, 'erro': str(exc)}

    Cliente.objects.filter(pk=cliente.pk).update(dominios_aplicados=_unir(desejados, pendentes))
    return {'ok': not falhou, 'mensagens': mensagens, 'erro': ''}


def _unir(a, b):
    return list(dict.fromkeys([*a, *b]))
