from django.db.models.signals import post_save
from django.dispatch import receiver


@receiver(post_save, sender='registry.Cliente')
def cliente_criado(sender, instance, created, **kwargs):
    if created and instance.status == 'aguardando_provisao':
        from .tasks import task_provisionar_cliente
        task_provisionar_cliente.delay(str(instance.pk))
