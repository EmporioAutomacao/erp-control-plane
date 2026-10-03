from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from botocore.exceptions import ClientError, EndpointConnectionError
from django.contrib.auth import get_user_model
from django.test import Client, SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from registry import cripto, storage_backup
from registry.models import BackupCliente, Cliente, ConfiguracaoArmazenamento, Plano
from registry.tasks import task_verificar_armazenamento


def _cfg(**extra):
    """Configuração em memória (sem banco) com a secret já cifrada."""
    cfg = ConfiguracaoArmazenamento(
        endpoint='http://rustfs-s3:9000', bucket='cp-backups', access_key='chave', regiao='us-east-1', **extra,
    )
    cfg.definir_secret('segredo-super-secreto')
    return cfg


def _erro_cliente(codigo):
    return ClientError({'Error': {'Code': codigo, 'Message': 'x'}}, 'HeadBucket')


class CriptoTests(SimpleTestCase):
    def test_ida_e_volta(self):
        token = cripto.cifrar('minha-secret')
        self.assertNotIn('minha-secret', token)
        self.assertEqual(cripto.decifrar(token), 'minha-secret')

    def test_vazio_continua_vazio(self):
        self.assertEqual(cripto.cifrar(''), '')
        self.assertEqual(cripto.decifrar(''), '')

    def test_secret_key_trocada_nao_decifra(self):
        token = cripto.cifrar('minha-secret')
        with override_settings(SECRET_KEY='outra-chave-completamente-diferente'):
            with self.assertRaises(cripto.SegredoIlegivel):
                cripto.decifrar(token)


class ModeloTests(SimpleTestCase):
    def test_secret_fica_cifrada_e_property_decifra(self):
        cfg = _cfg()
        self.assertNotIn('segredo-super-secreto', cfg.secret_key_cifrada)
        self.assertEqual(cfg.secret_key, 'segredo-super-secreto')
        self.assertTrue(cfg.completa)

    def test_incompleta_sem_secret(self):
        cfg = ConfiguracaoArmazenamento(endpoint='http://x', bucket='b', access_key='k')
        self.assertFalse(cfg.completa)


class TestarConexaoTests(SimpleTestCase):
    def _s3(self, **erros):
        s3 = MagicMock()
        for metodo, exc in erros.items():
            getattr(s3, metodo).side_effect = exc
        return s3

    def _testar(self, cfg, s3):
        with patch('registry.storage_backup._cliente', return_value=s3):
            return storage_backup.testar_conexao(cfg)

    def test_incompleta_nao_testa(self):
        status, msg = storage_backup.testar_conexao(ConfiguracaoArmazenamento())
        self.assertEqual(status, 'nao_testado')
        self.assertIn('incompleta', msg)

    def test_conectado_grava_le_e_apaga_objeto_de_teste(self):
        s3 = self._s3()
        status, msg = self._testar(_cfg(), s3)
        self.assertEqual(status, 'conectado')
        self.assertIn('cp-backups', msg)
        s3.head_bucket.assert_called_once_with(Bucket='cp-backups')
        chave = s3.put_object.call_args.kwargs['Key']
        self.assertTrue(chave.startswith('_cp-healthcheck/'))
        s3.delete_object.assert_called_once_with(Bucket='cp-backups', Key=chave)

    def test_bucket_inexistente(self):
        status, msg = self._testar(_cfg(), self._s3(head_bucket=_erro_cliente('404')))
        self.assertEqual(status, 'erro')
        self.assertIn('Bucket não encontrado', msg)

    def test_credenciais_invalidas(self):
        status, msg = self._testar(_cfg(), self._s3(head_bucket=_erro_cliente('InvalidAccessKeyId')))
        self.assertEqual(status, 'erro')
        self.assertIn('Credenciais recusadas', msg)

    def test_sem_permissao_de_escrita(self):
        status, msg = self._testar(_cfg(), self._s3(put_object=_erro_cliente('AccessDenied')))
        self.assertEqual(status, 'erro')
        self.assertIn('Acesso negado', msg)

    def test_endpoint_inacessivel(self):
        exc = EndpointConnectionError(endpoint_url='http://rustfs-s3:9000')
        status, msg = self._testar(_cfg(), self._s3(head_bucket=exc))
        self.assertEqual(status, 'erro')
        self.assertIn('conectar ao endpoint', msg)

    def test_objeto_de_teste_e_apagado_mesmo_se_a_leitura_falhar(self):
        s3 = self._s3(get_object=_erro_cliente('500'))
        status, _ = self._testar(_cfg(), s3)
        self.assertEqual(status, 'erro')
        s3.delete_object.assert_called_once()

    def test_endpoint_publico_fora_vira_atencao(self):
        cfg = _cfg(endpoint_publico='https://s3.exemplo.com.br')
        with patch('requests.get', side_effect=RuntimeError('timeout')):
            status, msg = self._testar(cfg, self._s3())
        self.assertEqual(status, 'atencao')
        self.assertIn('endpoint público', msg)

    def test_endpoint_publico_com_erro_http_vira_atencao(self):
        cfg = _cfg(endpoint_publico='https://s3.exemplo.com.br')
        with patch('requests.get', return_value=SimpleNamespace(status_code=502)):
            status, _ = self._testar(cfg, self._s3())
        self.assertEqual(status, 'atencao')

    def test_endpoint_publico_ok_fica_conectado(self):
        cfg = _cfg(endpoint_publico='https://s3.exemplo.com.br')
        with patch('requests.get', return_value=SimpleNamespace(status_code=200)) as get:
            status, _ = self._testar(cfg, self._s3())
        self.assertEqual(status, 'conectado')
        get.assert_called_once()
        self.assertEqual(get.call_args.args[0], 'https://s3.exemplo.com.br/health')

    def test_secret_ilegivel_pede_para_salvar_de_novo(self):
        cfg = _cfg()
        with override_settings(SECRET_KEY='outra-chave-completamente-diferente'):
            status, msg = storage_backup.testar_conexao(cfg)
        self.assertEqual(status, 'erro')
        self.assertIn('novamente', msg)


class SemConfiguracaoTests(TestCase):
    def test_enviar_baixar_apagar_url_exigem_configuracao(self):
        for chamada in (
            lambda: storage_backup.enviar('x', 'k'),
            lambda: storage_backup.baixar('s3://k', 'x'),
            lambda: storage_backup.apagar('s3://k'),
            lambda: storage_backup.url_download('s3://k', 'n'),
        ):
            with self.assertRaises(storage_backup.ArmazenamentoNaoConfigurado):
                chamada()
        self.assertFalse(storage_backup.configurado())

    def test_task_de_verificacao_sem_config_nao_quebra(self):
        self.assertEqual(task_verificar_armazenamento(), 'nao_testado')


class StatusTests(TestCase):
    def setUp(self):
        self.cfg = _cfg()
        self.cfg.status_conexao = 'atencao'
        self.cfg.save()

    def test_envio_ok_nao_apaga_aviso_de_atencao(self):
        storage_backup.registrar_envio_ok('acme')
        self.cfg.refresh_from_db()
        self.assertEqual(self.cfg.status_conexao, 'atencao')

    def test_envio_ok_recupera_de_erro(self):
        ConfiguracaoArmazenamento.objects.filter(pk=1).update(status_conexao='erro')
        storage_backup.registrar_envio_ok('acme')
        self.cfg.refresh_from_db()
        self.assertEqual(self.cfg.status_conexao, 'conectado')
        self.assertIsNotNone(self.cfg.verificado_em)

    def test_verificar_e_registrar_grava_resultado(self):
        with patch('registry.storage_backup.testar_conexao', return_value=('erro', 'sem rede')):
            storage_backup.verificar_e_registrar()
        self.cfg.refresh_from_db()
        self.assertEqual((self.cfg.status_conexao, self.cfg.status_mensagem), ('erro', 'sem rede'))


class AdminTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_superuser(
            username='armaz_admin', email='armaz@example.com', password='SenhaSegura123!',
        )
        self.c = Client()
        self.c.force_login(self.user)
        self.add_url = reverse('admin:registry_configuracaoarmazenamento_add')
        self.dados = {
            'endpoint': 'http://rustfs-s3:9000', 'endpoint_publico': 'https://s3.exemplo.com.br',
            'bucket': 'cp-backups', 'access_key': 'chave', 'secret_key': 'segredo-super-secreto', 'regiao': 'us-east-1',
        }

    def _salvar(self, dados=None, resultado=('conectado', 'Conexão OK')):
        with patch('registry.storage_backup.testar_conexao', return_value=resultado):
            return self.c.post(self.add_url, dados or self.dados, follow=True)

    def test_formulario_inicial_sugere_valores(self):
        resp = self.c.get(self.add_url)
        self.assertContains(resp, 'http://rustfs-s3:9000')
        self.assertContains(resp, 'cp-backups')

    def test_salvar_cifra_a_secret_e_testa_a_conexao(self):
        resp = self._salvar()
        cfg = ConfiguracaoArmazenamento.obter()
        self.assertEqual(cfg.secret_key, 'segredo-super-secreto')
        self.assertNotIn('segredo-super-secreto', cfg.secret_key_cifrada)
        self.assertEqual(cfg.status_conexao, 'conectado')
        self.assertContains(resp, 'Conexão testada após salvar')

    def test_secret_obrigatoria_na_criacao(self):
        dados = {**self.dados, 'secret_key': ''}
        resp = self.c.post(self.add_url, dados)
        self.assertContains(resp, 'Informe a secret key.')
        self.assertIsNone(ConfiguracaoArmazenamento.obter())

    def test_secret_nunca_e_reexibida_e_vazio_mantem_a_atual(self):
        self._salvar()
        change = reverse('admin:registry_configuracaoarmazenamento_change', args=[1])
        self.assertNotContains(self.c.get(change), 'segredo-super-secreto')

        with patch('registry.storage_backup.testar_conexao', return_value=('conectado', 'ok')):
            self.c.post(change, {**self.dados, 'secret_key': '', 'bucket': 'outro-bucket'})
        cfg = ConfiguracaoArmazenamento.obter()
        self.assertEqual(cfg.bucket, 'outro-bucket')
        self.assertEqual(cfg.secret_key, 'segredo-super-secreto')

    def test_secret_nova_substitui_a_antiga(self):
        self._salvar()
        change = reverse('admin:registry_configuracaoarmazenamento_change', args=[1])
        with patch('registry.storage_backup.testar_conexao', return_value=('conectado', 'ok')):
            self.c.post(change, {**self.dados, 'secret_key': 'nova-secret'})
        self.assertEqual(ConfiguracaoArmazenamento.obter().secret_key, 'nova-secret')

    def test_so_um_registro_e_nao_exclui(self):
        self._salvar()
        self.assertEqual(self.c.get(self.add_url).status_code, 403)
        delete = reverse('admin:registry_configuracaoarmazenamento_delete', args=[1])
        self.assertEqual(self.c.post(delete, {'post': 'yes'}).status_code, 403)

    def test_pagina_mostra_status_e_botao_de_teste(self):
        self._salvar(resultado=('erro', 'Bucket não encontrado.'))
        change = reverse('admin:registry_configuracaoarmazenamento_change', args=[1])
        resp = self.c.get(change)
        self.assertContains(resp, 'id="armaz-badge"')
        self.assertContains(resp, 'Erro')
        self.assertContains(resp, 'Bucket não encontrado.')
        self.assertContains(resp, reverse('admin:registry_configuracaoarmazenamento_testar'))
        self.assertContains(resp, reverse('admin:registry_configuracaoarmazenamento_verificar'))

    def test_changelist_mostra_o_status(self):
        self._salvar()
        resp = self.c.get(reverse('admin:registry_configuracaoarmazenamento_changelist'))
        self.assertContains(resp, 'Conectado')

    def test_json_verificar_devolve_o_status_atual(self):
        self._salvar()
        with patch('registry.storage_backup.testar_conexao', return_value=('atencao', 'público fora')):
            resp = self.c.get(reverse('admin:registry_configuracaoarmazenamento_verificar'))
        self.assertEqual(resp.json(), {'status': 'atencao', 'rotulo': 'Atenção', 'mensagem': 'público fora'})
        self.assertEqual(ConfiguracaoArmazenamento.obter().status_conexao, 'atencao')

    def test_botao_testar_atualiza_o_status_e_volta_para_a_tela(self):
        self._salvar()
        with patch('registry.storage_backup.testar_conexao', return_value=('erro', 'sem rede')):
            resp = self.c.get(reverse('admin:registry_configuracaoarmazenamento_testar'), follow=True)
        self.assertContains(resp, 'sem rede')
        self.assertEqual(ConfiguracaoArmazenamento.obter().status_conexao, 'erro')

    def test_item_na_sidebar(self):
        resp = self.c.get(reverse('admin:index'))
        self.assertContains(resp, 'Armazenamento')


class FluxosDoClienteSemConfiguracaoTests(TestCase):
    """Sem cadastro em Configurações, upload/download avisam em vez de quebrar."""

    def setUp(self):
        self.user = get_user_model().objects.create_superuser(
            username='armaz_cli', email='armazcli@example.com', password='SenhaSegura123!',
        )
        plano = Plano.objects.create(slug='basico-armaz', nome='Basico Armaz', preco_mensal='0.00')
        self.cliente = Cliente.objects.create(
            slug='cliente-armaz', nome='Cliente Armaz', cnpj='44.555.666/0001-88',
            email_contato='armaz-cli@example.com', subdominio='cliente-armaz.ararasuite.com.br',
            plano=plano, status='ativo',
        )
        self.c = Client()
        self.c.force_login(self.user)

    def test_download_de_backup_remoto_sem_config_avisa(self):
        backup = BackupCliente.objects.create(
            cliente=self.cliente, status='concluido', progresso=100,
            arquivo_path='s3://clientes/cliente-armaz/backup.tar.gz', tamanho_bytes=10,
        )
        url = reverse('admin:registry_cliente_backup_download', args=[self.cliente.pk, backup.pk])
        resp = self.c.get(url, follow=True)
        self.assertContains(resp, 'Armazenamento de backups não configurado')

    def test_upload_manual_sem_config_avisa_e_nao_cria_registro(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        import io
        import tarfile
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode='w:gz') as tar:
            info = tarfile.TarInfo('db.sql')
            info.size = 2
            tar.addfile(info, io.BytesIO(b'ok'))
        url = reverse('admin:registry_cliente_backup_upload', args=[self.cliente.pk])
        resp = self.c.post(
            url, {'arquivo': SimpleUploadedFile('b.tar.gz', buf.getvalue())}, follow=True,
        )
        self.assertContains(resp, 'Armazenamento de backups não configurado')
        self.assertFalse(BackupCliente.objects.filter(cliente=self.cliente).exists())
