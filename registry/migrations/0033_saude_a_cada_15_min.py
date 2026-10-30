from django.db import migrations

# A 0030 agendou a saúde dos clientes a cada 10 min; o padrão passa a 15 min. Só mexe se a tarefa ainda está no
# intervalo que a 0030 criou — um intervalo trocado à mão no admin é mantido.
TASK = 'registry.tasks.task_verificar_saude_todas'
NOME = 'Saúde dos clientes (/health/)'


def _trocar(apps, de, para, criar_se_ausente):
    IntervalSchedule = apps.get_model('django_celery_beat', 'IntervalSchedule')
    PeriodicTask = apps.get_model('django_celery_beat', 'PeriodicTask')
    novo, _ = IntervalSchedule.objects.get_or_create(every=para, period='minutes')
    tarefas = PeriodicTask.objects.filter(task=TASK)
    if tarefas.exists():
        mudou = tarefas.filter(interval__every=de, interval__period='minutes').update(interval=novo)
    elif criar_se_ausente and not PeriodicTask.objects.filter(name=NOME).exists():
        PeriodicTask.objects.create(name=NOME, task=TASK, interval=novo, enabled=True)
        mudou = 1
    else:
        mudou = 0
    if mudou:
        # Modelos históricos não disparam o signal que avisa o beat: marca a mudança à mão.
        from django.utils import timezone
        apps.get_model('django_celery_beat', 'PeriodicTasks').objects.update_or_create(
            ident=1, defaults={'last_update': timezone.now()},
        )


def para_15_min(apps, schema_editor):
    _trocar(apps, 10, 15, criar_se_ausente=True)


def para_10_min(apps, schema_editor):
    _trocar(apps, 15, 10, criar_se_ausente=False)


class Migration(migrations.Migration):

    dependencies = [
        ('registry', '0032_hosts_recursos_automaticos'),
        ('django_celery_beat', '0019_alter_periodictasks_options'),
    ]

    operations = [
        migrations.RunPython(para_15_min, para_10_min),
    ]
