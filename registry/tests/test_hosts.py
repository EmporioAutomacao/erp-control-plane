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
            nome='Hostinger8vCPU32gb400gb', ip='10.8.0.2', regiao_id='hostinger', cpu_total=8, ram_gb_total=32,
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
        HostInfraestrutura.objects.create(nome='inativo', ip='10.0.0.9', regiao_id='anapolis', cpu_total=1,
                                          ram_gb_total=1, ativo=False)
        with patch('registry.tasks.task_pingar_host.apply_async') as ping:
            task_verificar_hosts()
        ping.assert_called_once_with(args=[self.host.pk, 'srv-hostinger'], queue='no.srv-hostinger', expires=240)

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
