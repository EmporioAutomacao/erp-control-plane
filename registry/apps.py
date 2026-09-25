from django.apps import AppConfig


class RegistryConfig(AppConfig):
    name = 'registry'
    verbose_name = 'Cadastros'

    def ready(self):
        import registry.signals  # noqa
        from django.apps import apps
        apps.get_app_config('django_celery_beat').verbose_name = 'Tarefas Periódicas'

        # m2m_changed em Cliente.versoes_permitidas e Cliente.modulos_ativos:
        # o sender e o through model auto-gerado pelo ManyToManyField, sem
        # nome estavel pra usar com @receiver(sender='app.Model') como os
        # outros sinais deste app.
        from django.db.models.signals import m2m_changed
        from .models import Cliente
        from .signals import modulos_ativos_changed, versoes_permitidas_changed
        m2m_changed.connect(versoes_permitidas_changed, sender=Cliente.versoes_permitidas.through)
        m2m_changed.connect(modulos_ativos_changed, sender=Cliente.modulos_ativos.through)
