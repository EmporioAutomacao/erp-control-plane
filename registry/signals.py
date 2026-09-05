from django.db.models.signals import post_save
from django.dispatch import receiver


@receiver(post_save, sender='registry.Cliente')
def cliente_criado(sender, instance, created, **kwargs):
    if created and instance.status == 'aguardando_provisao':
        from .tasks import task_provisionar_cliente
        task_provisionar_cliente.delay(str(instance.pk))


def versoes_permitidas_changed(sender, instance, action, **kwargs):
    """Dispara o push automatico de versoes permitidas pro erp do cliente
    sempre que a curadoria muda (Cliente.versoes_permitidas, editado pelo
    admin via filter_horizontal). Full-sync (ver SincronizadorVersoes): nao
    importa se foi post_add/post_remove/post_clear, sempre reenvia o
    conjunto atual inteiro.

    Conectado imperativamente em RegistryConfig.ready() -- o `sender` aqui e
    o through model auto-gerado pelo ManyToManyField, que nao tem um nome
    estavel pra usar com @receiver(sender='app.Model').
    """
    if action not in ('post_add', 'post_remove', 'post_clear'):
        return

    if not instance.integracao_secret:
        # Sem segredo ainda -- nada a sincronizar. Fica pendente ate alguem
        # clicar "Aplicar Configuracoes" (gera o segredo) ou reprovisionar.
        return

    from .tasks import task_sincronizar_versoes_agente
    task_sincronizar_versoes_agente.delay(str(instance.pk))


@receiver(post_save, sender='registry.VersaoAgente')
def versao_agente_atualizada(sender, instance, created, **kwargs):
    """Se o catalogo mestre mudar (corrigir um sha256, aposentar uma versao
    com `ativo=False`, trocar a versao minima de ERP exigida etc.), reenvia
    a curadoria de todo cliente que tenha essa versao marcada como permitida
    -- senao a mudanca so chegaria no erp de cada cliente na proxima vez que
    alguem mexesse na curadoria dele especificamente."""
    if created:
        return  # versao nova ainda nao esta na curadoria de ninguem

    from .tasks import task_sincronizar_versoes_agente
    clientes_afetados = instance.clientes.exclude(integracao_secret='').values_list('pk', flat=True)
    for cliente_id in clientes_afetados:
        task_sincronizar_versoes_agente.delay(str(cliente_id))
