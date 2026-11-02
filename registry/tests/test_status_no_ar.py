from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from registry.models import Cliente, Plano
from registry.provisioning import MotorProvisionamento
from registry.tasks import task_reativar_cliente, task_suspender_cliente


def _cliente(status='ativo', **campos):
    plano, _ = Plano.objects.get_or_create(slug='basico', defaults={'nome': 'Básico', 'preco_mensal': 0})
    with patch('registry.tasks.task_provisionar_cliente.delay'):
        return Cliente.objects.create(
            nome='Acme', slug='acme', cnpj='12.345.678/0001-90', email_contato='a@a.com',
            subdominio='acme.ararasuite.com.br', plano=plano, status=status, versao_erp='0.0.148', **campos,
        )


class SinalDeStatusTests(TestCase):
    def _trocar(self, de, para):
        cliente = _cliente(status=de)
        cliente.status = para
        with patch('registry.tasks.task_suspender_cliente.delay') as suspender, \
                patch('registry.tasks.task_reativar_cliente.delay') as reativar, \
                self.captureOnCommitCallbacks(execute=True):
            cliente.save()
        return suspender, reativar

    def test_sair_do_ar_para_web_e_cron(self):
        for de in Cliente.STATUS_NO_AR:
            for para in Cliente.STATUS_FORA_DO_AR:
                with self.subTest(de=de, para=para):
                    Cliente.objects.all().delete()
                    suspender, reativar = self._trocar(de, para)
                    suspender.assert_called_once()
                    reativar.assert_not_called()

    def test_voltar_ao_ar_religa(self):
        for de in Cliente.STATUS_FORA_DO_AR:
            for para in ('ativo', 'trial'):
                with self.subTest(de=de, para=para):
                    Cliente.objects.all().delete()
                    suspender, reativar = self._trocar(de, para)
                    reativar.assert_called_once()
                    suspender.assert_not_called()

    def test_sem_passar_de_um_grupo_a_outro_nao_mexe_em_replicas(self):
        casos = [
            ('suspenso', 'cancelado'), ('ativo', 'trial'), ('ativo', 'ativo'),
            # nunca implantado: não há serviço para escalar
            ('aguardando_provisao', 'cancelado'), ('erro_provisao', 'suspenso'),
            ('erro_provisao', 'ativo'),
        ]
        for de, para in casos:
            with self.subTest(de=de, para=para):
                Cliente.objects.all().delete()
                suspender, reativar = self._trocar(de, para)
                suspender.assert_not_called()
                reativar.assert_not_called()

    def test_so_dispara_depois_do_commit(self):
        cliente = _cliente()
        cliente.status = 'suspenso'
        with patch('registry.tasks.task_suspender_cliente.delay') as suspender, \
                self.captureOnCommitCallbacks(execute=False) as callbacks:
            cliente.save()
        suspender.assert_not_called()
        self.assertTrue(callbacks)


class TasksDeStatusTests(TestCase):
    def setUp(self):
        self.motor = patch('registry.provisioning.MotorProvisionamento').start()
        self.addCleanup(patch.stopall)

    def _atualizar(self, cliente, status, **campos):
        Cliente.objects.filter(pk=cliente.pk).update(status=status, **campos)

    def test_suspender_nao_troca_o_status_e_data_so_em_suspenso(self):
        cliente = _cliente()
        self._atualizar(cliente, 'cancelado')
        task_suspender_cliente(str(cliente.pk))
        self.motor.return_value.suspender.assert_called_once()
        cliente.refresh_from_db()
        self.assertEqual(cliente.status, 'cancelado')
        self.assertIsNone(cliente.data_suspensao)

    def test_suspender_grava_a_data(self):
        cliente = _cliente()
        self._atualizar(cliente, 'suspenso')
        task_suspender_cliente(str(cliente.pk))
        cliente.refresh_from_db()
        self.assertEqual(cliente.status, 'suspenso')
        self.assertIsNotNone(cliente.data_suspensao)

    def test_suspender_ignora_quem_ja_voltou_ao_ar(self):
        cliente = _cliente()
        task_suspender_cliente(str(cliente.pk))
        self.motor.return_value.suspender.assert_not_called()

    def test_reativar_mantem_trial_e_limpa_a_data(self):
        cliente = _cliente()
        self._atualizar(cliente, 'trial', data_suspensao='2026-01-01')
        task_reativar_cliente(str(cliente.pk))
        self.motor.return_value.reativar.assert_called_once()
        cliente.refresh_from_db()
        self.assertEqual(cliente.status, 'trial')
        self.assertIsNone(cliente.data_suspensao)

    def test_reativar_ignora_quem_ja_saiu_do_ar(self):
        cliente = _cliente()
        self._atualizar(cliente, 'suspenso')
        task_reativar_cliente(str(cliente.pk))
        self.motor.return_value.reativar.assert_not_called()


class MotorForaDoArTests(TestCase):
    def setUp(self):
        self.run = patch(
            'registry.provisioning.subprocess.run',
            return_value=SimpleNamespace(returncode=0, stdout='{}', stderr=''),
        ).start()
        patch('registry.provisioning.docker.from_env').start()
        patch('registry.versoes.exigir_sem_downgrade').start()
        patch('registry.versoes.registrar_versao_vista').start()
        self.addCleanup(patch.stopall)

    def _comandos(self):
        return [c.args[0] for c in self.run.call_args_list]

    def test_cron_criado_fora_do_ar_nasce_em_zero(self):
        cliente = _cliente(status='suspenso')
        motor = MotorProvisionamento(cliente)
        spec_web = {'TaskTemplate': {'ContainerSpec': {'Image': 'img:1'}}}
        with patch.object(motor, '_spec_servico', side_effect=[spec_web, None]):
            motor.sincronizar_servico_cron()
        criar = self._comandos()[-1]
        self.assertEqual(criar[criar.index('--replicas') + 1], '0')

    def test_cron_criado_no_ar_nasce_em_um(self):
        cliente = _cliente()
        motor = MotorProvisionamento(cliente)
        spec_web = {'TaskTemplate': {'ContainerSpec': {'Image': 'img:1'}}}
        with patch.object(motor, '_spec_servico', side_effect=[spec_web, None]):
            motor.sincronizar_servico_cron()
        criar = self._comandos()[-1]
        self.assertEqual(criar[criar.index('--replicas') + 1], '1')

    def test_atualizar_versao_fora_do_ar_nao_espera_o_web(self):
        cliente = _cliente(status='suspenso')
        motor = MotorProvisionamento(cliente)
        with patch.object(motor, '_aguardar_servico') as aguardar, patch.object(motor, '_aguardar_http') as http, \
                patch.object(motor, 'sincronizar_servico_cron') as cron:
            motor.atualizar_versao('0.0.149')
        aguardar.assert_not_called()
        http.assert_not_called()
        cron.assert_called_once_with('0.0.149')

    def test_suspender_sem_servico_de_cron_nao_falha(self):
        cliente = _cliente(status='suspenso')
        motor = MotorProvisionamento(cliente)
        with patch.object(motor, '_spec_servico', return_value=None):
            motor.suspender()
        self.assertEqual(self._comandos(), [['docker', 'service', 'scale', 'acme_web=0']])


class AdminDeStatusTests(TestCase):
    def setUp(self):
        self.client.force_login(get_user_model().objects.create_superuser('admin', 'a@a.com', 'x'))

    def test_pagina_carrega_a_confirmacao(self):
        cliente = _cliente()
        resposta = self.client.get(reverse('admin:registry_cliente_change', args=[cliente.pk]))
        self.assertContains(resposta, 'registry/confirma_status_cliente')
