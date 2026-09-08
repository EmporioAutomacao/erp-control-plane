from unittest.mock import patch

from django.test import TestCase

from registry.cp_push import SincronizadorVersoes
from registry.models import Cliente, Plano, VersaoAgente

SHA = "a" * 64


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
