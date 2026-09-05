"""
Varre os Releases do GitHub do repo `pdv-local` e atualiza o catalogo mestre
de versoes do SyncAgent/PDV (VersaoAgente).

Alternativa ao push vindo do CI (registry/release_api.py): em vez de alguem
(ou o workflow de release) ter que avisar o CP toda vez que uma versao nova
sai, o CP mesmo verifica sob demanda -- botao "Verificar novas versoes" na
tela Clientes > Versoes do Agente.

So popula o CATALOGO (global) -- nao permite a versao pra nenhum cliente
sozinho, mesmo comportamento do release_api.py.
"""

import requests
from django.conf import settings

from .release_api import upsert_versao_agente

GITHUB_API_BASE = "https://api.github.com"


def _repo():
    return getattr(settings, "PDV_LOCAL_GITHUB_REPO", "EmporioAutomacao/pdv-local")


def fetch_github_releases(per_page=30):
    url = f"{GITHUB_API_BASE}/repos/{_repo()}/releases"
    headers = {"Accept": "application/vnd.github+json"}
    token = (getattr(settings, "GITHUB_TOKEN", "") or "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"

    response = requests.get(url, params={"per_page": per_page}, headers=headers, timeout=20)
    response.raise_for_status()
    return response.json()


def _find_asset(assets, suffix):
    for asset in assets:
        if asset.get("name", "").endswith(suffix):
            return asset
    return None


def _fetch_sha256_content(asset):
    response = requests.get(asset["browser_download_url"], timeout=20)
    response.raise_for_status()
    # Conteudo tipico do arquivo .sha256: "HASH  nome-do-arquivo.zip"
    primeira_palavra = response.text.strip().split()
    if not primeira_palavra:
        raise ValueError("arquivo .sha256 vazio")
    return primeira_palavra[0].upper()


def sync_versoes_from_github(per_page=30):
    """Varre os releases do GitHub e faz upsert de VersaoAgente pra cada um
    que tiver um pacote `pdv-local-vX.Y.Z.zip` + `.zip.sha256` publicados.
    Ignora rascunhos e pre-releases. Nunca apaga nada do catalogo -- so
    cria/atualiza. `erp_minimo` de uma versao ja cadastrada nunca e mexido
    aqui (o GitHub nao tem essa informacao; fica so no controle manual do
    admin)."""
    releases = fetch_github_releases(per_page=per_page)

    criadas = []
    atualizadas = []
    ignoradas = []

    for release in releases:
        if release.get("draft") or release.get("prerelease"):
            continue

        tag = release.get("tag_name") or ""
        versao = tag[1:] if tag.startswith("v") else tag
        if not versao:
            continue

        assets = release.get("assets", [])
        zip_asset = _find_asset(assets, ".zip")
        sha_asset = _find_asset(assets, ".zip.sha256")

        if not zip_asset or not sha_asset:
            ignoradas.append((versao, "pacote .zip ou .zip.sha256 ausente neste release"))
            continue

        try:
            sha256 = _fetch_sha256_content(sha_asset)
        except Exception as exc:
            ignoradas.append((versao, f"falha ao ler o .sha256: {exc}"))
            continue

        try:
            _, created = upsert_versao_agente(
                versao=versao,
                download_url=zip_asset["browser_download_url"],
                sha256=sha256,
                release_notes=(release.get("body") or "")[:2000],
                # erp_minimo intencionalmente omitido (None) -- upsert_versao_agente
                # preserva o valor ja cadastrado manualmente, se houver.
            )
        except Exception as exc:
            ignoradas.append((versao, str(exc)))
            continue

        (criadas if created else atualizadas).append(versao)

    return {"criadas": criadas, "atualizadas": atualizadas, "ignoradas": ignoradas}
