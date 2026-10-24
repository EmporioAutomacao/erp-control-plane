"""
Relay do callback OAuth da API de Integracao do Arpa (Arpa Salesforce).

O portal do desenvolvedor do Arpa aceita **um unico** `redirect_uri` por
aplicativo e exige que ele seja identico em toda autorizacao. Cada cliente da
plataforma tem o ERP num endereco proprio (`<slug>.ararasuite.com.br`), entao o
`redirect_uri` cadastrado no Arpa aponta para o CP, que so repassa:

    Arpa --302--> https://<CP>/integracoes/arpa/oauth/callback/?code=...&state=<cliente>.<nonce>
    CP   --302--> https://<subdominio do cliente>/arpa/oauth/retorno/?code=...&state=...

`<cliente>` e o `CP_CLIENTE_ID` (UUID do `Cliente`, que o ERP ja recebe por
env no provisionamento) ou o slug do cliente.

Quem troca o `code` pelo token e o ERP do cliente (envia este mesmo
`redirect_uri` no POST /oauth/token e confere o nonce que guardou na sessao).
O CP nao conhece `client_secret` nem tokens e nao grava/loga o `code`.

Seguranca:
- o destino e sempre `site_url_cliente()` de um `Cliente` cadastrado e ativo,
  achado pelo slug do `state` -- nunca uma URL vinda da querystring (sem open
  redirect);
- `state` fora do formato `<slug>.<nonce>` ou slug desconhecido/inativo -> pagina
  de erro, sem redirecionar.

Plano do lado ERP: docs/modulos/arpa/escopo-v1.md (decisao D5) no repo `erp`.
"""

import re
import uuid
from urllib.parse import urlencode

from django.http import HttpResponseRedirect
from django.shortcuts import render
from django.views.decorators.http import require_GET

from .models import Cliente
from .provisioning import site_url_cliente

# Rota do ERP que recebe o repasse (app `arpa_pedidos`, repo `erp`).
ERP_RETORNO_PATH = '/arpa/oauth/retorno/'

# Status em que a instancia do ERP esta no ar para receber o retorno.
STATUS_ACEITOS = ('ativo', 'trial', 'atualizando')

# <cliente>.<nonce>: cliente = UUID (CP_CLIENTE_ID) ou slug no formato do
# SlugField; nonce url-safe (secrets.token_urlsafe).
STATE_RE = re.compile(r'^(?P<cliente>[-a-zA-Z0-9_]{1,50})\.(?P<nonce>[A-Za-z0-9_-]{8,128})$')

# Parametros do Arpa repassados ao ERP. `error`/`error_description` cobrem o
# usuario clicar em "Cancelar" na tela de autorizacao (padrao OAuth2).
PARAMS_REPASSADOS = ('code', 'state', 'error', 'error_description')
TAMANHO_MAXIMO_PARAMETRO = 2048


def _filtro_cliente(identificador):
    """UUID valido -> busca por id (o ERP so conhece o CP_CLIENTE_ID); senao, por slug."""
    try:
        return {'id': uuid.UUID(identificador)}
    except ValueError:
        return {'slug': identificador}


def _erro(request, mensagem, status):
    response = render(
        request,
        'registry/arpa_oauth_erro.html',
        {'mensagem': mensagem},
        status=status,
    )
    response['Cache-Control'] = 'no-store'
    response['Referrer-Policy'] = 'no-referrer'
    return response


@require_GET
def arpa_oauth_callback(request):
    state = (request.GET.get('state') or '').strip()
    match = STATE_RE.match(state)
    if not match:
        return _erro(request, 'O retorno do Arpa veio sem identificacao valida do cliente.', 400)

    code = (request.GET.get('code') or '').strip()
    error = (request.GET.get('error') or '').strip()
    if not code and not error:
        return _erro(request, 'O retorno do Arpa veio sem o codigo de autorizacao.', 400)

    cliente = Cliente.objects.filter(
        **_filtro_cliente(match.group('cliente')), status__in=STATUS_ACEITOS,
    ).only('slug', 'subdominio').first()
    if cliente is None:
        return _erro(request, 'Cliente nao encontrado ou inativo na plataforma.', 404)

    params = {}
    for nome in PARAMS_REPASSADOS:
        valor = (request.GET.get(nome) or '').strip()
        if not valor:
            continue
        if len(valor) > TAMANHO_MAXIMO_PARAMETRO:
            return _erro(request, 'O retorno do Arpa veio com parametros invalidos.', 400)
        params[nome] = valor

    destino = f"{site_url_cliente(cliente)}{ERP_RETORNO_PATH}?{urlencode(params)}"
    response = HttpResponseRedirect(destino)
    response['Cache-Control'] = 'no-store'
    response['Referrer-Policy'] = 'no-referrer'
    return response
