import threading
import time
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.db.models import Value
from django.test import Client, SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone

from registry import storage_backup
from registry.admin import ClienteAdmin
from registry.log_operacao import LogOperacao, Progresso, fmt_bytes, rodar_com_pulso, tamanho_pasta
from registry.models import BackupCliente, Cliente, Plano
from registry.tasks import task_backup_cliente, task_restaurar_no
from registry.tests.helpers_storage import instalar_fake_bucket


def _texto_da_expressao(expr):
    """O texto de `Concat(F(campo), Value(texto))` (o Django aninha as expressões)."""
    return next(e.value for e in expr.flatten() if isinstance(e, Value))


def _linhas_do_log(filter_mock, campo='log'):
    """Textos anexados ao log (cada `.update(log=Concat(F, Value(texto)))` capturado no mock)."""
    return [
        _texto_da_expressao(c.kwargs[campo])
        for c in filter_mock.return_value.update.call_args_list
        if c.kwargs.get(campo) is not None and not isinstance(c.kwargs.get(campo), str)
    ]


class FormatosTests(SimpleTestCase):
    def test_fmt_bytes(self):
        self.assertEqual(fmt_bytes(512), '512 B')
        self.assertEqual(fmt_bytes(2048), '2.0 KB')
        self.assertEqual(fmt_bytes(5 * 1024 * 1024), '5.0 MB')
        self.assertEqual(fmt_bytes(3 * 1024 ** 3), '3.0 GB')

    def test_progresso_texto(self):
        p = Progresso(total=200)
        p.somar(50)
        self.assertEqual(p.texto(), '50 B de 200 B (25%)')
        self.assertEqual(Progresso().texto(), '0 B')

    def test_tamanho_pasta(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / 'a').write_bytes(b'x' * 10)
            (Path(d) / 'sub').mkdir()
            (Path(d) / 'sub' / 'b').write_bytes(b'y' * 5)
            self.assertEqual(tamanho_pasta(d), 15)


class LogOperacaoTests(SimpleTestCase):
    def setUp(self):
        self.filter = MagicMock()
        p = patch('registry.models.BackupCliente.objects.filter', self.filter)
        p.start()
        self.addCleanup(p.stop)

    def test_linha_anexa_com_horario_e_renova_o_heartbeat(self):
        LogOperacao(7).linha('olá')
        self.filter.assert_called_with(pk=7)
        kwargs = self.filter.return_value.update.call_args.kwargs
        texto = _texto_da_expressao(kwargs['log'])
        self.assertRegex(texto, r'^\[\d\d:\d\d:\d\d\] olá\n$')
        self.assertIn('atividade_em', kwargs)

    def test_campo_da_restauracao(self):
        LogOperacao(7, 'log_restauracao').linha('x')
        self.assertIn('log_restauracao', self.filter.return_value.update.call_args.kwargs)

    def test_pulso_renova_so_o_heartbeat(self):
        LogOperacao(7).pulso()
        self.assertEqual(list(self.filter.return_value.update.call_args.kwargs), ['atividade_em'])

    def test_reiniciar_limpa_e_marca_o_inicio_da_restauracao(self):
        LogOperacao(7, 'log_restauracao').reiniciar()
        kwargs = self.filter.return_value.update.call_args.kwargs
        self.assertEqual(kwargs['log_restauracao'], '')
        self.assertIn('restauracao_iniciada_em', kwargs)
        LogOperacao(7).reiniciar()
        self.assertNotIn('restauracao_iniciada_em', self.filter.return_value.update.call_args.kwargs)

    def test_campo_invalido_e_recusado(self):
        with self.assertRaises(AssertionError):
            LogOperacao(1, 'mensagem')


class RodarComPulsoTests(SimpleTestCase):
    def test_devolve_o_resultado_e_registra_pulsos_e_linhas(self):
        log = MagicMock()
        r = rodar_com_pulso(
            log, 'pg_dump', lambda x: (time.sleep(0.45), x * 2)[1], 21,
            medir=lambda: '12 MB', intervalo=0.05, linha_a_cada=0.15,
        )
        self.assertEqual(r, 42)
        self.assertTrue(log.pulso.called)
        linhas = [c.args[0] for c in log.linha.call_args_list]
        self.assertTrue(linhas)
        self.assertTrue(all(l.startswith('pg_dump em execução há ') and l.endswith('· 12 MB') for l in linhas))

    def test_funcao_rapida_nao_gera_linha(self):
        log = MagicMock()
        self.assertEqual(rodar_com_pulso(log, 'x', lambda: 1, intervalo=0.5), 1)
        log.linha.assert_not_called()

    def test_excecao_da_funcao_e_propagada_inclusive_timeout(self):
        for exc in (RuntimeError('boom'), TimeoutError('socket')):
            def falha(e=exc):
                raise e
            with self.assertRaises(type(exc)):
                rodar_com_pulso(MagicMock(), 'x', falha, intervalo=0.05)

    def test_medir_com_erro_nao_derruba_o_pulso(self):
        log = MagicMock()
        rodar_com_pulso(log, 'x', time.sleep, 0.3, medir=lambda: 1 / 0, intervalo=0.05, linha_a_cada=0.1)
        self.assertTrue(log.linha.called)
        self.assertTrue(all(c.args[0].endswith('s') for c in log.linha.call_args_list))

    def test_o_log_so_e_escrito_na_thread_principal(self):
        threads = []
        log = MagicMock()
        log.linha.side_effect = lambda *_: threads.append(threading.current_thread())
        log.pulso.side_effect = lambda *_: threads.append(threading.current_thread())
        rodar_com_pulso(log, 'x', time.sleep, 0.3, intervalo=0.05, linha_a_cada=0.1)
        self.assertTrue(threads)
        self.assertTrue(all(t is threading.main_thread() for t in threads))


class StorageComProgressoTests(SimpleTestCase):
    def test_enviar_passa_o_callback_e_define_o_total(self):
        import tempfile
        s3 = MagicMock()
        cfg = SimpleNamespace(bucket='b')
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(b'x' * 100)
        progresso = Progresso()
        with patch('registry.storage_backup._cliente', return_value=s3):
            storage_backup.enviar(f.name, 'k', progresso=progresso, cfg=cfg)
        self.assertEqual(progresso.total, 100)
        self.assertEqual(s3.upload_file.call_args.kwargs['Callback'], progresso.somar)

    def test_baixar_descobre_o_total_pelo_head_object(self):
        s3 = MagicMock()
        s3.head_object.return_value = {'ContentLength': 4096}
        progresso = Progresso()
        with patch('registry.storage_backup._cliente', return_value=s3):
            storage_backup.baixar('s3://k', 'destino', progresso=progresso, cfg=SimpleNamespace(bucket='b'))
        self.assertEqual(progresso.total, 4096)
        self.assertEqual(s3.download_file.call_args.kwargs['Callback'], progresso.somar)

    def test_sem_progresso_nao_passa_callback(self):
        s3 = MagicMock()
        with patch('registry.storage_backup._cliente', return_value=s3):
            storage_backup.baixar('s3://k', 'destino', cfg=SimpleNamespace(bucket='b'))
        self.assertIsNone(s3.download_file.call_args.kwargs['Callback'])


class LogsDasTasksTests(SimpleTestCase):
    """O log conta a história do backup/restauração, etapa por etapa."""

    def setUp(self):
        import tempfile
        from pathlib import Path
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = patch.dict('os.environ', {
            'CP_BACKUP_DIR': str(Path(self.tmp.name) / 'b'), 'CLIENTES_BASE_PATH': str(Path(self.tmp.name) / 'c'),
            'NODE_HOSTNAME': 'worker-7',
        })
        env.start()
        self.addCleanup(env.stop)
        p = patch('registry.models.Cliente.objects.get', return_value=SimpleNamespace(pk='c', slug='acme'))
        p.start()
        self.addCleanup(p.stop)
        self.filter = MagicMock()
        self.filter.return_value.order_by.return_value.values_list.return_value = []
        p = patch('registry.models.BackupCliente.objects.filter', self.filter)
        p.start()
        self.addCleanup(p.stop)
        self.bucket = instalar_fake_bucket(self)

    def _fake_run(self, db='db-ctr\n', web='web-ctr\n', dump=b'-- dump', rc=0):
        def _run(cmd, *a, **kw):
            if cmd[:2] == ['docker', 'ps']:
                alvo = cmd[-1]
                saida = db if '_db' in alvo else web
                return SimpleNamespace(stdout=saida, stderr='', returncode=0)
            if cmd[1] == 'exec':
                return SimpleNamespace(stdout=dump, stderr=b'senha incorreta', returncode=rc)
            if cmd[1] == 'cp':
                return SimpleNamespace(stdout=b'', stderr=b'', returncode=0)
            raise AssertionError(cmd)
        return _run

    def test_backup_completo_conta_as_etapas_na_ordem(self):
        with patch('subprocess.run', side_effect=self._fake_run()):
            task_backup_cliente('c', 7, config_files={'.env': 'Rk9PPWJhcg=='})
        texto = ''.join(_linhas_do_log(self.filter))
        etapas = [
            'Worker "worker-7" iniciou o backup', '[1/4] Banco de dados', 'Container do banco encontrado',
            'pg_dump concluído: 7 B', 'db.sql compactado em', '[2/4] Mídia', 'Mídia copiada', '[3/4] Configuração do cliente',
            'Configuração adicionada (1 arquivo(s))', 'Finalizando o arquivo', 'Arquivo pronto',
            '[4/4] Enviando ao armazenamento (bucket "fake")', 'Envio concluído', 'CONCLUÍDO',
        ]
        posicoes = [texto.index(e) for e in etapas]
        self.assertEqual(posicoes, sorted(posicoes))
        self.assertNotIn('ERRO', texto)

    def test_avisos_e_erro_ficam_no_log(self):
        with patch('subprocess.run', side_effect=self._fake_run(db='', web='')):
            task_backup_cliente('c', 7, config_files={})
        texto = ''.join(_linhas_do_log(self.filter))
        self.assertIn('AVISO: container acme_db não encontrado', texto)
        self.assertIn('AVISO: container acme_web não encontrado', texto)
        self.assertIn('ERRO: nenhum conteúdo foi capturado', texto)

    def test_falha_do_pg_dump_vira_aviso_no_log(self):
        with patch('subprocess.run', side_effect=self._fake_run(rc=1)):
            task_backup_cliente('c', 7, config_files={'.env': 'Rk9PPWJhcg=='})
        texto = ''.join(_linhas_do_log(self.filter))
        self.assertIn('AVISO: pg_dump falhou (código 1): senha incorreta', texto)
        self.assertIn('CONCLUÍDO', texto)
        self.assertIn('(com avisos)', texto)

    def test_excecao_registra_erro_no_log(self):
        with patch('subprocess.run', side_effect=self._fake_run()), \
                patch('registry.storage_backup.enviar', side_effect=RuntimeError('bucket fora')):
            with self.assertRaises(RuntimeError):
                task_backup_cliente('c', 7, config_files={'.env': 'Rk9PPWJhcg=='})
        texto = ''.join(_linhas_do_log(self.filter))
        self.assertIn('ERRO: Falha ao enviar o backup para o armazenamento', texto)

    def test_restauracao_usa_o_log_proprio(self):
        import io
        import tarfile
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode='w:gz') as tar:
            info = tarfile.TarInfo('db.sql')
            info.size = 4
            tar.addfile(info, io.BytesIO(b'-- x\n'[:4]))
        tar_bytes = buf.getvalue()
        backup = SimpleNamespace(pk=7, arquivo_path='s3://clientes/acme/b.tar.gz')

        def run(cmd, *a, **kw):
            if cmd[:2] == ['docker', 'ps']:
                return SimpleNamespace(stdout='db-ctr\n', stderr='', returncode=0)
            return SimpleNamespace(stdout=b'', stderr=b'', returncode=0)

        with patch('registry.models.BackupCliente.objects.get', return_value=backup), \
                patch('registry.tasks.task_finalizar_restauracao.apply_async'), \
                patch('subprocess.run', side_effect=run), \
                patch.object(self.bucket, 'baixar', side_effect=lambda c, d, **kw: open(d, 'wb').write(tar_bytes)), \
                patch('registry.storage_backup.baixar', side_effect=lambda c, d, **kw: open(d, 'wb').write(tar_bytes)):
            task_restaurar_no('c', 7)
        texto = ''.join(_linhas_do_log(self.filter, 'log_restauracao'))
        for etapa in ('iniciou a restauração', '[1/4] Baixando', 'Download concluído', '[2/4] Extraindo',
                      '[3/4] Restaurando o banco', 'Banco restaurado em', '[4/4] Restaurando a mídia',
                      'AVISO: o backup não contém media/', 'Pedindo ao manager para religar o web'):
            self.assertIn(etapa, texto)
        self.assertEqual(_linhas_do_log(self.filter, 'log'), [])  # não mistura com o log do backup


class _Base(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_superuser('con_admin', 'con@example.com', 'SenhaSegura123!')
        plano = Plano.objects.create(slug='basico-con', nome='Basico Con', preco_mensal='0.00')
        self.cliente = Cliente.objects.create(
            slug='cliente-con', nome='Cliente Con', cnpj='55.666.777/0001-99', email_contato='con-cli@example.com',
            subdominio='cliente-con.ararasuite.com.br', plano=plano, status='ativo',
        )
        self.c = Client()
        self.c.force_login(self.user)
        self.pk = self.cliente.pk

    def _url(self, nome, backup, **kw):
        return reverse(f'admin:registry_cliente_{nome}', args=[self.pk, backup.pk]) + kw.get('qs', '')


class StatusDoConsoleTests(_Base):
    def test_campos_novos_do_backup_em_andamento(self):
        b = BackupCliente.objects.create(cliente=self.cliente)
        BackupCliente.objects.filter(pk=b.pk).update(
            log='[10:00:00] Tarefa enviada à fila "no.w". Aguardando um worker do nó iniciar…\n',
            atividade_em=timezone.now() - timedelta(seconds=45), criado_em=timezone.now() - timedelta(seconds=60),
        )
        j = self.c.get(self._url('backup_status', b)).json()
        self.assertTrue(j['ativo'] and j['ativo_operacao'])
        self.assertEqual(j['operacao'], 'backup')
        self.assertIn('Aguardando um worker', j['log'])
        self.assertTrue(40 <= j['sem_atividade_s'] <= 55)
        self.assertTrue(55 <= j['decorrido_s'] <= 75)
        self.assertTrue(j['aguardando_worker'])

    def test_sem_nenhuma_linha_de_log_a_tarefa_nem_comecou(self):
        b = BackupCliente.objects.create(cliente=self.cliente)
        j = self.c.get(self._url('backup_status', b)).json()
        self.assertTrue(j['sem_inicio'])
        BackupCliente.objects.filter(pk=b.pk).update(log='[10:00:00] Backup solicitado.\n')
        self.assertFalse(self.c.get(self._url('backup_status', b)).json()['sem_inicio'])

    def test_worker_que_iniciou_nao_esta_mais_aguardando(self):
        b = BackupCliente.objects.create(cliente=self.cliente)
        BackupCliente.objects.filter(pk=b.pk).update(
            log='Aguardando um worker do nó iniciar…\nWorker "w" iniciou o backup.\n')
        self.assertFalse(self.c.get(self._url('backup_status', b)).json()['aguardando_worker'])

    def test_restauracao_usa_o_log_e_o_inicio_proprios(self):
        b = BackupCliente.objects.create(cliente=self.cliente, status='concluido', progresso=100)
        BackupCliente.objects.filter(pk=b.pk).update(
            log='log do backup', log_restauracao='log da restauração', restaurando=True,
            restauracao_iniciada_em=timezone.now() - timedelta(seconds=20), atividade_em=timezone.now(),
            criado_em=timezone.now() - timedelta(days=1),
        )
        j = self.c.get(self._url('backup_status', b)).json()
        self.assertEqual((j['operacao'], j['log']), ('restauracao', 'log da restauração'))
        self.assertTrue(j['ativo_operacao'])
        self.assertLess(j['decorrido_s'], 40)  # conta da restauração, não da criação do backup

    def test_op_restauracao_de_operacao_ja_terminada(self):
        b = BackupCliente.objects.create(cliente=self.cliente, status='concluido', progresso=100)
        BackupCliente.objects.filter(pk=b.pk).update(log='bk', log_restauracao='rst', mensagem_restauracao='aviso x')
        j = self.c.get(self._url('backup_status', b, qs='?op=restauracao')).json()
        self.assertEqual((j['log'], j['ativo_operacao'], j['mensagem']), ('rst', False, 'aviso x'))
        j2 = self.c.get(self._url('backup_status', b)).json()
        self.assertEqual(j2['log'], 'bk')

    def test_contrato_antigo_continua_valendo(self):
        b = BackupCliente.objects.create(cliente=self.cliente)
        j = self.c.get(self._url('backup_status', b)).json()
        for chave in ('status', 'progresso', 'restaurando', 'ativo', 'mensagem'):
            self.assertIn(chave, j)


class InterromperTests(_Base):
    def _parado(self, segundos):
        b = BackupCliente.objects.create(cliente=self.cliente)
        BackupCliente.objects.filter(pk=b.pk).update(atividade_em=timezone.now() - timedelta(seconds=segundos))
        return b

    def test_so_aceita_post(self):
        b = self._parado(500)
        self.assertEqual(self.c.get(self._url('backup_interromper', b)).status_code, 405)

    def test_recusa_se_a_atividade_e_recente(self):
        b = self._parado(30)
        r = self.c.post(self._url('backup_interromper', b))
        self.assertEqual((r.status_code, r.json()['motivo']), (409, 'recente'))
        b.refresh_from_db()
        self.assertEqual(b.status, 'em_andamento')

    def test_recusa_operacao_que_nao_esta_ativa(self):
        b = BackupCliente.objects.create(cliente=self.cliente, status='concluido')
        r = self.c.post(self._url('backup_interromper', b))
        self.assertEqual((r.status_code, r.json()['motivo']), (409, 'nao_ativa'))

    def test_interrompe_backup_parado(self):
        b = self._parado(300)
        r = self.c.post(self._url('backup_interromper', b))
        self.assertTrue(r.json()['ok'])
        b.refresh_from_db()
        self.assertEqual(b.status, 'erro')
        self.assertIn('Interrompida manualmente', b.mensagem)
        self.assertIn('ERRO: Interrompida manualmente', b.log)

    def test_interromper_restauracao_religa_o_web(self):
        b = BackupCliente.objects.create(cliente=self.cliente, status='concluido', progresso=100)
        BackupCliente.objects.filter(pk=b.pk).update(
            restaurando=True, atividade_em=timezone.now() - timedelta(seconds=400),
            restauracao_iniciada_em=timezone.now() - timedelta(seconds=500),
        )
        with patch('registry.tasks._scale_web') as scale:
            r = self.c.post(self._url('backup_interromper', b))
        self.assertTrue(r.json()['ok'])
        scale.assert_called_once_with('cliente-con', 1)
        b.refresh_from_db()
        self.assertFalse(b.restaurando)
        self.assertIn('Interrompida manualmente', b.mensagem_restauracao)
        self.assertEqual(b.status, 'concluido')  # o backup em si continua válido


class RedirectEConsoleTests(_Base):
    def test_novo_backup_redireciona_para_o_console(self):
        with patch('registry.tasks.task_iniciar_backup_cliente.delay') as delay:
            r = self.c.get(reverse('admin:registry_cliente_backup', args=[self.pk]))
        b = BackupCliente.objects.get(cliente=self.cliente)
        delay.assert_called_once()
        self.assertEqual(r.status_code, 302)
        self.assertTrue(r.url.endswith(f'/change/?acompanhar={b.pk}'))

    def test_restaurar_redireciona_para_o_console_da_restauracao(self):
        b = BackupCliente.objects.create(cliente=self.cliente, status='concluido', progresso=100)
        with patch('registry.tasks.task_restaurar_cliente.delay'):
            r = self.c.get(self._url('restaurar', b))
        self.assertTrue(r.url.endswith(f'/change/?acompanhar={b.pk}&op=restauracao'))

    def _pagina(self):
        return self.c.get(reverse('admin:registry_cliente_change', args=[self.pk])).content.decode()

    def test_console_renderizado_com_a_operacao_em_andamento(self):
        b = BackupCliente.objects.create(cliente=self.cliente)
        html = self._pagina()
        self.assertIn('id="bk-console"', html)
        self.assertIn(f'data-ativo-id="{b.pk}"', html)
        self.assertIn('data-ativo-op="backup"', html)
        self.assertIn('/backups/0/status/', html)
        self.assertIn('/backups/0/interromper/', html)
        self.assertNotIn('location.reload()', html.split('bpbar-')[1])  # não recarrega mais sozinho

    def test_console_sem_operacao_ativa_nao_abre_sozinho(self):
        BackupCliente.objects.create(cliente=self.cliente, status='concluido', progresso=100)
        self.assertIn('data-ativo-id=""', self._pagina())

    def test_restauracao_em_andamento_abre_como_restauracao(self):
        b = BackupCliente.objects.create(cliente=self.cliente, status='concluido', progresso=100)
        BackupCliente.objects.filter(pk=b.pk).update(restaurando=True)
        html = self._pagina()
        self.assertIn(f'data-ativo-id="{b.pk}"', html)
        self.assertIn('data-ativo-op="restauracao"', html)

    def test_log_guardado_aparece_expansivel_e_escapado(self):
        BackupCliente.objects.create(
            cliente=self.cliente, status='erro', progresso=90, mensagem='falhou',
            log='[10:00:00] ERRO: <script>alert(1)</script>\n', log_restauracao='[11:00:00] restaurou\n',
        )
        html = self._pagina()
        self.assertIn('📜 Log', html)
        self.assertIn('Log da última restauração', html)
        self.assertNotIn('<script>alert(1)</script>', html)
        self.assertIn('&lt;script&gt;alert(1)&lt;/script&gt;', html)

    def test_backup_antigo_sem_log_nao_mostra_o_botao(self):
        BackupCliente.objects.create(cliente=self.cliente, status='concluido', progresso=100)
        self.assertNotIn('📜 Log', self._pagina())


class AbasDoClienteTests(_Base):
    # campos das 11 seções + 3 históricos antes da divisão em abas
    CAMPOS_ANTES = [
        'id', 'slug', 'nome', 'cnpj', 'email_contato', 'telefone',
        'painel_acesso',
        'host', 'versao_erp', 'stack_path', 'subdominio', 'dominio_custom',
        'versao_atual',
        'plano', 'modulos_ativos', 'tema_site', 'acoes_plano_sync',
        'atualizacao_automatica_agente', 'versoes_permitidas', 'acoes_versoes',
        'asaas_customer_id', 'asaas_subscription_id', 'badge_isencao', 'isento_cobranca', 'motivo_isencao',
        'status_badge', 'status', 'trial_ate', 'data_ativacao', 'data_suspensao', 'data_cancelamento',
        'acoes_provisionamento',
        'lista_backups',
        'observacoes', 'criado_em', 'atualizado_em',
    ]

    def test_nenhum_campo_se_perdeu_nem_duplicou(self):
        depois = [campo for _, cfg in ClienteAdmin.fieldsets for campo in cfg['fields']]
        self.assertCountEqual(depois, self.CAMPOS_ANTES)
        self.assertEqual(len(depois), len(set(depois)))

    def test_todo_fieldset_e_uma_aba(self):
        for nome, cfg in ClienteAdmin.fieldsets:
            self.assertIn('tab', cfg['classes'], nome)

    def test_historicos_viram_abas(self):
        for inline in ClienteAdmin.inlines:
            self.assertTrue(inline.tab, inline.__name__)

    def test_pagina_renderiza_as_abas_e_o_script(self):
        html = self.c.get(reverse('admin:registry_cliente_change', args=[self.pk])).content.decode()
        for nome in ('Cadastro', 'Acesso e Infraestrutura', 'Plano e Faturamento', 'Versões', 'Status e Ações', 'Backups'):
            self.assertIn(nome, html)
        self.assertEqual(html.count('class="tab-wrapper'), 6)
        self.assertIn('registry/abas_cliente.js', html)

    def test_pagina_de_adicionar_tambem_funciona(self):
        r = self.c.get(reverse('admin:registry_cliente_add'))
        self.assertEqual(r.status_code, 200)
