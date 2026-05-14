from celery import shared_task
from django.utils import timezone


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def task_provisionar_cliente(self, cliente_id):
    from .models import Cliente
    from .provisioning import MotorProvisionamento

    try:
        cliente = Cliente.objects.get(pk=cliente_id)
    except Cliente.DoesNotExist:
        return

    Cliente.objects.filter(pk=cliente_id).update(status='provisionando')
    cliente.refresh_from_db()

    motor = MotorProvisionamento(cliente)
    try:
        motor.executar()
    except Exception as exc:
        Cliente.objects.filter(pk=cliente_id).update(status='erro_provisao')
        raise self.retry(exc=exc)


@shared_task(bind=True, max_retries=2, default_retry_delay=30)
def task_atualizar_versao(self, cliente_id, versao_nova):
    from .models import Cliente, AtualizacaoVersao
    from .provisioning import MotorProvisionamento

    cliente = Cliente.objects.get(pk=cliente_id)
    atualizacao = AtualizacaoVersao.objects.create(
        cliente=cliente,
        versao_anterior=cliente.versao_erp,
        versao_nova=versao_nova,
        status='executando',
    )
    motor = MotorProvisionamento(cliente)
    try:
        motor.atualizar_versao(versao_nova)
        atualizacao.status = 'concluida'
        atualizacao.concluida_em = timezone.now()
        atualizacao.save(update_fields=['status', 'concluida_em'])
    except Exception as exc:
        atualizacao.status = 'erro'
        atualizacao.mensagem = str(exc)
        atualizacao.save(update_fields=['status', 'mensagem'])
        raise self.retry(exc=exc)


@shared_task
def task_suspender_cliente(cliente_id):
    from .models import Cliente
    from .provisioning import MotorProvisionamento

    cliente = Cliente.objects.get(pk=cliente_id)
    MotorProvisionamento(cliente).suspender()
    cliente.status = 'suspenso'
    cliente.data_suspensao = timezone.now().date()
    cliente.save(update_fields=['status', 'data_suspensao'])


@shared_task
def task_reativar_cliente(cliente_id):
    from .models import Cliente
    from .provisioning import MotorProvisionamento

    cliente = Cliente.objects.get(pk=cliente_id)
    MotorProvisionamento(cliente).reativar()
    cliente.status = 'ativo'
    cliente.data_suspensao = None
    cliente.save(update_fields=['status', 'data_suspensao'])


@shared_task
def task_verificar_saude_todas():
    from .models import Cliente

    clientes = Cliente.objects.filter(status__in=['ativo', 'trial'])
    for cliente in clientes:
        task_verificar_saude_cliente.delay(str(cliente.pk))


@shared_task
def task_verificar_saude_cliente(cliente_id):
    from .models import Cliente, VerificacaoSaude
    import urllib.request
    import time

    cliente = Cliente.objects.get(pk=cliente_id)
    url = f'{cliente.url}/health/'
    inicio = time.monotonic()
    try:
        resp = urllib.request.urlopen(url, timeout=10)
        status_http = resp.status
        online = status_http == 200
    except Exception:
        status_http = 0
        online = False
    latencia_ms = int((time.monotonic() - inicio) * 1000)

    VerificacaoSaude.objects.create(
        cliente=cliente,
        status_http=status_http,
        latencia_ms=latencia_ms,
        online=online,
    )
