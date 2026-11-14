"""Vários domínios próprios por cliente (registry/dominios.py, registry/cloudflare.py, admin)."""
import importlib
import json
import re
from types import SimpleNamespace
from unittest.mock import patch

from django.apps import apps as django_apps
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import Client, TestCase
from django.urls import reverse

from registry import cloudflare, dominios
from registry.models import Cliente, DominioCliente, Plano
from registry.provisioning import MotorProvisionamento


def _d(dominio, modo='servir', principal=False, www=True):
    return SimpleNamespace(dominio=dominio, modo=modo, principal=principal, incluir_www=www)


def _c(slug='acme', subdominio='acme.ararasuite.com.br'):
    return SimpleNamespace(slug=slug, subdominio=subdominio, pk=None)


class _ComCliente(TestCase):
    def setUp(self):
        self.plano = Plano.objects.create(slug='p-dom', nome='P', preco_mensal='0.00')
        self.cliente = self._novo('acme')

    def _novo(self, slug, **extra):
        return Cliente.objects.create(
            slug=slug, nome=slug, cnpj=f'00.000.{Cliente.objects.count():03d}/0001-00', email_contato=f'{slug}@x.invalid',
            subdominio=f'{slug}.ararasuite.com.br', plano=self.plano, status='ativo', **extra,
        )

    def _validar(self, **kw):
        d = DominioCliente(cliente=kw.pop('cliente', self.cliente), **kw)
        d.full_clean()
        return d


class NormalizacaoEValidacaoTests(_ComCliente):
    def test_normaliza_url_colada(self):
        self.assertEqual(dominios.normalizar(' https://Loja.COM.br:443/painel/?x=1 '), 'loja.com.br')
        self.assertEqual(dominios.normalizar('loja.com.br.'), 'loja.com.br')

    def test_dominio_com_acento_vira_punycode(self):
        self.assertEqual(dominios.normalizar('https://Lojá.com.br/'), 'xn--loj-gla.com.br')
        d = self._validar(dominio='café.com.br')
        self.assertEqual(d.dominio, 'xn--caf-dma.com.br')

    def test_salva_normalizado(self):
        d = self._validar(dominio='HTTPS://Loja.com.br/')
        d.save()
        self.assertEqual(DominioCliente.objects.get().dominio, 'loja.com.br')

    def test_formato_invalido(self):
        for ruim in ('loja', 'lo ja.com.br', '-loja.com.br', 'loja..com.br'):
            with self.assertRaises(ValidationError, msg=ruim):
                self._validar(dominio=ruim)

    def test_recusa_dominio_da_plataforma(self):
        with self.assertRaises(ValidationError):
            self._validar(dominio='outro.ararasuite.com.br')
        with self.assertRaises(ValidationError):
            self._validar(dominio='ararasuite.com.br')

    def test_recusa_duplicado_e_www_de_outro_cliente(self):
        outro = self._novo('beta')
        DominioCliente.objects.create(cliente=outro, dominio='loja.com.br', incluir_www=True)
        with self.assertRaises(ValidationError):
            self._validar(dominio='loja.com.br')
        with self.assertRaises(ValidationError):
            self._validar(dominio='www.loja.com.br')

    def test_principal_precisa_servir(self):
        with self.assertRaises(ValidationError) as ctx:
            self._validar(dominio='loja.com.br', modo='redirecionar', principal=True)
        self.assertIn('principal', ctx.exception.message_dict)

    def test_dois_principais_barrados_no_banco(self):
        from django.db import IntegrityError, transaction
        DominioCliente.objects.create(cliente=self.cliente, dominio='a.com.br', principal=True)
        with self.assertRaises(IntegrityError), transaction.atomic():
            DominioCliente.objects.create(cliente=self.cliente, dominio='b.com.br', principal=True)

    def test_www_explicito_desliga_incluir_www(self):
        d = self._validar(dominio='www.loja.com.br', incluir_www=True)
        self.assertFalse(d.incluir_www)

    def test_host_publico_e_url(self):
        self.assertEqual(self.cliente.url, 'https://acme.ararasuite.com.br')
        DominioCliente.objects.create(cliente=self.cliente, dominio='redir.com.br', modo='redirecionar')
        self.assertEqual(self.cliente.host_publico, 'acme.ararasuite.com.br')
        DominioCliente.objects.create(cliente=self.cliente, dominio='loja.com.br', principal=True)
        self.assertEqual(self.cliente.url, 'https://loja.com.br')


class EnvTests(TestCase):
    def test_allowed_hosts_so_dominios_que_servem(self):
        lista = [_d('loja.com.br', principal=True), _d('antiga.com.br', 'redirecionar'), _d('erp.loja2.com.br')]
        self.assertEqual(dominios.allowed_hosts(_c(), lista), 'acme.ararasuite.com.br,loja.com.br,erp.loja2.com.br')
        self.assertEqual(
            dominios.csrf_origins(_c(), lista),
            'https://acme.ararasuite.com.br,https://loja.com.br,https://erp.loja2.com.br',
        )

    def test_sem_dominios(self):
        self.assertEqual(dominios.allowed_hosts(_c(), []), 'acme.ararasuite.com.br')


class LabelsTraefikTests(TestCase):
    R = 'traefik.http.routers.'
    M = 'traefik.http.middlewares.'

    def test_servir_com_www(self):
        lb = dominios.labels_traefik(_c(), [_d('loja.com.br', principal=True)])
        self.assertEqual(lb[f'{self.R}acme-erp-custom-loja_com_br.rule'], 'Host(`loja.com.br`)')
        self.assertEqual(lb[f'{self.R}acme-erp-custom-loja_com_br.service'], 'acme-erp')
        self.assertNotIn(f'{self.R}acme-erp-custom-loja_com_br.middlewares', lb)
        self.assertEqual(lb[f'{self.R}acme-erp-custom-loja_com_br-www.rule'], 'Host(`www.loja.com.br`)')
        self.assertEqual(lb[f'{self.R}acme-erp-custom-loja_com_br-www.middlewares'], 'acme-redir-loja_com_br')
        self.assertEqual(lb[f'{self.M}acme-redir-loja_com_br.redirectregex.replacement'], 'https://loja.com.br${1}')
        self.assertEqual(lb[f'{self.M}acme-redir-loja_com_br.redirectregex.permanent'], 'true')

    def test_servir_sem_www(self):
        lb = dominios.labels_traefik(_c(), [_d('erp.loja.com.br', www=False)])
        self.assertFalse(any('-www.' in k for k in lb))
        self.assertFalse(any(k.startswith(self.M) for k in lb))

    def test_redirecionar_vai_para_o_principal(self):
        lb = dominios.labels_traefik(_c(), [_d('loja.com.br', principal=True), _d('antiga.com.br', 'redirecionar')])
        self.assertEqual(lb[f'{self.R}acme-erp-custom-antiga_com_br.middlewares'], 'acme-redir-loja_com_br')
        self.assertEqual(lb[f'{self.R}acme-erp-custom-antiga_com_br-www.middlewares'], 'acme-redir-loja_com_br')

    def test_redirecionar_sem_principal_vai_para_o_subdominio(self):
        lb = dominios.labels_traefik(_c(), [_d('antiga.com.br', 'redirecionar')])
        mw = f'{self.M}acme-redir-acme_ararasuite_com_br.redirectregex.replacement'
        self.assertEqual(lb[mw], 'https://acme.ararasuite.com.br${1}')

    def test_nomes_nao_colidem(self):
        lb = dominios.labels_traefik(_c(), [_d('a-b.com', www=False), _d('a.b-com', www=False)])
        self.assertEqual(len([k for k in lb if k.endswith('.rule')]), 2)


class ReconciliarTests(TestCase):
    def test_remove_legado_e_obsoletos_sem_tocar_no_resto(self):
        atuais = {
            'traefik.enable': 'true',
            'traefik.http.routers.acme-erp.rule': 'Host(`acme.ararasuite.com.br`)',
            'traefik.http.services.acme-erp.loadbalancer.server.port': '8000',
            'traefik.http.routers.acme-erp-custom.rule': 'Host(`velho.com.br`)',
            'traefik.http.routers.acme-erp-custom-www.rule': 'Host(`www.velho.com.br`)',
            'traefik.http.middlewares.acme-www-redirect.redirectregex.regex': 'x',
            'traefik.http.middlewares.acme-redir-sumiu_com_br.redirectregex.regex': 'x',
            'traefik.http.routers.acme-erp-custom-loja_com_br.rule': 'Host(`loja.com.br`)',
        }
        desejadas = dominios.labels_traefik(_c(), [_d('loja.com.br', www=False)])
        args = dominios.args_reconciliar('acme', atuais, desejadas)
        rm = [args[i + 1] for i, a in enumerate(args) if a == '--label-rm']
        add = [args[i + 1].split('=', 1)[0] for i, a in enumerate(args) if a == '--label-add']
        self.assertCountEqual(rm, [
            'traefik.http.routers.acme-erp-custom.rule',
            'traefik.http.routers.acme-erp-custom-www.rule',
            'traefik.http.middlewares.acme-www-redirect.redirectregex.regex',
            'traefik.http.middlewares.acme-redir-sumiu_com_br.redirectregex.regex',
        ])
        self.assertFalse(set(rm) & set(add))
        self.assertIn('traefik.http.routers.acme-erp-custom-loja_com_br.rule', add)

    def test_nao_remove_labels_de_outro_slug(self):
        args = dominios.args_reconciliar('acme', {'traefik.http.routers.outro-erp-custom.rule': 'x'}, {})
        self.assertEqual(args, [])


class StackYamlTests(TestCase):
    def test_labels_dos_dominios_no_stack_com_escape(self):
        motor = MotorProvisionamento.__new__(MotorProvisionamento)
        labels = dominios.labels_traefik(_c(), [_d('loja.com.br', principal=True), _d('velha.com.br', 'redirecionar')])
        texto = motor._gerar_stack_yaml(
            slug='acme', subdominio='acme.ararasuite.com.br', versao='0.0.100', regiao='anapolis',
            env_vars={'POSTGRES_PASSWORD': 'x'}, labels_dominios=labels,
        )
        self.assertIn('https://loja.com.br$${1}', texto)
        # linhas `- "<label>"` são strings YAML entre aspas: mesma sintaxe de escape do JSON aqui
        itens = [json.loads(m) for m in re.findall(r'^\s+- ("traefik\..*")$', texto, re.M)]
        # o compose troca $$ por $ no deploy
        lidas = dict(item.replace('$$', '$').split('=', 1) for item in itens)
        for chave, valor in labels.items():
            self.assertEqual(lidas[chave], valor)
        self.assertEqual(lidas['traefik.http.routers.acme-erp.rule'], 'Host(`acme.ararasuite.com.br`)')


class _ApiFalsa:
    tunnel_id = 'tun'
    tunnel_nome = 'tunel'
    tunnel_cname = 'tun.cfargotunnel.com'

    def __init__(self, zonas=(), registros=None, ingress=None, criar_falha=False):
        self.zonas = {z: {'id': f'z-{z}', 'name': z} for z in zonas}
        self.recs = registros or {}  # (tipo, nome) -> [rec]
        self.config = {'ingress': ingress or [{'service': 'http_status:404'}], 'outro': 1}
        self.criar_falha = criar_falha
        self.apagados, self.gravados, self.criadas = [], [], []

    def ler_ingress(self):
        return dict(self.config)

    def gravar_ingress(self, config):
        self.config = config

    def zona(self, nome):
        return self.zonas.get(nome)

    def criar_zona(self, nome):
        if self.criar_falha:
            raise cloudflare.ErroCloudflare('Cloudflare 400: root domain')
        self.criadas.append(nome)
        self.zonas[nome] = {'id': f'z-{nome}', 'name': nome, 'name_servers': ['a.ns', 'b.ns']}
        return self.zonas[nome]

    def registros(self, zona_id, tipo, nome):
        return self.recs.get((tipo, nome), [])

    def apagar_registro(self, zona_id, rid):
        self.apagados.append(rid)

    def gravar_registro(self, zona_id, payload, registro_id=None):
        self.gravados.append((zona_id, payload['name'], registro_id))


class CloudflareTests(_ComCliente):
    def test_ingress_sem_duplicatas_e_catch_all_no_fim(self):
        regras = [
            {'hostname': 'loja.com.br', 'service': 'velho'},
            {'hostname': 'outro.com', 'service': 'x'},
            {'hostname': 'sumiu.com.br', 'service': 'x'},
            {'service': 'http_status:404'},
        ]
        novo = cloudflare.novo_ingress(regras, ['loja.com.br', 'www.loja.com.br'], ['sumiu.com.br'])
        self.assertEqual([r.get('hostname') for r in novo], ['loja.com.br', 'www.loja.com.br', 'outro.com', None])
        self.assertEqual(novo[0]['service'], cloudflare.SERVICO_TRAEFIK)

    def test_subdominio_acha_a_zona_do_raiz(self):
        DominioCliente.objects.create(cliente=self.cliente, dominio='erp.loja.com.br', incluir_www=False)
        api = _ApiFalsa(zonas=['loja.com.br'])
        r = cloudflare.sincronizar(self.cliente, api=api)
        self.assertTrue(r['ok'], r)
        self.assertEqual(api.gravados, [('z-loja.com.br', 'erp.loja.com.br', None)])
        self.assertEqual(api.criadas, [])
        self.cliente.refresh_from_db()
        self.assertEqual(self.cliente.dominios_aplicados, ['erp.loja.com.br'])

    def test_sem_zona_cria_a_do_dominio(self):
        DominioCliente.objects.create(cliente=self.cliente, dominio='loja.com.br')
        api = _ApiFalsa()
        r = cloudflare.sincronizar(self.cliente, api=api)
        self.assertTrue(r['ok'])
        self.assertEqual(api.criadas, ['loja.com.br'])
        self.assertTrue(any('nameservers' in t for _, t in r['mensagens']))

    def test_subdominio_sem_zona_explica(self):
        DominioCliente.objects.create(cliente=self.cliente, dominio='erp.loja.com.br', incluir_www=False)
        r = cloudflare.sincronizar(self.cliente, api=_ApiFalsa(criar_falha=True))
        self.assertFalse(r['ok'])
        self.assertTrue(any('domínio raiz' in t for _, t in r['mensagens']))

    def test_remove_so_cname_nosso_e_guarda_pendentes(self):
        Cliente.objects.filter(pk=self.cliente.pk).update(
            dominios_aplicados=['velho.com.br', 'www.velho.com.br', 'terceiro.com.br'])
        self.cliente.refresh_from_db()
        api = _ApiFalsa(
            zonas=['velho.com.br', 'terceiro.com.br'],
            registros={
                ('CNAME', 'velho.com.br'): [{'id': 'r1', 'content': 'tun.cfargotunnel.com'}],
                ('CNAME', 'www.velho.com.br'): [{'id': 'r2', 'content': 'tun.cfargotunnel.com'}],
                ('CNAME', 'terceiro.com.br'): [{'id': 'r3', 'content': 'site.vercel.app'}],
            },
            ingress=[{'hostname': 'velho.com.br', 'service': 'x'}, {'service': 'http_status:404'}],
        )
        r = cloudflare.sincronizar(self.cliente, api=api)
        self.assertTrue(r['ok'], r)
        self.assertCountEqual(api.apagados, ['r1', 'r2'])
        self.assertTrue(any('vercel' in t for _, t in r['mensagens']))
        self.assertEqual(api.config['ingress'], [{'service': 'http_status:404'}])
        self.cliente.refresh_from_db()
        self.assertEqual(self.cliente.dominios_aplicados, [])

    def test_falha_geral_mantem_removidos_para_a_proxima(self):
        Cliente.objects.filter(pk=self.cliente.pk).update(dominios_aplicados=['velho.com.br'])
        self.cliente.refresh_from_db()
        DominioCliente.objects.create(cliente=self.cliente, dominio='loja.com.br', incluir_www=False)
        with patch.object(cloudflare.Api, 'da_configuracao', side_effect=cloudflare.ErroCloudflare('sem token')):
            r = cloudflare.sincronizar(self.cliente)
        self.assertFalse(r['ok'])
        self.assertEqual(r['erro'], 'sem token')
        self.cliente.refresh_from_db()
        self.assertEqual(self.cliente.dominios_aplicados, ['loja.com.br', 'velho.com.br'])

    def test_avisa_hostname_que_o_certificado_universal_nao_cobre(self):
        DominioCliente.objects.create(cliente=self.cliente, dominio='erp.loja.com.br', incluir_www=True)
        r = cloudflare.sincronizar(self.cliente, api=_ApiFalsa(zonas=['loja.com.br']))
        avisos = [t for n, t in r['mensagens'] if n == 'warning']
        self.assertEqual(len(avisos), 1)
        self.assertIn('www.erp.loja.com.br', avisos[0])

    def test_apex_e_www_nao_geram_aviso_de_certificado(self):
        DominioCliente.objects.create(cliente=self.cliente, dominio='loja.com.br')
        r = cloudflare.sincronizar(self.cliente, api=_ApiFalsa(zonas=['loja.com.br']))
        self.assertEqual([t for n, t in r['mensagens'] if n == 'warning'], [])

    def test_nao_remove_hostname_que_agora_e_de_outro_cliente(self):
        Cliente.objects.filter(pk=self.cliente.pk).update(dominios_aplicados=['loja.com.br'])
        self.cliente.refresh_from_db()
        DominioCliente.objects.create(cliente=self._novo('beta'), dominio='loja.com.br', incluir_www=False)
        api = _ApiFalsa(zonas=['loja.com.br'], registros={
            ('CNAME', 'loja.com.br'): [{'id': 'r1', 'content': 'tun.cfargotunnel.com'}]})
        cloudflare.sincronizar(self.cliente, api=api)
        self.assertEqual(api.apagados, [])


class MigrationDadosTests(_ComCliente):
    def test_dominio_custom_vira_principal(self):
        mig = importlib.import_module('registry.migrations.0035_dominios_cliente')

        class _ClienteAntigo:
            objects = SimpleNamespace(exclude=lambda **kw: SimpleNamespace(iterator=lambda: [
                SimpleNamespace(pk=self.cliente.pk, dominio_custom='Loja.com.br'),
            ]), filter=Cliente.objects.filter)

        class _Apps:
            def get_model(_, app, nome):
                return _ClienteAntigo if nome == 'Cliente' else django_apps.get_model(app, nome)

        mig._copiar_para_dominios(_Apps(), None)
        d = DominioCliente.objects.get(cliente=self.cliente)
        self.assertEqual((d.dominio, d.modo, d.principal, d.incluir_www), ('loja.com.br', 'servir', True, True))
        self.cliente.refresh_from_db()
        self.assertEqual(self.cliente.dominios_aplicados, ['loja.com.br', 'www.loja.com.br'])


class AdminDominiosTests(_ComCliente):
    def setUp(self):
        super().setUp()
        self.user = get_user_model().objects.create_superuser('dom_admin', 'd@x.invalid', 'SenhaSegura123!')
        self.c = Client()
        self.c.force_login(self.user)

    @patch('registry.cloudflare.sincronizar', return_value={'ok': True, 'mensagens': [('success', 'cf ok')], 'erro': ''})
    @patch('registry.provisioning.MotorProvisionamento.sincronizar_servico_cron')
    @patch('registry.swarm.labels_do_servico', return_value={
        'traefik.http.routers.acme-erp-custom.rule': 'Host(`velho.com.br`)'})
    @patch('subprocess.run')
    def test_aplicar_configuracoes_reconcilia(self, run, _labels, _cron, cf):
        run.return_value = SimpleNamespace(returncode=0, stdout='', stderr='')
        DominioCliente.objects.create(cliente=self.cliente, dominio='loja.com.br', principal=True)
        DominioCliente.objects.create(cliente=self.cliente, dominio='antiga.com.br', modo='redirecionar')
        r = self.c.get(reverse('admin:registry_cliente_aplicar_modulos', args=[self.cliente.pk]), follow=True)
        cmd = run.call_args[0][0]
        self.assertIn('ALLOWED_HOSTS=acme.ararasuite.com.br,loja.com.br', cmd)
        self.assertIn('--label-rm', cmd)
        self.assertIn('traefik.http.routers.acme-erp-custom.rule', cmd)
        self.assertIn('traefik.http.routers.acme-erp-custom-antiga_com_br.middlewares=acme-redir-loja_com_br', cmd)
        self.assertEqual(cmd[-1], 'acme_web')
        cf.assert_called_once()
        self.assertContains(r, 'cf ok')

    def test_inline_somente_leitura_com_migracao_ativa(self):
        from django.contrib.admin.sites import site
        from registry.admin import DominioClienteInline
        inline = DominioClienteInline(Cliente, site)
        req = SimpleNamespace(user=self.user)
        self.assertTrue(inline.has_add_permission(req, self.cliente))
        with patch('registry.admin._migracao_ativa', return_value=True):
            self.assertFalse(inline.has_add_permission(req, self.cliente))
            self.assertFalse(inline.has_change_permission(req, self.cliente))
            self.assertFalse(inline.has_delete_permission(req, self.cliente))

    def _post_dominios(self, linhas):
        url = reverse('admin:registry_cliente_change', args=[self.cliente.pk])
        html = self.c.get(url)
        form = html.context['adminform'].form
        data = {}
        for nome, campo in form.fields.items():
            valor = form.initial.get(nome, campo.initial)
            if valor is None:
                continue
            if isinstance(valor, (list, tuple)) or hasattr(valor, 'all'):
                data[nome] = [getattr(v, 'pk', v) for v in (valor.all() if hasattr(valor, 'all') else valor)]
            elif hasattr(valor, 'pk'):
                data[nome] = valor.pk
            elif isinstance(valor, bool):
                if valor:
                    data[nome] = 'on'
            else:
                data[nome] = valor
        for fs in html.context['inline_admin_formsets']:
            p = fs.formset.prefix
            linhas_fs = linhas if fs.formset.model is DominioCliente else []
            data.update({f'{p}-TOTAL_FORMS': str(len(linhas_fs) if linhas_fs else fs.formset.total_form_count()),
                         f'{p}-INITIAL_FORMS': '0' if linhas_fs else str(fs.formset.initial_form_count()),
                         f'{p}-MIN_NUM_FORMS': '0', f'{p}-MAX_NUM_FORMS': '1000'})
            if not linhas_fs:
                for i, f in enumerate(fs.formset.forms):
                    if f.instance.pk:
                        data[f'{p}-{i}-id'] = f.instance.pk
                        data[f'{p}-{i}-cliente'] = self.cliente.pk
            for i, linha in enumerate(linhas_fs):
                for k, v in linha.items():
                    data[f'{p}-{i}-{k}'] = v
        return self.c.post(url, data)

    def test_formset_recusa_dois_principais(self):
        r = self._post_dominios([
            {'dominio': 'a.com.br', 'modo': 'servir', 'principal': 'on'},
            {'dominio': 'b.com.br', 'modo': 'servir', 'principal': 'on'},
        ])
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'no máximo um domínio como principal')
        self.assertFalse(DominioCliente.objects.exists())

    def test_salvar_dominios_avisa_para_aplicar(self):
        r = self._post_dominios([{'dominio': 'https://Loja.com.br/', 'modo': 'servir', 'principal': 'on', 'incluir_www': 'on'}])
        self.assertEqual(r.status_code, 302, getattr(r, 'context', None) and r.context['errors'])
        self.assertEqual(DominioCliente.objects.get().dominio, 'loja.com.br')
        r = self.c.get(r['Location'])
        self.assertContains(r, 'Aplicar Configurações para')


class TrocaDePrincipalTests(_ComCliente):
    """Trocar o principal num salvamento só (o full_clean não pode olhar o principal antigo no banco)."""

    def setUp(self):
        super().setUp()
        self.user = get_user_model().objects.create_superuser('dom_troca', 't@x.invalid', 'SenhaSegura123!')
        self.c = Client()
        self.c.force_login(self.user)

    def _post(self, existentes, novas=()):
        """`existentes`: {pk: {'principal': bool, 'DELETE': bool}}; `novas`: linhas novas."""
        url = reverse('admin:registry_cliente_change', args=[self.cliente.pk])
        html = self.c.get(url)
        form = html.context['adminform'].form
        data = {}
        for nome, campo in form.fields.items():
            valor = form.initial.get(nome, campo.initial)
            if valor is None:
                continue
            if hasattr(valor, 'all') or isinstance(valor, (list, tuple)):
                data[nome] = [getattr(v, 'pk', v) for v in (valor.all() if hasattr(valor, 'all') else valor)]
            elif hasattr(valor, 'pk'):
                data[nome] = valor.pk
            elif isinstance(valor, bool):
                if valor:
                    data[nome] = 'on'
            else:
                data[nome] = valor
        for f in html.context['inline_admin_formsets']:
            fs, p = f.formset, f.formset.prefix
            extras = list(novas) if fs.model is DominioCliente else []
            data.update({f'{p}-TOTAL_FORMS': str(fs.initial_form_count() + len(extras)),
                         f'{p}-INITIAL_FORMS': str(fs.initial_form_count()),
                         f'{p}-MIN_NUM_FORMS': '0', f'{p}-MAX_NUM_FORMS': '1000'})
            for i, x in enumerate(fs.forms[:fs.initial_form_count()]):
                data[f'{p}-{i}-id'] = x.instance.pk
                data[f'{p}-{i}-cliente'] = self.cliente.pk
                if fs.model is DominioCliente:
                    alvo = existentes.get(x.instance.pk, {})
                    data[f'{p}-{i}-dominio'] = x.instance.dominio
                    data[f'{p}-{i}-modo'] = x.instance.modo
                    if alvo.get('principal', x.instance.principal):
                        data[f'{p}-{i}-principal'] = 'on'
                    if alvo.get('DELETE'):
                        data[f'{p}-{i}-DELETE'] = 'on'
            for k, linha in enumerate(extras, start=fs.initial_form_count()):
                for campo, valor in linha.items():
                    data[f'{p}-{k}-{campo}'] = valor
        return self.c.post(url, data)

    def _estado(self):
        return sorted(DominioCliente.objects.values_list('dominio', 'principal'))

    def test_troca_entre_existentes(self):
        a = DominioCliente.objects.create(cliente=self.cliente, dominio='a.com.br', principal=True)
        b = DominioCliente.objects.create(cliente=self.cliente, dominio='b.com.br')
        r = self._post({a.pk: {'principal': False}, b.pk: {'principal': True}})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self._estado(), [('a.com.br', False), ('b.com.br', True)])

    def test_principal_passa_para_linha_nova(self):
        a = DominioCliente.objects.create(cliente=self.cliente, dominio='a.com.br', principal=True)
        r = self._post({a.pk: {'principal': False}}, [{'dominio': 'c.com.br', 'modo': 'servir', 'principal': 'on'}])
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self._estado(), [('a.com.br', False), ('c.com.br', True)])

    def test_apaga_o_principal_e_marca_outro(self):
        a = DominioCliente.objects.create(cliente=self.cliente, dominio='a.com.br', principal=True)
        b = DominioCliente.objects.create(cliente=self.cliente, dominio='b.com.br')
        r = self._post({a.pk: {'DELETE': True}, b.pk: {'principal': True}})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self._estado(), [('b.com.br', True)])

    def test_dois_principais_entre_existentes_continua_recusado(self):
        a = DominioCliente.objects.create(cliente=self.cliente, dominio='a.com.br', principal=True)
        b = DominioCliente.objects.create(cliente=self.cliente, dominio='b.com.br')
        r = self._post({a.pk: {'principal': True}, b.pk: {'principal': True}})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self._estado(), [('a.com.br', True), ('b.com.br', False)])


class AjudaDominiosTests(TestCase):
    def test_secao_de_dominios_renderiza(self):
        u = get_user_model().objects.create_superuser('ajuda_dom', 'a@x.invalid', 'SenhaSegura123!')
        self.client.force_login(u)
        html = self.client.get('/admin/ajuda/').content.decode()
        trecho = html[html.index('Domínios próprios</p>'):html.index('Cloudflare — configuração do CP</p>')]
        self.assertIn('{{json .Spec.Labels}}', trecho)
        for texto in ('Exemplos práticos', 'Como conferir se está no ar', 'SyncAgent/PDV', 'certificado universal'):
            self.assertIn(texto, trecho)
