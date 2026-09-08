from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver


@receiver(post_save, sender='registry.Cliente')
def cliente_criado(sender, instance, created, **kwargs):
    if created and instance.status == 'aguardando_provisao':
        from .tasks import task_provisionar_cliente
        task_provisionar_cliente.delay(str(instance.pk))


@receiver(pre_save, sender='registry.Cliente')
def _cliente_captura_auto_update(sender, instance, **kwargs):
    """Guarda o valor de `atualizacao_automatica_agente` que esta no banco
    antes deste save, pra `_cliente_auto_update_alterado` saber se o toggle
    mudou (post_save nao recebe o estado anterior)."""
    if not instance.pk:
        instance._auto_update_anterior = False
        return
    anterior = (
        sender.objects.filter(pk=instance.pk)
        .values_list('atualizacao_automatica_agente', flat=True)
        .first()
    )
    instance._auto_update_anterior = bool(anterior)


@receiver(post_save, sender='registry.Cliente')
def _cliente_auto_update_alterado(sender, instance, created, **kwargs):
    """Liga/desliga o modo "atualizacao automatica" -> reenvia a curadoria pro
    erp do cliente na hora. Ligar passa a mandar o catalogo inteiro; desligar
    volta a mandar so `versoes_permitidas` (ver cp_push.SincronizadorVersoes).

    So o toggle via admin dispara isto -- `Cliente.objects.filter().update()`
    (provisionamento, tasks) nao aciona signals."""
    if created:
        return
    if getattr(instance, '_auto_update_anterior', False) == instance.atualizacao_automatica_agente:
        return
    if not instance.integracao_secret:
        return

    from .tasks import task_sincronizar_versoes_agente
    task_sincronizar_versoes_agente.delay(str(instance.pk))


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
    """Mudanca no catalogo mestre -> reenvia a curadoria pros clientes afetados:

    - clientes em modo AUTOMATICO recebem qualquer mudanca, inclusive uma
      versao NOVA (created=True) -- e assim que um release recem-descoberto
      (botao "Verificar novas versoes no GitHub" / CI do pdv-local) chega
      sozinho no erp deles;
    - clientes com curadoria MANUAL so re-sincronizam quando a versao ja esta
      na curadoria deles (corrigir sha256, aposentar com ativo=False, trocar
      erp_minimo etc.) -- nunca no created (versao nova ainda nao curada).
    """
    from .models import Cliente
    from .tasks import task_sincronizar_versoes_agente

    auto = (
        Cliente.objects.filter(atualizacao_automatica_agente=True)
        .exclude(integracao_secret='')
        .values_list('pk', flat=True)
    )
    curados = (
        ()
        if created
        else instance.clientes.exclude(integracao_secret='').values_list('pk', flat=True)
    )

    for cliente_id in set(auto) | set(curados):
        task_sincronizar_versoes_agente.delay(str(cliente_id))
