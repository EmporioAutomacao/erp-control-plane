"""As verificações periódicas nascem agendadas pelas migrations — antes eram criadas à mão por ambiente."""
import importlib

from django.apps import apps
from django.test import TestCase
from django_celery_beat.models import IntervalSchedule, PeriodicTask

agenda = importlib.import_module('registry.migrations.0030_agenda_verificacoes_periodicas')

TASKS = [
    'registry.tasks.task_verificar_saude_todas',
    'registry.tasks.task_coletar_versoes_todas',
    'registry.tasks.task_verificar_armazenamento',
    'registry.tasks.task_verificar_atualizacao',
    'registry.tasks.task_vigiar_migracoes',
    'registry.tasks.task_verificar_hosts',
]


class AgendaPeriodicaTests(TestCase):
    def test_banco_novo_ja_tem_todas_as_verificacoes_ativas(self):
        for task in TASKS:
            self.assertTrue(PeriodicTask.objects.filter(task=task, enabled=True).exists(), task)

    def test_nao_duplica_nem_reativa_o_que_ja_existia(self):
        PeriodicTask.objects.filter(task='registry.tasks.task_coletar_versoes_todas').delete()
        intervalo, _ = IntervalSchedule.objects.get_or_create(every=1, period='hours')
        PeriodicTask.objects.create(name='coleta criada à mão', task='registry.tasks.task_coletar_versoes_todas',
                                    interval=intervalo, enabled=False)
        PeriodicTask.objects.filter(task='registry.tasks.task_verificar_saude_todas').delete()

        agenda.agendar(apps, None)

        coleta = PeriodicTask.objects.filter(task='registry.tasks.task_coletar_versoes_todas')
        self.assertEqual(list(coleta.values_list('name', 'enabled')), [('coleta criada à mão', False)])
        saude = PeriodicTask.objects.get(task='registry.tasks.task_verificar_saude_todas')
        self.assertEqual((saude.enabled, saude.interval.every, saude.interval.period), (True, 10, 'minutes'))
