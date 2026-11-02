import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test import TestCase, override_settings

from registry import versoes
from registry.admin import ClienteAdminForm
from registry.models import BackupCliente, Cliente, Plano
from registry.provisioning import MotorProvisionamento
from registry.tasks import task_atualizar_versao, task_provisionar_cliente, task_restaurar_cliente


def _cliente(**campos):
    plano, _ = Plano.objects.get_or_create(
        slug='basico',
        defaults={'nome': 'Básico', 'preco_mensal': 0},
    )
    dados = {
        'nome': 'Acme', 'slug': 'acme', 'cnpj': '12.345.678/0001-90', 'email_contato': 'a@a.com',
        'subdominio': 'acme.ararasuite.com.br', 'plano': plano, 'status': 'ativo', 'versao_erp': '0.0.100',
    }
    dados.update(campos)
    with patch('registry.tasks.task_provisionar_cliente.delay'):
        return Cliente.objects.create(**dados)


def _sem_servico():
    return patch('registry.versoes.tag_em_execucao', return_value='')


class ResolverVersaoTests(TestCase):
    def test_cada_fonte_de_referencia_barra_sozinha(self):
        for campos, tag in (
            ({'versao_erp_maxima': '0.0.100'}, ''),
            ({'versao_erp_detectada': '0.0.100'}, ''),
            ({}, '0.0.100'),  # versao_erp atras da imagem em execucao
        ):
            cliente = SimpleNamespace(slug='acme', versao_erp='0.0.99', data_ativacao=None, **campos)
            with self.subTest(campos=campos, tag=tag), patch('registry.versoes.tag_em_execucao', return_value=tag):
                with self.assertRaisesMessage(versoes.DowngradeRecusado, 'Downgrade recusado'):
                    versoes.resolver_versao(cliente)

    def test_alvo_digitado_errado_nao_vira_piso(self):
        # 9.9.9 foi digitado e nunca implantado: precisa dar para corrigir.
        cliente = SimpleNamespace(slug='acme', versao_erp='9.9.9', versao_erp_maxima='0.0.100', data_ativacao=None)
        with _sem_servico():
            self.assertEqual(versoes.resolver_versao(cliente, '0.0.101'), '0.0.101')

    def test_comparacao_numerica(self):
        cliente = SimpleNamespace(slug='acme', versao_erp='10.0.0', versao_erp_maxima='9.0.0', data_ativacao=None)
        with _sem_servico():
            self.assertEqual(versoes.resolver_versao(cliente), '10.0.0')
        cliente = SimpleNamespace(slug='acme', versao_erp='0.0.99', versao_erp_maxima='0.0.100', data_ativacao=None)
        with _sem_servico(), self.assertRaises(versoes.DowngradeRecusado):
            versoes.resolver_versao(cliente)

    def test_mesma_versao_aceita(self):
        cliente = SimpleNamespace(slug='acme', versao_erp='0.0.100', versao_erp_maxima='0.0.100', data_ativacao=None)
        with patch('registry.versoes.tag_em_execucao', return_value='0.0.100'):
            self.assertEqual(versoes.resolver_versao(cliente), '0.0.100')

    def test_latest_com_versao_conhecida_fica_fixado_nela(self):
        for alvo in ('latest', ''):
            cliente = SimpleNamespace(slug='acme', versao_erp=alvo, versao_erp_detectada='0.0.100', data_ativacao=None)
            with self.subTest(alvo=alvo), _sem_servico():
                self.assertEqual(versoes.resolver_versao(cliente), '0.0.100')

    def test_primeira_implantacao_aceita_latest_inclusive_em_retentativa(self):
        cliente = SimpleNamespace(slug='acme', versao_erp='latest', data_ativacao=None)
        for tag in ('', 'latest'):
            with self.subTest(tag=tag), patch('registry.versoes.tag_em_execucao', return_value=tag):
                self.assertEqual(versoes.resolver_versao(cliente), 'latest')

    def test_cliente_ativado_sem_versao_conhecida_e_recusado(self):
        cliente = SimpleNamespace(slug='acme', versao_erp='latest', data_ativacao='2026-01-01')
        with patch('registry.versoes.tag_em_execucao', return_value='latest'):
            with self.assertRaises(versoes.DowngradeRecusado):
                versoes.resolver_versao(cliente)

    def test_exigir_sem_downgrade_recusa_tag_nao_semver(self):
        for tag in ('latest', '', 'dev'):
            with self.subTest(tag=tag), self.assertRaises(versoes.DowngradeRecusado):
                versoes.exigir_sem_downgrade(SimpleNamespace(slug='acme'), tag)

    @override_settings(DEBUG=True)
    def test_dev_aceita_tag_livre(self):
        versoes.exigir_sem_downgrade(SimpleNamespace(slug='acme'), 'dev')


class TagEmExecucaoTests(TestCase):
    def _run(self, returncode=0, stdout='', stderr=''):
        return patch('registry.versoes.subprocess.run',
                     return_value=SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr))

    def test_le_a_tag_sem_digest(self):
        with self._run(stdout='emporioautomacao/ararasuite-erp:0.0.70@sha256:abc\n'):
            self.assertEqual(versoes.tag_em_execucao('acme'), '0.0.70')

    def test_servico_inexistente_nao_e_erro(self):
        with self._run(returncode=1, stderr='Status: Error: no such service: acme_web, Code: 1'):
            self.assertEqual(versoes.tag_em_execucao('acme'), '')

    def test_falha_na_consulta_nao_libera(self):
        with self._run(returncode=1, stderr='This node is not a swarm manager.'):
            with self.assertRaises(versoes.DowngradeRecusado):
                versoes.tag_em_execucao('acme')
        with patch('registry.versoes.subprocess.run', side_effect=FileNotFoundError('docker')):
            with self.assertRaises(versoes.DowngradeRecusado):
                versoes.tag_em_execucao('acme')


class RegistrarVersaoVistaTests(TestCase):
    def test_sobe_nunca_reduz_e_registra_a_regressao(self):
        cliente = _cliente()
        versoes.registrar_versao_vista(cliente.pk, '0.0.100')
        versoes.registrar_versao_vista(cliente.pk, 'latest')
        cliente.refresh_from_db()
        self.assertEqual(cliente.versao_erp_maxima, '0.0.100')

        with self.assertLogs('registry.versoes', level='ERROR') as logs:
            versoes.registrar_versao_vista(cliente.pk, '0.0.99')
        self.assertIn('regrediu', logs.output[0])
        cliente.refresh_from_db()
        self.assertEqual(cliente.versao_erp_maxima, '0.0.100')

    def test_regrediu(self):
        self.assertTrue(versoes.regrediu(SimpleNamespace(versao_erp_detectada='0.0.99', versao_erp_maxima='0.0.100')))
        self.assertFalse(versoes.regrediu(SimpleNamespace(versao_erp_detectada='0.0.100', versao_erp_maxima='0.0.100')))
        self.assertFalse(versoes.regrediu(SimpleNamespace(versao_erp_detectada='', versao_erp_maxima='0.0.100')))


class MotorRecusaTests(TestCase):
    def setUp(self):
        self.cliente = _cliente(versao_erp='0.0.99', versao_erp_maxima='0.0.100')
        self.motor = MotorProvisionamento.__new__(MotorProvisionamento)
        self.motor.cliente = self.cliente
        patcher = patch('registry.versoes.tag_em_execucao', return_value='0.0.100')
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_executar_recusa_sem_docker_nem_arquivo(self):
        for modo in (False, True):
            with tempfile.TemporaryDirectory() as base, self.subTest(modo_migracao=modo):
                with patch('registry.provisioning.CLIENTES_BASE', Path(base)), \
                        patch('registry.provisioning.subprocess.run') as run:
                    with self.assertRaises(versoes.DowngradeRecusado):
                        self.motor.executar(modo_migracao=modo)
                run.assert_not_called()
                self.assertEqual(list(Path(base).iterdir()), [])
        self.assertEqual(self.cliente.logs.get(etapa='gerar_stack').status, 'erro')

    def test_atualizar_versao_recusa_sem_docker(self):
        with patch('registry.provisioning.subprocess.run') as run, self.assertRaises(versoes.DowngradeRecusado):
            self.motor.atualizar_versao('0.0.99')
        run.assert_not_called()

    def test_cron_e_run_manage_recusam_tag_antiga(self):
        with patch('registry.provisioning.subprocess.run') as run:
            with self.assertRaises(versoes.DowngradeRecusado):
                self.motor.sincronizar_servico_cron('0.0.99')
            with self.assertRaises(versoes.DowngradeRecusado):
                self.motor._run_manage('acme', '0.0.99', {}, ['migrate'])
        run.assert_not_called()

    def test_task_provisionar_nao_retenta_e_cliente_ativo_continua_ativo(self):
        with patch('registry.tasks.task_provisionar_cliente.retry') as retry, \
                patch('registry.provisioning.docker.from_env'):
            task_provisionar_cliente.apply(args=[str(self.cliente.pk)])
        retry.assert_not_called()
        self.cliente.refresh_from_db()
        self.assertEqual(self.cliente.status, 'ativo')

    def test_task_atualizar_registra_recusa_sem_retentar(self):
        with patch('registry.tasks.task_atualizar_versao.retry') as retry, \
                patch('registry.provisioning.docker.from_env'):
            task_atualizar_versao.apply(args=[str(self.cliente.pk), '0.0.99'])
        retry.assert_not_called()
        atualizacao = self.cliente.atualizacoes.get()
        self.assertEqual(atualizacao.status, 'erro')
        self.assertIn('Downgrade recusado', atualizacao.mensagem)


class FormularioClienteTests(TestCase):
    def _form(self, cliente, versao):
        dados = {}
        for campo in ClienteAdminForm(instance=cliente).fields:
            valor = getattr(cliente, f'{campo}_id', None) if hasattr(cliente, f'{campo}_id') else None
            if valor is None and campo not in ('modulos_ativos', 'versoes_permitidas'):
                valor = getattr(cliente, campo, '')
            dados[campo] = valor if valor is not None else ''
        dados['versao_erp'] = versao
        return ClienteAdminForm(data=dados, instance=cliente)

    def test_recusa_versao_menor_e_aceita_igual_e_maior(self):
        cliente = _cliente(versao_erp='0.0.100', versao_erp_maxima='0.0.100')
        with patch('registry.versoes.tag_em_execucao', return_value='0.0.100'):
            form = self._form(cliente, '0.0.99')
            form.is_valid()
            self.assertIn('versao_erp', form.errors)
            self.assertIn('Downgrade recusado', form.errors['versao_erp'][0])

            form = self._form(cliente, '0.0.101')
            form.is_valid()
            self.assertNotIn('versao_erp', form.errors)

    def test_recusa_latest_em_cliente_com_versao_conhecida(self):
        cliente = _cliente(versao_erp='0.0.100', versao_erp_maxima='0.0.100')
        with _sem_servico():
            form = self._form(cliente, 'latest')
            form.is_valid()
        self.assertIn('versao_erp', form.errors)

    def test_nao_consulta_o_swarm_se_a_versao_nao_mudou(self):
        cliente = _cliente(versao_erp='0.0.100')
        with patch('registry.versoes.tag_em_execucao') as tag:
            self._form(cliente, '0.0.100').is_valid()
        tag.assert_not_called()


class RestauracaoTests(TestCase):
    def setUp(self):
        self.cliente = _cliente(versao_erp_detectada='0.0.100')

    def _backup(self, versao):
        return BackupCliente.objects.create(cliente=self.cliente, status='concluido', versao_erp=versao)

    def test_backup_mais_novo_recusado_sem_parar_o_web(self):
        backup = self._backup('0.0.101')
        with patch('registry.versoes.tag_em_execucao', return_value='0.0.100'), \
                patch('registry.tasks._parar_app') as parar, \
                patch('registry.tasks.task_restaurar_no.apply_async') as despachar:
            task_restaurar_cliente.apply(args=[str(self.cliente.pk), backup.pk])
        parar.assert_not_called()
        despachar.assert_not_called()
        backup.refresh_from_db()
        self.assertFalse(backup.restaurando)
        self.assertIn('Restauração recusada', backup.mensagem_restauracao)

    def test_backup_igual_ou_mais_antigo_aceito(self):
        with patch('registry.versoes.tag_em_execucao', return_value='0.0.100'):
            versoes.exigir_restauracao_compativel(self.cliente, '0.0.100')
            versoes.exigir_restauracao_compativel(self.cliente, '0.0.50')

    def test_backup_sem_versao_nao_e_conferido(self):
        with patch('registry.versoes.tag_em_execucao') as tag:
            versoes.exigir_restauracao_compativel(self.cliente, '')
        tag.assert_not_called()

    def test_versao_do_stack(self):
        self.assertEqual(versoes.versao_do_stack('    image: emporioautomacao/ararasuite-erp:0.0.71\n'), '0.0.71')
        self.assertEqual(versoes.versao_do_stack('    image: emporioautomacao/ararasuite-erp:latest\n'), '')
        self.assertEqual(versoes.versao_do_stack('sem imagem'), '')


class GuardaVersaoTests(TestCase):
    def setUp(self):
        with connection.cursor() as cursor:
            cursor.execute('DROP TABLE IF EXISTS guarda_versao')

    def test_versao_menor_recusada_e_maxima_nunca_reduz(self):
        with override_settings(VERSION='0.0.100'):
            call_command('guarda_versao', 'verificar')
            call_command('guarda_versao', 'registrar')
        with override_settings(VERSION='0.0.99'):
            with self.assertRaisesMessage(CommandError, 'DOWNGRADE RECUSADO'):
                call_command('guarda_versao', 'verificar')
            with self.assertRaises(CommandError):
                call_command('guarda_versao', 'registrar')
        with override_settings(VERSION='0.0.100'):
            call_command('guarda_versao', 'verificar')
        with connection.cursor() as cursor:
            cursor.execute('SELECT versao_maxima FROM guarda_versao WHERE id = 1')
            self.assertEqual(cursor.fetchone()[0], '0.0.100')

    @override_settings(VERSION='latest', DEBUG=False)
    def test_versao_invalida_recusada(self):
        with self.assertRaises(CommandError):
            call_command('guarda_versao', 'verificar')
