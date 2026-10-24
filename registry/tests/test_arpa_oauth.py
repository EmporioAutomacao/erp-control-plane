from urllib.parse import parse_qs, urlsplit

from django.test import TestCase

from registry.models import Cliente, Plano


class ArpaOauthCallbackTests(TestCase):
    """GET /integracoes/arpa/oauth/callback/ -- redirect_uri unico do app da
    Arara Suite no portal Arpa. So repassa code/state ao ERP do cliente achado
    pelo slug do state; nunca redireciona para URL vinda da querystring."""

    URL = "/integracoes/arpa/oauth/callback/"
    NONCE = "Q2hhdmUtZGUtdGVzdGUtMTIz"

    def setUp(self):
        self.plano = Plano.objects.create(slug="p", nome="Plano", preco_mensal="0.00")
        self._seq = 0

    def _cliente(self, slug="loja", *, status="ativo", **extra):
        self._seq += 1
        return Cliente.objects.create(
            slug=slug, nome=slug, cnpj=f"00.000.000/{self._seq:04d}-00",
            email_contato=f"{slug}@example.invalid",
            subdominio=f"{slug}.ararasuite.com.br",
            plano=self.plano, status=status, versao_erp="0.0.100",
            **extra,
        )

    def _state(self, slug="loja"):
        return f"{slug}.{self.NONCE}"

    def _destino(self, response):
        partes = urlsplit(response["Location"])
        return partes, {k: v[0] for k, v in parse_qs(partes.query).items()}

    def test_repassa_code_e_state_para_o_erp_do_cliente(self):
        self._cliente()
        response = self.client.get(self.URL, {"code": "QC8gIz", "state": self._state()})

        self.assertEqual(response.status_code, 302)
        partes, params = self._destino(response)
        self.assertEqual(partes.scheme, "https")
        self.assertEqual(partes.netloc, "loja.ararasuite.com.br")
        self.assertEqual(partes.path, "/arpa/oauth/retorno/")
        self.assertEqual(params, {"code": "QC8gIz", "state": self._state()})
        self.assertEqual(response["Cache-Control"], "no-store")
        self.assertEqual(response["Referrer-Policy"], "no-referrer")

    def test_aceita_id_do_cliente_no_state(self):
        cliente = self._cliente()
        state = f"{cliente.id}.{self.NONCE}"
        response = self.client.get(self.URL, {"code": "abc", "state": state})

        self.assertEqual(response.status_code, 302)
        partes, params = self._destino(response)
        self.assertEqual(partes.netloc, "loja.ararasuite.com.br")
        self.assertEqual(params["state"], state)

    def test_id_de_cliente_inexistente_nao_redireciona(self):
        self._cliente()
        state = f"00000000-0000-0000-0000-000000000000.{self.NONCE}"
        response = self.client.get(self.URL, {"code": "abc", "state": state})

        self.assertEqual(response.status_code, 404)

    def test_usa_subdominio_mesmo_com_dominio_proprio(self):
        self._cliente(dominio_custom="loja.com.br")
        response = self.client.get(self.URL, {"code": "abc", "state": self._state()})

        self.assertEqual(urlsplit(response["Location"]).netloc, "loja.ararasuite.com.br")

    def test_repassa_erro_quando_usuario_cancela(self):
        self._cliente()
        response = self.client.get(
            self.URL,
            {"error": "access_denied", "error_description": "Usuario cancelou", "state": self._state()},
        )

        self.assertEqual(response.status_code, 302)
        _, params = self._destino(response)
        self.assertEqual(
            params,
            {"error": "access_denied", "error_description": "Usuario cancelou", "state": self._state()},
        )

    def test_nao_repassa_parametros_desconhecidos(self):
        self._cliente()
        response = self.client.get(
            self.URL,
            {"code": "abc", "state": self._state(), "next": "https://evil.example/"},
        )

        _, params = self._destino(response)
        self.assertNotIn("next", params)

    def test_codifica_code_com_caracteres_especiais(self):
        self._cliente()
        response = self.client.get(self.URL, {"code": "a&b=c/d", "state": self._state()})

        _, params = self._destino(response)
        self.assertEqual(params["code"], "a&b=c/d")

    def test_aceita_trial_e_atualizando(self):
        for status in ("trial", "atualizando"):
            with self.subTest(status=status):
                self._cliente(slug=f"c-{status}", status=status)
                response = self.client.get(self.URL, {"code": "abc", "state": self._state(f"c-{status}")})
                self.assertEqual(response.status_code, 302)

    def test_cliente_inativo_nao_redireciona(self):
        for status in ("suspenso", "cancelado", "trial_expirado", "aguardando_provisao", "erro_provisao"):
            with self.subTest(status=status):
                self._cliente(slug=f"c-{status}", status=status)
                response = self.client.get(self.URL, {"code": "abc", "state": self._state(f"c-{status}")})
                self.assertEqual(response.status_code, 404)
                self.assertFalse(response.has_header("Location"))

    def test_slug_desconhecido_nao_redireciona(self):
        response = self.client.get(self.URL, {"code": "abc", "state": self._state("nao-existe")})

        self.assertEqual(response.status_code, 404)
        self.assertContains(response, "Conectar ao Arpa", status_code=404)

    def test_state_invalido_nao_redireciona(self):
        self._cliente()
        invalidos = [
            "",
            "loja",                                   # sem nonce
            "loja.",                                  # nonce vazio
            "loja.curto",                             # nonce curto demais
            f"loja.{self.NONCE}&x=1",                 # caractere fora do formato
            f"https://evil.example/.{self.NONCE}",    # tentativa de URL no slug
            f"{'a' * 51}.{self.NONCE}",               # slug longo demais
        ]
        for state in invalidos:
            with self.subTest(state=state):
                response = self.client.get(self.URL, {"code": "abc", "state": state})
                self.assertEqual(response.status_code, 400)
                self.assertFalse(response.has_header("Location"))

    def test_sem_code_e_sem_error_nao_redireciona(self):
        self._cliente()
        response = self.client.get(self.URL, {"state": self._state()})

        self.assertEqual(response.status_code, 400)

    def test_parametro_grande_demais_nao_redireciona(self):
        self._cliente()
        response = self.client.get(self.URL, {"code": "x" * 3000, "state": self._state()})

        self.assertEqual(response.status_code, 400)

    def test_somente_get(self):
        self._cliente()
        response = self.client.post(self.URL, {"code": "abc", "state": self._state()})

        self.assertEqual(response.status_code, 405)
