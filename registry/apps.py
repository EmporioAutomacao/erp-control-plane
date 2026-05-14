from django.apps import AppConfig


class RegistryConfig(AppConfig):
    name = 'registry'
    verbose_name = 'Cadastros'

    def ready(self):
        import registry.signals  # noqa
        from django.apps import apps
        apps.get_app_config('django_celery_beat').verbose_name = 'Tarefas Periódicas'
