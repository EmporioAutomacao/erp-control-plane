"""Verificações de Saúde: só o processo automático grava; a tela é somente leitura."""
import importlib
import urllib.error
from datetime import timedelta
from unittest.mock import MagicMock, patch

from django.apps import apps
from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone
from django_celery_beat.models import IntervalSchedule, PeriodicTask

from registry import tasks
from registry.models import Cliente, Plano, VerificacaoSaude

agenda = importlib.import_module('registry.migrations.0033_saude_a_cada_15_min')
TASK = 'registry.tasks.task_verificar_saude_todas'


def _cliente():
    plano = Plano.objects.create(slug='basico', nome='Básico', preco_mensal=10)
    return Cliente.objects.create(
        nome='Acme', slug='acme', cnpj='12.345.678/0001-90', email_contato='acme@example.com',
        subdominio='acme.ararasuite.com.br', plano=plano, status='ativo',
    )


class AdminSomenteLeituraTests(TestCase):
    def setUp(self):
        self.c = Client()
        self.c.force_login(get_user_model().objects.create_superuser('admin', 'admin@example.com', 'senha'))
        self.cliente = _cliente()

    def test_sem_cadastro_edicao_nem_exclusao(self):
        v = VerificacaoSaude.objects.create(cliente=self.cliente, status_http=200, latencia_ms=120, online=True)
        self.assertEqual(self.c.get(reverse('admin:registry_verificacaosaude_add')).status_code, 403)
        self.assertEqual(self.c.post(reverse('admin:registry_verificacaosaude_delete', args=[v.pk])).status_code, 403)
        self.assertEqual(self.c.post(reverse('admin:registry_verificacaosaude_change', args=[v.pk]),
                                     {'status_http': 500}).status_code, 403)
        v.refresh_from_db()
        self.assertEqual(v.status_http, 200)

        resp = self.c.get(reverse('admin:registry_verificacaosaude_changelist'))
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, reverse('admin:registry_verificacaosaude_add'))
        self.assertContains(resp, '120 ms')

    def test_verificar_agora_dispara_a_task(self):
        with patch.object(tasks.task_verificar_saude_todas, 'delay') as delay:
            self.c.get(reverse('admin:registry_verificacaosaude_verificar_agora'))
            delay.assert_not_called()
            resp = self.c.post(reverse('admin:registry_verificacaosaude_verificar_agora'))
        delay.assert_called_once_with()
        self.assertRedirects(resp, reverse('admin:registry_verificacaosaude_changelist'))


class TaskSaudeTests(TestCase):
    def setUp(self):
        self.cliente = _cliente()

    def _verificar(self, efeito):
        with patch('urllib.request.urlopen', side_effect=efeito) as urlopen:
            tasks.task_verificar_saude_cliente(str(self.cliente.pk))
        self.urlopen = urlopen
        return VerificacaoSaude.objects.get(cliente=self.cliente)

    def test_online_grava_latencia_e_versao(self):
        resp = MagicMock(status=200)
        resp.read.return_value = b'{"status":"ok","version":"0.0.150"}'
        v = self._verificar([resp])
        self.assertEqual((v.online, v.status_http), (True, 200))
        self.assertIsNotNone(v.latencia_ms)
        self.cliente.refresh_from_db()
        self.assertEqual(self.cliente.versao_erp_detectada, '0.0.150')

    def test_envia_user_agent_proprio(self):
        # O Cloudflare devolve 403 para "Python-urllib/x.y" (visto contra um cliente real em produção).
        self._verificar(TimeoutError())
        req = self.urlopen.call_args.args[0]
        self.assertEqual(req.full_url, 'https://acme.ararasuite.com.br/health/')
        self.assertTrue(req.get_header('User-agent').startswith('AraraSuite-CP/'))

    def test_erro_http_guarda_o_codigo_real(self):
        v = self._verificar(urllib.error.HTTPError('u', 503, 'Service Unavailable', {}, None))
        self.assertEqual((v.online, v.status_http), (False, 503))
        self.assertIsNotNone(v.latencia_ms)

    def test_sem_resposta_nao_tem_latencia(self):
        v = self._verificar(TimeoutError('timed out'))
        self.assertEqual((v.online, v.status_http, v.latencia_ms), (False, 0, None))

    def test_retencao_apaga_so_o_que_passou_de_30_dias(self):
        antiga = VerificacaoSaude.objects.create(cliente=self.cliente, online=True)
        recente = VerificacaoSaude.objects.create(cliente=self.cliente, online=True)
        VerificacaoSaude.objects.filter(pk=antiga.pk).update(verificado_em=timezone.now() - timedelta(days=31))
        VerificacaoSaude.objects.filter(pk=recente.pk).update(verificado_em=timezone.now() - timedelta(days=29))
        with patch.object(tasks.task_verificar_saude_cliente, 'delay') as delay:
            tasks.task_verificar_saude_todas()
        delay.assert_called_once_with(str(self.cliente.pk))
        self.assertEqual(list(VerificacaoSaude.objects.values_list('pk', flat=True)), [recente.pk])


class AgendaQuinzeMinutosTests(TestCase):
    def _intervalo(self):
        t = PeriodicTask.objects.get(task=TASK)
        return t.interval.every, t.interval.period

    def test_banco_novo_fica_em_15_min(self):
        self.assertEqual(self._intervalo(), (15, 'minutes'))

    def test_padrao_antigo_de_10_min_passa_a_15(self):
        dez, _ = IntervalSchedule.objects.get_or_create(every=10, period='minutes')
        PeriodicTask.objects.filter(task=TASK).update(interval=dez)
        agenda.para_15_min(apps, None)
        self.assertEqual(self._intervalo(), (15, 'minutes'))

    def test_intervalo_trocado_a_mao_e_mantido(self):
        hora, _ = IntervalSchedule.objects.get_or_create(every=1, period='hours')
        PeriodicTask.objects.filter(task=TASK).update(interval=hora)
        agenda.para_15_min(apps, None)
        self.assertEqual(self._intervalo(), (1, 'hours'))

    def test_cria_se_nao_existir(self):
        PeriodicTask.objects.filter(task=TASK).delete()
        agenda.para_15_min(apps, None)
        self.assertEqual(self._intervalo(), (15, 'minutes'))
        self.assertTrue(PeriodicTask.objects.get(task=TASK).enabled)
