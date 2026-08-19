import io
import os
import tarfile
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test.utils import override_settings
from django.urls import reverse

from registry.models import BackupCliente, Cliente, Plano
from registry.tests.playwright_base import PlaywrightAdminTestCase


def _fake_run_sem_containers(cmd, *args, **kwargs):
    if cmd[:2] == ['docker', 'ps']:
        return SimpleNamespace(stdout='', stderr='', returncode=0)
    raise AssertionError(f'comando docker inesperado no teste (sem containers do cliente): {cmd}')


def _make_tar_bytes(members):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode='w:gz') as tar:
        for name, data in members.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


@override_settings(CELERY_TASK_ALWAYS_EAGER=True, CELERY_TASK_EAGER_PROPAGATES=True)
class BackupClientePlaywrightTests(PlaywrightAdminTestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_superuser(
            username='admin_backup_pw',
            email='admin-backup-pw@example.com',
            password='SenhaSegura123!',
        )
        self.plano = Plano.objects.create(slug='basico-pw', nome='Basico PW', preco_mensal='0.00')
        # status='ativo' evita que o signal post_save dispare task_provisionar_cliente
        # (que so roda para clientes criados com status='aguardando_provisao').
        self.cliente = Cliente.objects.create(
            slug='pw-backup-teste',
            nome='Cliente Playwright Backup',
            cnpj='11.222.333/0001-55',
            email_contato='cliente-pw@example.com',
            subdominio='pw-backup-teste.ararasuite.com.br',
            plano=self.plano,
            status='ativo',
        )

        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.cp_backup_dir = os.path.join(self.tmpdir.name, 'backups')
        self.clientes_base_path = os.path.join(self.tmpdir.name, 'clientes')

        self.env_patch = patch.dict(os.environ, {
            'CP_BACKUP_DIR': self.cp_backup_dir,
            'CLIENTES_BASE_PATH': self.clientes_base_path,
        })
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)

    def _disparar_backup(self):
        url_change = f'{self.live_server_url}{reverse("admin:registry_cliente_change", args=[self.cliente.pk])}'
        self.page.goto(url_change)
        self.page.once('dialog', lambda dialog: dialog.accept())
        self.page.get_by_role('link', name='Novo Backup').click()
        self.page.wait_for_url(url_change)

    def test_backup_sem_containers_do_cliente_mostra_badge_de_erro_com_motivo(self):
        self.autenticar_admin(self.user)

        with patch('subprocess.run', side_effect=_fake_run_sem_containers):
            self._disparar_backup()

        linha_backup = self.page.locator('tr', has_text='pw-backup-teste_db')
        linha_backup.wait_for()
        texto = linha_backup.inner_text()

        self.assertIn('Erro', texto)
        self.assertIn('pw-backup-teste_db', texto)
        # Nenhum link de download deve aparecer para um backup vazio/com erro.
        self.assertEqual(linha_backup.get_by_text('Baixar').count(), 0)

        # A UI trunca a mensagem em 100 caracteres; o motivo completo (todas as
        # etapas puladas) fica registrado por inteiro no banco.
        backup = BackupCliente.objects.get(cliente=self.cliente)
        self.assertEqual(backup.status, 'erro')
        self.assertIn('pw-backup-teste_db', backup.mensagem)
        self.assertIn('pw-backup-teste_web', backup.mensagem)
        self.assertIn('Diretorio de configuracao', backup.mensagem)
        self.assertEqual(backup.tamanho_bytes, 0)
        self.assertEqual(backup.arquivo_path, '')

    def test_backup_com_config_mas_sem_containers_mostra_badge_concluido_com_aviso(self):
        self.autenticar_admin(self.user)

        cliente_dir = os.path.join(self.clientes_base_path, self.cliente.slug)
        os.makedirs(cliente_dir)
        with open(os.path.join(cliente_dir, '.env'), 'w', encoding='utf-8') as f:
            f.write('FOO=bar\n')

        with patch('subprocess.run', side_effect=_fake_run_sem_containers):
            self._disparar_backup()

        linha_backup = self.page.locator('tr', has_text='pw-backup-teste_db')
        linha_backup.wait_for()
        texto = linha_backup.inner_text()

        self.assertIn('Concluído', texto)
        self.assertIn('pw-backup-teste_db', texto)
        self.assertEqual(linha_backup.get_by_text('Baixar').count(), 1)

        backup = BackupCliente.objects.get(cliente=self.cliente)
        self.assertEqual(backup.status, 'concluido')
        self.assertGreater(backup.tamanho_bytes, 0)
        self.assertIn('pw-backup-teste_db', backup.mensagem)
        self.assertIn('pw-backup-teste_web', backup.mensagem)

    def test_importar_backup_manual_mostra_tag_manual_na_lista(self):
        self.autenticar_admin(self.user)
        url_change = f'{self.live_server_url}{reverse("admin:registry_cliente_change", args=[self.cliente.pk])}'
        tar_bytes = _make_tar_bytes({'db.sql': b'-- dump', 'media/foo.txt': b'conteudo'})

        self.page.goto(url_change)
        self.page.get_by_role('link', name='Importar Backup').click()
        self.page.locator('input[type="file"][name="arquivo"]').set_input_files({
            'name': 'meu_backup.tar.gz',
            'mimeType': 'application/gzip',
            'buffer': tar_bytes,
        })
        self.page.get_by_role('button', name='Enviar backup').click()
        self.page.wait_for_url(url_change)

        linha_backup = self.page.locator('tr', has_text='manual')
        linha_backup.wait_for()
        texto = linha_backup.inner_text()
        self.assertIn('Concluído', texto)
        self.assertEqual(linha_backup.get_by_text('Baixar').count(), 1)

        backup = BackupCliente.objects.get(cliente=self.cliente)
        self.assertEqual(backup.origem, 'manual')
        self.assertEqual(backup.status, 'concluido')
        self.assertEqual(backup.mensagem, '')
