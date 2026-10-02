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
    """Simula `docker ps` / `docker exec pg_dump` / `docker cp` conforme o comando recebido."""
    if cmd[:2] == ['docker', 'ps']:
        alvo = cmd[-1]
        if '_db' in alvo and getattr(_fake_run, 'db_ctr', None):
            return SimpleNamespace(stdout=_fake_run.db_ctr, stderr='', returncode=0)
        if '_web' in alvo and getattr(_fake_run, 'web_ctr', None):
            return SimpleNamespace(stdout=_fake_run.web_ctr, stderr='', returncode=0)
        return SimpleNamespace(stdout='', stderr='', returncode=0)
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
        _fake_run.web_ctr = None
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

    def _status_updates(self):
        return [
            call.kwargs for call in self.filter_mock.return_value.update.call_args_list
            if 'status' in call.kwargs
        ]

    def _backup_files(self):
        return list((self.cp_backup_dir / 'clientes' / 'acme').glob('backup_acme_*.tar.gz'))

    def test_sem_containers_e_sem_config_marca_erro(self):
        task_backup_cliente(cliente_id='cliente-id', backup_id='backup-id')

        updates = self._status_updates()
        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0]['status'], 'erro')
        self.assertIn('acme_db', updates[0]['mensagem'])
        self.assertIn('acme_web', updates[0]['mensagem'])
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
        self.assertIn('acme_web', updates[0]['mensagem'])
        self.assertEqual(len(self._backup_files()), 1)

    def test_todas_etapas_ok_marca_concluido_sem_avisos(self):
        _fake_run.db_ctr = 'db-container-id\n'
        _fake_run.pg_dump_stdout = b'-- dump completo do postgres'
        _fake_run.web_ctr = 'web-container-id\n'
        cliente_dir = self.clientes_base_path / 'acme'
        cliente_dir.mkdir(parents=True)
        (cliente_dir / 'stack.yml').write_text('version: "3.8"', encoding='utf-8')

        task_backup_cliente(cliente_id='cliente-id', backup_id='backup-id')

        updates = self._status_updates()
        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0]['status'], 'concluido')
        self.assertEqual(updates[0]['mensagem'], '')
        self.assertEqual(len(self._backup_files()), 1)


_ENV_BUCKET = {
    'BACKUP_S3_ENDPOINT': 'http://rustfs:9000', 'BACKUP_S3_BUCKET': 'b',
    'BACKUP_S3_ACCESS_KEY': 'k', 'BACKUP_S3_SECRET_KEY': 's',
}


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
        _fake_run.web_ctr = None
        _fake_run.pg_dump_stdout = b'-- dump'
        _fake_run.pg_dump_stderr = b''
        _fake_run.pg_dump_returncode = 0
        _fake_run.docker_cp_stderr = b''
        _fake_run.docker_cp_returncode = 0
        p = patch('subprocess.run', side_effect=_fake_run)
        p.start()
        self.addCleanup(p.stop)

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

        with tarfile.open(self._tars_locais()[0]) as tar:
            self.assertIn('config/.env', tar.getnames())
            self.assertIn('db.sql', tar.getnames())

    def test_config_vazio_gera_aviso(self):
        task_backup_cliente('cliente-id', 'backup-id', config_files={})
        self.assertIn('configuracao', self._update_final()['mensagem'])

    def test_com_bucket_envia_remove_arquivo_local_e_grava_chave(self):
        with patch.dict('os.environ', _ENV_BUCKET), \
                patch('registry.storage_backup.enviar', side_effect=lambda f, c: f's3://{c}') as enviar:
            task_backup_cliente('cliente-id', 'backup-id', config_files={})

        chave = enviar.call_args.args[1]
        self.assertTrue(chave.startswith('clientes/acme/backup_acme_'))
        self.assertEqual(self._update_final()['arquivo_path'], f's3://{chave}')
        self.assertEqual(self._tars_locais(), [])

    def test_falha_no_envio_marca_erro(self):
        with patch.dict('os.environ', _ENV_BUCKET), \
                patch('registry.storage_backup.enviar', side_effect=RuntimeError('bucket fora')):
            with self.assertRaises(RuntimeError):
                task_backup_cliente('cliente-id', 'backup-id', config_files={})

        self.assertEqual(self._update_final()['status'], 'erro')
        self.assertEqual(self._tars_locais(), [])

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
        self.backup = SimpleNamespace(pk='backup-id', arquivo_path=str(self.arquivo))
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

        def _fake_run(cmd, *args, **kwargs):
            if cmd[:2] == ['docker', 'service']:
                return SimpleNamespace(stdout='', stderr=b'', returncode=0)
            if cmd[:2] == ['docker', 'ps']:
                alvo = cmd[-1]
                if '_db' in alvo and self.db_ctr:
                    return SimpleNamespace(stdout=self.db_ctr, stderr='', returncode=0)
                return SimpleNamespace(stdout='', stderr='', returncode=0)
            if cmd[1] == 'exec' and 'psql' in cmd:
                return SimpleNamespace(stdout=b'', stderr=self.psql_stderr, returncode=self.psql_returncode)
            if cmd[1] == 'run':
                return SimpleNamespace(stdout=b'', stderr=self.docker_run_stderr, returncode=self.docker_run_returncode)
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

    def test_caminho_da_midia_passado_ao_docker_e_absoluto(self):
        self.db_ctr = 'db-ctr\n'
        self._escrever_backup({'db.sql': b'-- dump', 'media/foo.txt': b'x'})

        task_restaurar_no(cliente_id='cliente-id', backup_id='backup-id')

        docker_run_calls = [
            call for call in self.run_mock.call_args_list
            if call.args[0][1] == 'run'
        ]
        self.assertEqual(len(docker_run_calls), 1)
        volume_arg = docker_run_calls[0].args[0][6]
        # formato '<host>:<container_path>:<modo>' - o host pode ter um ':' proprio
        # (drive letter do Windows), entao separamos pelos 2 ultimos ':'.
        host_path = volume_arg.rsplit(':', 2)[0]
        self.assertTrue(Path(host_path).is_absolute())

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

        def _baixar(caminho, destino):
            Path(destino).write_bytes(tar_bytes)

        with patch('registry.storage_backup.baixar', side_effect=_baixar) as baixar:
            task_restaurar_no(cliente_id='cliente-id', backup_id='backup-id')

        baixar.assert_called_once()
        self.assertEqual(self._mensagem_restauracao(), '')
        self.finalizar_mock.assert_called_once()


class TaskRestaurarClienteOrquestracaoTests(SimpleTestCase):
    def setUp(self):
        self.cliente = SimpleNamespace(pk='cliente-id', slug='acme')
        for nome, alvo, kw in [
            ('cliente_get', 'registry.models.Cliente.objects.get', {'return_value': self.cliente}),
            ('filtro', 'registry.models.BackupCliente.objects.filter', {}),
            ('sleep', 'time.sleep', {}),
            ('scale_web', 'registry.tasks._scale_web', {}),
        ]:
            p = patch(alvo, **kw)
            setattr(self, nome, p.start())
            self.addCleanup(p.stop)

    def test_despacha_para_a_fila_do_no_do_cliente(self):
        with patch('registry.nos.hostname_do_no', return_value='worker-2'), \
                patch('registry.tasks.task_restaurar_no.apply_async') as apply_async:
            task_restaurar_cliente(cliente_id='cliente-id', backup_id='backup-id')

        self.scale_web.assert_called_once_with('acme', 0)
        apply_async.assert_called_once_with(args=['cliente-id', 'backup-id'], queue='no.worker-2')

    def test_falha_no_despacho_religa_o_web(self):
        with patch('registry.nos.hostname_do_no', return_value=None), \
                patch('registry.tasks.task_restaurar_no.apply_async', side_effect=RuntimeError('redis fora')):
            with self.assertRaises(RuntimeError):
                task_restaurar_cliente(cliente_id='cliente-id', backup_id='backup-id')

        self.assertEqual([c.args for c in self.scale_web.call_args_list], [('acme', 0), ('acme', 1)])

    def test_finalizar_religa_o_web(self):
        task_finalizar_restauracao('cliente-id', 'backup-id')
        self.scale_web.assert_called_once_with('acme', 1)


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
        self.assertTrue(Path(backup.arquivo_path).name.startswith('manual_cliente-upload_'))
        self.assertTrue(Path(backup.arquivo_path).exists())

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
