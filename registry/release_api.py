"""
Endpoint chamado pelo workflow de release do repo `pdv-local` (GitHub
Actions), logo depois de publicar um Release novo: registra a versao
automaticamente no catalogo mestre do CP (`VersaoAgente`), sem precisar
cadastrar na mao em Clientes > Versoes do Agente toda vez que sai uma versao.

So popula o CATALOGO (global) -- NAO permite a versao pra nenhum cliente
sozinho. Permitir continua sendo uma decisao manual, por cliente, em
Cliente > Versoes do SyncAgent/PDV (o proposito de existir a curadoria).

Canal separado do push CP->erp (registry/cp_push.py, sentido oposto): aqui a
origem e o CI do pdv-local, autenticado por PDV_LOCAL_RELEASE_TOKEN (nao o
Cliente.integracao_secret de nenhum cliente especifico).
"""

import hmac
import json

from django.conf import settings
from django.core.exceptions import ValidationError
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .models import VersaoAgente, sha256_hex_validator


def upsert_versao_agente(versao, download_url, sha256, release_notes="", erp_minimo=None):
    """Cria ou atualiza uma VersaoAgente pelo numero da versao. Reusado pelo
    endpoint HTTP, pelo comando de management `register_versao_agente` e pela
    varredura de Releases do GitHub (`registry.github_releases`).

    `erp_minimo=None` (padrao) significa "nao mexer" -- preserva o que ja
    estiver cadastrado na versao existente (ou deixa em branco, se for uma
    versao nova). So sobrescreve quando o chamador passar uma string
    explicita (mesmo que vazia, pra limpar de proposito)."""
    versao = str(versao or "").strip()
    download_url = str(download_url or "").strip()
    sha256 = str(sha256 or "").strip().upper()

    if not versao:
        raise ValueError("versao e obrigatoria")
    if not download_url:
        raise ValueError("download_url e obrigatoria")

    sha256_hex_validator(sha256)  # levanta ValidationError se invalido

    defaults = dict(
        download_url=download_url,
        sha256=sha256,
        release_notes=str(release_notes or ""),
        ativo=True,
    )
    if erp_minimo is not None:
        defaults["erp_minimo"] = str(erp_minimo)

    return VersaoAgente.objects.update_or_create(versao=versao, defaults=defaults)


@csrf_exempt
@require_POST
def register_pdv_local_release(request):
    provided = _extract_bearer(request)
    expected = (getattr(settings, "PDV_LOCAL_RELEASE_TOKEN", "") or "").strip()
    if not expected or not provided or not hmac.compare_digest(provided, expected):
        return JsonResponse({"error": "unauthorized"}, status=401)

    try:
        payload = json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "invalid_json"}, status=400)

    if not isinstance(payload, dict):
        return JsonResponse({"error": "invalid_payload"}, status=400)

    try:
        versao_agente, created = upsert_versao_agente(
            versao=payload.get("versao"),
            download_url=payload.get("download_url"),
            sha256=payload.get("sha256"),
            release_notes=payload.get("release_notes"),
            erp_minimo=payload.get("erp_minimo"),
        )
    except ValueError as exc:
        return JsonResponse({"error": "invalid_payload", "detail": str(exc)}, status=400)
    except ValidationError as exc:
        return JsonResponse({"error": "invalid_sha256", "detail": str(exc)}, status=400)

    return JsonResponse({
        "status": "ok",
        "versao": versao_agente.versao,
        "created": created,
    })


def _extract_bearer(request):
    header = request.headers.get("Authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer":
        return None
    token = token.strip()
    return token or None
