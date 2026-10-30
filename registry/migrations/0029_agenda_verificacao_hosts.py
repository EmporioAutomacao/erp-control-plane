from django.db import migrations

NOME = 'Verificação dos hosts de infraestrutura'
TASK = 'registry.tasks.task_verificar_hosts'


def agendar(apps, schema_editor):
    """O status dos hosts só fica fresco com a verificação periódica: nasce agendada, a cada 5 minutos."""
    IntervalSchedule = apps.get_model('django_celery_beat', 'IntervalSchedule')
    PeriodicTask = apps.get_model('django_celery_beat', 'PeriodicTask')
    intervalo, _ = IntervalSchedule.objects.get_or_create(every=5, period='minutes')
    PeriodicTask.objects.get_or_create(name=NOME, defaults={'task': TASK, 'interval': intervalo, 'enabled': True})
    # Modelos históricos não disparam o signal que avisa o beat: marca a mudança à mão.
    from django.utils import timezone
    apps.get_model('django_celery_beat', 'PeriodicTasks').objects.update_or_create(
        ident=1, defaults={'last_update': timezone.now()},
    )


def desagendar(apps, schema_editor):
    apps.get_model('django_celery_beat', 'PeriodicTask').objects.filter(name=NOME).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('registry', '0028_status_hosts'),
        ('django_celery_beat', '0019_alter_periodictasks_options'),
    ]

    operations = [
        migrations.RunPython(agendar, desagendar),
    ]
