import logging
import os
import tarfile
from datetime import datetime
from pathlib import Path

from celery import shared_task
from django.utils import timezone

logger = logging.getLogger(__name__)


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
def task_backup_clientes():
    clientes_base = Path(os.getenv('CLIENTES_BASE_PATH', '/opt/clientes'))
    backup_dir = Path(os.getenv('CP_BACKUP_DIR', '/opt/backups/cp'))
    manter = int(os.getenv('CP_BACKUP_MANTER', '7'))

    if not clientes_base.exists():
        logger.warning('CLIENTES_BASE_PATH não existe: %s', clientes_base)
        return

    backup_dir.mkdir(parents=True, exist_ok=True)

    nome = f"clientes_{datetime.now().strftime('%Y%m%d_%H%M%S')}.tar.gz"
    destino = backup_dir / nome

    with tarfile.open(destino, 'w:gz') as tar:
        tar.add(clientes_base, arcname='clientes')

    tamanho_mb = destino.stat().st_size / 1024 / 1024
    logger.info('Backup criado: %s (%.1f MB)', destino, tamanho_mb)

    # Remove backups antigos mantendo apenas os N mais recentes
    backups = sorted(backup_dir.glob('clientes_*.tar.gz'), key=lambda p: p.stat().st_mtime)
    for antigo in backups[:-manter]:
        antigo.unlink()
        logger.info('Backup antigo removido: %s', antigo)


@shared_task
def task_enviar_email_boas_vindas(cliente_id):
    from .models import Cliente
    from .email import enviar_email_boas_vindas

    try:
        cliente = Cliente.objects.get(pk=cliente_id)
    except Cliente.DoesNotExist:
        return
    try:
        enviar_email_boas_vindas(cliente)
    except Exception as exc:
        logger.error('Falha ao enviar e-mail de boas-vindas para %s: %s', cliente_id, exc)


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
