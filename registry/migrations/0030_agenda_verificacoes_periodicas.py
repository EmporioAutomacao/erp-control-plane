from django.db import migrations

# Verificações que a documentação mandava criar à mão em Agendamentos → Tarefas Periódicas. Quando um ambiente
# ficava sem elas (ex.: criadas só no dev), o status/versão dos clientes nunca atualizava em produção.
AGENDA = [
    ('Saúde dos clientes (/health/)', 'registry.tasks.task_verificar_saude_todas', 10, 'minutes'),
    ('Versões e instalações dos clientes', 'registry.tasks.task_coletar_versoes_todas', 30, 'minutes'),
    ('Conexão com o armazenamento de backups', 'registry.tasks.task_verificar_armazenamento', 15, 'minutes'),
    ('Verificar atualizações do CP', 'registry.tasks.task_verificar_atualizacao', 6, 'hours'),
]


def agendar(apps, schema_editor):
    """Cria só o que falta: se já existe qualquer tarefa periódica para a mesma task (mesmo desativada, de
    propósito), ela é mantida como está — nem duplicada nem reativada."""
    IntervalSchedule = apps.get_model('django_celery_beat', 'IntervalSchedule')
    PeriodicTask = apps.get_model('django_celery_beat', 'PeriodicTask')
    criou = False
    for nome, task, every, period in AGENDA:
        if PeriodicTask.objects.filter(task=task).exists() or PeriodicTask.objects.filter(name=nome).exists():
            continue
        intervalo, _ = IntervalSchedule.objects.get_or_create(every=every, period=period)
        PeriodicTask.objects.create(name=nome, task=task, interval=intervalo, enabled=True)
        criou = True
    if criou:
        # Modelos históricos não disparam o signal que avisa o beat: marca a mudança à mão.
        from django.utils import timezone
        apps.get_model('django_celery_beat', 'PeriodicTasks').objects.update_or_create(
            ident=1, defaults={'last_update': timezone.now()},
        )


def desagendar(apps, schema_editor):
    apps.get_model('django_celery_beat', 'PeriodicTask').objects.filter(name__in=[n for n, *_ in AGENDA]).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('registry', '0029_agenda_verificacao_hosts'),
        ('django_celery_beat', '0019_alter_periodictasks_options'),
    ]

    operations = [
        migrations.RunPython(agendar, desagendar),
    ]
