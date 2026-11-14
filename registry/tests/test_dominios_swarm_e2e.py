"""E2E opcional: "Aplicar Configurações" contra um Swarm real (CP_E2E_SWARM=1).

Pré-requisito: Swarm local com o serviço `acme_web` criado (ver docs/registry/dominios-multiplos.md,
seção "Teste E2E com Swarm local"). O Cloudflare é simulado; o `docker service update` é real.
"""
import json
import os
import subprocess
import time
import unittest
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse

from registry.models import Cliente, DominioCliente, Plano


def _labels():
    r = subprocess.run(['docker', 'service', 'inspect', 'acme_web', '--format', '{{json .Spec.Labels}}'],
                       capture_output=True, text=True, check=True)
    return json.loads(r.stdout)


def _aguardar_update(timeout=90):
    fim = time.time() + timeout
    while time.time() < fim:
        r = subprocess.run(['docker', 'service', 'inspect', 'acme_web', '--format', '{{json .UpdateStatus}}'],
                           capture_output=True, text=True)
        estado = (json.loads(r.stdout or 'null') or {}).get('State')
        if estado in (None, 'completed'):
            return
        time.sleep(1)
    raise AssertionError('acme_web não terminou o update')


@unittest.skipUnless(os.getenv('CP_E2E_SWARM') == '1', 'E2E de Swarm desligado (CP_E2E_SWARM=1)')
class AplicarConfiguracoesSwarmE2E(TestCase):
    def setUp(self):
        patch('registry.provisioning.MotorProvisionamento.sincronizar_servico_cron').start()
        self.cf = patch('registry.cloudflare.sincronizar',
                        return_value={'ok': True, 'mensagens': [], 'erro': ''}).start()
        self.addCleanup(patch.stopall)
        user = get_user_model().objects.create_superuser('e2e', 'e2e@x.invalid', 'SenhaSegura123!')
        self.c = Client()
        self.c.force_login(user)
        plano = Plano.objects.create(slug='p-e2e', nome='P', preco_mensal='0.00')
        self.cliente = Cliente.objects.create(
            slug='acme', nome='Acme', cnpj='00.000.999/0001-00', email_contato='a@x.invalid',
            subdominio='acme.ararasuite.com.br', plano=plano, status='ativo',
        )

    def _aplicar(self):
        r = self.c.get(reverse('admin:registry_cliente_aplicar_modulos', args=[self.cliente.pk]), follow=True)
        msgs = [str(m) for m in r.context['messages']]
        self.assertTrue(any('Configurações aplicadas' in m for m in msgs), msgs)
        _aguardar_update()
        return _labels()

    def test_reconcilia_legado_adiciona_e_remove(self):
        DominioCliente.objects.create(cliente=self.cliente, dominio='loja.com.br', principal=True)
        DominioCliente.objects.create(cliente=self.cliente, dominio='antiga.com.br', modo='redirecionar')
        lb = self._aplicar()
        self.assertFalse([k for k in lb if k.startswith('traefik.http.routers.acme-erp-custom.')], lb)
        self.assertFalse([k for k in lb if 'acme-www-redirect' in k], lb)
        self.assertNotIn('traefik.http.routers.acme-erp-custom-www.rule', lb)
        self.assertEqual(lb['traefik.http.routers.acme-erp-custom-loja_com_br.rule'], 'Host(`loja.com.br`)')
        self.assertEqual(lb['traefik.http.routers.acme-erp-custom-antiga_com_br.middlewares'], 'acme-redir-loja_com_br')
        self.assertEqual(lb['traefik.http.routers.acme-erp.rule'], 'Host(`acme.ararasuite.com.br`)')
        self.assertEqual(lb['traefik.enable'], 'true')
        self.assertEqual(lb['traefik.http.services.acme-erp.loadbalancer.server.port'], '80')

        # tira o antiga e aplica de novo: os routers dele somem, o resto fica
        DominioCliente.objects.filter(dominio='antiga.com.br').delete()
        lb = self._aplicar()
        self.assertFalse([k for k in lb if 'antiga_com_br' in k], lb)
        self.assertIn('traefik.http.routers.acme-erp-custom-loja_com_br.rule', lb)
        self.assertIn('traefik.http.middlewares.acme-redir-loja_com_br.redirectregex.replacement', lb)
        env = subprocess.run(['docker', 'service', 'inspect', 'acme_web', '--format',
                              '{{json .Spec.TaskTemplate.ContainerSpec.Env}}'], capture_output=True, text=True).stdout
        self.assertIn('ALLOWED_HOSTS=acme.ararasuite.com.br,loja.com.br', env)
        self.assertIn('CSRF_TRUSTED_ORIGINS=https://acme.ararasuite.com.br,https://loja.com.br', env)
