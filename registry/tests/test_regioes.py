from django.contrib.auth import get_user_model
from django.db.models import ProtectedError
from django.test import Client, TestCase
from django.urls import reverse

from registry.models import Cliente, HostInfraestrutura, Plano, Regiao
from registry.provisioning import MotorProvisionamento


def _host(regiao, nome='host-1'):
    return HostInfraestrutura.objects.create(nome=nome, ip='10.0.0.1', regiao=regiao, cpu_total=8, ram_gb_total=32)


class RegiaoModeloTests(TestCase):
    def test_regioes_antigas_criadas_pela_migration(self):
        self.assertTrue({'anapolis', 'brasilia'} <= set(Regiao.objects.values_list('slug', flat=True)))

    def test_host_guarda_o_slug_da_regiao(self):
        regiao = Regiao.objects.create(slug='hostinger', nome='Hostinger')
        host = _host(regiao)
        self.assertEqual(host.regiao_id, 'hostinger')
        self.assertEqual(str(host), 'host-1 (hostinger)')

    def test_regiao_com_host_nao_pode_ser_apagada(self):
        regiao = Regiao.objects.create(slug='hostinger', nome='Hostinger')
        _host(regiao)
        with self.assertRaises(ProtectedError):
            regiao.delete()

    def test_provisionamento_usa_o_slug_no_placement(self):
        regiao = Regiao.objects.create(slug='hostinger', nome='Hostinger')
        cliente = Cliente(slug='acme', subdominio='acme.ararasuite.com.br', host=_host(regiao))
        motor = MotorProvisionamento.__new__(MotorProvisionamento)
        motor.cliente = cliente
        stack = motor._gerar_stack_yaml(
            slug='acme', subdominio='acme.ararasuite.com.br', versao='0.0.71',
            regiao=cliente.host.regiao_id, env_vars={'POSTGRES_PASSWORD': 'senha'},
        )
        self.assertIn('node.labels.region == hostinger', stack)


class RegiaoAdminTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_superuser('admin', 'admin@example.com', 'senha')
        self.c = Client()
        self.c.force_login(self.user)

    def test_cadastra_regiao_nova(self):
        resp = self.c.post(reverse('admin:registry_regiao_add'), {
            'nome': 'Hostinger', 'slug': 'hostinger', 'ativo': 'on', 'observacoes': '',
        })
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(Regiao.objects.filter(slug='hostinger', nome='Hostinger').exists())

    def test_rejeita_codigo_invalido_para_label(self):
        resp = self.c.post(reverse('admin:registry_regiao_add'), {
            'nome': 'São Paulo', 'slug': 'São Paulo', 'ativo': 'on', 'observacoes': '',
        })
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(Regiao.objects.filter(nome='São Paulo').exists())

    def test_codigo_nao_editavel_e_mostra_comando_do_label(self):
        Regiao.objects.create(slug='hostinger', nome='Hostinger')
        url = reverse('admin:registry_regiao_change', args=['hostinger'])
        resp = self.c.get(url)
        self.assertContains(resp, 'docker node update --label-add region=hostinger')
        self.assertNotContains(resp, 'name="slug"')

        self.c.post(url, {'nome': 'Hostinger BR', 'slug': 'outra', 'ativo': 'on', 'observacoes': ''})
        self.assertTrue(Regiao.objects.filter(slug='hostinger', nome='Hostinger BR').exists())
        self.assertFalse(Regiao.objects.filter(slug='outra').exists())

    def test_lista_conta_hosts_e_clientes(self):
        regiao = Regiao.objects.create(slug='hostinger', nome='Hostinger')
        host = _host(regiao)
        plano = Plano.objects.create(slug='basico', nome='Básico', preco_mensal=10)
        Cliente.objects.create(
            nome='Acme', slug='acme', cnpj='12.345.678/0001-90', email_contato='acme@example.com',
            subdominio='acme.ararasuite.com.br', plano=plano, host=host, status='ativo',
        )
        resp = self.c.get(reverse('admin:registry_regiao_changelist'))
        self.assertEqual(resp.status_code, 200)
        linha = resp.context['cl'].result_list.get(slug='hostinger')
        self.assertEqual((linha._qtd_hosts, linha._qtd_clientes), (1, 1))

    def test_host_so_oferece_regioes_ativas_mas_mantem_a_atual(self):
        inativa = Regiao.objects.create(slug='antiga', nome='Antiga', ativo=False)
        Regiao.objects.create(slug='hostinger', nome='Hostinger')

        resp = self.c.get(reverse('admin:registry_hostinfraestrutura_add'))
        opcoes = set(resp.context['adminform'].form.fields['regiao'].queryset.values_list('slug', flat=True))
        self.assertIn('hostinger', opcoes)
        self.assertNotIn('antiga', opcoes)

        host = _host(inativa)
        resp = self.c.get(reverse('admin:registry_hostinfraestrutura_change', args=[host.pk]))
        opcoes = set(resp.context['adminform'].form.fields['regiao'].queryset.values_list('slug', flat=True))
        self.assertIn('antiga', opcoes)
