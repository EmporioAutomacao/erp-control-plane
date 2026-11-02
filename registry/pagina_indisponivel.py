"""Página do catch-all do Traefik: responde por qualquer host que não tem roteador.

O Traefik reescreve todo caminho desses hosts para ROTA (`replacepath` em `stack-prod.yml`), então
domínio desconhecido nunca alcança o admin. O middleware vem antes de todos os outros porque o host
não está no ALLOWED_HOSTS (o CommonMiddleware responderia 400), e não toca em banco nem sessão:
robô varrendo domínio não pode pesar no painel. Doc: docs/registry/pagina-indisponivel.md
"""
from functools import lru_cache

from django.http import HttpResponseNotFound
from django.template.loader import render_to_string

ROTA = '/_indisponivel/'


@lru_cache(maxsize=1)
def _html():
    return render_to_string('registry/pagina_indisponivel.html')


class PaginaIndisponivelMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.path != ROTA:
            return self.get_response(request)
        resposta = HttpResponseNotFound(_html())
        resposta['Cache-Control'] = 'no-store'
        return resposta
