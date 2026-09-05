from unittest.mock import patch

from django.test import TestCase

from registry.models import VersaoAgente


def _fake_release(tag, draft=False, prerelease=False, has_assets=True):
    versao = tag.lstrip("v")
    assets = []
    if has_assets:
        assets = [
            {
                "name": f"pdv-local-{tag}.zip",
                "browser_download_url": f"https://example.invalid/{tag}/pdv-local-{tag}.zip",
            },
            {
                "name": f"pdv-local-{tag}.zip.sha256",
                "browser_download_url": f"https://example.invalid/{tag}/pdv-local-{tag}.zip.sha256",
            },
        ]
    return {
        "tag_name": tag,
        "draft": draft,
        "prerelease": prerelease,
        "body": f"Notas da {versao}",
        "assets": assets,
    }


class _FakeResponse:
    def __init__(self, json_data=None, text_data=""):
        self._json_data = json_data
        self.text = text_data

    def raise_for_status(self):
        pass

    def json(self):
        return self._json_data


class SyncVersoesFromGithubTests(TestCase):
    """registry.github_releases.sync_versoes_from_github -- varredura dos
    Releases do GitHub que alimenta o botao "Verificar novas versoes" na
    tela Clientes > Versoes do Agente. requests.get mockado -- nunca bate na
    API real do GitHub num teste."""

    def test_creates_versoes_for_each_release_with_zip_and_sha256(self):
        releases = [_fake_release("v1.6.0"), _fake_release("v1.5.1")]

        def fake_get(url, *args, **kwargs):
            if url.endswith("/releases"):
                return _FakeResponse(json_data=releases)
            return _FakeResponse(text_data=f"{'B' * 64}  pdv-local-v1.6.0.zip")

        with patch("registry.github_releases.requests.get", side_effect=fake_get):
            from registry.github_releases import sync_versoes_from_github
            resultado = sync_versoes_from_github()

        self.assertCountEqual(resultado["criadas"], ["1.6.0", "1.5.1"])
        self.assertEqual(VersaoAgente.objects.count(), 2)
        v160 = VersaoAgente.objects.get(versao="1.6.0")
        self.assertEqual(v160.sha256, "B" * 64)
        self.assertEqual(v160.release_notes, "Notas da 1.6.0")

    def test_skips_draft_and_prerelease(self):
        releases = [_fake_release("v1.7.0", draft=True), _fake_release("v1.6.0-beta", prerelease=True)]

        def fake_get(url, *args, **kwargs):
            return _FakeResponse(json_data=releases)

        with patch("registry.github_releases.requests.get", side_effect=fake_get):
            from registry.github_releases import sync_versoes_from_github
            resultado = sync_versoes_from_github()

        self.assertEqual(resultado["criadas"], [])
        self.assertEqual(VersaoAgente.objects.count(), 0)

    def test_ignores_release_without_zip_asset(self):
        releases = [_fake_release("v1.6.0", has_assets=False)]

        def fake_get(url, *args, **kwargs):
            return _FakeResponse(json_data=releases)

        with patch("registry.github_releases.requests.get", side_effect=fake_get):
            from registry.github_releases import sync_versoes_from_github
            resultado = sync_versoes_from_github()

        self.assertEqual(resultado["criadas"], [])
        self.assertEqual(len(resultado["ignoradas"]), 1)
        self.assertEqual(resultado["ignoradas"][0][0], "1.6.0")

    def test_does_not_overwrite_manually_set_erp_minimo_on_resync(self):
        VersaoAgente.objects.create(
            versao="1.6.0", download_url="http://old/1.6.0.zip", sha256="A" * 64, erp_minimo="0.0.90",
        )
        releases = [_fake_release("v1.6.0")]

        def fake_get(url, *args, **kwargs):
            if url.endswith("/releases"):
                return _FakeResponse(json_data=releases)
            return _FakeResponse(text_data=f"{'C' * 64}  pdv-local-v1.6.0.zip")

        with patch("registry.github_releases.requests.get", side_effect=fake_get):
            from registry.github_releases import sync_versoes_from_github
            resultado = sync_versoes_from_github()

        self.assertEqual(resultado["atualizadas"], ["1.6.0"])
        versao_agente = VersaoAgente.objects.get(versao="1.6.0")
        self.assertEqual(versao_agente.sha256, "C" * 64)  # atualizou o que o GitHub sabe
        self.assertEqual(versao_agente.erp_minimo, "0.0.90")  # preservou o que so o admin sabe
