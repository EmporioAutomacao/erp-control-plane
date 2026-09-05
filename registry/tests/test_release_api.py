import json

from django.test import TestCase, override_settings

from registry.models import VersaoAgente

TOKEN = "test-release-token"


class RegisterPdvLocalReleaseTests(TestCase):
    """POST /v1/releases/pdv-local:register -- chamado pelo workflow de
    release do repo pdv-local (GitHub Actions), autenticado por
    PDV_LOCAL_RELEASE_TOKEN (nao o Cliente.integracao_secret de ninguem)."""

    URL = "/v1/releases/pdv-local:register"

    def _post(self, payload, token=None):
        headers = {"HTTP_AUTHORIZATION": f"Bearer {token}"} if token else {}
        return self.client.post(self.URL, data=json.dumps(payload), content_type="application/json", **headers)

    @override_settings(PDV_LOCAL_RELEASE_TOKEN=TOKEN)
    def test_requires_bearer_token(self):
        response = self._post({"versao": "1.6.0", "download_url": "http://x/1.6.0.zip", "sha256": "A" * 64})
        self.assertEqual(response.status_code, 401)

    @override_settings(PDV_LOCAL_RELEASE_TOKEN=TOKEN)
    def test_rejects_wrong_token(self):
        response = self._post(
            {"versao": "1.6.0", "download_url": "http://x/1.6.0.zip", "sha256": "A" * 64},
            token="token-errado",
        )
        self.assertEqual(response.status_code, 401)

    @override_settings(PDV_LOCAL_RELEASE_TOKEN="")
    def test_rejects_when_token_not_configured(self):
        response = self._post(
            {"versao": "1.6.0", "download_url": "http://x/1.6.0.zip", "sha256": "A" * 64},
            token="qualquer-coisa",
        )
        self.assertEqual(response.status_code, 401)

    @override_settings(PDV_LOCAL_RELEASE_TOKEN=TOKEN)
    def test_creates_versao_agente_without_permitting_for_any_client(self):
        response = self._post(
            {
                "versao": "1.6.0",
                "download_url": "https://github.com/EmporioAutomacao/pdv-local/releases/download/v1.6.0/pdv-local-v1.6.0.zip",
                "sha256": "725AD63205D4AFFE15EE580F8ED77BE2B93B1E5BA8FCCDF847B455620AAA2504",
                "release_notes": "Curadoria de versoes por cliente",
            },
            token=TOKEN,
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "ok")
        self.assertTrue(body["created"])

        versao_agente = VersaoAgente.objects.get(versao="1.6.0")
        self.assertTrue(versao_agente.ativo)
        self.assertEqual(versao_agente.clientes.count(), 0)  # ninguem permitido automaticamente

    @override_settings(PDV_LOCAL_RELEASE_TOKEN=TOKEN)
    def test_upserts_existing_version_instead_of_duplicating(self):
        VersaoAgente.objects.create(
            versao="1.6.0", download_url="http://old/1.6.0.zip", sha256="A" * 64, release_notes="antigo",
        )

        response = self._post(
            {"versao": "1.6.0", "download_url": "http://novo/1.6.0.zip", "sha256": "B" * 64, "release_notes": "novo"},
            token=TOKEN,
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["created"])
        self.assertEqual(VersaoAgente.objects.filter(versao="1.6.0").count(), 1)
        versao_agente = VersaoAgente.objects.get(versao="1.6.0")
        self.assertEqual(versao_agente.download_url, "http://novo/1.6.0.zip")
        self.assertEqual(versao_agente.release_notes, "novo")

    @override_settings(PDV_LOCAL_RELEASE_TOKEN=TOKEN)
    def test_rejects_invalid_sha256(self):
        response = self._post(
            {"versao": "1.6.0", "download_url": "http://x/1.6.0.zip", "sha256": "nao-e-um-hash"},
            token=TOKEN,
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "invalid_sha256")

    @override_settings(PDV_LOCAL_RELEASE_TOKEN=TOKEN)
    def test_rejects_missing_required_fields(self):
        response = self._post({"sha256": "A" * 64}, token=TOKEN)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "invalid_payload")
