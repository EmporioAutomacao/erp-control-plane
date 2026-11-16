"""M2b: serviço <slug>_mcp (Servidor MCP do ERP) por cliente. Sem Swarm: o Docker é simulado."""
import json
from types import SimpleNamespace
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase

from registry import dominios, mcp
from registry.models import Cliente, DominioCliente, Modulo, Plano
from registry.provisioning import MotorProvisionamento

SEGREDOS_WEB = ('POSTGRES_PASSWORD', 'DJANGO_SECRET_KEY', 'CP_SHARED_SECRET', 'EMPRESAS_CREDENCIAL_MASTER_KEY')

SPEC_WEB = {
    'Labels': {'traefik.enable': 'true'},
    'TaskTemplate': {
        'ContainerSpec': {
            'Image': 'emporioautomacao/ararasuite-erp:0.0.157@sha256:abc',
            'Env': ['POSTGRES_PASSWORD=pg-segredo', 'DJANGO_SECRET_KEY=dj-segredo', 'CP_SHARED_SECRET=cp-segredo'],
            'Mounts': [{'Type': 'volume', 'Source': 'acme_media', 'Target': '/app/media'}],
        },
        'Placement': {'Constraints': ['node.labels.region == anapolis']},
    },
}


class _Base(TestCase):
    def setUp(self):
        self.plano = Plano.objects.create(slug='p-mcp', nome='P', preco_mensal='0.00')
        self.agents, _ = Modulo.objects.get_or_create(slug='agents', defaults={'nome': 'Agentes', 'preco_mensal': 0})
        with patch('registry.tasks.task_provisionar_cliente.delay'):
            self.cliente = Cliente.objects.create(
                slug='acme', nome='Acme', cnpj='12.345.678/0001-90', email_contato='a@x.invalid',
                subdominio='acme.ararasuite.com.br', plano=self.plano, status='ativo', versao_erp='0.0.157',
            )
        patch('registry.provisioning.docker.from_env').start()
        patch('registry.versoes.exigir_sem_downgrade').start()
        self.addCleanup(patch.stopall)

    def contratar(self):
        with patch('registry.signals.modulos_ativos_changed'):
            self.cliente.modulos_ativos.add(self.agents)

    def dominio(self, nome, modo='servir', principal=False, www=True):
        return DominioCliente.objects.create(
            cliente=self.cliente, dominio=nome, modo=modo, principal=principal, incluir_www=www,
        )


class ReplicasTests(_Base):
    def test_sem_modulo_fica_em_zero(self):
        self.assertEqual(mcp.replicas_desejadas(self.cliente), 0)

    def test_com_modulo_e_no_ar_fica_em_um(self):
        self.contratar()
        for status in ('ativo', 'trial', 'atualizando', 'aguardando_provisao'):
            with self.subTest(status=status):
                self.cliente.status = status
                self.assertEqual(mcp.replicas_desejadas(self.cliente), 1)

    def test_fora_do_ar_fica_em_zero_mesmo_com_modulo(self):
        self.contratar()
        for status in Cliente.STATUS_FORA_DO_AR:
            with self.subTest(status=status):
                self.cliente.status = status
                self.assertEqual(mcp.replicas_desejadas(self.cliente), 0)

    def test_modulo_fora_de_qualquer_plano_da_fixture(self):
        # carregar_dados_iniciais põe módulo novo nos planos que o listam: o MCP ligaria sozinho.
        call_command('carregar_dados_iniciais')
        self.assertTrue(Modulo.objects.filter(slug='agents').exists())
        self.assertFalse(Plano.objects.filter(modulos_inclusos__slug='agents').exists())


class EnvELabelsTests(_Base):
    def test_env_sem_nenhum_segredo_do_web(self):
        env = mcp.env(self.cliente, '0.0.157')
        self.assertEqual(env, {
            'ERP_INTERNAL_URL': 'http://web:8000',
            'MCP_PUBLIC_URL': 'https://acme.ararasuite.com.br/mcp',
            'MCP_ALLOWED_HOSTS': 'acme.ararasuite.com.br',
            'MCP_PORT': '8001',
            'ERP_VERSION': '0.0.157',
        })
        for segredo in SEGREDOS_WEB:
            self.assertNotIn(segredo, env)

    def test_hosts_so_os_que_servem_sem_www_nem_redirect_nem_web(self):
        self.dominio('loja.com.br', principal=True)
        self.dominio('antiga.com.br', modo='redirecionar')
        lista = dominios.do_cliente(self.cliente)
        self.assertEqual(mcp.env(self.cliente, '1', lista)['MCP_ALLOWED_HOSTS'], 'acme.ararasuite.com.br,loja.com.br')

        labels = mcp.labels_traefik(self.cliente, lista)
        regras = {k: v for k, v in labels.items() if k.endswith('.rule')}
        self.assertEqual(regras, {
            'traefik.http.routers.acme-mcp.rule': 'Host(`acme.ararasuite.com.br`) && PathPrefix(`/mcp`)',
            'traefik.http.routers.acme-mcp-custom-loja_com_br.rule': 'Host(`loja.com.br`) && PathPrefix(`/mcp`)',
        })
        self.assertFalse([k for k in labels if 'antiga' in k or 'www' in k], labels)

    def test_labels_service_porta_e_limite_pelo_ip_do_cloudflare(self):
        labels = mcp.labels_traefik(self.cliente)
        self.assertEqual(labels['traefik.enable'], 'true')
        self.assertEqual(labels['traefik.http.services.acme-mcp.loadbalancer.server.port'], '8001')
        self.assertEqual(labels['traefik.http.routers.acme-mcp.service'], 'acme-mcp')
        self.assertEqual(labels['traefik.http.routers.acme-mcp.entrypoints'], 'web')
        self.assertEqual(labels['traefik.http.routers.acme-mcp.middlewares'], 'acme-mcp-ratelimit')
        self.assertEqual(
            labels['traefik.http.middlewares.acme-mcp-ratelimit.ratelimit.sourcecriterion.requestheadername'],
            'CF-Connecting-IP',
        )
        # Nunca reaproveita o service do web.
        self.assertFalse([k for k in labels if 'acme-erp' in k], labels)

    def test_reconciliar_remove_router_de_dominio_que_saiu_e_nao_toca_no_resto(self):
        atuais = {
            'traefik.http.routers.acme-mcp-custom-velho_com_br.rule': 'Host(`velho.com.br`) && PathPrefix(`/mcp`)',
            'com.docker.stack.namespace': 'acme',
            'traefik.http.routers.acme-erp.rule': 'Host(`acme.ararasuite.com.br`)',
        }
        args = mcp.args_reconciliar('acme', atuais, mcp.labels_traefik(self.cliente))
        removidas = [args[i + 1] for i, a in enumerate(args) if a == '--label-rm']
        self.assertEqual(removidas, ['traefik.http.routers.acme-mcp-custom-velho_com_br.rule'])

    def test_allowed_hosts_do_web_aceita_a_chamada_interna(self):
        self.assertIn('web', dominios.allowed_hosts(self.cliente).split(','))
        self.assertNotIn('web', mcp.env(self.cliente, '1')['MCP_ALLOWED_HOSTS'].split(','))


class StackTests(_Base):
    def _stack(self, **kw):
        motor = MotorProvisionamento(self.cliente)
        return motor._gerar_stack_yaml(
            'acme', 'acme.ararasuite.com.br', '0.0.157', 'anapolis',
            {'POSTGRES_PASSWORD': 'pg-segredo', 'DJANGO_SECRET_KEY': 'dj-segredo', 'CP_SHARED_SECRET': 'cp-segredo'},
            servico_mcp=motor.spec_mcp('0.0.157'), **kw,
        )

    @staticmethod
    def _bloco(stack):
        return stack.split('\n  mcp:\n')[1].split('\n  db:')[0]

    def test_bloco_mcp_sem_segredos_sem_volumes_e_com_entrypoint_proprio(self):
        self.contratar()
        bloco = self._bloco(self._stack())
        self.assertIn('image: emporioautomacao/ararasuite-erp:0.0.157', bloco)
        self.assertIn('entrypoint: ["python", "-m", "mcp_erp"]', bloco)
        self.assertIn('ERP_INTERNAL_URL: "http://web:8000"', bloco)
        self.assertIn('replicas: 1', bloco)
        self.assertIn('node.labels.region == anapolis', bloco)
        self.assertIn('"traefik.enable=true"', bloco)
        self.assertIn('Host(`acme.ararasuite.com.br`) && PathPrefix(`/mcp`)', bloco)
        self.assertNotIn('volumes', bloco)
        for segredo in ('pg-segredo', 'dj-segredo', 'cp-segredo', *SEGREDOS_WEB):
            self.assertNotIn(segredo, bloco)

    def test_sem_modulo_nasce_em_zero(self):
        self.assertIn('replicas: 0', self._bloco(self._stack()))

    def test_modo_migracao_parado_e_fora_do_traefik(self):
        self.contratar()
        bloco = self._bloco(self._stack(replicas_app=0, traefik=False))
        self.assertIn('replicas: 0', bloco)
        self.assertIn('"traefik.enable=false"', bloco)
        self.assertNotIn('"traefik.enable=true"', bloco)


class SincronizarServicoTests(_Base):
    def _rodar(self, servicos, **kw):
        chamadas = []

        def run(cmd, **kwargs):
            chamadas.append(cmd)
            if cmd[:3] == ['docker', 'service', 'inspect']:
                spec = servicos.get(cmd[-1])
                if spec is None:
                    return SimpleNamespace(returncode=1, stdout='', stderr='no such service')
                return SimpleNamespace(returncode=0, stdout=json.dumps(spec), stderr='')
            return SimpleNamespace(returncode=0, stdout='', stderr='')

        with patch('registry.provisioning.subprocess.run', side_effect=run):
            MotorProvisionamento(self.cliente).sincronizar_servico_mcp(**kw)
        return chamadas[-1]

    def test_cria_sem_env_nem_volumes_do_web(self):
        self.contratar()
        cmd = self._rodar({'acme_web': SPEC_WEB}, versao='0.0.158')
        self.assertEqual(cmd[:3], ['docker', 'service', 'create'])
        self.assertEqual(cmd[-1], 'emporioautomacao/ararasuite-erp:0.0.158')
        self.assertIn('acme_mcp', cmd)
        self.assertEqual(cmd[cmd.index('--replicas') + 1], '1')
        self.assertIn('python -m mcp_erp', cmd)
        self.assertIn('acme_internal', cmd)
        self.assertIn('traefik-public', cmd)
        self.assertIn('node.labels.region == anapolis', cmd)
        self.assertIn('ERP_VERSION=0.0.158', cmd)
        self.assertIn('traefik.http.services.acme-mcp.loadbalancer.server.port=8001', cmd)
        self.assertNotIn('--mount', cmd)
        texto = ' '.join(cmd)
        for segredo in ('pg-segredo', 'dj-segredo', 'cp-segredo', *SEGREDOS_WEB):
            self.assertNotIn(segredo, texto)

    def test_cliente_sem_modulo_cria_em_zero(self):
        cmd = self._rodar({'acme_web': SPEC_WEB})
        self.assertEqual(cmd[cmd.index('--replicas') + 1], '0')

    def test_atualiza_imagem_env_replicas_e_remove_router_que_saiu(self):
        existente = {'Labels': {
            'traefik.http.routers.acme-mcp-custom-velho_com_br.rule': 'Host(`velho.com.br`) && PathPrefix(`/mcp`)',
        }}
        cmd = self._rodar({'acme_web': SPEC_WEB, 'acme_mcp': existente})
        self.assertEqual(cmd[:3], ['docker', 'service', 'update'])
        self.assertEqual(cmd[-1], 'acme_mcp')
        # Sem versão informada usa a imagem do web, sem o digest.
        self.assertIn('emporioautomacao/ararasuite-erp:0.0.157', cmd)
        self.assertIn('ERP_VERSION=0.0.157', cmd)
        self.assertEqual(cmd[cmd.index('--replicas') + 1], '0')
        self.assertIn('traefik.http.routers.acme-mcp-custom-velho_com_br.rule', cmd)
        self.assertIn('--label-rm', cmd)
        self.assertNotIn('pg-segredo', ' '.join(cmd))

    def test_traefik_segue_o_do_web(self):
        web_fora = {**SPEC_WEB, 'Labels': {'traefik.enable': 'false'}}
        cmd = self._rodar({'acme_web': web_fora})
        self.assertIn('traefik.enable=false', cmd)

    def test_falha_do_docker_vira_erro_com_stderr(self):
        def run(cmd, **kwargs):
            if cmd[:3] == ['docker', 'service', 'inspect'] and cmd[-1] == 'acme_web':
                return SimpleNamespace(returncode=0, stdout=json.dumps(SPEC_WEB), stderr='')
            if cmd[:3] == ['docker', 'service', 'inspect']:
                return SimpleNamespace(returncode=1, stdout='', stderr='no such service')
            return SimpleNamespace(returncode=1, stdout='', stderr='network traefik-public not found')

        with patch('registry.provisioning.subprocess.run', side_effect=run):
            with self.assertRaisesMessage(RuntimeError, 'network traefik-public not found'):
                MotorProvisionamento(self.cliente).sincronizar_servico_mcp()


class SuspenderReativarTests(_Base):
    def test_suspender_para_o_mcp(self):
        motor = MotorProvisionamento(self.cliente)
        with patch('registry.provisioning.subprocess.run') as run, \
                patch.object(motor, '_spec_servico', return_value={}):
            motor.suspender()
        comandos = [c.args[0] for c in run.call_args_list]
        self.assertIn(['docker', 'service', 'scale', 'acme_mcp=0'], comandos)

    def test_suspender_sem_servico_mcp_nao_falha(self):
        motor = MotorProvisionamento(self.cliente)
        existentes = {'acme_cron': {}}
        with patch('registry.provisioning.subprocess.run') as run, \
                patch.object(motor, '_spec_servico', side_effect=lambda nome, obrigatorio=True: existentes.get(nome)):
            motor.suspender()
        comandos = [c.args[0] for c in run.call_args_list]
        self.assertNotIn(['docker', 'service', 'scale', 'acme_mcp=0'], comandos)

    def test_reativar_volta_a_seguir_o_modulo(self):
        motor = MotorProvisionamento(self.cliente)
        with patch('registry.provisioning.subprocess.run'), \
                patch.object(motor, '_spec_servico', return_value={}), \
                patch.object(motor, 'sincronizar_servico_mcp') as mcp_sync:
            motor.reativar()
        mcp_sync.assert_called_once_with()
