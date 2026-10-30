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


def _adiar_se_migrando(task, cliente_id, args):
    """Se o cliente estiver em migração de servidor, reagenda a task para daqui a 5 min."""
    from .models import MigracaoCliente
    if not MigracaoCliente.objects.filter(cliente_id=cliente_id).exclude(status__in=MigracaoCliente.STATUS_FINAIS).exists():
        return False
    logger.warning('%s adiada: cliente %s em migracao de servidor', task.name, cliente_id)
    task.apply_async(args=args, countdown=300)
    return True


@shared_task(bind=True, max_retries=2, default_retry_delay=30)
def task_atualizar_versao(self, cliente_id, versao_nova):
    if _adiar_se_migrando(task_atualizar_versao, cliente_id, [cliente_id, versao_nova]):
        return
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
    if _adiar_se_migrando(task_suspender_cliente, cliente_id, [cliente_id]):
        return
    from .models import Cliente
    from .provisioning import MotorProvisionamento

    cliente = Cliente.objects.get(pk=cliente_id)
    MotorProvisionamento(cliente).suspender()
    cliente.status = 'suspenso'
    cliente.data_suspensao = timezone.now().date()
    cliente.save(update_fields=['status', 'data_suspensao'])


@shared_task
def task_reativar_cliente(cliente_id):
    if _adiar_se_migrando(task_reativar_cliente, cliente_id, [cliente_id]):
        return
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
def task_iniciar_backup_cliente(cliente_id, backup_id, migracao_id=None):
    """Etapa do manager: lê o config do cliente e despacha o backup para o nó certo.

    O `.env`/`stack.yml` do cliente só existem em /opt/clientes do manager; o dump e a
    mídia só são visíveis no daemon Docker do nó que hospeda o cliente. Por isso o
    config segue como argumento (pequeno) para a task que roda no nó.
    """
    import base64
    from . import storage_backup
    from .log_operacao import LogOperacao
    from .models import Cliente, BackupCliente
    from .nos import fila_do_cliente

    log = LogOperacao(backup_id)
    log.linha('Backup solicitado. Verificando a configuração do armazenamento…')
    if not storage_backup.configurado():
        log.linha(f'ERRO: {storage_backup.MSG_NAO_CONFIGURADO}')
        BackupCliente.objects.filter(pk=backup_id).update(
            status='erro', progresso=100, mensagem=storage_backup.MSG_NAO_CONFIGURADO,
        )
        logger.error('Backup %s abortado: %s', backup_id, storage_backup.MSG_NAO_CONFIGURADO)
        if migracao_id:
            _avisar_migracao_backup(migracao_id, backup_id)
        return

    cliente = Cliente.objects.get(pk=cliente_id)
    cliente_dir = Path(os.getenv('CLIENTES_BASE_PATH', '/opt/clientes')) / cliente.slug
    log.linha(f'Cliente "{cliente.slug}": lendo o .env e o stack.yml…')

    config_files = {}
    if cliente_dir.exists():
        for arq in cliente_dir.rglob('*'):
            if arq.is_file() and arq.stat().st_size <= 1024 * 1024:
                config_files[arq.relative_to(cliente_dir).as_posix()] = (
                    base64.b64encode(arq.read_bytes()).decode('ascii')
                )

    log.linha(f'Configuração lida ({len(config_files)} arquivo(s)). Descobrindo o nó do cliente…')
    fila = fila_do_cliente(cliente.slug)
    logger.info('Backup de %s despachado para a fila %s', cliente.slug, fila)
    try:
        task_backup_cliente.apply_async(
            args=[str(cliente_id), backup_id],
            kwargs={'config_files': config_files, 'migracao_id': migracao_id}, queue=fila,
        )
        log.linha(f'Tarefa enviada à fila "{fila}". Aguardando um worker do nó iniciar…')
    except Exception as exc:
        log.linha(f'ERRO: falha ao despachar o backup para a fila {fila}: {exc}')
        BackupCliente.objects.filter(pk=backup_id).update(
            status='erro', progresso=100, mensagem=f'Falha ao despachar o backup para a fila {fila}: {exc}',
        )
        if migracao_id:
            _avisar_migracao_backup(migracao_id, backup_id)
        raise


@shared_task(bind=True, max_retries=0)
def task_backup_cliente(self, cliente_id, backup_id, config_files=None, migracao_id=None):
    """Executa o backup. Deve rodar no worker do nó que hospeda `{slug}_db` e os volumes.

    `config_files` ({caminho_relativo: conteudo_base64}) vem de `task_iniciar_backup_cliente`;
    quando None (chamada direta/dev), o config é lido do disco local.
    `migracao_id`: o resultado (status, avisos, sha256) volta para a migração no manager.
    """
    try:
        _executar_backup(cliente_id, backup_id, config_files)
    finally:
        if migracao_id:
            _avisar_migracao_backup(migracao_id, backup_id)


def _avisar_migracao_backup(migracao_id, backup_id):
    from .models import BackupCliente
    b = BackupCliente.objects.filter(pk=backup_id).values('status', 'mensagem', 'sha256').first() or {}
    task_migracao_avancar.apply_async(
        args=[migracao_id, 'backup_terminado', {
            'status': b.get('status', 'erro'), 'avisos': b.get('mensagem', ''), 'sha256': b.get('sha256', ''),
        }],
        queue='celery',
    )


def _executar_backup(cliente_id, backup_id, config_files=None):
    import base64
    import io
    import shutil
    import socket
    import subprocess
    import time
    from . import storage_backup
    from .log_operacao import LogOperacao, Progresso, fmt_bytes, rodar_com_pulso, tamanho_pasta
    from .models import Cliente, BackupCliente
    from .nos import container_local
    from .volumes import copiar_volume_para_pasta, sha256_arquivo

    def _prog(pct):
        BackupCliente.objects.filter(pk=backup_id).update(progresso=pct)

    log = LogOperacao(backup_id)
    no_host = os.getenv('NODE_HOSTNAME') or socket.gethostname()
    log.linha(f'Worker "{no_host}" iniciou o backup.')

    if not storage_backup.configurado():
        log.linha(f'ERRO: {storage_backup.MSG_NAO_CONFIGURADO}')
        BackupCliente.objects.filter(pk=backup_id).update(
            status='erro', progresso=100, mensagem=storage_backup.MSG_NAO_CONFIGURADO,
        )
        logger.error('Backup %s abortado: %s', backup_id, storage_backup.MSG_NAO_CONFIGURADO)
        return

    cliente = Cliente.objects.get(pk=cliente_id)
    slug = cliente.slug
    inicio_total = time.monotonic()
    # .resolve() garante um caminho absoluto com drive letter no Windows -
    # necessario para os argumentos '-v'/paths passados a comandos docker.
    backup_base = (Path(os.getenv('CP_BACKUP_DIR', '/opt/backups/cp')) / 'clientes' / slug).resolve()
    backup_base.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    arquivo = backup_base / f'backup_{slug}_{timestamp}.tar.gz'

    avisos = []

    try:
        _prog(5)
        with tarfile.open(arquivo, 'w:gz', compresslevel=6) as tar:
            # 1. DB dump via container do banco
            _prog(10)
            log.linha(f'[1/5] Banco de dados: procurando o container {slug}_db…')
            db_ctr = container_local(slug, 'db')
            if db_ctr:
                log.linha(f'Container do banco encontrado ({str(db_ctr)[:12]}). Executando pg_dump…')
                t0 = time.monotonic()
                dump = rodar_com_pulso(
                    log, 'pg_dump', subprocess.run,
                    ['docker', 'exec', db_ctr,
                     'pg_dump', '--clean', '--if-exists', '-U', f'erp_{slug}', f'erp_{slug}'],
                    capture_output=True, timeout=300,
                )
                if dump.returncode == 0:
                    data = dump.stdout
                    log.linha(f'pg_dump concluído: {fmt_bytes(len(data))} em {int(time.monotonic() - t0)}s. Compactando db.sql…')
                    info = tarfile.TarInfo(name='db.sql')
                    info.size = len(data)
                    t1 = time.monotonic()
                    rodar_com_pulso(log, 'Compactação do dump', tar.addfile, info, io.BytesIO(data))
                    log.linha(f'db.sql compactado em {int(time.monotonic() - t1)}s.')
                else:
                    stderr = dump.stderr.decode(errors='replace').strip()
                    logger.error('pg_dump falhou para %s (codigo %s): %s', slug, dump.returncode, stderr)
                    avisos.append(f'pg_dump falhou (codigo {dump.returncode}): {stderr[:300]}')
                    log.linha(f'AVISO: pg_dump falhou (código {dump.returncode}): {stderr[:200]}')
            else:
                avisos.append(f'Container {slug}_db nao encontrado - dump do banco nao incluido.')
                log.linha(f'AVISO: container {slug}_db não encontrado neste nó — dump do banco não incluído.')
            _prog(55)

            # 2. Volumes de arquivos (mídia e backups internos do ERP), lidos direto do volume:
            #    funciona com o web parado (cliente suspenso ou migração congelada).
            for passo, (volume, rotulo) in enumerate((('media', 'Mídia'), ('backups', 'Backups internos do ERP')), 2):
                log.linha(f'[{passo}/5] {rotulo}: lendo o volume {slug}_{volume}…')
                pasta_tmp = backup_base / f'_{volume}_tmp_{timestamp}'
                pasta_tmp.mkdir(parents=True, exist_ok=True)
                try:
                    cp = rodar_com_pulso(
                        log, f'Cópia do volume {volume}', copiar_volume_para_pasta, slug, volume, pasta_tmp,
                        medir=lambda pasta=pasta_tmp: f'{fmt_bytes(tamanho_pasta(pasta))} copiados',
                    )
                    if cp is None:
                        avisos.append(f'Volume {slug}_{volume} nao encontrado - {volume} nao incluido.')
                        log.linha(f'AVISO: volume {slug}_{volume} não encontrado neste nó — {volume} não incluído.')
                    elif cp.returncode == 0:
                        log.linha(f'{rotulo} copiado: {fmt_bytes(tamanho_pasta(pasta_tmp))}. Compactando…')
                        rodar_com_pulso(log, f'Compactação de {volume}', tar.add, str(pasta_tmp), arcname=volume)
                    else:
                        stderr = (cp.stderr or b'').decode(errors='replace').strip()
                        logger.error('Copia do volume %s falhou para %s: %s', volume, slug, stderr)
                        avisos.append(f'Copia do volume {volume} falhou: {stderr[:300]}')
                        log.linha(f'AVISO: cópia do volume {volume} falhou: {stderr[:200]}')
                finally:
                    shutil.rmtree(pasta_tmp, ignore_errors=True)
            _prog(80)

            # 3. Arquivos de configuração do cliente
            log.linha('[4/5] Configuração do cliente (.env e stack.yml)…')
            if config_files is not None:
                if config_files:
                    for rel, conteudo in config_files.items():
                        data = base64.b64decode(conteudo)
                        info = tarfile.TarInfo(name=f'config/{rel}')
                        info.size = len(data)
                        tar.addfile(info, io.BytesIO(data))
                    log.linha(f'Configuração adicionada ({len(config_files)} arquivo(s)).')
                else:
                    avisos.append('Diretorio de configuracao do cliente nao encontrado.')
                    log.linha('AVISO: diretório de configuração do cliente não encontrado.')
            else:
                clientes_base = Path(os.getenv('CLIENTES_BASE_PATH', '/opt/clientes'))
                cliente_dir = clientes_base / slug
                if cliente_dir.exists():
                    tar.add(str(cliente_dir), arcname='config')
                    log.linha('Configuração adicionada.')
                else:
                    avisos.append('Diretorio de configuracao do cliente nao encontrado.')
                    log.linha('AVISO: diretório de configuração do cliente não encontrado.')
            _prog(90)
            log.linha('Finalizando o arquivo (tar.gz)…')

            n_entries = len(tar.getmembers())

        if n_entries == 0:
            arquivo.unlink()
            mensagem = ' '.join(avisos) if avisos else 'Nenhum conteudo foi capturado para o backup.'
            log.linha(f'ERRO: nenhum conteúdo foi capturado — o backup foi descartado. {mensagem}'[:600])
            BackupCliente.objects.filter(pk=backup_id).update(
                status='erro',
                progresso=100,
                mensagem=mensagem,
            )
            logger.error('Backup vazio para %s: %s', slug, mensagem)
            return

        tamanho = arquivo.stat().st_size
        sha256 = sha256_arquivo(arquivo)
        log.linha(f'Arquivo pronto: {fmt_bytes(tamanho)} (SHA-256 {sha256[:16]}…).')

        # 4. Envia para o bucket (o disco do nó é só temporário) e atualiza o sinalizador
        cfg = storage_backup.exigir_configuracao()  # resolvido aqui: a thread do envio não acessa o banco
        log.linha(f'[5/5] Enviando ao armazenamento (bucket "{cfg.bucket}")…')
        progresso = Progresso(tamanho)
        t_envio = time.monotonic()
        try:
            caminho = rodar_com_pulso(
                log, 'Envio ao armazenamento', storage_backup.enviar,
                arquivo, f'clientes/{slug}/{arquivo.name}', progresso=progresso, cfg=cfg,
                medir=progresso.texto,
            )
        except Exception as exc:
            motivo = storage_backup._traduzir_erro(exc)
            storage_backup.registrar_status('erro', f'Falha ao enviar um backup: {motivo}')
            raise RuntimeError(f'Falha ao enviar o backup para o armazenamento: {motivo}') from exc
        arquivo.unlink()
        storage_backup.registrar_envio_ok(slug)
        log.linha(f'Envio concluído em {int(time.monotonic() - t_envio)}s.')
        log.linha(
            f'CONCLUÍDO: {fmt_bytes(tamanho)} em {int(time.monotonic() - inicio_total)}s'
            f'{" (com avisos)" if avisos else ""}.'
        )

        BackupCliente.objects.filter(pk=backup_id).update(
            status='concluido',
            progresso=100,
            arquivo_path=caminho,
            tamanho_bytes=tamanho,
            sha256=sha256,
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
        removidos = 0
        for antigo in excedentes:
            removidos += 1
            if storage_backup.eh_remoto(antigo.arquivo_path):
                try:
                    storage_backup.apagar(antigo.arquivo_path)
                except Exception as exc:
                    logger.warning('Nao foi possivel apagar %s do bucket: %s', antigo.arquivo_path, exc)
        excedentes.delete()
        if removidos:
            log.linha(f'Retenção: {removidos} backup(s) antigo(s) removido(s).')

    except Exception as exc:
        if arquivo.exists():
            arquivo.unlink()
        log.linha(f'ERRO: {exc}')
        BackupCliente.objects.filter(pk=backup_id).update(status='erro', mensagem=str(exc))
        logger.error('Erro no backup de %s: %s', slug, exc)
        raise


def _scale_web(slug, replicas):
    import subprocess
    subprocess.run(['docker', 'service', 'scale', f'{slug}_web={replicas}'], capture_output=True, timeout=60)


def _replicas_app(slug):
    """Réplicas atuais de web e cron ({'web': n, 'cron': n|None}); cron None = serviço inexistente."""
    from .swarm import replicas
    web = replicas(f'{slug}_web')
    return {'web': 1 if web is None else web, 'cron': replicas(f'{slug}_cron')}


def _parar_app(slug):
    """Para web e cron — o cron também escreve no banco e na mídia."""
    import subprocess
    for servico in ('web', 'cron'):
        subprocess.run(['docker', 'service', 'scale', f'{slug}_{servico}=0'], capture_output=True, timeout=60)


def _religar_app(slug, replicas_app):
    """Devolve web e cron às réplicas de antes (sem `replicas_app`, comportamento antigo: web=1)."""
    import subprocess
    if not replicas_app:
        _scale_web(slug, 1)
        return
    for servico in ('web', 'cron'):
        n = replicas_app.get(servico)
        if n is not None:
            subprocess.run(['docker', 'service', 'scale', f'{slug}_{servico}={n}'], capture_output=True, timeout=60)


@shared_task(bind=True, max_retries=0)
def task_restaurar_cliente(self, cliente_id, backup_id):
    """Etapa do manager: derruba o web e despacha a restauração para o nó do cliente.

    `docker service scale` só funciona em nó manager, enquanto o `psql`/volume de
    mídia só existem no nó do cliente — por isso a restauração é dividida em
    manager (esta task) → nó (`task_restaurar_no`) → manager (`task_finalizar_restauracao`).
    """
    import time
    from .log_operacao import LogOperacao
    from .models import Cliente, BackupCliente
    from .nos import fila_do_cliente

    cliente = Cliente.objects.get(pk=cliente_id)
    slug = cliente.slug

    log = LogOperacao(backup_id, 'log_restauracao')
    log.reiniciar()
    BackupCliente.objects.filter(pk=backup_id).update(restaurando=True, progresso=0)
    replicas_app = None
    try:
        replicas_app = _replicas_app(slug)
        BackupCliente.objects.filter(pk=backup_id).update(replicas_restauracao=replicas_app)
        log.linha(f'Restauração solicitada. Parando {slug}_web e {slug}_cron (réplicas atuais: {replicas_app})…')
        BackupCliente.objects.filter(pk=backup_id).update(progresso=10)
        _parar_app(slug)
        log.linha('Aguardando 10s para o web e o cron encerrarem…')
        time.sleep(10)
        fila = fila_do_cliente(slug)
        logger.info('Restauracao de %s despachada para a fila %s', slug, fila)
        task_restaurar_no.apply_async(
            args=[str(cliente_id), backup_id], kwargs={'replicas_app': replicas_app}, queue=fila,
        )
        log.linha(f'Tarefa enviada à fila "{fila}". Aguardando um worker do nó iniciar…')
    except Exception as exc:
        # nada foi restaurado: religa o app e libera o backup
        log.linha(f'ERRO: {exc}. Religando o web e o cron…')
        _religar_app(slug, replicas_app)
        BackupCliente.objects.filter(pk=backup_id).update(
            restaurando=False, progresso=100, mensagem_restauracao=f'Erro inesperado: {exc}',
        )
        raise


@shared_task(bind=True, max_retries=0)
def task_restaurar_no(self, cliente_id, backup_id, replicas_app=None, migracao_id=None):
    """Etapa do nó: baixa o backup e restaura banco e volumes. Sempre aciona a finalização.

    Com `migracao_id` não religa nada: devolve o resultado para a migração no manager.
    """
    import shutil
    import socket
    import subprocess
    import time
    from . import storage_backup
    from .log_operacao import LogOperacao, Progresso, fmt_bytes, rodar_com_pulso
    from .models import Cliente, BackupCliente
    from .nos import container_local
    from .volumes import restaurar_pasta_no_volume, sha256_arquivo

    def _prog(pct):
        BackupCliente.objects.filter(pk=backup_id).update(progresso=pct)

    cliente = Cliente.objects.get(pk=cliente_id)
    backup = BackupCliente.objects.get(pk=backup_id)
    slug = cliente.slug
    log = LogOperacao(backup_id, 'log_restauracao')
    log.linha(f'Worker "{os.getenv("NODE_HOSTNAME") or socket.gethostname()}" iniciou a restauração.')

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
            cfg = storage_backup.exigir_configuracao()  # resolvido aqui: a thread não acessa o banco
            progresso = Progresso()
            log.linha('[1/4] Baixando o backup do armazenamento…')
            rodar_com_pulso(
                log, 'Download do backup', storage_backup.baixar,
                backup.arquivo_path, arquivo, progresso=progresso, cfg=cfg, medir=progresso.texto,
            )
            log.linha(f'Download concluído: {fmt_bytes(progresso.feito)}.')
        else:
            arquivo = Path(backup.arquivo_path)
            if not arquivo.exists():
                raise FileNotFoundError(f'Arquivo de backup não encontrado: {arquivo}')
            log.linha('[1/4] Backup local encontrado no disco do nó.')

        if backup.sha256:
            log.linha('Conferindo o SHA-256 do arquivo…')
            sha = rodar_com_pulso(log, 'SHA-256', sha256_arquivo, arquivo)
            if sha != backup.sha256:
                raise RuntimeError(f'SHA-256 do backup não confere (esperado {backup.sha256}, obtido {sha}).')
            log.linha('SHA-256 confere.')

        # 2. Extrai o backup
        _prog(25)
        log.linha('[2/4] Extraindo o backup…')
        with tarfile.open(arquivo, 'r:gz') as tar:
            tar.extractall(restore_dir)

        # 3. Restaura o banco de dados
        _prog(40)
        log.linha('[3/4] Restaurando o banco de dados…')
        db_sql = restore_dir / 'db.sql'
        db_ctr = container_local(slug, 'db')
        if not db_sql.exists():
            avisos.append('Backup nao contem db.sql - banco nao foi restaurado.')
            log.linha('AVISO: o backup não contém db.sql — banco não restaurado.')
        elif not db_ctr:
            avisos.append(f'Container {slug}_db nao encontrado - banco nao foi restaurado.')
            log.linha(f'AVISO: container {slug}_db não encontrado neste nó — banco não restaurado.')
        else:
            from .volumes import pg_pronto
            limite_pg = time.monotonic() + 180
            while not pg_pronto(db_ctr, slug):
                if time.monotonic() > limite_pg:
                    raise TimeoutError(f'{slug}_db não aceitou conexões em 180s')
                log.linha('Aguardando o Postgres aceitar conexões…')
                time.sleep(5)
            log.linha(f'Executando psql no container do banco ({fmt_bytes(db_sql.stat().st_size)} de SQL)…')
            t0 = time.monotonic()
            psql = rodar_com_pulso(
                log, 'psql', subprocess.run,
                ['docker', 'exec', '-i', db_ctr,
                 'psql', '-v', 'ON_ERROR_STOP=1', '-U', f'erp_{slug}', '-d', f'erp_{slug}'],
                input=db_sql.read_bytes(),
                capture_output=True, timeout=300,
            )
            if psql.returncode != 0:
                stderr = psql.stderr.decode(errors='replace').strip()
                logger.error('Restauracao do banco falhou para %s: %s', slug, stderr)
                avisos.append(f'Restauracao do banco falhou: {stderr[:300]}')
                log.linha(f'AVISO: restauração do banco falhou: {stderr[:200]}')
            else:
                log.linha(f'Banco restaurado em {int(time.monotonic() - t0)}s.')
        _prog(75)

        # 4. Restaura os volumes de arquivos: container auxiliar monta o volume e recebe por `docker cp`
        log.linha('[4/4] Restaurando a mídia e os backups internos do ERP…')
        for volume, rotulo in (('media', 'mídia'), ('backups', 'backups internos')):
            pasta = restore_dir / volume
            if not pasta.exists():
                if volume == 'media':
                    avisos.append('Backup nao contem media/ - arquivos de midia nao foram restaurados.')
                    log.linha('AVISO: o backup não contém media/ — mídia não restaurada.')
                else:
                    # backups anteriores a esta versão não traziam o volume `backups`
                    log.linha('O backup não contém backups/ (gerado antes desta versão) — volume mantido.')
                continue
            resultado = rodar_com_pulso(
                log, f'Restauração de {rotulo}', restaurar_pasta_no_volume, slug, volume, pasta,
            )
            if resultado.returncode != 0:
                stderr = (resultado.stderr or b'').decode(errors='replace').strip()
                logger.error('Restauracao do volume %s falhou para %s: %s', volume, slug, stderr)
                avisos.append(f'Restauracao do volume {volume} falhou: {stderr[:300]}')
                log.linha(f'AVISO: restauração de {rotulo} falhou: {stderr[:200]}')
            else:
                log.linha(f'{rotulo.capitalize()} restaurado.')
        _prog(90)

    except Exception as exc:
        avisos.append(f'Erro inesperado: {exc}')
        log.linha(f'ERRO: {exc}')
        raise
    finally:
        shutil.rmtree(restore_dir, ignore_errors=True)
        BackupCliente.objects.filter(pk=backup_id).update(mensagem_restauracao=' '.join(avisos))
        if avisos:
            logger.warning('Restauracao de %s concluida com avisos: %s', slug, ' '.join(avisos))
        if migracao_id:
            log.linha('Restauração no nó terminada. Devolvendo o resultado à migração (o app continua parado).')
            BackupCliente.objects.filter(pk=backup_id).update(restaurando=False, progresso=100)
            task_migracao_avancar.apply_async(
                args=[migracao_id, 'restauracao_terminada', {'avisos': ' '.join(avisos)}], queue='celery',
            )
        else:
            log.linha('Restauração no nó terminada. Pedindo ao manager para religar o web e o cron…')
            # Religar é feito no manager, mesmo se a restauração falhou
            task_finalizar_restauracao.apply_async(
                args=[str(cliente_id), backup_id], kwargs={'replicas_app': replicas_app},
            )

    logger.info('Restauração concluída para %s a partir de %s', slug, backup.arquivo_path)


@shared_task
def task_finalizar_restauracao(cliente_id, backup_id, replicas_app=None):
    """Etapa do manager: devolve web e cron às réplicas de antes e libera o backup."""
    from .log_operacao import LogOperacao
    from .models import Cliente, BackupCliente

    cliente = Cliente.objects.get(pk=cliente_id)
    log = LogOperacao(backup_id, 'log_restauracao')
    try:
        log.linha(f'Religando {cliente.slug}_web e {cliente.slug}_cron ({replicas_app or "web=1"})…')
        _religar_app(cliente.slug, replicas_app)
    finally:
        log.linha('CONCLUÍDO: restauração finalizada.')
        BackupCliente.objects.filter(pk=backup_id).update(restaurando=False, progresso=100)


@shared_task
def task_verificar_armazenamento():
    """Testa a conexão com o bucket e atualiza o sinalizador do admin.

    Agendar no django-celery-beat (ex.: a cada 15 min), como o health check.
    """
    from . import storage_backup
    status, mensagem = storage_backup.verificar_e_registrar()
    logger.info('Verificacao do armazenamento de backups: %s - %s', status, mensagem)
    return status


@shared_task
def task_verificar_atualizacao():
    """Consulta o Docker Hub e atualiza a versão disponível do CP (Configurações > Verificar Atualizações).

    Agendar no django-celery-beat (ex.: a cada 6 horas), como o health check.
    """
    from . import atualizacoes
    resultado = atualizacoes.verificar_e_registrar()
    logger.info('Verificacao de atualizacoes do CP: %s - %s', resultado['status'], resultado['mensagem'])
    return resultado['status']


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


# ── Migração de servidor (orquestração em registry/migracao.py) ─────────────

@shared_task
def task_migracao_iniciar(migracao_id):
    """Manager: dispara as sondas dos nós de origem e destino."""
    from . import migracao
    try:
        migracao.disparar_sondas(migracao_id)
    except Exception as exc:
        migracao.falhar(migracao_id, f'Falha ao enviar as sondas: {exc}')


@shared_task
def task_migracao_avancar(migracao_id, evento, dados=None):
    """Manager: recebe o resultado de uma etapa e executa a próxima."""
    from . import migracao
    migracao.avancar(migracao_id, evento, dados)


@shared_task
def task_vigiar_migracoes():
    """Manager (beat, 1 min): etapas paradas além do limite voltam para a origem ou pedem intervenção."""
    from . import migracao
    migracao.vigiar()


def _hostname_local():
    import socket
    return os.getenv('NODE_HOSTNAME') or socket.gethostname()


def _log_migracao(migracao_id):
    from .log_operacao import LogOperacao
    from .models import MigracaoCliente
    return LogOperacao(migracao_id, modelo=MigracaoCliente)


@shared_task
def task_migracao_sondar(migracao_id, lado):
    """Nó: confirma que o worker certo responde e mede o que os gates G9–G11 precisam."""
    from .models import MigracaoCliente
    from .nos import container_local
    from .volumes import VOLUMES_ARQUIVOS, VOLUMES_STACK, espaco_livre_docker, manifesto_volume, pg_pronto, tamanho_banco, volume_existe

    m = MigracaoCliente.objects.select_related('cliente').get(pk=migracao_id)
    slug = m.cliente.slug
    esperado = m.no_origem if lado == 'origem' else m.no_destino
    dados = {'lado': lado, 'hostname': _hostname_local()}
    try:
        if dados['hostname'] != esperado:
            raise RuntimeError(f'a sonda rodou em {dados["hostname"]}, não em {esperado}')
        if lado == 'origem':
            db_ctr = container_local(slug, 'db')
            if not db_ctr or not pg_pronto(db_ctr, slug):
                raise RuntimeError(f'{slug}_db não está rodando/pronto neste nó')
            dados['banco_bytes'] = tamanho_banco(db_ctr, slug)
            dados['volumes_bytes'] = {
                v: manifesto_volume(slug, v, com_hash=False)['bytes'] for v in VOLUMES_ARQUIVOS
            }
        else:
            dados['volumes_existentes'] = [f'{slug}_{v}' for v in VOLUMES_STACK if volume_existe(slug, v)]
            dados['livre_bytes'] = espaco_livre_docker()
    except Exception as exc:
        dados['erro'] = str(exc)
    task_migracao_avancar.apply_async(args=[migracao_id, 'sonda', dados], queue='celery')


@shared_task
def task_migracao_manifesto(migracao_id, lado):
    """Nó: inventário exato do banco e dos volumes (`lado` = origem, destino ou reversao)."""
    from .log_operacao import rodar_com_pulso
    from .models import MigracaoCliente
    from .nos import container_local
    from .volumes import VOLUMES_ARQUIVOS, manifesto_banco, manifesto_volume, pg_pronto

    m = MigracaoCliente.objects.select_related('cliente').get(pk=migracao_id)
    slug = m.cliente.slug
    log = _log_migracao(migracao_id)
    evento = f'manifesto_{lado}'
    try:
        log.linha(f'Worker "{_hostname_local()}" inventariando banco e volumes ({lado})…')
        db_ctr = container_local(slug, 'db')
        if not db_ctr:
            raise RuntimeError(f'{slug}_db não está rodando neste nó')
        import time as _time
        limite = _time.monotonic() + 180
        while not pg_pronto(db_ctr, slug):
            if _time.monotonic() > limite:
                raise TimeoutError(f'{slug}_db não aceitou conexões em 180s')
            _time.sleep(5)
        manifesto = {
            'banco': rodar_com_pulso(log, 'Contagem das tabelas', manifesto_banco, db_ctr, slug),
            'volumes': {
                v: rodar_com_pulso(log, f'Inventário do volume {v}', manifesto_volume, slug, v)
                for v in VOLUMES_ARQUIVOS
            },
        }
        dados = {'manifesto': manifesto}
    except Exception as exc:
        dados = {'erro': str(exc)}
    task_migracao_avancar.apply_async(args=[migracao_id, evento, dados], queue='celery')


_SCRIPT_HEALTH_INTERNO = (
    "import json, sys, urllib.request\n"
    "req = urllib.request.Request('http://127.0.0.1:8000/health/', headers={'Host': sys.argv[1], 'X-Forwarded-Proto': 'https'})\n"
    "resp = urllib.request.urlopen(req, timeout=10)\n"
    "print(json.dumps({'status': resp.status, 'corpo': resp.read().decode('utf-8', 'replace')[:500]}))\n"
)


@shared_task
def task_migracao_teste_interno(migracao_id):
    """Nó de destino: `/health/` de dentro do container do web, antes de o Traefik expor o ERP."""
    import json as _json
    import subprocess
    import time as _time
    from .models import MigracaoCliente
    from .nos import container_local

    m = MigracaoCliente.objects.select_related('cliente').get(pk=migracao_id)
    slug = m.cliente.slug
    log = _log_migracao(migracao_id)
    limite = _time.monotonic() + 300
    ultimo = 'container do web ainda não iniciou'
    dados = None
    while _time.monotonic() < limite:
        web_ctr = container_local(slug, 'web')
        if web_ctr:
            r = subprocess.run(
                ['docker', 'exec', web_ctr, 'python', '-c', _SCRIPT_HEALTH_INTERNO, m.cliente.subdominio],
                capture_output=True, text=True, timeout=60,
            )
            if r.returncode == 0:
                try:
                    resposta = _json.loads((r.stdout or '').strip().splitlines()[-1])
                    versao = str(_json.loads(resposta['corpo']).get('version'))
                    if resposta['status'] == 200 and versao == m.versao_erp:
                        dados = {'ok': True, 'detalhe': f'/health/ 200, versão {versao}'}
                        break
                    ultimo = f'status {resposta["status"]}, versão {versao}'
                except (ValueError, KeyError, IndexError) as exc:
                    ultimo = f'resposta inesperada: {exc}'
            else:
                ultimo = (r.stderr or '').strip()[-300:] or 'falha no docker exec'
        log.linha(f'Aguardando o web do destino responder… ({ultimo[:150]})')
        _time.sleep(10)
    if dados is None:
        dados = {'ok': False, 'detalhe': f'/health/ não respondeu 200 com a versão {m.versao_erp} em 300s ({ultimo})'}
    task_migracao_avancar.apply_async(args=[migracao_id, 'teste_interno', dados], queue='celery')


# ── Saúde dos hosts (registry/hosts.py) ─────────────────────────────────────

@shared_task
def task_verificar_hosts():
    """Manager (beat, 5 min): avalia cada host ativo no Swarm e pede um ping ao worker do CP no nó."""
    from .hosts import verificar_host
    from .models import HostInfraestrutura
    for host in HostInfraestrutura.objects.filter(ativo=True):
        try:
            verificar_host(host)
        except Exception:
            logger.exception('Falha ao verificar o host %s', host.nome)
            continue
        _pedir_ping_host(host.pk)


def _pedir_ping_host(host_id):
    from .models import HostInfraestrutura
    from .nos import fila_do_no
    hostname = HostInfraestrutura.objects.filter(pk=host_id).values_list('no_hostname', flat=True).first()
    if hostname:
        # expira: um nó parado não acumula pings velhos que chegariam todos de uma vez ao voltar
        task_pingar_host.apply_async(args=[host_id, hostname], queue=fila_do_no(hostname), expires=240)


@shared_task
def task_pingar_host(host_id, hostname_esperado):
    """Nó: prova que o worker do CP neste nó consome a fila e mede disco e clientes. Reavalia no manager."""
    from .atualizacoes import descobrir_stack
    from .hosts import registrar_ping
    from .volumes import disco_docker, stacks_de_clientes_no_no
    hostname = _hostname_local()
    if hostname != hostname_esperado:
        logger.error('Ping do host %s rodou em %s, esperado %s', host_id, hostname, hostname_esperado)
        return
    livre = total = containers = None
    try:
        livre, total = disco_docker()
    except Exception:
        logger.warning('Ping do host %s: não foi possível medir o disco', host_id, exc_info=True)
    try:
        containers = stacks_de_clientes_no_no(ignorar=descobrir_stack())
    except Exception:
        logger.warning('Ping do host %s: não foi possível contar os clientes', host_id, exc_info=True)
    registrar_ping(host_id, hostname, livre, total, containers)
    task_reavaliar_host.apply_async(args=[host_id], queue='celery')


@shared_task
def task_reavaliar_host(host_id):
    """Manager: recalcula o status depois que o worker do nó respondeu."""
    from .hosts import verificar_host
    from .models import HostInfraestrutura
    host = HostInfraestrutura.objects.filter(pk=host_id).first()
    if host is not None:
        verificar_host(host)
