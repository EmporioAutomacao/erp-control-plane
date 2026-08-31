from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase

from registry.provisioning import ERP_IMAGE, MotorProvisionamento


class MotorProvisionamentoImagemTests(SimpleTestCase):
    def setUp(self):
        self.motor = MotorProvisionamento.__new__(MotorProvisionamento)
        self.motor.cliente = SimpleNamespace(
            pk='cliente-id',
            slug='acme',
            subdominio='acme.ararasuite.com.br',
        )

    def test_stack_usa_repositorio_ararasuite_erp_e_versao_informada(self):
        stack = self.motor._gerar_stack_yaml(
            slug='acme',
            subdominio='acme.ararasuite.com.br',
            versao='0.0.71',
            regiao='anapolis',
            env_vars={'POSTGRES_PASSWORD': 'senha'},
        )

        self.assertEqual(ERP_IMAGE, 'emporioautomacao/ararasuite-erp')
        self.assertIn(
            'image: emporioautomacao/ararasuite-erp:0.0.71',
            stack,
        )

    def test_stack_propaga_vinculo_cp_para_o_ambiente_do_web(self):
        stack = self.motor._gerar_stack_yaml(
            slug='acme',
            subdominio='acme.ararasuite.com.br',
            versao='0.0.71',
            regiao='anapolis',
            env_vars={
                'POSTGRES_PASSWORD': 'senha',
                'CP_CLIENTE_ID': '11111111-2222-3333-4444-555555555555',
                'CP_CLIENTE_NOME': 'Anapolis',
            },
        )

        self.assertIn('CP_CLIENTE_ID: "11111111-2222-3333-4444-555555555555"', stack)
        self.assertIn('CP_CLIENTE_NOME: "Anapolis"', stack)

    @patch('registry.models.Cliente.objects.filter')
    @patch('registry.provisioning.CHECK_HTTP_HEALTH', False)
    @patch('registry.provisioning.subprocess.run')
    def test_atualizar_versao_usa_repositorio_ararasuite_erp(
        self,
        run_mock,
        cliente_filter_mock,
    ):
        self.motor._aguardar_servico = lambda *args, **kwargs: None

        self.motor.atualizar_versao('0.0.71')

        run_mock.assert_called_once_with(
            [
                'docker',
                'service',
                'update',
                '--with-registry-auth',
                '--image',
                'emporioautomacao/ararasuite-erp:0.0.71',
                'acme_web',
            ],
            check=True,
        )
        cliente_filter_mock.assert_called_once_with(pk='cliente-id')
        cliente_filter_mock.return_value.update.assert_called_once_with(
            versao_erp='0.0.71',
        )
