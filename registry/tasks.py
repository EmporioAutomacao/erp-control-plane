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
def task_sincronizar_versoes_agente(self, cliente_id, iniciada_por_id=None, com_retry=True):
    """Dispara SincronizadorVersoes.sincronizar() em background. Dois chamadores:

    - push automatico (registry.signals, quando a curadoria de um cliente ou o
      catalogo mestre mudam): `.delay(str(cliente_id))` -> com_retry=True,
      iniciada_por_id=None. Retenta 3x em erro.
    - botao manual do admin ("Sincronizar Versoes com o ERP"):
      `.delay(str(pk), iniciada_por_id=request.user.id, com_retry=False)`.
      Passa por task (nao mais sincrono) so pra gravar o log linha a linha e o
      admin mostrar "ao vivo" via polling; NAO retenta (o operador ve o erro na
      hora e reclica se quiser)."""
    from django.contrib.auth import get_user_model
    from .cp_push import SincronizadorVersoes
    from .models import Cliente

    try:
        cliente = Cliente.objects.get(pk=cliente_id)
    except Cliente.DoesNotExist:
        return

    iniciada_por = None
    if iniciada_por_id:
        iniciada_por = get_user_model().objects.filter(pk=iniciada_por_id).first()

    registro = SincronizadorVersoes(cliente, iniciada_por=iniciada_por).sincronizar()
    if registro.status == 'erro' and com_retry:
        raise self.retry(exc=Exception(registro.mensagem_erro or 'Falha ao sincronizar versoes.'))


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def task_sincronizar_plano_cliente(self, cliente_id):
    """Dispara SincronizadorPlano.sincronizar() em background. Chamada pelos
    signals de registry.signals quando plano/status/modulos_ativos de um
    Cliente mudam (ver SincronizadorPlano em cp_push.py). Retenta 3x em
    erro -- ao contrario do botao manual (view sincrona, sem task), aqui
    ninguem esta olhando a tela na hora."""
    from .cp_push import SincronizadorPlano
    from .models import Cliente

    try:
        cliente = Cliente.objects.get(pk=cliente_id)
    except Cliente.DoesNotExist:
        return

    resultado = SincronizadorPlano(cliente).sincronizar()
    if not resultado['sucesso']:
        raise self.retry(exc=Exception(resultado['mensagem']))


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
def task_coletar_versoes_cliente(cliente_id):
    """Consulta o erp do cliente (GET /v1/cp/status, fallback /health/) e grava
    a versão do ERP em execução + as instalações do SyncAgent/PDV.
    Ver registry.cp_pull.ColetorVersoes. Alimenta o painel "Versão Atual"."""
    from .cp_pull import ColetorVersoes
    from .models import Cliente

    try:
        cliente = Cliente.objects.get(pk=cliente_id)
    except Cliente.DoesNotExist:
        return
    ColetorVersoes(cliente).coletar()


@shared_task
def task_coletar_versoes_todas():
    """Agendável no django-celery-beat (ex.: a cada 30 min) — mesmo padrão do
    health check. Varre só clientes ativos/trial."""
    from .models import Cliente

    for cliente in Cliente.objects.filter(status__in=['ativo', 'trial']):
        task_coletar_versoes_cliente.delay(str(cliente.pk))


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
def task_iniciar_backup_cliente(cliente_id, backup_id):
    """Etapa do manager: lê o config do cliente e despacha o backup para o nó certo.

    O `.env`/`stack.yml` do cliente só existem em /opt/clientes do manager; o dump e a
    mídia só são visíveis no daemon Docker do nó que hospeda o cliente. Por isso o
    config segue como argumento (pequeno) para a task que roda no nó.
    """
    import base64
    from .models import Cliente, BackupCliente
    from .nos import fila_do_cliente

    cliente = Cliente.objects.get(pk=cliente_id)
    cliente_dir = Path(os.getenv('CLIENTES_BASE_PATH', '/opt/clientes')) / cliente.slug

    config_files = {}
    if cliente_dir.exists():
        for arq in cliente_dir.rglob('*'):
            if arq.is_file() and arq.stat().st_size <= 1024 * 1024:
                config_files[arq.relative_to(cliente_dir).as_posix()] = (
                    base64.b64encode(arq.read_bytes()).decode('ascii')
                )

    fila = fila_do_cliente(cliente.slug)
    logger.info('Backup de %s despachado para a fila %s', cliente.slug, fila)
    try:
        task_backup_cliente.apply_async(
            args=[str(cliente_id), backup_id], kwargs={'config_files': config_files}, queue=fila,
        )
    except Exception as exc:
        BackupCliente.objects.filter(pk=backup_id).update(
            status='erro', progresso=100, mensagem=f'Falha ao despachar o backup para a fila {fila}: {exc}',
        )
        raise


@shared_task(bind=True, max_retries=0)
def task_backup_cliente(self, cliente_id, backup_id, config_files=None):
    """Executa o backup. Deve rodar no worker do nó que hospeda `{slug}_db`/`{slug}_web`.

    `config_files` ({caminho_relativo: conteudo_base64}) vem de `task_iniciar_backup_cliente`;
    quando None (chamada direta/dev), o config é lido do disco local.
    """
    import base64
    import io
    import shutil
    import socket
    import subprocess
    from . import storage_backup
    from .models import Cliente, BackupCliente
    from .nos import container_local

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
            db_ctr = container_local(slug, 'db')
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
            web_ctr = container_local(slug, 'web')
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
            if config_files is not None:
                if config_files:
                    for rel, conteudo in config_files.items():
                        data = base64.b64decode(conteudo)
                        info = tarfile.TarInfo(name=f'config/{rel}')
                        info.size = len(data)
                        tar.addfile(info, io.BytesIO(data))
                else:
                    avisos.append('Diretorio de configuracao do cliente nao encontrado.')
            else:
                clientes_base = Path(os.getenv('CLIENTES_BASE_PATH', '/opt/clientes'))
                cliente_dir = clientes_base / slug
                if cliente_dir.exists():
                    tar.add(str(cliente_dir), arcname='config')
                else:
                    avisos.append('Diretorio de configuracao do cliente nao encontrado.')
            _prog(90)

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

        # 4. Envia para o bucket (o disco do nó é só temporário) ou, sem bucket
        # configurado (dev de nó único), mantém o arquivo local.
        if storage_backup.configurado():
            caminho = storage_backup.enviar(arquivo, f'clientes/{slug}/{arquivo.name}')
            arquivo.unlink()
        else:
            caminho = str(arquivo)
            logger.warning('BACKUP_S3_* nao configurado: backup de %s mantido apenas no disco do no.', slug)

        BackupCliente.objects.filter(pk=backup_id).update(
            status='concluido',
            progresso=100,
            arquivo_path=caminho,
            tamanho_bytes=tamanho,
            mensagem=' '.join(avisos),
            no=os.getenv('NODE_HOSTNAME') or socket.gethostname(),
        )
        logger.info('Backup concluido: %s (%.1f MB)', caminho, tamanho / 1024 / 1024)
        if avisos:
            logger.warning('Backup de %s concluido com avisos: %s', slug, ' '.join(avisos))

        # Manter apenas os 3 mais recentes (arquivos/objetos e registros)
        todos_arquivos = sorted(backup_base.glob(f'backup_{slug}_*.tar.gz'), key=lambda p: p.stat().st_mtime)
        for antigo in todos_arquivos[:-3]:
            antigo.unlink()
        ids_manter = list(
            BackupCliente.objects.filter(cliente=cliente, status='concluido', origem='automatico')
            .order_by('-criado_em').values_list('pk', flat=True)[:3]
        )
        excedentes = BackupCliente.objects.filter(
            cliente=cliente, status='concluido', origem='automatico',
        ).exclude(pk__in=ids_manter)
        for antigo in excedentes:
            if storage_backup.eh_remoto(antigo.arquivo_path):
                try:
                    storage_backup.apagar(antigo.arquivo_path)
                except Exception as exc:
                    logger.warning('Nao foi possivel apagar %s do bucket: %s', antigo.arquivo_path, exc)
        excedentes.delete()

    except Exception as exc:
        if arquivo.exists():
            arquivo.unlink()
        BackupCliente.objects.filter(pk=backup_id).update(status='erro', mensagem=str(exc))
        logger.error('Erro no backup de %s: %s', slug, exc)
        raise


def _scale_web(slug, replicas):
    import subprocess
    subprocess.run(['docker', 'service', 'scale', f'{slug}_web={replicas}'], capture_output=True, timeout=60)


@shared_task(bind=True, max_retries=0)
def task_restaurar_cliente(self, cliente_id, backup_id):
    """Etapa do manager: derruba o web e despacha a restauração para o nó do cliente.

    `docker service scale` só funciona em nó manager, enquanto o `psql`/volume de
    mídia só existem no nó do cliente — por isso a restauração é dividida em
    manager (esta task) → nó (`task_restaurar_no`) → manager (`task_finalizar_restauracao`).
    """
    import time
    from .models import Cliente, BackupCliente
    from .nos import fila_do_cliente

    cliente = Cliente.objects.get(pk=cliente_id)
    slug = cliente.slug

    BackupCliente.objects.filter(pk=backup_id).update(restaurando=True, progresso=0)
    try:
        BackupCliente.objects.filter(pk=backup_id).update(progresso=10)
        _scale_web(slug, 0)
        time.sleep(10)
        fila = fila_do_cliente(slug)
        logger.info('Restauracao de %s despachada para a fila %s', slug, fila)
        task_restaurar_no.apply_async(args=[str(cliente_id), backup_id], queue=fila)
    except Exception as exc:
        # nada foi restaurado: religa o web e libera o backup
        _scale_web(slug, 1)
        BackupCliente.objects.filter(pk=backup_id).update(
            restaurando=False, progresso=100, mensagem_restauracao=f'Erro inesperado: {exc}',
        )
        raise


@shared_task(bind=True, max_retries=0)
def task_restaurar_no(self, cliente_id, backup_id):
    """Etapa do nó: baixa o backup e restaura banco e mídia. Sempre aciona a finalização."""
    import shutil
    import subprocess
    from . import storage_backup
    from .models import Cliente, BackupCliente
    from .nos import container_local

    def _prog(pct):
        BackupCliente.objects.filter(pk=backup_id).update(progresso=pct)

    cliente = Cliente.objects.get(pk=cliente_id)
    backup = BackupCliente.objects.get(pk=backup_id)
    slug = cliente.slug

    # .resolve() garante um caminho absoluto com drive letter no Windows -
    # sem isso, 'docker run -v <path>:...' rejeita o caminho ("not a valid
    # Windows path") mesmo que o Python consiga ler/escrever nele normalmente.
    restore_dir = (Path(os.getenv('CP_BACKUP_DIR', '/opt/backups/cp')) / f'_restore_{slug}').resolve()
    restore_dir.mkdir(parents=True, exist_ok=True)

    avisos = []

    try:
        # 1. Obtém o arquivo (do bucket ou do disco local)
        _prog(20)
        if storage_backup.eh_remoto(backup.arquivo_path):
            arquivo = restore_dir / '_backup.tar.gz'
            storage_backup.baixar(backup.arquivo_path, arquivo)
        else:
            arquivo = Path(backup.arquivo_path)
            if not arquivo.exists():
                raise FileNotFoundError(f'Arquivo de backup não encontrado: {arquivo}')

        # 2. Extrai o backup
        _prog(25)
        with tarfile.open(arquivo, 'r:gz') as tar:
            tar.extractall(restore_dir)

        # 3. Restaura o banco de dados
        _prog(40)
        db_sql = restore_dir / 'db.sql'
        db_ctr = container_local(slug, 'db')
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
        BackupCliente.objects.filter(pk=backup_id).update(mensagem_restauracao=' '.join(avisos))
        if avisos:
            logger.warning('Restauracao de %s concluida com avisos: %s', slug, ' '.join(avisos))
        # Religar o web é feito no manager, mesmo se a restauração falhou
        task_finalizar_restauracao.apply_async(args=[str(cliente_id), backup_id])

    logger.info('Restauração concluída para %s a partir de %s', slug, backup.arquivo_path)


@shared_task
def task_finalizar_restauracao(cliente_id, backup_id):
    """Etapa do manager: religa o web do cliente e libera o backup."""
    from .models import Cliente, BackupCliente

    cliente = Cliente.objects.get(pk=cliente_id)
    try:
        _scale_web(cliente.slug, 1)
    finally:
        BackupCliente.objects.filter(pk=backup_id).update(restaurando=False, progresso=100)


@shared_task
def task_verificar_saude_cliente(cliente_id):
    from .models import Cliente, VerificacaoSaude
    import json
    import urllib.request
    import time

    cliente = Cliente.objects.get(pk=cliente_id)
    url = f'{cliente.url}/health/'
    inicio = time.monotonic()
    versao_erp = None
    try:
        resp = urllib.request.urlopen(url, timeout=10)
        status_http = resp.status
        online = status_http == 200
        # O corpo de /health/ é {"status":"ok","version":"X.Y.Z"} — de graça,
        # já que a resposta está aqui. Mantém a versão do ERP fresca mesmo sem
        # a coleta completa (task_coletar_versoes_*) agendada.
        try:
            corpo = json.loads(resp.read().decode('utf-8'))
            versao_erp = str(corpo.get('version') or '') or None
        except Exception:
            pass
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
    if versao_erp:
        Cliente.objects.filter(pk=cliente_id).update(versao_erp_detectada=versao_erp[:20])
