"""Serviço ``<slug>_mcp``: o Servidor MCP do ERP de cada cliente (fase M2b).

Plano completo: ``docs/projeto-agentes/mcp-erp-plano.md`` no repositório do ERP.

- Mesma imagem do web, entrypoint ``python -m mcp_erp`` (uvicorn na 8001), nunca roda migrations.
- **Env montado aqui, nunca copiado do web** e **sem volumes**: o MCP fica exposto em ``/mcp`` e não
  pode carregar ``POSTGRES_PASSWORD``, ``DJANGO_SECRET_KEY``, ``CP_SHARED_SECRET`` nem a chave mestra.
  Ele só fala com ``http://web:8000`` pela rede interna da stack e não guarda credencial nenhuma.
- Liga/desliga pelo módulo ``agents`` do cliente (opt-in, fora de qualquer plano da fixture), com a
  mesma regra de status do cron: cliente fora do ar tem o MCP em 0.
- Routers só nos hosts que servem o ERP (subdomínio + domínios ``servir``). Num domínio
  ``redirecionar`` o router ``Host && PathPrefix(/mcp)`` venceria o redirect (regra mais longa).
"""
from . import dominios

MODULO = 'agents'
PORTA = 8001
COMANDO = ['python', '-m', 'mcp_erp']
ERP_INTERNAL_URL = 'http://web:8000'

# Limite por IP real do chamador. O tráfego chega pelo Cloudflare: sem o cabeçalho, todos os clientes
# cairiam num balde só. O limite de verdade continua sendo o AgentRateThrottle (por agente) no ERP.
RATELIMIT_MEDIA_POR_MINUTO = 120
RATELIMIT_RAJADA = 60
CABECALHO_IP = 'CF-Connecting-IP'


def nome_servico(slug):
    return f'{slug}_mcp'


def modulo_contratado(cliente):
    if getattr(cliente, 'pk', None) is None:
        return False
    return cliente.modulos_ativos.filter(slug=MODULO).exists()


def replicas_desejadas(cliente):
    """1 com o módulo ``agents`` e o cliente fora de ``STATUS_FORA_DO_AR`` (mesma regra do cron); senão 0."""
    from .models import Cliente
    if cliente.status in Cliente.STATUS_FORA_DO_AR:
        return 0
    return 1 if modulo_contratado(cliente) else 0


def hosts(cliente, lista_dominios=None):
    """Hosts aceitos pelo MCP e com router ``/mcp``: os mesmos que servem o ERP, sem ``web``."""
    return dominios.hosts_servidos(cliente, lista_dominios)


def env(cliente, versao, lista_dominios=None):
    return {
        'ERP_INTERNAL_URL': ERP_INTERNAL_URL,
        'MCP_PUBLIC_URL': f'https://{cliente.subdominio}/mcp',
        'MCP_ALLOWED_HOSTS': ','.join(hosts(cliente, lista_dominios)),
        'MCP_PORT': str(PORTA),
        'ERP_VERSION': versao,
    }


def prefixos_gerenciados(slug):
    return (
        f'traefik.http.routers.{slug}-mcp',
        f'traefik.http.middlewares.{slug}-mcp-',
        f'traefik.http.services.{slug}-mcp',
    )


def labels_traefik(cliente, lista_dominios=None, habilitado=True):
    """Labels (valor bruto) do ``<slug>_mcp``: um router por host servido, service e limite de taxa."""
    slug = cliente.slug
    servico = f'{slug}-mcp'
    ratelimit = f'{slug}-mcp-ratelimit'
    labels = {'traefik.enable': 'true' if habilitado else 'false'}
    for host in hosts(cliente, lista_dominios):
        nome = servico if host == cliente.subdominio else f'{servico}-custom-{dominios.chave(host)}'
        base = f'traefik.http.routers.{nome}'
        labels[f'{base}.rule'] = f'Host(`{host}`) && PathPrefix(`/mcp`)'
        labels[f'{base}.entrypoints'] = 'web'
        labels[f'{base}.service'] = servico
        labels[f'{base}.middlewares'] = ratelimit
    labels[f'traefik.http.services.{servico}.loadbalancer.server.port'] = str(PORTA)
    base = f'traefik.http.middlewares.{ratelimit}.ratelimit'
    labels[f'{base}.average'] = str(RATELIMIT_MEDIA_POR_MINUTO)
    labels[f'{base}.period'] = '1m'
    labels[f'{base}.burst'] = str(RATELIMIT_RAJADA)
    labels[f'{base}.sourcecriterion.requestheadername'] = CABECALHO_IP
    return labels


def args_reconciliar(slug, atuais, desejadas):
    """``--label-rm``/``--label-add`` que levam as labels do MCP de ``atuais`` a ``desejadas``."""
    return dominios.args_reconciliar(slug, atuais, desejadas, prefixos=prefixos_gerenciados(slug))
