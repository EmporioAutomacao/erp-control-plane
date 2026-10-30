import io
import tarfile
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, SimpleTestCase, TestCase
from django.urls import reverse

from registry.models import BackupCliente, Cliente, Plano
from registry.tests.helpers_storage import instalar_fake_bucket
from registry.tasks import (
    task_backup_cliente, task_finalizar_restauracao, task_iniciar_backup_cliente,
    task_restaurar_cliente, task_restaurar_no,
)


def _make_tar_bytes(members):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode='w:gz') as tar:
        for name, data in members.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _fake_run(cmd, *args, **kwargs):
    """Simula `docker ps` / `docker exec pg_dump` / volumes (`inspect`, auxiliar, `cp`) conforme o comando."""
    if cmd[:2] == ['docker', 'ps']:
        alvo = cmd[-1]
        if '_db' in alvo and getattr(_fake_run, 'db_ctr', None):
            return SimpleNamespace(stdout=_fake_run.db_ctr, stderr='', returncode=0)
        return SimpleNamespace(stdout='', stderr='', returncode=0)
    if cmd[:3] == ['docker', 'volume', 'inspect']:
        existe = cmd[3] in getattr(_fake_run, 'volumes', set())
        return SimpleNamespace(stdout=b'', stderr=b'', returncode=0 if existe else 1)
    if cmd[1] == 'create':
        return SimpleNamespace(stdout=b'aux-ctr\n', stderr=b'', returncode=0)
    if cmd[1] == 'rm':
        return SimpleNamespace(stdout=b'', stderr=b'', returncode=0)
    if cmd[1] == 'exec' and 'pg_dump' in cmd:
        return SimpleNamespace(
            stdout=_fake_run.pg_dump_stdout,
            stderr=_fake_run.pg_dump_stderr,
            returncode=_fake_run.pg_dump_returncode,
        )
    if cmd[1] == 'cp':
        return SimpleNamespace(
            stdout=b'', stderr=_fake_run.docker_cp_stderr, returncode=_fake_run.docker_cp_returncode,
        )
    raise AssertionError(f'comando inesperado: {cmd}')


class TaskBackupClienteTests(SimpleTestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.cp_backup_dir = Path(self.tmpdir.name) / 'backups'
        self.clientes_base_path = Path(self.tmpdir.name) / 'clientes'

        _fake_run.db_ctr = None
        _fake_run.volumes = set()
        _fake_run.pg_dump_stdout = b''
        _fake_run.pg_dump_stderr = b''
        _fake_run.pg_dump_returncode = 0
        _fake_run.docker_cp_stderr = b''
        _fake_run.docker_cp_returncode = 0

        self.env_patch = patch.dict('os.environ', {
            'CP_BACKUP_DIR': str(self.cp_backup_dir),
            'CLIENTES_BASE_PATH': str(self.clientes_base_path),
        })
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)

        self.cliente = SimpleNamespace(pk='cliente-id', slug='acme')
        self.cliente_get_patch = patch('registry.models.Cliente.objects.get', return_value=self.cliente)
        self.cliente_get_patch.start()
        self.addCleanup(self.cliente_get_patch.stop)

        self.filter_mock = MagicMock()
        self.filter_mock.return_value.order_by.return_value.values_list.return_value = []
        self.filter_patch = patch('registry.models.BackupCliente.objects.filter', self.filter_mock)
        self.filter_patch.start()
        self.addCleanup(self.filter_patch.stop)

        self.run_patch = patch('subprocess.run', side_effect=_fake_run)
        self.run_patch.start()
        self.addCleanup(self.run_patch.stop)

        self.bucket = instalar_fake_bucket(self)

    def _status_updates(self):
        return [
            call.kwargs for call in self.filter_mock.return_value.update.call_args_list
            if 'status' in call.kwargs
        ]

    def _backup_files(self):
        return self.bucket.arquivos('clientes/acme', 'backup_acme_*.tar.gz')

    def test_sem_containers_e_sem_config_marca_erro(self):
        task_backup_cliente(cliente_id='cliente-id', backup_id='backup-id')

        updates = self._status_updates()
        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0]['status'], 'erro')
        self.assertIn('acme_db', updates[0]['mensagem'])
        self.assertIn('acme_media', updates[0]['mensagem'])
        self.assertIn('acme_backups', updates[0]['mensagem'])
        self.assertEqual(self._backup_files(), [])

    def test_pg_dump_falha_e_demais_etapas_ausentes_marca_erro(self):
        _fake_run.db_ctr = 'db-container-id\n'
        _fake_run.pg_dump_returncode = 1
        _fake_run.pg_dump_stderr = b'password authentication failed for user "erp_acme"'

        task_backup_cliente(cliente_id='cliente-id', backup_id='backup-id')

        updates = self._status_updates()
        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0]['status'], 'erro')
        self.assertIn('password authentication failed', updates[0]['mensagem'])
        self.assertEqual(self._backup_files(), [])

    def test_apenas_config_presente_marca_concluido_com_avisos(self):
        cliente_dir = self.clientes_base_path / 'acme'
        cliente_dir.mkdir(parents=True)
        (cliente_dir / '.env').write_text('FOO=bar', encoding='utf-8')

        task_backup_cliente(cliente_id='cliente-id', backup_id='backup-id')

        updates = self._status_updates()
        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0]['status'], 'concluido')
        self.assertGreater(updates[0]['tamanho_bytes'], 0)
        self.assertIn('acme_db', updates[0]['mensagem'])
        self.assertIn('acme_media', updates[0]['mensagem'])
        self.assertIn('acme_backups', updates[0]['mensagem'])
        self.assertEqual(len(self._backup_files()), 1)

    def test_todas_etapas_ok_marca_concluido_sem_avisos(self):
        _fake_run.db_ctr = 'db-container-id\n'
        _fake_run.pg_dump_stdout = b'-- dump completo do postgres'
        _fake_run.volumes = {'acme_media', 'acme_backups'}
        cliente_dir = self.clientes_base_path / 'acme'
        cliente_dir.mkdir(parents=True)
        (cliente_dir / 'stack.yml').write_text('version: "3.8"', encoding='utf-8')

        task_backup_cliente(cliente_id='cliente-id', backup_id='backup-id')

        updates = self._status_updates()
        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0]['status'], 'concluido')
        self.assertEqual(updates[0]['mensagem'], '')
        self.assertEqual(len(self._backup_files()), 1)

    def test_volumes_sao_lidos_do_volume_mesmo_com_o_web_parado(self):
        _fake_run.db_ctr = 'db-container-id\n'
        _fake_run.pg_dump_stdout = b'-- dump'
        _fake_run.volumes = {'acme_media', 'acme_backups'}

        with patch('subprocess.run', side_effect=_fake_run) as run:
            task_backup_cliente(cliente_id='cliente-id', backup_id='backup-id')

        comandos = [c.args[0] for c in run.call_args_list]
        self.assertFalse(any('_web' in ' '.join(c) for c in comandos))  # nunca depende do container do web
        criados = [c for c in comandos if c[:2] == ['docker', 'create']]
        self.assertEqual([c[3] for c in criados], ['acme_media:/dados:ro', 'acme_backups:/dados:ro'])
        with tarfile.open(self._backup_files()[0]) as tar:
            nomes = tar.getnames()
        self.assertIn('media', nomes)
        self.assertIn('backups', nomes)

    def test_volume_inexistente_nao_e_criado_e_vira_aviso(self):
        _fake_run.db_ctr = 'db-container-id\n'
        _fake_run.pg_dump_stdout = b'-- dump'
        _fake_run.volumes = {'acme_backups'}

        with patch('subprocess.run', side_effect=_fake_run) as run:
            task_backup_cliente(cliente_id='cliente-id', backup_id='backup-id')

        comandos = [c.args[0] for c in run.call_args_list]
        self.assertFalse(any(c[:2] in (['docker', 'create'], ['docker', 'run']) and 'acme_media:/dados:ro' in c
                             for c in comandos))
        self.assertIn('acme_media nao encontrado', self._status_updates()[0]['mensagem'])

    def test_grava_o_sha256_do_arquivo(self):
        import hashlib
        _fake_run.db_ctr = 'db-container-id\n'
        _fake_run.pg_dump_stdout = b'-- dump'

        task_backup_cliente(cliente_id='cliente-id', backup_id='backup-id')

        sha = self._status_updates()[0]['sha256']
        self.assertEqual(sha, hashlib.sha256(self._backup_files()[0].read_bytes()).hexdigest())

    def test_com_migracao_devolve_o_resultado_ao_manager(self):
        _fake_run.db_ctr = 'db-container-id\n'
        _fake_run.pg_dump_stdout = b'-- dump'
        self.filter_mock.return_value.values.return_value.first.return_value = {
            'status': 'concluido', 'mensagem': '', 'sha256': 'abc',
        }
        with patch('registry.tasks.task_migracao_avancar.apply_async') as avancar:
            task_backup_cliente(cliente_id='cliente-id', backup_id='backup-id', migracao_id=9)

        avancar.assert_called_once_with(
            args=[9, 'backup_terminado', {'status': 'concluido', 'avisos': '', 'sha256': 'abc'}], queue='celery',
        )


class BackupMultiNoTests(SimpleTestCase):
    """Config por argumento, envio ao bucket e despacho por fila."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.cp_backup_dir = Path(self.tmpdir.name) / 'backups'
        env = patch.dict('os.environ', {
            'CP_BACKUP_DIR': str(self.cp_backup_dir),
            'CLIENTES_BASE_PATH': str(Path(self.tmpdir.name) / 'clientes'),
        })
        env.start()
        self.addCleanup(env.stop)
        self.cliente = SimpleNamespace(pk='cliente-id', slug='acme')
        p = patch('registry.models.Cliente.objects.get', return_value=self.cliente)
        p.start()
        self.addCleanup(p.stop)
        self.filter_mock = MagicMock()
        self.filter_mock.return_value.order_by.return_value.values_list.return_value = []
        p = patch('registry.models.BackupCliente.objects.filter', self.filter_mock)
        p.start()
        self.addCleanup(p.stop)
        _fake_run.db_ctr = 'db-ctr\n'
        _fake_run.volumes = set()
        _fake_run.pg_dump_stdout = b'-- dump'
        _fake_run.pg_dump_stderr = b''
        _fake_run.pg_dump_returncode = 0
        _fake_run.docker_cp_stderr = b''
        _fake_run.docker_cp_returncode = 0
        p = patch('subprocess.run', side_effect=_fake_run)
        p.start()
        self.addCleanup(p.stop)
        self.bucket = instalar_fake_bucket(self)

    def _update_final(self):
        return [
            c.kwargs for c in self.filter_mock.return_value.update.call_args_list if 'status' in c.kwargs
        ][-1]

    def _tars_locais(self):
        return list((self.cp_backup_dir / 'clientes' / 'acme').glob('*.tar.gz'))

    def test_config_recebido_por_argumento_entra_no_tar_sem_ler_o_disco(self):
        import base64
        config = {'.env': base64.b64encode(b'FOO=bar').decode()}

        task_backup_cliente('cliente-id', 'backup-id', config_files=config)

        with tarfile.open(self.bucket.arquivos('clientes/acme', 'backup_acme_*.tar.gz')[0]) as tar:
            self.assertIn('config/.env', tar.getnames())
            self.assertIn('db.sql', tar.getnames())

    def test_config_vazio_gera_aviso(self):
        task_backup_cliente('cliente-id', 'backup-id', config_files={})
        self.assertIn('configuracao', self._update_final()['mensagem'])

    def test_envia_ao_bucket_remove_arquivo_local_e_grava_chave(self):
        task_backup_cliente('cliente-id', 'backup-id', config_files={})

        objetos = self.bucket.arquivos('clientes/acme', 'backup_acme_*.tar.gz')
        self.assertEqual(len(objetos), 1)
        self.assertEqual(self._update_final()['arquivo_path'], f's3://clientes/acme/{objetos[0].name}')
        self.assertEqual(self._tars_locais(), [])
        self.bucket.registrar_envio_ok.assert_called_once_with('acme')

    def test_falha_no_envio_marca_erro_e_sinaliza_no_status(self):
        with patch('registry.storage_backup.enviar', side_effect=RuntimeError('bucket fora')):
            with self.assertRaises(RuntimeError):
                task_backup_cliente('cliente-id', 'backup-id', config_files={})

        self.assertEqual(self._update_final()['status'], 'erro')
        self.assertEqual(self._tars_locais(), [])
        self.assertEqual(self.bucket.registrar_status.call_args.args[0], 'erro')

    def test_sem_configuracao_aborta_antes_do_dump(self):
        with patch('registry.storage_backup.configurado', return_value=False):
            task_backup_cliente('cliente-id', 'backup-id', config_files={})

        update = self._update_final()
        self.assertEqual(update['status'], 'erro')
        self.assertIn('Configurações → Armazenamento', update['mensagem'])
        self.assertEqual(self._tars_locais(), [])

    def test_iniciar_sem_configuracao_nao_despacha(self):
        with patch('registry.storage_backup.configurado', return_value=False), \
                patch('registry.tasks.task_backup_cliente.apply_async') as apply_async:
            task_iniciar_backup_cliente('cliente-id', 'backup-id')

        apply_async.assert_not_called()
        self.assertEqual(self._update_final()['status'], 'erro')

    def test_iniciar_despacha_para_a_fila_do_no_com_o_config(self):
        cfg = Path(self.tmpdir.name) / 'clientes' / 'acme'
        cfg.mkdir(parents=True)
        (cfg / '.env').write_text('X=1', encoding='utf-8')

        with patch('registry.nos.hostname_do_no', return_value='worker-1'), \
                patch('registry.tasks.task_backup_cliente.apply_async') as apply_async:
            task_iniciar_backup_cliente('cliente-id', 'backup-id')

        kwargs = apply_async.call_args.kwargs
        self.assertEqual(kwargs['queue'], 'no.worker-1')
        self.assertEqual(list(kwargs['kwargs']['config_files']), ['.env'])


class NosTests(SimpleTestCase):
    def test_hostname_do_no_le_a_primeira_linha(self):
        from registry import nos
        with patch('subprocess.run', return_value=SimpleNamespace(stdout='worker-3\n', returncode=0)):
            self.assertEqual(nos.hostname_do_no('acme'), 'worker-3')
            self.assertEqual(nos.fila_do_cliente('acme'), 'no.worker-3')

    def test_sem_swarm_cai_na_fila_padrao(self):
        from registry import nos
        with patch('subprocess.run', return_value=SimpleNamespace(stdout='', returncode=1)):
            self.assertEqual(nos.fila_do_cliente('acme'), 'celery')
        with patch('subprocess.run', side_effect=FileNotFoundError):
            self.assertEqual(nos.fila_do_cliente('acme'), 'celery')

    def test_filtro_do_container_e_ancorado(self):
        from registry import nos
        with patch('subprocess.run', return_value=SimpleNamespace(stdout='abc\n', returncode=0)) as run:
            self.assertEqual(nos.container_local('acme', 'db'), 'abc')
        self.assertEqual(run.call_args.args[0][-1], 'name=^acme_db(\\.|$)')


class TaskRestaurarNoTests(SimpleTestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.cp_backup_dir = Path(self.tmpdir.name) / 'backups'

        self.env_patch = patch.dict('os.environ', {'CP_BACKUP_DIR': str(self.cp_backup_dir)})
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)

        self.cliente = SimpleNamespace(pk='cliente-id', slug='acme')
        self.cliente_get_patch = patch('registry.models.Cliente.objects.get', return_value=self.cliente)
        self.cliente_get_patch.start()
        self.addCleanup(self.cliente_get_patch.stop)

        self.arquivo = self.cp_backup_dir / 'backup_acme_teste.tar.gz'
        self.cp_backup_dir.mkdir(parents=True)
        self.backup = SimpleNamespace(pk='backup-id', arquivo_path=str(self.arquivo), sha256='')
        self.backup_get_patch = patch('registry.models.BackupCliente.objects.get', return_value=self.backup)
        self.backup_get_patch.start()
        self.addCleanup(self.backup_get_patch.stop)

        self.filter_mock = MagicMock()
        self.filter_patch = patch('registry.models.BackupCliente.objects.filter', self.filter_mock)
        self.filter_patch.start()
        self.addCleanup(self.filter_patch.stop)

        self.finalizar_patch = patch('registry.tasks.task_finalizar_restauracao.apply_async')
        self.finalizar_mock = self.finalizar_patch.start()
        self.addCleanup(self.finalizar_patch.stop)

        self.db_ctr = None
        self.web_ctr = None
        self.psql_returncode = 0
        self.psql_stderr = b''
        self.docker_run_returncode = 0
        self.docker_run_stderr = b''
        self.docker_cp_returncode = 0
        self.docker_cp_stderr = b''

        def _fake_run(cmd, *args, **kwargs):
            if cmd[:2] == ['docker', 'service']:
                return SimpleNamespace(stdout='', stderr=b'', returncode=0)
            if cmd[:3] == ['docker', 'volume', 'inspect']:
                return SimpleNamespace(stdout=b'', stderr=b'', returncode=0)
            if cmd[1] == 'exec' and 'pg_isready' in cmd:
                return SimpleNamespace(stdout=b'', stderr=b'', returncode=0)
            if cmd[:2] == ['docker', 'ps']:
                alvo = cmd[-1]
                if '_db' in alvo and self.db_ctr:
                    return SimpleNamespace(stdout=self.db_ctr, stderr='', returncode=0)
                return SimpleNamespace(stdout='', stderr='', returncode=0)
            if cmd[1] == 'exec' and 'psql' in cmd:
                return SimpleNamespace(stdout=b'', stderr=self.psql_stderr, returncode=self.psql_returncode)
            if cmd[1] == 'run':  # limpa o volume de mídia
                return SimpleNamespace(stdout=b'', stderr=self.docker_run_stderr, returncode=self.docker_run_returncode)
            if cmd[1] == 'create':
                return SimpleNamespace(stdout=b'aux-ctr-id\n', stderr=b'', returncode=0)
            if cmd[1] == 'cp':
                return SimpleNamespace(stdout=b'', stderr=self.docker_cp_stderr, returncode=self.docker_cp_returncode)
            if cmd[1] == 'rm':
                return SimpleNamespace(stdout=b'', stderr=b'', returncode=0)
            raise AssertionError(f'comando inesperado: {cmd}')

        self.run_patch = patch('subprocess.run', side_effect=_fake_run)
        self.run_mock = self.run_patch.start()
        self.addCleanup(self.run_patch.stop)

    def _mensagem_restauracao(self):
        updates = [
            call.kwargs for call in self.filter_mock.return_value.update.call_args_list
            if 'mensagem_restauracao' in call.kwargs
        ]
        return updates[-1]['mensagem_restauracao']

    def _escrever_backup(self, members):
        with tarfile.open(self.arquivo, 'w:gz') as tar:
            for name, data in members.items():
                info = tarfile.TarInfo(name=name)
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))

    def test_backup_sem_db_sql_e_sem_media_gera_avisos(self):
        self._escrever_backup({'config/.env': b'X=1'})

        task_restaurar_no(cliente_id='cliente-id', backup_id='backup-id')

        mensagem = self._mensagem_restauracao()
        self.assertIn('db.sql', mensagem)
        self.assertIn('media', mensagem)

    def test_container_db_nao_encontrado_gera_aviso(self):
        self._escrever_backup({'db.sql': b'-- dump'})

        task_restaurar_no(cliente_id='cliente-id', backup_id='backup-id')

        mensagem = self._mensagem_restauracao()
        self.assertIn('acme_db', mensagem)
        self.assertIn('nao encontrado', mensagem)

    def test_psql_falha_gera_aviso_com_stderr(self):
        self.db_ctr = 'db-ctr\n'
        self.psql_returncode = 3
        self.psql_stderr = b'ERROR:  relation "produtos" does not exist'
        self._escrever_backup({'db.sql': b'-- dump quebrado'})

        task_restaurar_no(cliente_id='cliente-id', backup_id='backup-id')

        mensagem = self._mensagem_restauracao()
        self.assertIn('relation "produtos" does not exist', mensagem)

    def test_sucesso_sem_avisos(self):
        self.db_ctr = 'db-ctr\n'
        self._escrever_backup({'db.sql': b'-- dump ok', 'media/foo.txt': b'x'})

        task_restaurar_no(cliente_id='cliente-id', backup_id='backup-id')

        self.assertEqual(self._mensagem_restauracao(), '')

    def _comandos(self):
        return [call.args[0] for call in self.run_mock.call_args_list]

    def test_midia_e_restaurada_sem_bind_mount_de_pasta_do_host(self):
        self.db_ctr = 'db-ctr\n'
        self._escrever_backup({'db.sql': b'-- dump', 'media/foo.txt': b'x'})

        task_restaurar_no(cliente_id='cliente-id', backup_id='backup-id')

        comandos = self._comandos()
        # nenhum comando monta uma pasta do host (-v <pasta>:...): só o volume nomeado do cliente
        montagens = [c for c in comandos if c[:2] in (['docker', 'run'], ['docker', 'create'])]
        self.assertTrue(montagens)
        for cmd in montagens:
            volumes = [cmd[i + 1] for i, arg in enumerate(cmd) if arg == '-v']
            self.assertEqual(volumes, ['acme_media:/dados'])
        # sequência: limpa o volume -> cria o auxiliar -> docker cp -> remove o auxiliar
        passos = [c[1] for c in comandos if c[0] == 'docker' and c[1] in ('run', 'create', 'cp', 'rm')]
        self.assertEqual(passos, ['run', 'create', 'cp', 'rm'])

    def test_docker_cp_da_midia_le_de_caminho_absoluto_e_vai_para_o_volume(self):
        self.db_ctr = 'db-ctr\n'
        self._escrever_backup({'db.sql': b'-- dump', 'media/foo.txt': b'x'})

        task_restaurar_no(cliente_id='cliente-id', backup_id='backup-id')

        cp = next(c for c in self._comandos() if c[:2] == ['docker', 'cp'])
        origem = cp[2]
        # "<pasta>/." (conteúdo da pasta); no Windows a pasta tem drive letter
        self.assertTrue(origem.endswith(('/.', '\\.')), origem)
        self.assertTrue(Path(origem[:-2]).is_absolute())
        self.assertEqual(cp[3], 'aux-ctr-id:/dados/')

    def test_auxiliar_e_removido_mesmo_se_o_docker_cp_falhar(self):
        self.db_ctr = 'db-ctr\n'
        self.docker_cp_returncode = 1
        self.docker_cp_stderr = b'no space left on device'
        self._escrever_backup({'db.sql': b'-- dump', 'media/foo.txt': b'x'})

        task_restaurar_no(cliente_id='cliente-id', backup_id='backup-id')

        self.assertIn(['docker', 'rm', '-f', 'aux-ctr-id'], self._comandos())
        self.assertIn('no space left on device', self._mensagem_restauracao())

    def test_falha_ao_limpar_o_volume_nao_chega_a_copiar(self):
        self.db_ctr = 'db-ctr\n'
        self.docker_run_returncode = 1
        self.docker_run_stderr = b'volume em uso'
        self._escrever_backup({'db.sql': b'-- dump', 'media/foo.txt': b'x'})

        task_restaurar_no(cliente_id='cliente-id', backup_id='backup-id')

        self.assertFalse(any(c[:2] == ['docker', 'cp'] for c in self._comandos()))
        self.assertIn('volume em uso', self._mensagem_restauracao())

    def test_finalizacao_e_sempre_acionada_mesmo_com_erro(self):
        # arquivo de backup inexistente -> FileNotFoundError, mas o web precisa religar
        with self.assertRaises(FileNotFoundError):
            task_restaurar_no(cliente_id='cliente-id', backup_id='backup-id')

        self.finalizar_mock.assert_called_once()
        self.assertIn('Erro inesperado', self._mensagem_restauracao())

    def test_backup_remoto_e_baixado_do_bucket(self):
        self.db_ctr = 'db-ctr\n'
        self.backup.arquivo_path = 's3://clientes/acme/backup_acme_x.tar.gz'
        tar_bytes = _make_tar_bytes({'db.sql': b'-- dump', 'media/foo.txt': b'x'})

        def _baixar(caminho, destino, **kwargs):
            Path(destino).write_bytes(tar_bytes)

        with patch('registry.storage_backup.exigir_configuracao', return_value=SimpleNamespace(bucket='b')),                 patch('registry.storage_backup.baixar', side_effect=_baixar) as baixar:
            task_restaurar_no(cliente_id='cliente-id', backup_id='backup-id')

        baixar.assert_called_once()
        self.assertEqual(self._mensagem_restauracao(), '')
        self.finalizar_mock.assert_called_once()

    def test_restaura_tambem_o_volume_de_backups_internos(self):
        self.db_ctr = 'db-ctr\n'
        self._escrever_backup({'db.sql': b'-- dump', 'media/foo.txt': b'x', 'backups/b1.zip': b'y'})

        task_restaurar_no(cliente_id='cliente-id', backup_id='backup-id')

        limpos = [c for c in self._comandos() if c[:2] == ['docker', 'run']]
        self.assertEqual([c[c.index('-v') + 1] for c in limpos], ['acme_media:/dados', 'acme_backups:/dados'])
        self.assertEqual(self._mensagem_restauracao(), '')

    def test_sha256_divergente_aborta_antes_de_tocar_no_banco(self):
        self.db_ctr = 'db-ctr\n'
        self._escrever_backup({'db.sql': b'-- dump', 'media/foo.txt': b'x'})
        self.backup.sha256 = '0' * 64

        with self.assertRaises(RuntimeError):
            task_restaurar_no(cliente_id='cliente-id', backup_id='backup-id')

        self.assertFalse(any('psql' in c for c in self._comandos()))
        self.assertIn('SHA-256', self._mensagem_restauracao())
        self.finalizar_mock.assert_called_once()

    def test_sha256_igual_segue_a_restauracao(self):
        import hashlib
        self.db_ctr = 'db-ctr\n'
        self._escrever_backup({'db.sql': b'-- dump', 'media/foo.txt': b'x'})
        self.backup.sha256 = hashlib.sha256(self.arquivo.read_bytes()).hexdigest()

        task_restaurar_no(cliente_id='cliente-id', backup_id='backup-id')

        self.assertEqual(self._mensagem_restauracao(), '')

    def test_finalizacao_devolve_as_replicas_de_antes(self):
        self.db_ctr = 'db-ctr\n'
        self._escrever_backup({'db.sql': b'-- dump', 'media/foo.txt': b'x'})

        task_restaurar_no(cliente_id='cliente-id', backup_id='backup-id', replicas_app={'web': 0, 'cron': 0})

        self.assertEqual(self.finalizar_mock.call_args.kwargs['kwargs'], {'replicas_app': {'web': 0, 'cron': 0}})

    def test_com_migracao_nao_religa_e_devolve_o_resultado(self):
        self.db_ctr = 'db-ctr\n'
        self._escrever_backup({'db.sql': b'-- dump', 'media/foo.txt': b'x'})

        with patch('registry.tasks.task_migracao_avancar.apply_async') as avancar:
            task_restaurar_no(cliente_id='cliente-id', backup_id='backup-id', migracao_id=5)

        self.finalizar_mock.assert_not_called()
        self.assertFalse(any(c[:3] == ['docker', 'service', 'scale'] for c in self._comandos()))
        avancar.assert_called_once_with(args=[5, 'restauracao_terminada', {'avisos': ''}], queue='celery')


class TaskRestaurarClienteOrquestracaoTests(SimpleTestCase):
    def setUp(self):
        self.cliente = SimpleNamespace(pk='cliente-id', slug='acme')
        for nome, alvo, kw in [
            ('cliente_get', 'registry.models.Cliente.objects.get', {'return_value': self.cliente}),
            ('filtro', 'registry.models.BackupCliente.objects.filter', {}),
            ('sleep', 'time.sleep', {}),
            ('replicas', 'registry.tasks._replicas_app', {'return_value': {'web': 1, 'cron': 1}}),
            ('parar', 'registry.tasks._parar_app', {}),
            ('religar', 'registry.tasks._religar_app', {}),
        ]:
            p = patch(alvo, **kw)
            setattr(self, nome, p.start())
            self.addCleanup(p.stop)

    def test_despacha_para_a_fila_do_no_do_cliente_com_web_e_cron_parados(self):
        with patch('registry.nos.hostname_do_no', return_value='worker-2'), \
                patch('registry.tasks.task_restaurar_no.apply_async') as apply_async:
            task_restaurar_cliente(cliente_id='cliente-id', backup_id='backup-id')

        self.parar.assert_called_once_with('acme')
        apply_async.assert_called_once_with(
            args=['cliente-id', 'backup-id'], kwargs={'replicas_app': {'web': 1, 'cron': 1}}, queue='no.worker-2',
        )
        self.filtro.return_value.update.assert_any_call(replicas_restauracao={'web': 1, 'cron': 1})

    def test_falha_no_despacho_religa_com_as_replicas_de_antes(self):
        with patch('registry.nos.hostname_do_no', return_value=None), \
                patch('registry.tasks.task_restaurar_no.apply_async', side_effect=RuntimeError('redis fora')):
            with self.assertRaises(RuntimeError):
                task_restaurar_cliente(cliente_id='cliente-id', backup_id='backup-id')

        self.religar.assert_called_once_with('acme', {'web': 1, 'cron': 1})

    def test_finalizar_devolve_as_replicas(self):
        task_finalizar_restauracao('cliente-id', 'backup-id', replicas_app={'web': 0, 'cron': 0})
        self.religar.assert_called_once_with('acme', {'web': 0, 'cron': 0})

    def test_finalizar_sem_replicas_mantem_o_comportamento_antigo(self):
        task_finalizar_restauracao('cliente-id', 'backup-id')
        self.religar.assert_called_once_with('acme', None)


class ReligarAppTests(SimpleTestCase):
    def test_cliente_suspenso_continua_parado(self):
        from registry.tasks import _religar_app
        with patch('subprocess.run') as run:
            _religar_app('acme', {'web': 0, 'cron': 0})
        self.assertEqual([c.args[0][-1] for c in run.call_args_list], ['acme_web=0', 'acme_cron=0'])

    def test_sem_cron_nao_escala_o_cron(self):
        from registry.tasks import _religar_app
        with patch('subprocess.run') as run:
            _religar_app('acme', {'web': 1, 'cron': None})
        self.assertEqual([c.args[0][-1] for c in run.call_args_list], ['acme_web=1'])


class UploadBackupViewTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_superuser(
            username='upload_admin', email='upload@example.com', password='SenhaSegura123!',
        )
        self.plano = Plano.objects.create(slug='basico-upload', nome='Basico Upload', preco_mensal='0.00')
        # status='ativo' evita que o signal post_save dispare task_provisionar_cliente.
        self.cliente = Cliente.objects.create(
            slug='cliente-upload', nome='Cliente Upload', cnpj='22.333.444/0001-66',
            email_contato='upload-cliente@example.com', subdominio='cliente-upload.ararasuite.com.br',
            plano=self.plano, status='ativo',
        )

        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.env_patch = patch.dict('os.environ', {'CP_BACKUP_DIR': self.tmpdir.name})
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)

        self.bucket = instalar_fake_bucket(self)

        self.django_client = Client()
        self.django_client.force_login(self.user)
        self.url = reverse('admin:registry_cliente_backup_upload', args=[self.cliente.pk])

    def test_upload_valido_com_db_e_media_cria_backup_sem_avisos(self):
        tar_bytes = _make_tar_bytes({'db.sql': b'-- dump', 'media/foo.txt': b'conteudo'})
        upload = SimpleUploadedFile('meu_backup.tar.gz', tar_bytes, content_type='application/gzip')

        resp = self.django_client.post(self.url, {'arquivo': upload})

        self.assertRedirects(resp, reverse('admin:registry_cliente_change', args=[self.cliente.pk]))
        backup = BackupCliente.objects.get(cliente=self.cliente)
        self.assertEqual(backup.origem, 'manual')
        self.assertEqual(backup.status, 'concluido')
        self.assertEqual(backup.mensagem, '')
        self.assertGreater(backup.tamanho_bytes, 0)
        self.assertTrue(backup.arquivo_path.startswith('s3://clientes/cliente-upload/manual_cliente-upload_'))
        self.assertTrue(self.bucket.existe(backup.arquivo_path))
        # o arquivo recebido no web é temporário: não sobra nada no disco
        self.assertEqual(list(Path(self.tmpdir.name).rglob('*.tar.gz')), [])

    def test_upload_sem_media_gera_aviso_mas_ainda_conclui(self):
        tar_bytes = _make_tar_bytes({'db.sql': b'-- dump'})
        upload = SimpleUploadedFile('so_db.tar.gz', tar_bytes, content_type='application/gzip')

        self.django_client.post(self.url, {'arquivo': upload})

        backup = BackupCliente.objects.get(cliente=self.cliente)
        self.assertEqual(backup.status, 'concluido')
        self.assertIn('media', backup.mensagem)

    def test_upload_extensao_invalida_e_rejeitado(self):
        upload = SimpleUploadedFile('backup.zip', b'conteudo qualquer', content_type='application/zip')

        resp = self.django_client.post(self.url, {'arquivo': upload})

        self.assertRedirects(resp, self.url)
        self.assertFalse(BackupCliente.objects.filter(cliente=self.cliente).exists())

    def test_upload_tar_corrompido_e_rejeitado(self):
        upload = SimpleUploadedFile('backup.tar.gz', b'isso nao e um tar.gz valido', content_type='application/gzip')

        resp = self.django_client.post(self.url, {'arquivo': upload})

        self.assertRedirects(resp, self.url)
        self.assertFalse(BackupCliente.objects.filter(cliente=self.cliente).exists())


class RetencaoBackupProtegeManuaisTests(TestCase):
    def setUp(self):
        self.plano = Plano.objects.create(slug='basico-retencao', nome='Basico Retencao', preco_mensal='0.00')
        self.cliente = Cliente.objects.create(
            slug='cliente-retencao', nome='Cliente Retencao', cnpj='33.444.555/0001-77',
            email_contato='retencao@example.com', subdominio='cliente-retencao.ararasuite.com.br',
            plano=self.plano, status='ativo',
        )

        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.env_patch = patch.dict('os.environ', {
            'CP_BACKUP_DIR': self.tmpdir.name,
            'CLIENTES_BASE_PATH': str(Path(self.tmpdir.name) / 'clientes_config'),
        })
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)

        # config presente para o novo backup automatico capturar algo e chegar na etapa de retencao
        config_dir = Path(self.tmpdir.name) / 'clientes_config' / 'cliente-retencao'
        config_dir.mkdir(parents=True)
        (config_dir / '.env').write_text('X=1', encoding='utf-8')

        # 3 backups automaticos "antigos" + 1 manual, todos ja concluidos
        for _ in range(3):
            BackupCliente.objects.create(
                cliente=self.cliente, status='concluido', progresso=100,
                origem='automatico', tamanho_bytes=10,
            )
        self.manual = BackupCliente.objects.create(
            cliente=self.cliente, status='concluido', progresso=100,
            origem='manual', tamanho_bytes=20,
        )

        self.run_patch = patch(
            'subprocess.run',
            side_effect=lambda cmd, **kw: SimpleNamespace(stdout='', stderr=b'', returncode=0),
        )
        self.run_patch.start()
        self.addCleanup(self.run_patch.stop)

        self.bucket = instalar_fake_bucket(self)

    def test_novo_backup_automatico_nao_apaga_backup_manual(self):
        novo = BackupCliente.objects.create(cliente=self.cliente)

        task_backup_cliente(str(self.cliente.pk), str(novo.pk))

        self.assertTrue(BackupCliente.objects.filter(pk=self.manual.pk, origem='manual').exists())
        self.assertEqual(
            BackupCliente.objects.filter(cliente=self.cliente, origem='automatico').count(), 3,
        )
        self.assertEqual(
            BackupCliente.objects.filter(cliente=self.cliente).count(), 4,
        )
