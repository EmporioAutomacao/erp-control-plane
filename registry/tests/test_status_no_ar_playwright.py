from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.urls import reverse

from registry.models import Cliente, Plano
from registry.tests.playwright_base import PlaywrightAdminTestCase


class StatusNoArPlaywrightTests(PlaywrightAdminTestCase):
    def setUp(self):
        for alvo in ('registry.versoes.tag_em_execucao',):
            p = patch(alvo, return_value='')
            p.start()
            self.addCleanup(p.stop)
        self.suspender = patch('registry.tasks.task_suspender_cliente.delay').start()
        self.reativar = patch('registry.tasks.task_reativar_cliente.delay').start()
        self.addCleanup(patch.stopall)
        self.user = get_user_model().objects.create_superuser('admin_status_pw', 'pw@example.com', 'SenhaSegura123!')
        self.plano = Plano.objects.create(slug='basico-status-pw', nome='Basico', preco_mensal='0.00')
        self.dialogos = []

    def _abrir(self, status):
        cliente = Cliente.objects.create(
            slug='pw-status', nome='Cliente Status', cnpj='11.222.333/0001-66', email_contato='a@example.com',
            subdominio='pw-status.ararasuite.com.br', plano=self.plano, status=status, versao_erp='0.0.148',
        )
        self.autenticar_admin(self.user)
        self.page.goto(self.live_server_url + reverse('admin:registry_cliente_change', args=[cliente.pk]))
        self.page.wait_for_load_state('load')
        self.page.locator('[x-data*="activeFieldsetTab"] nav a', has_text='Status e Ações').first.click()
        return cliente

    def _salvar(self, novo, aceitar):
        def responder(dialogo):
            self.dialogos.append(dialogo.message)
            dialogo.accept() if aceitar else dialogo.dismiss()

        # A página é da classe inteira: o listener não pode sobrar para o próximo teste.
        self.page.on('dialog', responder)
        self.addCleanup(self.page.remove_listener, 'dialog', responder)
        self.page.locator('#id_status').select_option(novo)
        self.page.locator('[name="_continue"]').first.click()
        self.page.wait_for_load_state('load')

    def test_cancelar_a_confirmacao_nao_salva_nem_para_o_site(self):
        cliente = self._abrir('ativo')
        self._salvar('suspenso', aceitar=False)
        self.assertEqual(len(self.dialogos), 1)
        self.assertIn('tira o site e o cron', self.dialogos[0])
        cliente.refresh_from_db()
        self.assertEqual(cliente.status, 'ativo')
        self.suspender.assert_not_called()

    def test_confirmar_salva_avisa_e_para_o_site(self):
        cliente = self._abrir('ativo')
        self._salvar('cancelado', aceitar=True)
        self.page.get_by_text('estão sendo tirados do ar').first.wait_for()
        cliente.refresh_from_db()
        self.assertEqual(cliente.status, 'cancelado')
        self.suspender.assert_called_once_with(str(cliente.pk))
        self.reativar.assert_not_called()

    def test_voltar_ao_ar_confirma_avisa_e_religa(self):
        cliente = self._abrir('suspenso')
        self._salvar('ativo', aceitar=True)
        self.assertIn('religa o site e o cron', self.dialogos[0])
        self.page.get_by_text('estão sendo religados').first.wait_for()
        cliente.refresh_from_db()
        self.assertEqual(cliente.status, 'ativo')
        self.reativar.assert_called_once_with(str(cliente.pk))
        self.suspender.assert_not_called()

    def test_mudanca_sem_efeito_no_site_nao_pede_confirmacao(self):
        cliente = self._abrir('ativo')
        self._salvar('trial', aceitar=True)
        self.page.get_by_text('foi alterado com sucesso').first.wait_for()
        self.assertEqual(self.dialogos, [])
        cliente.refresh_from_db()
        self.assertEqual(cliente.status, 'trial')
        self.suspender.assert_not_called()
        self.reativar.assert_not_called()
