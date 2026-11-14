"""Aba Domínios do Cliente no navegador: inline, validação, aviso de aplicar e Aplicar Configurações."""
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.urls import reverse

from registry.models import Cliente, DominioCliente, Plano
from registry.tests.playwright_base import PlaywrightAdminTestCase


class DominiosPlaywrightTests(PlaywrightAdminTestCase):
    def setUp(self):
        patch('registry.versoes.tag_em_execucao', return_value='').start()
        self.addCleanup(patch.stopall)
        self.user = get_user_model().objects.create_superuser('admin_dom_pw', 'dom@example.com', 'SenhaSegura123!')
        plano = Plano.objects.create(slug='basico-dom-pw', nome='Basico', preco_mensal='0.00')
        self.cliente = Cliente.objects.create(
            slug='pw-dom', nome='Cliente Dominios', cnpj='11.222.333/0001-77', email_contato='d@example.com',
            subdominio='pw-dom.ararasuite.com.br', plano=plano, status='ativo', versao_erp='0.0.148',
        )
        self.url = self.live_server_url + reverse('admin:registry_cliente_change', args=[self.cliente.pk])
        self.autenticar_admin(self.user)
        # a página é da classe inteira: não herdar a aba lembrada do teste anterior
        self.page.goto(self.url)
        self.page.evaluate('sessionStorage.clear()')

    def _aba(self, nome):
        """Inline (barra #tabs-items) ou fieldset (dentro de General)."""
        inline = self.page.locator('#tabs-items a', has_text=nome)
        if inline.count():
            inline.first.click()
            return
        self.page.locator('#tabs-items a[href="#general"]').click()
        self.page.locator('[x-data*="activeFieldsetTab"] nav a', has_text=nome).first.click()

    def _abrir(self, aba='Domínios'):
        self.page.goto(self.url)
        self.page.wait_for_load_state('load')
        self.page.wait_for_timeout(300)  # abas_cliente.js restaura a aba lembrada após 150 ms
        self._aba(aba)

    def _nova_linha(self, i, dominio, modo='servir', principal=False, www=True):
        grupo = self.page.locator('#dominios-group')
        grupo.locator('.add-row a, a.add-row, [class*="add-row"] a').first.click()
        self.page.locator(f'#id_dominios-{i}-dominio').fill(dominio)
        self.page.locator(f'#id_dominios-{i}-modo').select_option(modo)
        self.page.locator(f'#id_dominios-{i}-principal').set_checked(principal)
        self.page.locator(f'#id_dominios-{i}-incluir_www').set_checked(www)

    def _salvar(self):
        self.page.locator('[name="_continue"]').first.click()
        self.page.wait_for_load_state('load')

    def test_aba_dominios_existe_e_lembra_depois_de_salvar(self):
        self._abrir()
        self._nova_linha(0, 'https://Loja.com.br/painel', principal=True)
        self._nova_linha(1, 'antiga.com.br', modo='redirecionar')
        self._salvar()
        self.page.get_by_text('Clique em ⚙ Aplicar Configurações para').first.wait_for()
        self.assertEqual(
            sorted(DominioCliente.objects.values_list('dominio', 'modo', 'principal')),
            [('antiga.com.br', 'redirecionar', False), ('loja.com.br', 'servir', True)],
        )
        # depois do reload a aba Domínios continua aberta (abas_cliente.js) com o valor normalizado
        campo = self.page.locator('#id_dominios-0-dominio')
        campo.wait_for(state='visible')
        self.assertEqual(campo.input_value(), 'loja.com.br')

    def test_erro_no_inline_abre_a_aba_mesmo_lembrando_outra(self):
        self._abrir()
        self._nova_linha(0, 'outro.ararasuite.com.br')
        self.page.evaluate("sessionStorage.setItem('cliente-aba-inline', '#logs')")
        self._salvar()
        erro = self.page.get_by_text('Domínios da AraraSuite não entram aqui').first
        erro.wait_for(state='visible')

    def test_aba_inline_lembrada_sem_erro_abre_ela_e_nao_a_de_dominios(self):
        # sem erro de validação, nada na aba Domínios pode ser confundido com erro (falso positivo)
        DominioCliente.objects.create(cliente=self.cliente, dominio='loja.com.br', principal=True)
        self._abrir('Logs de Provisionamento')
        self.page.goto(self.url)
        self.page.wait_for_load_state('load')
        self.page.wait_for_timeout(400)
        self.assertIn('active', self.page.locator('#tabs-items a[href="#logs"]').get_attribute('class') or '')
        self.assertFalse(self.page.locator('#id_dominios-0-dominio').is_visible())

    def test_dois_principais_mostra_erro_na_tela_e_nao_salva(self):
        self._abrir()
        self._nova_linha(0, 'a.com.br', principal=True)
        self._nova_linha(1, 'b.com.br', principal=True)
        self._salvar()
        self.page.get_by_text('Marque no máximo um domínio como principal').first.wait_for(state='visible')
        self.assertFalse(DominioCliente.objects.exists())

    def test_dominio_da_plataforma_mostra_erro_no_campo(self):
        self._abrir()
        self._nova_linha(0, 'outro.ararasuite.com.br')
        self._salvar()
        self.page.get_by_text('Domínios da AraraSuite não entram aqui').first.wait_for(state='visible')
        self.assertFalse(DominioCliente.objects.exists())

    def test_painel_de_acesso_lista_os_enderecos(self):
        DominioCliente.objects.create(cliente=self.cliente, dominio='loja.com.br', principal=True)
        DominioCliente.objects.create(cliente=self.cliente, dominio='antiga.com.br', modo='redirecionar', incluir_www=False)
        self._abrir('Acesso e Infraestrutura')
        self.page.locator('a[href="https://loja.com.br"]').first.wait_for(state='visible')
        texto = self.page.locator('body').inner_text()
        self.assertIn('principal — abre o ERP', texto)
        self.assertIn('redireciona para loja.com.br', texto)
        self.assertIn('www.loja.com.br', texto)
        self.assertNotIn('www.antiga.com.br', texto)

    def test_botao_aplicar_configuracoes_publica_os_dominios(self):
        DominioCliente.objects.create(cliente=self.cliente, dominio='loja.com.br', principal=True)
        run = patch('subprocess.run', return_value=SimpleNamespace(returncode=0, stdout='', stderr='')).start()
        patch('registry.swarm.labels_do_servico', return_value={}).start()
        patch('registry.provisioning.MotorProvisionamento.sincronizar_servico_cron').start()
        cf = patch('registry.cloudflare.sincronizar', return_value={
            'ok': True, 'mensagens': [('success', 'Cloudflare: loja.com.br criado')], 'erro': ''}).start()

        def aceitar(dialogo):
            dialogo.accept()
        self.page.on('dialog', aceitar)
        self.addCleanup(self.page.remove_listener, 'dialog', aceitar)

        self._abrir('Status e Ações')
        self.page.locator('a[href$="/aplicar-modulos/"]').first.click()
        self.page.wait_for_load_state('load')
        self.page.get_by_text('Cloudflare: loja.com.br criado').first.wait_for()
        self.page.get_by_text('domínios=loja.com.br (+ pw-dom.ararasuite.com.br)').first.wait_for()
        cmd = run.call_args[0][0]
        self.assertIn('ALLOWED_HOSTS=pw-dom.ararasuite.com.br,loja.com.br', cmd)
        self.assertIn('traefik.http.routers.pw-dom-erp-custom-loja_com_br.rule=Host(`loja.com.br`)', cmd)
        cf.assert_called_once()

    def test_migracao_ativa_deixa_a_aba_somente_leitura(self):
        DominioCliente.objects.create(cliente=self.cliente, dominio='loja.com.br', principal=True)
        patch('registry.admin._migracao_ativa', return_value=True).start()
        self._abrir()
        self.page.locator('#dominios-group').get_by_text('loja.com.br').first.wait_for(state='visible')
        self.assertEqual(self.page.locator('#id_dominios-0-dominio').count(), 0)
        self.assertFalse(self.page.locator('#dominios-group').get_by_text('Adicionar outro(a)').is_visible())
        self.assertEqual(self.page.locator('#dominios-group input:not([type=hidden]), #dominios-group select').count(), 0)
