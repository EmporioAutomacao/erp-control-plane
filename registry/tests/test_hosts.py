"""Sinalizador de saúde dos Hosts de Infraestrutura (registry/hosts.py) — Swarm simulado."""
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import Client, SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone

from registry import hosts
from registry.models import HostInfraestrutura, Regiao


def _no(**extra):
    base = {
        'id': 'NODE1', 'hostname': 'srv-hostinger', 'status': 'Ready', 'endereco': '10.8.0.2',
        'disponibilidade': 'Active', 'labels': {'region': 'hostinger'}, 'arquitetura': 'x86_64',
        'cpus': 8.0, 'ram_bytes': 32 * 1024 ** 3,
    }
    return {**base, **extra}


class _Base(TestCase):
    def setUp(self):
        Regiao.objects.create(slug='hostinger', nome='Hostinger')
        self.host = HostInfraestrutura.objects.create(
            nome='Hostinger8vCPU32gb400gb', ip='10.8.0.2', regiao_id='hostinger',
            agente_respondeu_em=timezone.now(), disco_livre_bytes=300, disco_total_bytes=400,
        )
        self.no = _no()
        self.nos_regiao = [{'id': 'NODE1', 'hostname': 'srv-hostinger'}]
        for alvo, kw in [
            ('registry.swarm.swarm_ativo', {'return_value': True}),
            ('registry.swarm.inspecionar_no', {'side_effect': lambda n: self.no}),
            ('registry.swarm.nos_da_regiao', {'side_effect': lambda r: self.nos_regiao}),
        ]:
            p = patch(alvo, **kw)
            p.start()
            self.addCleanup(p.stop)

    def verificar(self):
        status, mensagem = hosts.verificar_host(self.host)
        self.host.refresh_from_db()
        self.assertEqual(self.host.status_conexao, status)
        self.assertEqual(self.host.status_mensagem, mensagem)
        return status, mensagem


class StatusTests(_Base):
    def test_tudo_ok_fica_online_e_grava_os_dados_do_no(self):
        status, _ = self.verificar()
        self.assertEqual(status, 'online')
        self.assertEqual((self.host.no_hostname, self.host.cpus_swarm), ('srv-hostinger', 8.0))
        self.assertIsNotNone(self.host.verificado_em)

    def test_preenche_o_swarm_node_id_quando_ha_um_unico_no(self):
        self.verificar()
        self.assertEqual(self.host.swarm_node_id, 'NODE1')

    def test_com_swarm_node_id_usa_o_id_e_nao_o_label(self):
        HostInfraestrutura.objects.filter(pk=self.host.pk).update(swarm_node_id='OUTRO')
        self.host.refresh_from_db()
        self.nos_regiao = []
        with patch('registry.swarm.inspecionar_no', return_value=self.no) as inspecionar:
            status, _ = self.verificar()
        inspecionar.assert_called_once_with('OUTRO')
        self.assertEqual(status, 'online')

    def test_no_down_fica_offline(self):
        self.no = _no(status='Down')
        self.assertEqual(self.verificar()[0], 'offline')

    def test_no_inexistente_fica_offline(self):
        self.nos_regiao = []
        status, mensagem = self.verificar()
        self.assertEqual(status, 'offline')
        self.assertIn('region=hostinger', mensagem)

    def test_node_id_que_nao_existe_fica_offline(self):
        HostInfraestrutura.objects.filter(pk=self.host.pk).update(swarm_node_id='SUMIU')
        self.host.refresh_from_db()
        with patch('registry.swarm.inspecionar_no', return_value=None):
            status, mensagem = self.verificar()
        self.assertEqual(status, 'offline')
        self.assertIn('SUMIU', mensagem)

    def test_dois_nos_na_regiao_sem_id_pede_o_id(self):
        self.nos_regiao = [{'id': 'A', 'hostname': 'a'}, {'id': 'B', 'hostname': 'b'}]
        status, mensagem = self.verificar()
        self.assertEqual(status, 'offline')
        self.assertIn('preencha o Swarm node ID', mensagem)

    def test_drain_fica_em_atencao(self):
        self.no = _no(disponibilidade='Drain')
        self.assertEqual(self.verificar()[0], 'atencao')

    def test_arm_fica_em_atencao(self):
        self.no = _no(arquitetura='aarch64')
        self.assertEqual(self.verificar()[0], 'atencao')

    def test_label_divergente_fica_em_atencao(self):
        self.no = _no(labels={'region': 'anapolis'})
        status, mensagem = self.verificar()
        self.assertEqual(status, 'atencao')
        self.assertIn('"anapolis"', mensagem)

    def test_ip_diferente_do_swarm_fica_em_atencao(self):
        self.no = _no(endereco='187.1.2.3')
        status, mensagem = self.verificar()
        self.assertEqual(status, 'atencao')
        self.assertIn('187.1.2.3', mensagem)

    def test_disco_quase_cheio_fica_em_atencao(self):
        HostInfraestrutura.objects.filter(pk=self.host.pk).update(disco_livre_bytes=40, disco_total_bytes=400)
        self.host.refresh_from_db()
        status, mensagem = self.verificar()
        self.assertEqual(status, 'atencao')
        self.assertIn('10%', mensagem)

    def test_worker_que_nunca_respondeu_fica_em_atencao(self):
        HostInfraestrutura.objects.filter(pk=self.host.pk).update(agente_respondeu_em=None)
        self.host.refresh_from_db()
        self.assertEqual(self.verificar()[0], 'atencao')

    def test_worker_sem_responder_ha_muito_tempo_fica_offline(self):
        HostInfraestrutura.objects.filter(pk=self.host.pk).update(
            agente_respondeu_em=timezone.now() - timedelta(seconds=hosts.LIMITE_SEM_RESPOSTA_S + 60))
        self.host.refresh_from_db()
        status, mensagem = self.verificar()
        self.assertEqual(status, 'offline')
        self.assertIn('agente-no', mensagem)

    def test_sem_swarm_fica_nao_verificado(self):
        with patch('registry.swarm.swarm_ativo', return_value=False):
            status, mensagem = self.verificar()
        self.assertEqual(status, 'nao_verificado')
        self.assertIn('Sem Swarm', mensagem)


class TasksTests(_Base):
    def test_verificacao_periodica_pede_ping_na_fila_do_no(self):
        from registry.tasks import task_verificar_hosts
        HostInfraestrutura.objects.create(nome='inativo', ip='10.0.0.9', regiao_id='anapolis', ativo=False)
        with patch('registry.tasks.task_pingar_host.apply_async') as ping:
            task_verificar_hosts()
        ping.assert_called_once_with(args=[self.host.pk, 'srv-hostinger', True], queue='no.srv-hostinger', expires=240)

    def test_ping_grava_disco_e_clientes_e_reavalia(self):
        from registry.tasks import task_pingar_host
        HostInfraestrutura.objects.filter(pk=self.host.pk).update(agente_respondeu_em=None)
        with patch.dict('os.environ', {'NODE_HOSTNAME': 'srv-hostinger'}), \
                patch('registry.volumes.disco_docker', return_value=(100, 400)), \
                patch('registry.volumes.stacks_de_clientes_no_no', return_value=3), \
                patch('registry.atualizacoes.descobrir_stack', return_value='cp'), \
                patch('registry.tasks.task_reavaliar_host.apply_async') as reavaliar:
            task_pingar_host(self.host.pk, 'srv-hostinger')
        self.host.refresh_from_db()
        self.assertIsNotNone(self.host.agente_respondeu_em)
        self.assertEqual((self.host.disco_livre_bytes, self.host.disco_total_bytes, self.host.containers_clientes),
                         (100, 400, 3))
        reavaliar.assert_called_once_with(args=[self.host.pk], queue='celery')

    def test_ping_no_no_errado_nao_conta(self):
        from registry.tasks import task_pingar_host
        HostInfraestrutura.objects.filter(pk=self.host.pk).update(agente_respondeu_em=None)
        with patch.dict('os.environ', {'NODE_HOSTNAME': 'outro'}):
            task_pingar_host(self.host.pk, 'srv-hostinger')
        self.host.refresh_from_db()
        self.assertIsNone(self.host.agente_respondeu_em)

    def test_verificacao_vem_agendada_pela_migration(self):
        from django_celery_beat.models import PeriodicTask
        t = PeriodicTask.objects.get(task='registry.tasks.task_verificar_hosts')
        self.assertEqual((t.enabled, t.interval.every, t.interval.period), (True, 5, 'minutes'))


class AdminTests(_Base):
    def setUp(self):
        super().setUp()
        self.user = get_user_model().objects.create_superuser('hosts', 'h@example.com', 'SenhaSegura123!')
        self.c = Client()
        self.c.force_login(self.user)

    def test_lista_mostra_a_coluna_status(self):
        hosts.verificar_host(self.host)
        html = self.c.get(reverse('admin:registry_hostinfraestrutura_changelist')).content.decode()
        self.assertIn('● Online', html)

    def test_formulario_mostra_painel_e_botao(self):
        html = self.c.get(reverse('admin:registry_hostinfraestrutura_change', args=[self.host.pk])).content.decode()
        self.assertIn('id="host-badge"', html)
        self.assertIn('Verificar agora', html)
        self.assertIn('Worker do CP respondeu', html)

    def test_formulario_nao_pede_cpu_nem_memoria(self):
        html = self.c.get(reverse('admin:registry_hostinfraestrutura_change', args=[self.host.pk])).content.decode()
        self.assertNotIn('name="cpu_total"', html)
        self.assertNotIn('name="ram_gb_total"', html)
        self.assertNotIn('name="cpus_swarm"', html)

    def test_lista_mostra_cpus_memoria_e_disco_coletados(self):
        HostInfraestrutura.objects.filter(pk=self.host.pk).update(
            cpus_swarm=8, ram_bytes_swarm=32 * 1024 ** 3,
            disco_livre_bytes=300 * 1024 ** 3, disco_total_bytes=400 * 1024 ** 3,
        )
        html = self.c.get(reverse('admin:registry_hostinfraestrutura_changelist')).content.decode()
        self.assertIn('32.0 GB', html)
        self.assertIn('300.0 GB livres de 400.0 GB', html)

    def test_lista_sem_coleta_mostra_traco_e_disco_baixo_em_vermelho(self):
        from registry.admin import HostInfraestruturaAdmin
        from django.contrib.admin.sites import site
        admin_host = HostInfraestruturaAdmin(HostInfraestrutura, site)
        vazio = HostInfraestrutura(nome='x', ip='10.0.0.1', regiao_id='hostinger')
        self.assertEqual((admin_host.cpus_lista(vazio), admin_host.memoria_lista(vazio), admin_host.disco_lista(vazio)),
                         ('—', '—', '—'))
        cheio = HostInfraestrutura(nome='y', ip='10.0.0.1', regiao_id='hostinger', cpus_swarm=4,
                                   disco_livre_bytes=10, disco_total_bytes=400)
        self.assertEqual(admin_host.cpus_lista(cheio), '4')
        self.assertIn('#b71c1c', admin_host.disco_lista(cheio))

    def test_verificar_json(self):
        with patch('registry.tasks.task_pingar_host.apply_async'):
            j = self.c.get(reverse('admin:registry_hostinfraestrutura_verificar', args=[self.host.pk])).json()
        self.assertEqual((j['status'], j['rotulo']), ('online', 'Online'))

    def test_verificar_agora_grava_e_volta_para_o_formulario(self):
        self.no = _no(status='Down')
        with patch('registry.tasks.task_pingar_host.apply_async') as ping:
            r = self.c.get(reverse('admin:registry_hostinfraestrutura_verificar_agora', args=[self.host.pk]))
        self.assertRedirects(r, reverse('admin:registry_hostinfraestrutura_change', args=[self.host.pk]),
                             fetch_redirect_response=False)
        self.host.refresh_from_db()
        self.assertEqual(self.host.status_conexao, 'offline')
        ping.assert_called_once()


class StacksNoNoTests(SimpleTestCase):
    def test_conta_stacks_com_banco_exceto_o_do_cp(self):
        from registry.volumes import stacks_de_clientes_no_no
        nomes = 'acme_db.1.x\nacme_web.1.y\nloja_db.1.z\ncp_db.1.w\ncp_web.1.v\n'
        with patch('subprocess.run', return_value=SimpleNamespace(returncode=0, stdout=nomes, stderr='')):
            self.assertEqual(stacks_de_clientes_no_no(ignorar='cp'), 2)


class DiscoClientesTests(_Base):
    def _cliente(self, slug, host):
        from registry.models import Cliente, Plano
        plano, _ = Plano.objects.get_or_create(slug='p', defaults={'nome': 'Plano', 'preco_mensal': '0.00'})
        n = Cliente.objects.count() + 1
        return Cliente.objects.create(
            slug=slug, nome=slug, cnpj=f'00.000.000/{n:04d}-00', email_contato=f'{slug}@example.invalid',
            subdominio=f'{slug}.ararasuite.com.br', plano=plano, status='ativo', versao_erp='0.0.100', host=host,
        )

    def test_le_saida_do_du_por_volume(self):
        from registry.volumes import _ler_du_volumes
        saida = '2048\tloja_pgdata/_data\n10\tloja_media/_data\n4\tminha_loja_backups/_data\n5\t*_media/_data\n'
        self.assertEqual(_ler_du_volumes(saida), {
            'loja': {'pgdata': 2048 * 1024, 'media': 10 * 1024},
            'minha_loja': {'backups': 4 * 1024},
        })

    def test_grava_so_clientes_do_host(self):
        outro = HostInfraestrutura.objects.create(nome='outro', ip='10.0.0.3', regiao_id='hostinger')
        loja = self._cliente('loja', self.host)
        vazio = self._cliente('vazio', self.host)
        fora = self._cliente('fora', outro)
        hosts.registrar_disco_clientes(self.host.pk, {
            'loja': {'pgdata': 300, 'media': 100}, 'fora': {'pgdata': 999},
        })
        for c in (loja, vazio, fora):
            c.refresh_from_db()
        self.assertEqual((loja.disco_bytes, loja.disco_detalhe), (400, {'pgdata': 300, 'media': 100}))
        self.assertIsNone(vazio.disco_bytes)
        self.assertIsNotNone(vazio.disco_medido_em)
        self.assertIsNone(fora.disco_medido_em)

    def test_mede_no_maximo_a_cada_30_min(self):
        from registry.models import Cliente
        loja = self._cliente('loja', self.host)
        self.assertTrue(hosts.medir_disco_clientes_agora(self.host.pk))
        Cliente.objects.filter(pk=loja.pk).update(disco_medido_em=timezone.now() - timedelta(minutes=10))
        self.assertFalse(hosts.medir_disco_clientes_agora(self.host.pk))
        Cliente.objects.filter(pk=loja.pk).update(disco_medido_em=timezone.now() - timedelta(minutes=31))
        self.assertTrue(hosts.medir_disco_clientes_agora(self.host.pk))

    def test_ping_com_medicao_grava_disco_dos_clientes(self):
        from registry.tasks import task_pingar_host
        loja = self._cliente('loja', self.host)
        with patch.dict('os.environ', {'NODE_HOSTNAME': 'srv-hostinger'}),                 patch('registry.volumes.disco_docker', return_value=(100, 400)),                 patch('registry.volumes.stacks_de_clientes_no_no', return_value=1),                 patch('registry.volumes.tamanho_volumes_clientes', return_value={'loja': {'pgdata': 5}}),                 patch('registry.atualizacoes.descobrir_stack', return_value='cp'),                 patch('registry.tasks.task_reavaliar_host.apply_async'):
            task_pingar_host(self.host.pk, 'srv-hostinger', True)
        loja.refresh_from_db()
        self.assertEqual(loja.disco_bytes, 5)

    def test_coluna_da_lista(self):
        from django.contrib import admin as dj_admin
        from registry.models import Cliente
        coluna = dj_admin.site._registry[Cliente].col_disco
        loja = self._cliente('loja', self.host)
        self.assertIn('—', coluna(loja))
        loja.disco_bytes, loja.disco_detalhe = 3 * 1024 ** 3, {'pgdata': 2 * 1024 ** 3, 'media': 1024 ** 3}
        loja.disco_medido_em = timezone.now()
        html = coluna(loja)
        self.assertIn('3.0 GB', html)
        self.assertIn('Banco: 2.0 GB', html)
