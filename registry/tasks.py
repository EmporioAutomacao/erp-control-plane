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


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def task_sincronizar_versoes_agente(self, cliente_id):
    """Dispara SincronizadorVersoes.sincronizar() em background -- usado pelo
    push automatico (registry.signals, quando a curadoria de um cliente ou o
    catalogo mestre mudam) e reaproveitavel por qualquer outro chamador
    assincrono futuro. O botao manual do admin ("Sincronizar Versoes com o
    ERP") chama SincronizadorVersoes diretamente, sem passar por task --
    quer feedback imediato na tela, nao retry em background."""
    from .cp_push import SincronizadorVersoes
    from .models import Cliente

    try:
        cliente = Cliente.objects.get(pk=cliente_id)
    except Cliente.DoesNotExist:
        return

    registro = SincronizadorVersoes(cliente).sincronizar()
    if registro.status == 'erro':
        raise self.retry(exc=Exception(registro.mensagem_erro or 'Falha ao sincronizar versoes.'))


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


@shared_task(bind=True, max_retries=0)
def task_backup_cliente(self, cliente_id, backup_id):
    import io
    import shutil
    import subprocess
    from .models import Cliente, BackupCliente

    def _prog(pct):
        BackupCliente.objects.filter(pk=backup_id).update(progresso=pct)

    cliente = Cliente.objects.get(pk=cliente_id)
    slug = cliente.slug
    # .resolve() garante um caminho absoluto com drive letter no Windows -
    # necessario para os argumentos '-v'/paths passados a comandos docker.
    backup_base = (Path(os.getenv('CP_BACKUP_DIR', '/opt/backups/cp')) / 'clientes' / slug).resolve()
    backup_base.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    arquivo = backup_base / f'backup_{slug}_{timestamp}.tar.gz'

    avisos = []

    try:
        _prog(5)
        with tarfile.open(arquivo, 'w:gz') as tar:
            # 1. DB dump via container do banco
            _prog(10)
            r = subprocess.run(
                ['docker', 'ps', '-q', '-f', f'name={slug}_db'],
                capture_output=True, text=True,
            )
            db_ctr = r.stdout.strip().splitlines()[0] if r.stdout.strip() else None
            if db_ctr:
                dump = subprocess.run(
                    ['docker', 'exec', db_ctr,
                     'pg_dump', '--clean', '--if-exists', '-U', f'erp_{slug}', f'erp_{slug}'],
                    capture_output=True, timeout=300,
                )
                if dump.returncode == 0:
                    info = tarfile.TarInfo(name='db.sql')
                    data = dump.stdout
                    info.size = len(data)
                    tar.addfile(info, io.BytesIO(data))
                else:
                    stderr = dump.stderr.decode(errors='replace').strip()
                    logger.error('pg_dump falhou para %s (codigo %s): %s', slug, dump.returncode, stderr)
                    avisos.append(f'pg_dump falhou (codigo {dump.returncode}): {stderr[:300]}')
            else:
                avisos.append(f'Container {slug}_db nao encontrado - dump do banco nao incluido.')
            _prog(55)

            # 2. Arquivos de mídia via docker cp do container web
            r2 = subprocess.run(
                ['docker', 'ps', '-q', '-f', f'name={slug}_web'],
                capture_output=True, text=True,
            )
            web_ctr = r2.stdout.strip().splitlines()[0] if r2.stdout.strip() else None
            if web_ctr:
                media_tmp = backup_base / f'_media_tmp_{timestamp}'
                media_tmp.mkdir(parents=True, exist_ok=True)
                try:
                    cp = subprocess.run(
                        ['docker', 'cp', f'{web_ctr}:/app/media/.', str(media_tmp)],
                        capture_output=True, timeout=120,
                    )
                    if cp.returncode == 0:
                        tar.add(str(media_tmp), arcname='media')
                    else:
                        stderr = cp.stderr.decode(errors='replace').strip()
                        logger.error('docker cp da midia falhou para %s: %s', slug, stderr)
                        avisos.append(f'docker cp da midia falhou: {stderr[:300]}')
                finally:
                    shutil.rmtree(media_tmp, ignore_errors=True)
            else:
                avisos.append(f'Container {slug}_web nao encontrado - midia nao incluida.')
            _prog(80)

            # 3. Arquivos de configuração do cliente
            clientes_base = Path(os.getenv('CLIENTES_BASE_PATH', '/opt/clientes'))
            cliente_dir = clientes_base / slug
            if cliente_dir.exists():
                tar.add(str(cliente_dir), arcname='config')
            else:
                avisos.append('Diretorio de configuracao do cliente nao encontrado.')
            _prog(95)

            n_entries = len(tar.getmembers())

        if n_entries == 0:
            arquivo.unlink()
            mensagem = ' '.join(avisos) if avisos else 'Nenhum conteudo foi capturado para o backup.'
            BackupCliente.objects.filter(pk=backup_id).update(
                status='erro',
                progresso=100,
                mensagem=mensagem,
            )
            logger.error('Backup vazio para %s: %s', slug, mensagem)
            return

        tamanho = arquivo.stat().st_size
        BackupCliente.objects.filter(pk=backup_id).update(
            status='concluido',
            progresso=100,
            arquivo_path=str(arquivo),
            tamanho_bytes=tamanho,
            mensagem=' '.join(avisos),
        )
        logger.info('Backup concluido: %s (%.1f MB)', arquivo, tamanho / 1024 / 1024)
        if avisos:
            logger.warning('Backup de %s concluido com avisos: %s', slug, ' '.join(avisos))

        # Manter apenas os 3 mais recentes (arquivos e registros)
        todos_arquivos = sorted(backup_base.glob(f'backup_{slug}_*.tar.gz'), key=lambda p: p.stat().st_mtime)
        for antigo in todos_arquivos[:-3]:
            antigo.unlink()
        ids_manter = list(
            BackupCliente.objects.filter(cliente=cliente, status='concluido', origem='automatico')
            .order_by('-criado_em').values_list('pk', flat=True)[:3]
        )
        BackupCliente.objects.filter(
            cliente=cliente, status='concluido', origem='automatico',
        ).exclude(pk__in=ids_manter).delete()

    except Exception as exc:
        if arquivo.exists():
            arquivo.unlink()
        BackupCliente.objects.filter(pk=backup_id).update(status='erro', mensagem=str(exc))
        logger.error('Erro no backup de %s: %s', slug, exc)
        raise


@shared_task(bind=True, max_retries=0)
def task_restaurar_cliente(self, cliente_id, backup_id):
    import shutil
    import subprocess
    import time
    from .models import Cliente, BackupCliente

    def _prog(pct):
        BackupCliente.objects.filter(pk=backup_id).update(progresso=pct)

    cliente = Cliente.objects.get(pk=cliente_id)
    backup = BackupCliente.objects.get(pk=backup_id)
    slug = cliente.slug
    arquivo = Path(backup.arquivo_path)

    if not arquivo.exists():
        raise FileNotFoundError(f'Arquivo de backup não encontrado: {arquivo}')

    BackupCliente.objects.filter(pk=backup_id).update(restaurando=True, progresso=0)

    # .resolve() garante um caminho absoluto com drive letter no Windows -
    # sem isso, 'docker run -v <path>:...' rejeita o caminho ("not a valid
    # Windows path") mesmo que o Python consiga ler/escrever nele normalmente.
    restore_dir = (Path(os.getenv('CP_BACKUP_DIR', '/opt/backups/cp')) / f'_restore_{slug}').resolve()
    restore_dir.mkdir(parents=True, exist_ok=True)

    avisos = []

    try:
        # 1. Para o serviço web do cliente
        _prog(10)
        subprocess.run(['docker', 'service', 'scale', f'{slug}_web=0'], capture_output=True, timeout=60)
        time.sleep(10)

        # 2. Extrai o backup
        _prog(25)
        with tarfile.open(arquivo, 'r:gz') as tar:
            tar.extractall(restore_dir)

        # 3. Restaura o banco de dados
        _prog(40)
        db_sql = restore_dir / 'db.sql'
        r = subprocess.run(
            ['docker', 'ps', '-q', '-f', f'name={slug}_db'],
            capture_output=True, text=True,
        )
        db_ctr = r.stdout.strip().splitlines()[0] if r.stdout.strip() else None
        if not db_sql.exists():
            avisos.append('Backup nao contem db.sql - banco nao foi restaurado.')
        elif not db_ctr:
            avisos.append(f'Container {slug}_db nao encontrado - banco nao foi restaurado.')
        else:
            psql = subprocess.run(
                ['docker', 'exec', '-i', db_ctr,
                 'psql', '-v', 'ON_ERROR_STOP=1', '-U', f'erp_{slug}', '-d', f'erp_{slug}'],
                input=db_sql.read_bytes(),
                capture_output=True, timeout=300,
            )
            if psql.returncode != 0:
                stderr = psql.stderr.decode(errors='replace').strip()
                logger.error('Restauracao do banco falhou para %s: %s', slug, stderr)
                avisos.append(f'Restauracao do banco falhou: {stderr[:300]}')
        _prog(75)

        # 4. Restaura a mídia via container alpine temporário com acesso ao volume
        media_dir = restore_dir / 'media'
        if not media_dir.exists():
            avisos.append('Backup nao contem media/ - arquivos de midia nao foram restaurados.')
        else:
            docker_run = subprocess.run(
                [
                    'docker', 'run', '--rm',
                    '-v', f'{slug}_media:/app/media',
                    '-v', f'{str(media_dir)}:/restore/media:ro',
                    'alpine', 'sh', '-c',
                    'rm -rf /app/media/* && cp -rp /restore/media/. /app/media/',
                ],
                capture_output=True, timeout=120,
            )
            if docker_run.returncode != 0:
                stderr = docker_run.stderr.decode(errors='replace').strip()
                logger.error('Restauracao da midia falhou para %s: %s', slug, stderr)
                avisos.append(f'Restauracao da midia falhou: {stderr[:300]}')
        _prog(90)

    except Exception as exc:
        avisos.append(f'Erro inesperado: {exc}')
        raise
    finally:
        shutil.rmtree(restore_dir, ignore_errors=True)
        subprocess.run(['docker', 'service', 'scale', f'{slug}_web=1'], capture_output=True, timeout=60)
        BackupCliente.objects.filter(pk=backup_id).update(
            restaurando=False, progresso=100, mensagem_restauracao=' '.join(avisos),
        )
        if avisos:
            logger.warning('Restauracao de %s concluida com avisos: %s', slug, ' '.join(avisos))

    logger.info('Restauração concluída para %s a partir de %s', slug, arquivo.name)


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
