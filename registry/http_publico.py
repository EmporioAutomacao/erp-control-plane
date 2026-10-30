"""Requisições do CP ao endereço público dos ERPs dos clientes (`https://<subdominio>/...`).

O Cloudflare responde 403 ao User-Agent padrão do urllib (`Python-urllib/x.y`). Sem um
User-Agent próprio, todo `/health/` público falha: a verificação de saúde dava todos os
clientes offline e a migração de servidor terminava em "exige intervenção" esperando um 200
que nunca vinha. Toda chamada ao endereço público de um cliente passa por aqui.
"""
import urllib.request


def user_agent():
    from django.conf import settings
    return f'AraraSuite-CP/{settings.VERSION} (health-check)'


def abrir(url, timeout=10):
    """`urllib.request.urlopen` com o User-Agent do CP. Mesmas exceções (HTTPError, URLError...)."""
    return urllib.request.urlopen(urllib.request.Request(url, headers={'User-Agent': user_agent()}), timeout=timeout)
