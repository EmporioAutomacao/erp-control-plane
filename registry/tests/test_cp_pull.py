from unittest.mock import patch

import requests
from django.test import Client, TestCase
from django.utils import timezone

from registry.cp_pull import ColetorVersoes
from registry.models import Cliente, InstalacaoAgente, Plano
from registry.tasks import task_coletar_versoes_cliente, task_coletar_versoes_todas


class _FakeResp:
    def __init__(self, status_code, json_data=None, text=""):
        self.status_code = status_code
        self.ok = status_code < 400
        self.text = text or (str(json_data) if json_data is not None else "")
        self._json = json_data

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json


class _Base(TestCase):
    def setUp(self):
        self.plano = Plano.objects.create(slug="p", nome="Plano", preco_mensal="0.00")
        self._seq = 0

    def _cliente(self, slug="loja", *, secret="segredo-abc", status="ativo", **extra):
        self._seq += 1
        return Cliente.objects.create(
            slug=slug, nome=slug, cnpj=f"00.000.000/{self._seq:04d}-00",
            email_contato=f"{slug}@example.invalid",
            subdominio=f"{slug}.ararasuite.com.br",
            plano=self.plano, status=status,
            integracao_secret=secret, versao_erp="0.0.100",
            **extra,
        )


class ColetarSucessoTests(_Base):
    def _resp_ok(self, instalacoes):
        return _FakeResp(200, {"status": "ok", "erp_version": "0.0.109", "instalacoes": instalacoes})

    def test_grava_versao_erp_e_reconcilia_instalacoes(self):
        cliente = self._cliente()
        InstalacaoAgente.objects.create(cliente=cliente, instance_id="velha", agent_version="1.0.0")

        payload = [
            {"instance_id": "loja-01", "installation_label": "Centro", "agent_version": "1.6.12",
             "last_seen_at": timezone.now().isoformat(), "active": True, "connectivity": "online"},
            {"instance_id": "loja-02", "installation_label": "Sul", "agent_version": "1.5.0",
             "last_seen_at": None, "active": False, "connectivity": "offline"},
        ]
        with patch("requests.get", return_value=self._resp_ok(payload)) as mock_get:
            res = ColetorVersoes(cliente).coletar()

        args, kwargs = mock_get.call_args
        self.assertEqual(args[0], "https://loja.ararasuite.com.br/v1/cp/status")
        self.assertEqual(kwargs["params"], {"cliente_id": str(cliente.id)})
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer segredo-abc")

        cliente.refresh_from_db()
        self.assertEqual(cliente.versao_erp_detectada, "0.0.109")
        self.assertEqual(cliente.deteccao_versoes_erro, "")
        self.assertIsNotNone(cliente.versoes_detectadas_em)
        self.assertTrue(res["ok"])

        ids = set(cliente.instalacoes_agente.values_list("instance_id", flat=True))
        self.assertEqual(ids, {"loja-01", "loja-02"})  # "velha" sumiu (full-sync)
        sul = cliente.instalacoes_agente.get(instance_id="loja-02")
        self.assertEqual(sul.agent_version, "1.5.0")
        self.assertFalse(sul.active)

    def test_payload_vazio_apaga_todas(self):
        cliente = self._cliente()
        InstalacaoAgente.objects.create(cliente=cliente, instance_id="x", agent_version="1.0.0")
        with patch("requests.get", return_value=self._resp_ok([])):
            ColetorVersoes(cliente).coletar()
        self.assertEqual(cliente.instalacoes_agente.count(), 0)


class ColetarFalhaTests(_Base):
    def test_404_faz_fallback_health(self):
        cliente = self._cliente()
        with patch("requests.get", return_value=_FakeResp(404, text="not found")), \
             patch("registry.cp_pull.urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.return_value.__enter__.return_value.read.return_value = \
                b'{"status":"ok","version":"0.0.107"}'
            res = ColetorVersoes(cliente).coletar()
        cliente.refresh_from_db()
        self.assertEqual(cliente.versao_erp_detectada, "0.0.107")
        self.assertIn("/v1/cp/status", cliente.deteccao_versoes_erro)
        self.assertFalse(res["ok"])

    def test_401_registra_erro(self):
        cliente = self._cliente()
        with patch("requests.get", return_value=_FakeResp(401, {"error": "unauthorized"})):
            ColetorVersoes(cliente).coletar()
        cliente.refresh_from_db()
        self.assertIn("401", cliente.deteccao_versoes_erro)

    def test_409_cliente_id_mismatch(self):
        cliente = self._cliente()
        with patch("requests.get", return_value=_FakeResp(409, {"error": "cliente_id_mismatch"})):
            ColetorVersoes(cliente).coletar()
        cliente.refresh_from_db()
        self.assertIn("cliente_id_mismatch", cliente.deteccao_versoes_erro)

    def test_timeout_nao_estoura(self):
        cliente = self._cliente()
        with patch("requests.get", side_effect=requests.exceptions.Timeout()):
            res = ColetorVersoes(cliente).coletar()
        cliente.refresh_from_db()
        self.assertIn("timeout", cliente.deteccao_versoes_erro.lower())
        self.assertFalse(res["ok"])

    def test_connection_error_nao_estoura(self):
        cliente = self._cliente()
        with patch("requests.get", side_effect=requests.exceptions.ConnectionError()):
            ColetorVersoes(cliente).coletar()
        cliente.refresh_from_db()
        self.assertIn("conectar", cliente.deteccao_versoes_erro.lower())

    def test_sem_segredo_tenta_so_health(self):
        cliente = self._cliente(secret="")
        with patch("registry.cp_pull.urllib.request.urlopen") as mock_urlopen, \
             patch("requests.get") as mock_get:
            mock_urlopen.return_value.__enter__.return_value.read.return_value = \
                b'{"status":"ok","version":"0.0.105"}'
            ColetorVersoes(cliente).coletar()
        mock_get.assert_not_called()
        cliente.refresh_from_db()
        self.assertEqual(cliente.versao_erp_detectada, "0.0.105")
        self.assertIn("segredo de integração", cliente.deteccao_versoes_erro)


class TasksTests(_Base):
    def test_task_cliente(self):
        cliente = self._cliente()
        with patch("registry.cp_pull.ColetorVersoes.coletar") as m:
            task_coletar_versoes_cliente(str(cliente.pk))
        m.assert_called_once()

    def test_task_todas_so_ativos_trial(self):
        ativo = self._cliente("ativa", status="ativo")
        self._cliente("suspensa", status="suspenso")
        with patch("registry.tasks.task_coletar_versoes_cliente.delay") as m:
            task_coletar_versoes_todas()
        chamados = {c.args[0] for c in m.call_args_list}
        self.assertEqual(chamados, {str(ativo.pk)})


class AdminViewTests(_Base):
    def setUp(self):
        super().setUp()
        from django.contrib.auth.models import User
        self.admin = User.objects.create_superuser("admin", "a@a.invalid", "x")
        self.client = Client()
        self.client.force_login(self.admin)

    def test_botao_dispara_task_e_redireciona(self):
        cliente = self._cliente()
        with patch("registry.tasks.task_coletar_versoes_cliente.delay") as m:
            resp = self.client.get(f"/admin/registry/cliente/{cliente.pk}/coletar-versoes/")
        m.assert_called_once_with(str(cliente.pk))
        self.assertEqual(resp.status_code, 302)
        self.assertIn("coletando=1", resp["Location"])

    def test_change_page_renderiza_painel_versao_atual(self):
        cliente = self._cliente()
        cliente.versao_erp_detectada = "0.0.109"
        cliente.deteccao_versoes_erro = "erro de teste"
        cliente.versoes_detectadas_em = timezone.now()
        cliente.save()
        InstalacaoAgente.objects.create(
            cliente=cliente, instance_id="loja-01", installation_label="Centro",
            agent_version="1.6.12", last_seen_at=timezone.now(),
            active=True, connectivity="online",
        )
        resp = self.client.get(f"/admin/registry/cliente/{cliente.pk}/change/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Verificar versões agora")
        self.assertContains(resp, "1.6.12")
        self.assertContains(resp, "mesma release do SyncAgent")
