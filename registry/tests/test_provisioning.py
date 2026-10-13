import json
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
                'CP_CLIENTE_CNPJ': '12.345.678/0001-90',
            },
        )

        self.assertIn('CP_CLIENTE_ID: "11111111-2222-3333-4444-555555555555"', stack)
        self.assertIn('CP_CLIENTE_NOME: "Anapolis"', stack)
        self.assertIn('CP_CLIENTE_CNPJ: "12.345.678/0001-90"', stack)

    def test_site_url_usa_sempre_o_subdominio(self):
        from registry.provisioning import site_url_cliente

        cliente = SimpleNamespace(subdominio='acme.ararasuite.com.br', dominio_custom='acme.com.br')
        self.assertEqual(site_url_cliente(cliente), 'https://acme.ararasuite.com.br')

    def test_stack_propaga_site_url(self):
        stack = self.motor._gerar_stack_yaml(
            slug='acme', subdominio='acme.ararasuite.com.br', versao='0.0.71', regiao='anapolis',
            env_vars={'POSTGRES_PASSWORD': 'senha', 'SITE_URL': 'https://acme.ararasuite.com.br'},
        )
        self.assertIn('SITE_URL: "https://acme.ararasuite.com.br"', stack)

    @patch('registry.models.Cliente.objects.filter')
    @patch('registry.provisioning.CHECK_HTTP_HEALTH', False)
    @patch('registry.provisioning.subprocess.run')
    def test_atualizar_versao_usa_repositorio_ararasuite_erp(
        self,
        run_mock,
        cliente_filter_mock,
    ):
        self.motor._aguardar_servico = lambda *args, **kwargs: None
        versoes_cron = []
        self.motor.sincronizar_servico_cron = versoes_cron.append

        self.motor.atualizar_versao('0.0.71')

        self.assertEqual(versoes_cron, ['0.0.71'])

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


SPEC_WEB = {
    'TaskTemplate': {
        'ContainerSpec': {
            'Image': 'emporioautomacao/ararasuite-erp:0.0.70@sha256:abc',
            'Env': ['POSTGRES_HOST=db', 'SITE_URL=https://acme.ararasuite.com.br'],
            'Mounts': [{'Type': 'volume', 'Source': 'acme_media', 'Target': '/app/media'}],
        },
        'Placement': {'Constraints': ['node.labels.region == anapolis']},
    },
}


class ServicoCronTests(SimpleTestCase):
    def setUp(self):
        self.motor = MotorProvisionamento.__new__(MotorProvisionamento)
        self.motor.cliente = SimpleNamespace(pk='cliente-id', slug='acme', subdominio='acme.ararasuite.com.br')

    def _fake_run(self, servicos):
        chamadas = []

        def run(cmd, **kwargs):
            chamadas.append(cmd)
            if cmd[:3] == ['docker', 'service', 'inspect']:
                spec = servicos.get(cmd[-1])
                if spec is None:
                    return SimpleNamespace(returncode=1, stdout='', stderr='no such service')
                return SimpleNamespace(returncode=0, stdout=json.dumps(spec), stderr='')
            return SimpleNamespace(returncode=0, stdout='', stderr='')

        return run, chamadas

    def test_stack_tem_servico_cron_com_executor_central(self):
        stack = self.motor._gerar_stack_yaml(
            slug='acme', subdominio='acme.ararasuite.com.br', versao='0.0.71', regiao='anapolis',
            env_vars={'POSTGRES_PASSWORD': 'senha'},
        )
        bloco_cron = stack.split('  cron:')[1].split('  db:')[0]
        self.assertIn('image: emporioautomacao/ararasuite-erp:0.0.71', bloco_cron)
        self.assertIn(
            'entrypoint: ["python", "manage.py", "executar_crons", "--loop", "--sleep", "60"]',
            bloco_cron,
        )
        self.assertNotIn('traefik', bloco_cron)

    def test_cria_cron_a_partir_do_web_quando_nao_existe(self):
        run, chamadas = self._fake_run({'acme_web': SPEC_WEB})
        with patch('registry.provisioning.subprocess.run', side_effect=run):
            self.motor.sincronizar_servico_cron('0.0.71')

        create = chamadas[-1]
        self.assertEqual(create[:3], ['docker', 'service', 'create'])
        self.assertEqual(create[-1], 'emporioautomacao/ararasuite-erp:0.0.71')
        self.assertIn('acme_cron', create)
        self.assertIn('python manage.py executar_crons --loop --sleep 60', create)
        self.assertIn('acme_internal', create)
        self.assertIn('type=volume,source=acme_media,target=/app/media', create)
        self.assertIn('node.labels.region == anapolis', create)
        self.assertIn('SITE_URL=https://acme.ararasuite.com.br', create)
        self.assertIn('com.docker.stack.namespace=acme', create)

    def test_atualiza_imagem_e_env_do_cron_existente(self):
        run, chamadas = self._fake_run({'acme_web': SPEC_WEB, 'acme_cron': SPEC_WEB})
        with patch('registry.provisioning.subprocess.run', side_effect=run):
            self.motor.sincronizar_servico_cron()

        update = chamadas[-1]
        self.assertEqual(update[:3], ['docker', 'service', 'update'])
        self.assertEqual(update[-1], 'acme_cron')
        # Sem versão informada usa a imagem atual do web, sem o digest.
        self.assertIn('emporioautomacao/ararasuite-erp:0.0.70', update)
        self.assertIn('POSTGRES_HOST=db', update)

    def test_falha_do_docker_vira_erro_com_stderr(self):
        def run(cmd, **kwargs):
            if cmd[:3] == ['docker', 'service', 'inspect'] and cmd[-1] == 'acme_web':
                return SimpleNamespace(returncode=0, stdout=json.dumps(SPEC_WEB), stderr='')
            if cmd[:3] == ['docker', 'service', 'inspect']:
                return SimpleNamespace(returncode=1, stdout='', stderr='no such service')
            return SimpleNamespace(returncode=1, stdout='', stderr='network not found')

        with patch('registry.provisioning.subprocess.run', side_effect=run):
            with self.assertRaisesMessage(RuntimeError, 'network not found'):
                self.motor.sincronizar_servico_cron('0.0.71')
