from django.db import migrations

NOME = 'Vigia das migrações de servidor'
TASK = 'registry.tasks.task_vigiar_migracoes'


def agendar(apps, schema_editor):
    """O vigia é parte da segurança da migração (gate G12): nasce agendado, a cada 1 minuto."""
    IntervalSchedule = apps.get_model('django_celery_beat', 'IntervalSchedule')
    PeriodicTask = apps.get_model('django_celery_beat', 'PeriodicTask')
    intervalo, _ = IntervalSchedule.objects.get_or_create(every=1, period='minutes')
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
        ('registry', '0026_migracao_cliente'),
        ('django_celery_beat', '0019_alter_periodictasks_options'),
    ]

    operations = [
        migrations.RunPython(agendar, desagendar),
    ]
