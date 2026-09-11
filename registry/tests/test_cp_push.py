from types import SimpleNamespace
from unittest.mock import patch

import requests
from django.contrib.auth.models import User
from django.test import Client, TestCase

from registry.cp_push import SincronizadorVersoes
from registry.models import Cliente, Plano, SincronizacaoVersoesAgente, VersaoAgente
from registry.tasks import task_sincronizar_versoes_agente

SHA = "a" * 64


class _FakeResp:
    def __init__(self, status_code, json_data=None, text="", raise_json=False):
        self.status_code = status_code
        self.ok = status_code < 400
        self.text = text or (str(json_data) if json_data is not None else "")
        self._json = json_data
        self._raise_json = raise_json

    def json(self):
        if self._raise_json or self._json is None:
            raise ValueError("no json")
        return self._json


def _versao(v, ativo=True, erp_minimo=""):
    return VersaoAgente.objects.create(
        versao=v,
        download_url=f"https://example.invalid/{v}/pdv-local-v{v}.zip",
        sha256=SHA,
        erp_minimo=erp_minimo,
        ativo=ativo,
    )


class _Base(TestCase):
    def setUp(self):
        # Todo push passa por task_sincronizar_versoes_agente.delay — sem broker
        # em teste isso estouraria; os testes de gatilho conferem as chamadas.
        patcher = patch("registry.tasks.task_sincronizar_versoes_agente.delay")
        self.delay = patcher.start()
        self.addCleanup(patcher.stop)

        self.plano = Plano.objects.create(slug="p", nome="Plano", preco_mensal="0.00")
        self._seq = 0

    def _cliente(self, slug, *, auto=False, secret="segredo-abc", **extra):
        self._seq += 1
        return Cliente.objects.create(
            slug=slug,
            nome=slug,
            cnpj=f"00.000.000/{self._seq:04d}-00",
            email_contato=f"{slug}@example.invalid",
            subdominio=f"{slug}.ararasuite.com.br",
            plano=self.plano,
            status="ativo",
            integracao_secret=secret,
            atualizacao_automatica_agente=auto,
            **extra,
        )


class MontarPayloadTests(_Base):
    def test_modo_curadoria_envia_so_as_versoes_permitidas(self):
        cliente = self._cliente("curado", auto=False)
        v15 = _versao("1.5.0")
        _versao("1.6.0")  # no catálogo mas não permitida pra este cliente
        cliente.versoes_permitidas.add(v15)

        payload = SincronizadorVersoes(cliente)._montar_payload()

        self.assertEqual([p["version"] for p in payload["packages"]], ["1.5.0"])
        self.assertEqual(payload["cliente_id"], str(cliente.id))

    def test_modo_automatico_envia_o_catalogo_ativo_inteiro(self):
        cliente = self._cliente("auto", auto=True)
        _versao("1.5.0")
        _versao("1.6.0")
        _versao("1.4.0", ativo=False)  # aposentada — nunca vai no push
        # nenhuma versão adicionada em versoes_permitidas de propósito

        payload = SincronizadorVersoes(cliente)._montar_payload()

        versoes = sorted(p["version"] for p in payload["packages"])
        self.assertEqual(versoes, ["1.5.0", "1.6.0"])

    def test_modo_automatico_ignora_a_curadoria_manual(self):
        cliente = self._cliente("auto2", auto=True)
        v15 = _versao("1.5.0")
        _versao("1.6.0")
        cliente.versoes_permitidas.add(v15)  # deveria ser ignorado no modo auto

        payload = SincronizadorVersoes(cliente)._montar_payload()

        self.assertEqual(sorted(p["version"] for p in payload["packages"]), ["1.5.0", "1.6.0"])


class ToggleClienteTests(_Base):
    def test_ligar_o_modo_automatico_dispara_sincronizacao(self):
        cliente = self._cliente("t1", auto=False)
        self.delay.reset_mock()

        cliente.atualizacao_automatica_agente = True
        cliente.save()

        self.delay.assert_called_once_with(str(cliente.pk))

    def test_desligar_o_modo_automatico_dispara_sincronizacao(self):
        cliente = self._cliente("t2", auto=True)
        self.delay.reset_mock()

        cliente.atualizacao_automatica_agente = False
        cliente.save()

        self.delay.assert_called_once_with(str(cliente.pk))

    def test_salvar_sem_mudar_o_flag_nao_dispara(self):
        cliente = self._cliente("t3", auto=True)
        self.delay.reset_mock()

        cliente.nome = "outro nome"
        cliente.save()

        self.delay.assert_not_called()

    def test_toggle_sem_integracao_secret_nao_dispara(self):
        cliente = self._cliente("t4", auto=False, secret="")
        self.delay.reset_mock()

        cliente.atualizacao_automatica_agente = True
        cliente.save()

        self.delay.assert_not_called()


class VersaoAgenteCatalogoTests(_Base):
    def test_versao_nova_dispara_so_pros_clientes_automaticos(self):
        auto = self._cliente("va-auto", auto=True)
        manual = self._cliente("va-manual", auto=False)
        self.delay.reset_mock()

        _versao("2.0.0")  # created=True

        chamados = {c.args[0] for c in self.delay.call_args_list}
        self.assertIn(str(auto.pk), chamados)
        self.assertNotIn(str(manual.pk), chamados)

    def test_versao_nova_ignora_cliente_automatico_sem_secret(self):
        self._cliente("va-auto-nosecret", auto=True, secret="")
        self.delay.reset_mock()

        _versao("2.1.0")

        self.delay.assert_not_called()

    def test_editar_versao_dispara_pros_automaticos_e_pros_que_a_curam(self):
        auto = self._cliente("ed-auto", auto=True)
        cura = self._cliente("ed-cura", auto=False)
        v = _versao("1.9.0")
        cura.versoes_permitidas.add(v)
        self.delay.reset_mock()

        v.release_notes = "corrigido"
        v.save()

        chamados = {c.args[0] for c in self.delay.call_args_list}
        self.assertEqual(chamados, {str(auto.pk), str(cura.pk)})


class SincronizarLogTests(_Base):
    """Log passo a passo que SincronizadorVersoes.sincronizar() grava.
    sincronizar() faz `import requests` local -> patch em 'requests.post'."""

    def _cliente_com_versao(self, slug, versao="1.5.0", **kw):
        cliente = self._cliente(slug, **kw)
        v = _versao(versao)
        cliente.versoes_permitidas.add(v)
        return cliente

    def test_sucesso_status_concluida_e_log(self):
        cliente = self._cliente_com_versao("s-ok")
        with patch("requests.post", return_value=_FakeResp(200, {"synced": 2, "retired": 0, "skipped": []})):
            reg = SincronizadorVersoes(cliente).sincronizar()

        self.assertEqual(reg.status, "concluida")
        self.assertEqual(reg.resposta_http_status, 200)
        self.assertIsNotNone(reg.concluida_em)
        for trecho in ("Modo: curadoria manual", "Pacote montado", "HTTP 200",
                       "2 sincronizada(s), 0 aposentada(s)", "Concluída com sucesso"):
            self.assertIn(trecho, reg.log)

    def test_sucesso_parseia_skipped(self):
        cliente = self._cliente_com_versao("s-skip")
        with patch("requests.post", return_value=_FakeResp(200, {"synced": 1, "retired": 0, "skipped": ["1.2.0", "1.3.0"]})):
            reg = SincronizadorVersoes(cliente).sincronizar()
        self.assertIn("2 ignorada(s) (1.2.0, 1.3.0)", reg.log)

    def test_sucesso_corpo_ilegivel_nao_quebra(self):
        cliente = self._cliente_com_versao("s-badjson")
        with patch("requests.post", return_value=_FakeResp(200, raise_json=True, text="not json")):
            reg = SincronizadorVersoes(cliente).sincronizar()
        self.assertEqual(reg.status, "concluida")
        self.assertIn("ilegível", reg.log)

    def test_http_401_mensagem_amigavel(self):
        cliente = self._cliente_com_versao("s-401")
        with patch("requests.post", return_value=_FakeResp(401, {"error": "unauthorized"})):
            reg = SincronizadorVersoes(cliente).sincronizar()
        self.assertEqual(reg.status, "erro")
        self.assertIn("segredo", reg.mensagem_erro.lower())
        self.assertIn("HTTP 401", reg.log)
        self.assertIn("Segredo de integração recusado", reg.log)
        self.assertIn("Terminou com erro", reg.log)

    def test_http_409_cliente_id_mismatch(self):
        cliente = self._cliente_com_versao("s-409")
        with patch("requests.post", return_value=_FakeResp(409, {"error": "cliente_id_mismatch"})):
            reg = SincronizadorVersoes(cliente).sincronizar()
        self.assertEqual(reg.status, "erro")
        self.assertIn("cliente_id_mismatch", reg.log)

    def test_http_400_invalid_payload_inclui_campo(self):
        cliente = self._cliente_com_versao("s-400")
        with patch("requests.post", return_value=_FakeResp(400, {"error": "invalid_payload", "field": "packages"})):
            reg = SincronizadorVersoes(cliente).sincronizar()
        self.assertIn('campo "packages"', reg.log)

    def test_http_erro_desconhecido_cai_no_corpo_cru(self):
        cliente = self._cliente_com_versao("s-503")
        with patch("requests.post", return_value=_FakeResp(503, text="Service Unavailable")):
            reg = SincronizadorVersoes(cliente).sincronizar()
        self.assertEqual(reg.status, "erro")
        self.assertIn("HTTP 503 — Service Unavailable", reg.log)

    def test_timeout_registra_erro(self):
        cliente = self._cliente_com_versao("s-timeout")
        with patch("requests.post", side_effect=requests.exceptions.Timeout()):
            reg = SincronizadorVersoes(cliente).sincronizar()
        self.assertEqual(reg.status, "erro")
        self.assertIsNotNone(reg.concluida_em)
        self.assertTrue(reg.mensagem_erro)
        self.assertIn("não respondeu em 15s", reg.log)

    def test_connection_error_registra_erro(self):
        cliente = self._cliente_com_versao("s-conn")
        with patch("requests.post", side_effect=requests.exceptions.ConnectionError("boom")):
            reg = SincronizadorVersoes(cliente).sincronizar()
        self.assertEqual(reg.status, "erro")
        self.assertIn("Não foi possível conectar", reg.log)

    def test_modo_no_log(self):
        auto = self._cliente_com_versao("s-modo-auto", "1.5.0", auto=True)
        manual = self._cliente_com_versao("s-modo-man", "1.6.0", auto=False)
        with patch("requests.post", return_value=_FakeResp(200, {"synced": 1, "retired": 0, "skipped": []})):
            reg_auto = SincronizadorVersoes(auto).sincronizar()
            reg_man = SincronizadorVersoes(manual).sincronizar()
        self.assertIn("catálogo automático", reg_auto.log)
        self.assertIn("curadoria manual", reg_man.log)

    def test_lista_vazia_no_log(self):
        cliente = self._cliente("s-vazio", auto=False)  # sem versoes_permitidas
        with patch("requests.post", return_value=_FakeResp(200, {"synced": 0, "retired": 0, "skipped": []})):
            reg = SincronizadorVersoes(cliente).sincronizar()
        self.assertIn("nenhuma versão", reg.log)

    def test_iniciada_por_gravado(self):
        user = User.objects.create_user("clicou")
        cliente = self._cliente_com_versao("s-user")
        with patch("requests.post", return_value=_FakeResp(200, {"synced": 1, "retired": 0, "skipped": []})):
            com = SincronizadorVersoes(cliente, iniciada_por=user).sincronizar()
            sem = SincronizadorVersoes(cliente).sincronizar()
        self.assertEqual(com.iniciada_por, user)
        self.assertIsNone(sem.iniciada_por)

    def test_log_persiste_incrementalmente(self):
        cliente = self._cliente_com_versao("s-incr")
        visto = {}

        def fake_post(*a, **k):
            reg = SincronizacaoVersoesAgente.objects.filter(cliente=cliente).first()
            visto["log"] = reg.log
            return _FakeResp(200, {"synced": 1, "retired": 0, "skipped": []})

        with patch("requests.post", side_effect=fake_post):
            SincronizadorVersoes(cliente).sincronizar()

        # durante o POST, as linhas ate "Enviando" ja estao no banco; "Concluída" ainda nao
        self.assertIn("Enviando", visto["log"])
        self.assertNotIn("Concluída", visto["log"])

    def test_log_sempre_preenchido(self):
        cliente = self._cliente_com_versao("s-sempre")
        with patch("requests.post", side_effect=requests.exceptions.ConnectionError()):
            reg = SincronizadorVersoes(cliente).sincronizar()
        self.assertNotEqual(reg.log, "")


class SincronizarTaskEViewTests(_Base):
    @patch("registry.cp_push.SincronizadorVersoes")
    def test_task_com_retry_false_nao_retenta(self, MockSinc):
        MockSinc.return_value.sincronizar.return_value = SimpleNamespace(status="erro", mensagem_erro="x")
        cliente = self._cliente("t-noretry")
        with patch.object(task_sincronizar_versoes_agente, "retry") as mock_retry:
            task_sincronizar_versoes_agente.apply(args=(str(cliente.pk),), kwargs={"com_retry": False})
        mock_retry.assert_not_called()

    @patch("registry.cp_push.SincronizadorVersoes")
    def test_task_com_retry_true_retenta(self, MockSinc):
        MockSinc.return_value.sincronizar.return_value = SimpleNamespace(status="erro", mensagem_erro="x")
        cliente = self._cliente("t-retry")
        with patch.object(task_sincronizar_versoes_agente, "retry", side_effect=RuntimeError("retry!")) as mock_retry:
            task_sincronizar_versoes_agente.apply(args=(str(cliente.pk),))
        mock_retry.assert_called_once()

    @patch("registry.cp_push.SincronizadorVersoes")
    def test_task_passa_iniciada_por(self, MockSinc):
        MockSinc.return_value.sincronizar.return_value = SimpleNamespace(status="concluida", mensagem_erro="")
        user = User.objects.create_user("via-task")
        cliente = self._cliente("t-user")
        task_sincronizar_versoes_agente.apply(args=(str(cliente.pk),), kwargs={"iniciada_por_id": user.id})
        _, kwargs = MockSinc.call_args
        self.assertEqual(kwargs["iniciada_por"], user)

    def test_view_sincronizar_enfileira_task_sem_retry(self):
        admin = User.objects.create_superuser("adm", "a@a.invalid", "x")
        c = Client()
        c.force_login(admin)
        cliente = self._cliente("v-sync")
        self.delay.reset_mock()

        resp = c.get(f"/admin/registry/cliente/{cliente.pk}/sincronizar-versoes/")

        self.assertEqual(resp.status_code, 302)
        self.delay.assert_called_once()
        args, kwargs = self.delay.call_args
        self.assertEqual(args[0], str(cliente.pk))
        self.assertEqual(kwargs, {"iniciada_por_id": admin.id, "com_retry": False})

    def test_view_sincronizar_sem_secret_nao_enfileira(self):
        admin = User.objects.create_superuser("adm2", "b@b.invalid", "x")
        c = Client()
        c.force_login(admin)
        cliente = self._cliente("v-nosecret", secret="")
        self.delay.reset_mock()

        c.get(f"/admin/registry/cliente/{cliente.pk}/sincronizar-versoes/")

        self.delay.assert_not_called()

    def test_view_status_sincronizacao_json(self):
        admin = User.objects.create_superuser("adm3", "c@c.invalid", "x")
        c = Client()
        c.force_login(admin)
        cliente = self._cliente("v-status")
        reg = SincronizacaoVersoesAgente.objects.create(cliente=cliente, status="enviando", log="linha 1\nlinha 2")

        resp = c.get(f"/admin/registry/cliente/{cliente.pk}/sincronizacoes/{reg.pk}/status/")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"status": "enviando", "log": "linha 1\nlinha 2", "ativo": True, "http": None})
