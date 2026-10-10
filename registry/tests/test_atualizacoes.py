from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import requests
from django.contrib.auth import get_user_model
from django.test import Client, SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from registry import atualizacoes
from registry.models import ConfiguracaoAtualizacao
from registry.tasks import task_verificar_atualizacao


def _cfg(**extra):
    base = dict(
        repositorio='emporioautomacao/ararasuite-cp', dockerhub_usuario='', dockerhub_token_cifrado='',
        dockerhub_token='', status_conexao='nao_testado', status_mensagem='', verificado_em=None,
        versao_disponivel='', versoes_recentes=[], atualizacao_container='', atualizacao_alvo='',
        atualizacao_tag_anterior='',
    )
    base.update(extra)
    return SimpleNamespace(**base)


def _resp(status=200, json=None, links=None):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = json or {}
    r.links = links or {}
    r.raise_for_status.side_effect = None if status < 400 else requests.HTTPError(f'HTTP {status}')
    return r


@override_settings(VERSION='0.0.43')
class VersoesTests(SimpleTestCase):
    def test_ordem_numerica_e_nao_alfabetica(self):
        tags = ['0.0.9', '0.0.10', '0.0.2', 'latest', '1.0.0-rc1', '0.0.43', 'v0.0.50', '0.0.43']
        self.assertEqual(atualizacoes.tags_semver(tags), ['0.0.2', '0.0.9', '0.0.10', '0.0.43'])
        self.assertEqual(atualizacoes.maior_versao(tags), '0.0.43')
        self.assertEqual(atualizacoes.maior_versao(['latest']), '')

    def test_ha_atualizacao(self):
        self.assertTrue(atualizacoes.ha_atualizacao('0.0.9', '0.0.10'))
        self.assertFalse(atualizacoes.ha_atualizacao('0.0.43', '0.0.43'))
        self.assertFalse(atualizacoes.ha_atualizacao('0.0.43', '0.0.42'))
        self.assertFalse(atualizacoes.ha_atualizacao('0.0.43', ''))

    def test_situacao(self):
        self.assertEqual(atualizacoes.situacao(_cfg()), 'nao_verificado')
        self.assertEqual(atualizacoes.situacao(_cfg(status_conexao='erro')), 'indisponivel')
        self.assertEqual(atualizacoes.situacao(_cfg(status_conexao='conectado', versao_disponivel='0.0.43')), 'atualizado')
        self.assertEqual(atualizacoes.situacao(_cfg(status_conexao='conectado', versao_disponivel='0.0.44')), 'disponivel')
        self.assertEqual(atualizacoes.situacao(_cfg(status_conexao='conectado')), 'nao_verificado')


class ListarTagsTests(SimpleTestCase):
    def test_fluxo_token_e_tags_com_paginacao(self):
        respostas = [
            _resp(json={'token': 'jwt'}),
            _resp(json={'tags': ['0.0.1', '0.0.2']}, links={'next': {'url': '/v2/emporioautomacao/ararasuite-cp/tags/list?n=1000&last=0.0.2'}}),
            _resp(json={'tags': ['0.0.3', 'latest']}),
        ]
        with patch('requests.get', side_effect=respostas) as get:
            tags = atualizacoes.listar_tags(_cfg())
        self.assertEqual(tags, ['0.0.1', '0.0.2', '0.0.3', 'latest'])
        auth_call, p1, p2 = get.call_args_list
        self.assertIsNone(auth_call.kwargs['auth'])  # anônimo sem credenciais
        self.assertEqual(auth_call.kwargs['params']['scope'], 'repository:emporioautomacao/ararasuite-cp:pull')
        self.assertEqual(p1.kwargs['headers']['Authorization'], 'Bearer jwt')
        self.assertTrue(p2.args[0].startswith('https://registry-1.docker.io/v2/'))

    def test_envia_credenciais_quando_informadas(self):
        cfg = _cfg(dockerhub_usuario='joao', dockerhub_token='pat-secreto', dockerhub_token_cifrado='x')
        with patch('requests.get', side_effect=[_resp(json={'token': 't'}), _resp(json={'tags': []})]) as get:
            atualizacoes.listar_tags(cfg)
        self.assertEqual(get.call_args_list[0].kwargs['auth'], ('joao', 'pat-secreto'))

    def test_credenciais_recusadas(self):
        with patch('requests.get', return_value=_resp(status=401)):
            with self.assertRaisesMessage(atualizacoes.ErroRegistry, 'Credenciais do Docker Hub recusadas'):
                atualizacoes.listar_tags(_cfg(dockerhub_usuario='a', dockerhub_token='b', dockerhub_token_cifrado='x'))

    def test_repositorio_privado_sem_permissao(self):
        with patch('requests.get', side_effect=[_resp(json={'token': 't'}), _resp(status=401)]):
            with self.assertRaisesMessage(atualizacoes.ErroRegistry, 'não encontrado ou sem permissão'):
                atualizacoes.listar_tags(_cfg())

    def test_repositorio_invalido_nao_chama_a_rede(self):
        with patch('requests.get') as get:
            with self.assertRaises(atualizacoes.ErroRegistry):
                atualizacoes.listar_tags(_cfg(repositorio='sem-barra'))
        get.assert_not_called()

    def test_verificar_traduz_timeout_e_rede(self):
        with patch('requests.get', side_effect=requests.exceptions.Timeout()):
            r = atualizacoes.verificar(_cfg())
        self.assertEqual((r['status'], 'demorou demais' in r['mensagem']), ('erro', True))
        with patch('requests.get', side_effect=requests.exceptions.ConnectionError()):
            self.assertIn('conectar ao Docker Hub', atualizacoes.verificar(_cfg())['mensagem'])

    def test_verificar_sem_tags_de_versao(self):
        with patch('registry.atualizacoes.listar_tags', return_value=['latest', 'dev']):
            r = atualizacoes.verificar(_cfg())
        self.assertEqual(r['status'], 'erro')
        self.assertIn('Nenhuma tag de versão', r['mensagem'])

    def test_verificar_devolve_a_maior_versao_e_as_recentes(self):
        with patch('registry.atualizacoes.listar_tags', return_value=['0.0.9', '0.0.10', '0.0.11', 'latest']):
            r = atualizacoes.verificar(_cfg())
        self.assertEqual((r['status'], r['disponivel']), ('conectado', '0.0.11'))
        self.assertEqual(r['recentes'], ['0.0.11', '0.0.10', '0.0.9'])

    def test_cache_de_60s_nao_consulta_a_rede(self):
        cfg = _cfg(status_conexao='conectado', verificado_em=timezone.now(), versao_disponivel='0.0.50',
                   versoes_recentes=['0.0.50'], status_mensagem='ok')
        with patch('registry.atualizacoes.listar_tags') as listar:
            r = atualizacoes.verificar(cfg, usar_cache=True)
        listar.assert_not_called()
        self.assertEqual(r['disponivel'], '0.0.50')


class MontarScriptTests(SimpleTestCase):
    def test_ordem_flags_e_imagem(self):
        servicos = ['cp_celery', 'cp_beat', 'cp_agente-no', 'cp_web']
        script = atualizacoes.montar_script('emporioautomacao/ararasuite-cp', '0.0.44', servicos)
        updates = [l for l in script.splitlines() if l.startswith('docker service update')]
        self.assertEqual([l.split()[-1] for l in updates], servicos)  # web por último
        for l in updates:
            self.assertIn('--with-registry-auth', l)
            self.assertIn('--update-failure-action rollback', l)
            self.assertIn('--detach=false', l)
            self.assertIn('--image emporioautomacao/ararasuite-cp:0.0.44', l)
        self.assertTrue(script.startswith('set -e'))
        self.assertNotIn('_db', script)
        self.assertNotIn('_redis', script)

    def test_recusa_valores_fora_do_formato(self):
        with self.assertRaises(AssertionError):
            atualizacoes.montar_script('emporioautomacao/ararasuite-cp', '0.0.44; rm -rf /', ['cp_web'])
        with self.assertRaises(AssertionError):
            atualizacoes.montar_script('x; reboot', '0.0.44', ['cp_web'])


def _docker_falso(swarm='true', stack='cp', servicos=None, imagem='emporioautomacao/ararasuite-cp:0.0.43@sha256:abc',
                  run_rc=0, estado='running|0'):
    servicos = servicos if servicos is not None else ['cp_db', 'cp_redis', 'cp_web', 'cp_celery', 'cp_beat', 'cp_agente-no']
    chamadas = []

    def _f(*args, timeout=30):
        chamadas.append(args)
        out = ''
        rc = 0
        if args[0] == 'info':
            out = swarm
        elif args[0] == 'inspect' and args[1].startswith('cp-atualizador'):
            out = estado
        elif args[0] == 'inspect':
            out = stack
        elif args[:2] == ('service', 'ls'):
            out = '\n'.join(servicos)
        elif args[:2] == ('service', 'inspect'):
            out = imagem
        elif args[0] == 'run':
            rc = run_rc
        elif args[0] == 'logs':
            out = 'linha de log'
        return SimpleNamespace(returncode=rc, stdout=out, stderr='falhou' if rc else '')

    _f.chamadas = chamadas
    return _f


@override_settings(VERSION='0.0.43')
class IniciarAtualizacaoTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_superuser('atz', 'atz@example.com', 'SenhaSegura123!')
        self.cfg = ConfiguracaoAtualizacao.objects.create(repositorio='emporioautomacao/ararasuite-cp')
        self.env = patch.dict('os.environ', {'HOSTNAME': 'abc123'})
        self.env.start()
        self.addCleanup(self.env.stop)
        p = patch('registry.atualizacoes.listar_tags', return_value=['0.0.43', '0.0.44', '0.0.45', 'latest'])
        p.start()
        self.addCleanup(p.stop)

    def _iniciar(self, versao='0.0.45', **docker):
        falso = _docker_falso(**docker)
        with patch('registry.atualizacoes._docker', falso):
            nome = atualizacoes.iniciar_atualizacao(self.cfg, versao, self.user)
        return nome, falso

    def test_sucesso_dispara_o_auxiliar_e_registra(self):
        nome, falso = self._iniciar()
        run = next(c for c in falso.chamadas if c[0] == 'run')
        self.assertIn('-d', run)
        self.assertNotIn('--rm', run)  # o log precisa sobreviver
        self.assertIn('/var/run/docker.sock:/var/run/docker.sock', run)
        self.assertIn('/root/.docker:/root/.docker:ro', run)
        self.assertIn('emporioautomacao/ararasuite-cp:0.0.43', run)  # imagem atual, sem o digest
        script = run[-1]
        self.assertIn('--image emporioautomacao/ararasuite-cp:0.0.45', script)
        self.assertLess(script.index('cp_celery'), script.index('cp_web'))
        self.assertNotIn('cp_db', script)
        self.cfg.refresh_from_db()
        self.assertEqual((self.cfg.atualizacao_alvo, self.cfg.atualizacao_container), ('0.0.45', nome))
        self.assertEqual(self.cfg.atualizacao_iniciada_por, self.user)
        self.assertIsNotNone(self.cfg.atualizacao_iniciada_em)

    def _recusa(self, mensagem, versao='0.0.45', **docker):
        with self.assertRaisesMessage(atualizacoes.ErroAtualizacao, mensagem):
            self._iniciar(versao, **docker)
        self.cfg.refresh_from_db()
        self.assertEqual(self.cfg.atualizacao_container, '')

    def test_recusa_downgrade_e_mesma_versao(self):
        self._recusa('downgrade', versao='0.0.42')
        self._recusa('downgrade', versao='0.0.43')

    def test_recusa_versao_inexistente_no_docker_hub(self):
        self._recusa('não existe no Docker Hub', versao='0.0.99')

    def test_recusa_versao_malformada(self):
        self._recusa('Versão inválida', versao='0.0.45; reboot')

    def test_recusa_sem_swarm_manager(self):
        self._recusa('Swarm manager', swarm='false')

    def test_recusa_sem_label_de_stack(self):
        self._recusa('identificar o stack', stack='')

    def test_recusa_sem_servico_web(self):
        self._recusa('não encontrado no Swarm', servicos=['cp_db', 'cp_celery'])

    def test_recusa_atualizacao_em_andamento(self):
        ConfiguracaoAtualizacao.objects.filter(pk=1).update(atualizacao_container='cp-atualizador-antigo')
        self.cfg.refresh_from_db()
        self._recusa_andamento()

    def _recusa_andamento(self):
        with patch('registry.atualizacoes._docker', _docker_falso(estado='running|0')):
            with self.assertRaisesMessage(atualizacoes.ErroAtualizacao, 'em andamento'):
                atualizacoes.iniciar_atualizacao(self.cfg, '0.0.45', self.user)

    def test_falha_ao_iniciar_o_auxiliar(self):
        self._recusa('Falha ao iniciar o container', run_rc=1)

    def test_remove_o_auxiliar_da_execucao_anterior(self):
        ConfiguracaoAtualizacao.objects.filter(pk=1).update(atualizacao_container='cp-atualizador-antigo')
        self.cfg.refresh_from_db()
        falso = _docker_falso(estado='exited|0')
        with patch('registry.atualizacoes._docker', falso):
            atualizacoes.iniciar_atualizacao(self.cfg, '0.0.45', self.user)
        self.assertIn(('rm', '-f', 'cp-atualizador-antigo'), falso.chamadas)


@override_settings(VERSION='0.0.43')
class EstadoAtualizacaoTests(SimpleTestCase):
    def _estado(self, **docker):
        cfg = _cfg(atualizacao_container='cp-atualizador-x', atualizacao_alvo='0.0.44')
        with patch('registry.atualizacoes._docker', _docker_falso(**docker)):
            return atualizacoes.estado_atualizacao(cfg)

    def test_nenhuma(self):
        self.assertEqual(atualizacoes.estado_atualizacao(_cfg())['estado'], 'nenhuma')

    def test_em_andamento_com_log(self):
        e = self._estado(estado='running|0')
        self.assertEqual((e['estado'], e['log']), ('em_andamento', 'linha de log'))

    def test_concluida_e_falhou(self):
        self.assertEqual(self._estado(estado='exited|0')['estado'], 'concluida')
        self.assertEqual(self._estado(estado='exited|1')['estado'], 'falhou')

    def test_auxiliar_removido_e_versao_ja_e_a_alvo(self):
        cfg = _cfg(atualizacao_container='cp-atualizador-x', atualizacao_alvo='0.0.43')
        falso = MagicMock(return_value=SimpleNamespace(returncode=1, stdout='', stderr='No such object'))
        with patch('registry.atualizacoes._docker', falso):
            self.assertEqual(atualizacoes.estado_atualizacao(cfg)['estado'], 'concluida')


@override_settings(VERSION='0.0.43')
class AdminAtualizacaoTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_superuser('atz_admin', 'atzadmin@example.com', 'SenhaSegura123!')
        self.c = Client()
        self.c.force_login(self.user)
        self.add = reverse('admin:registry_configuracaoatualizacao_add')
        self.dados = {'repositorio': 'emporioautomacao/ararasuite-cp', 'dockerhub_usuario': 'joao',
                      'dockerhub_token': 'pat-super-secreto'}
        self.ok = {'status': 'conectado', 'mensagem': 'Docker Hub acessível', 'disponivel': '0.0.44', 'recentes': ['0.0.44', '0.0.43']}
        # sem Swarm por padrão (evita chamar o `docker info` real); os testes que precisam sobrepõem
        p = patch('registry.atualizacoes.swarm_manager', return_value=False)
        p.start()
        self.addCleanup(p.stop)
        p = patch('registry.atualizacoes.stack_usa_latest', return_value=False)
        self.usa_latest = p.start()
        self.addCleanup(p.stop)

    def _salvar(self, resultado=None):
        with patch('registry.atualizacoes.verificar', return_value=resultado or self.ok):
            return self.c.post(self.add, self.dados, follow=True)

    def test_item_na_sidebar_e_formulario_inicial(self):
        self.assertContains(self.c.get(reverse('admin:index')), 'Verificar Atualizações')
        self.assertContains(self.c.get(self.add), 'emporioautomacao/ararasuite-cp')

    def test_salvar_cifra_o_token_e_consulta_o_docker_hub(self):
        resp = self._salvar()
        cfg = ConfiguracaoAtualizacao.obter()
        self.assertEqual(cfg.dockerhub_token, 'pat-super-secreto')
        self.assertNotIn('pat-super-secreto', cfg.dockerhub_token_cifrado)
        self.assertEqual((cfg.status_conexao, cfg.versao_disponivel), ('conectado', '0.0.44'))
        self.assertContains(resp, 'Consulta ao Docker Hub após salvar')

    def test_repositorio_invalido_e_rejeitado(self):
        self.dados['repositorio'] = 'sem barra'
        resp = self.c.post(self.add, self.dados)
        self.assertContains(resp, 'usuario/repositorio')
        self.assertIsNone(ConfiguracaoAtualizacao.obter())

    def test_token_nunca_reexibido_e_vazio_mantem(self):
        self._salvar()
        change = reverse('admin:registry_configuracaoatualizacao_change', args=[1])
        self.assertNotContains(self.c.get(change), 'pat-super-secreto')
        with patch('registry.atualizacoes.verificar', return_value=self.ok):
            self.c.post(change, {**self.dados, 'dockerhub_token': '', 'dockerhub_usuario': 'maria'})
        cfg = ConfiguracaoAtualizacao.obter()
        self.assertEqual((cfg.dockerhub_usuario, cfg.dockerhub_token), ('maria', 'pat-super-secreto'))

    def test_changelist_redireciona_para_o_formulario(self):
        lista = reverse('admin:registry_configuracaoatualizacao_changelist')
        self.assertRedirects(self.c.get(lista), self.add, fetch_redirect_response=False)
        self._salvar()
        self.assertRedirects(
            self.c.get(lista), reverse('admin:registry_configuracaoatualizacao_change', args=[1]),
            fetch_redirect_response=False,
        )

    def test_pagina_mostra_versoes_situacao_e_aviso_do_portainer(self):
        self._salvar()
        change = reverse('admin:registry_configuracaoatualizacao_change', args=[1])
        with patch('registry.atualizacoes.swarm_manager', return_value=True):
            resp = self.c.get(change)
        self.assertContains(resp, '0.0.43')
        self.assertContains(resp, '0.0.44')
        self.assertContains(resp, 'Atualização disponível')
        self.assertContains(resp, reverse('admin:registry_configuracaoatualizacao_atualizar', args=['0.0.44']))
        self.assertContains(resp, 'CP_VERSION')

    def test_sem_swarm_nao_oferece_o_botao(self):
        self._salvar()
        change = reverse('admin:registry_configuracaoatualizacao_change', args=[1])
        with patch('registry.atualizacoes.swarm_manager', return_value=False):
            resp = self.c.get(change)
        self.assertNotContains(resp, 'id="atz-btn"')
        self.assertContains(resp, 'só está disponível em um Swarm manager')

    def test_atualizado_nao_mostra_botao(self):
        self._salvar({**self.ok, 'disponivel': '0.0.43'})
        change = reverse('admin:registry_configuracaoatualizacao_change', args=[1])
        with patch('registry.atualizacoes.swarm_manager', return_value=True):
            resp = self.c.get(change)
        self.assertContains(resp, 'Atualizado')
        self.assertContains(resp, 'display:none;')  # botão presente mas oculto

    def test_json_verificar(self):
        self._salvar()
        with patch('registry.atualizacoes.verificar', return_value={**self.ok, 'disponivel': '0.0.50'}):
            j = self.c.get(reverse('admin:registry_configuracaoatualizacao_verificar')).json()
        self.assertEqual((j['situacao'], j['disponivel'], j['atual']), ('disponivel', '0.0.50', '0.0.43'))

    def test_verificar_agora_consulta_e_volta_para_a_tela(self):
        self._salvar()
        with patch('registry.atualizacoes.verificar', return_value={**self.ok, 'mensagem': 'consulta nova'}):
            resp = self.c.get(reverse('admin:registry_configuracaoatualizacao_verificar_agora'), follow=True)
        self.assertContains(resp, 'consulta nova')

    def test_tela_de_confirmacao(self):
        self._salvar()
        resp = self.c.get(reverse('admin:registry_configuracaoatualizacao_atualizar', args=['0.0.44']))
        self.assertContains(resp, 'Confirmar atualização para 0.0.44')
        self.assertContains(resp, 'CP_VERSION=0.0.44')

    def test_post_confirma_e_dispara_a_atualizacao(self):
        self._salvar()
        url = reverse('admin:registry_configuracaoatualizacao_atualizar', args=['0.0.44'])
        with patch('registry.atualizacoes.iniciar_atualizacao', return_value='cp-atualizador-x') as iniciar:
            resp = self.c.post(url, follow=True)
        iniciar.assert_called_once()
        self.assertEqual(iniciar.call_args.args[1:], ('0.0.44', self.user))
        self.assertContains(resp, 'Atualização para 0.0.44 iniciada')
        self.assertContains(resp, 'CP_VERSION=0.0.44')

    def test_post_com_erro_mostra_a_mensagem(self):
        self._salvar()
        url = reverse('admin:registry_configuracaoatualizacao_atualizar', args=['0.0.44'])
        with patch('registry.atualizacoes.iniciar_atualizacao', side_effect=atualizacoes.ErroAtualizacao('sem Swarm aqui')):
            resp = self.c.post(url, follow=True)
        self.assertContains(resp, 'sem Swarm aqui')

    def test_downgrade_e_recusado_antes_de_confirmar(self):
        self._salvar()
        url = reverse('admin:registry_configuracaoatualizacao_atualizar', args=['0.0.40'])
        with patch('registry.atualizacoes.iniciar_atualizacao') as iniciar:
            resp = self.c.post(url, follow=True)
        iniciar.assert_not_called()
        self.assertContains(resp, 'não é mais nova')

    def test_so_superusuario_atualiza(self):
        self._salvar()
        staff = get_user_model().objects.create_user('staff', 'staff@example.com', 'SenhaSegura123!', is_staff=True)
        c = Client()
        c.force_login(staff)
        url = reverse('admin:registry_configuracaoatualizacao_atualizar', args=['0.0.44'])
        with patch('registry.atualizacoes.iniciar_atualizacao') as iniciar:
            self.assertEqual(c.get(url).status_code, 403)
            self.assertEqual(c.post(url).status_code, 403)
        iniciar.assert_not_called()

    def test_andamento_da_ultima_atualizacao_aparece_com_log(self):
        self._salvar()
        ConfiguracaoAtualizacao.objects.filter(pk=1).update(
            atualizacao_alvo='0.0.44', atualizacao_container='cp-atualizador-x',
            atualizacao_iniciada_em=timezone.now(), atualizacao_iniciada_por=self.user,
        )
        change = reverse('admin:registry_configuracaoatualizacao_change', args=[1])
        with patch('registry.atualizacoes._docker', _docker_falso(estado='exited|1')), \
                patch('registry.atualizacoes.swarm_manager', return_value=True):
            resp = self.c.get(change)
        self.assertContains(resp, 'Falhou')
        self.assertContains(resp, 'linha de log')
        self.assertContains(resp, 'atz_admin')


@override_settings(VERSION='0.0.43')
class TaskVerificarAtualizacaoTests(TestCase):
    def test_sem_configuracao_nao_quebra(self):
        self.assertEqual(task_verificar_atualizacao(), 'nao_testado')

    def test_grava_o_resultado(self):
        ConfiguracaoAtualizacao.objects.create()
        with patch('registry.atualizacoes.verificar', return_value={
            'status': 'conectado', 'mensagem': 'ok', 'disponivel': '0.0.44', 'recentes': ['0.0.44'],
        }):
            self.assertEqual(task_verificar_atualizacao(), 'conectado')
        cfg = ConfiguracaoAtualizacao.obter()
        self.assertEqual((cfg.versao_disponivel, cfg.versoes_recentes), ('0.0.44', ['0.0.44']))


class TagDaImagemTests(SimpleTestCase):
    def test_tag_da_imagem(self):
        self.assertEqual(atualizacoes.tag_da_imagem('emporioautomacao/ararasuite-cp:latest'), 'latest')
        self.assertEqual(atualizacoes.tag_da_imagem('emporioautomacao/ararasuite-cp:0.0.45'), '0.0.45')
        self.assertEqual(atualizacoes.tag_da_imagem('emporioautomacao/ararasuite-cp'), 'latest')  # sem tag = latest
        self.assertEqual(atualizacoes.tag_da_imagem('registro:5000/emporio/cp:0.0.9'), '0.0.9')  # porta no host não é tag
        self.assertEqual(atualizacoes.tag_da_imagem('registro:5000/emporio/cp'), 'latest')


@override_settings(VERSION='0.0.43')
class StackUsaLatestTests(SimpleTestCase):
    def _usa(self, tag_atual, cfg=None, swarm=True, stack='cp'):
        imagem = f'emporioautomacao/ararasuite-cp:{tag_atual}' if tag_atual else None
        with patch('registry.atualizacoes.swarm_manager', return_value=swarm), \
                patch('registry.atualizacoes.descobrir_stack', return_value=stack), \
                patch('registry.atualizacoes.imagem_do_servico', return_value=imagem):
            return atualizacoes.stack_usa_latest(cfg)

    def test_web_em_latest(self):
        self.assertIs(self._usa('latest'), True)

    def test_tag_numerica_escolhida_pelo_stack(self):
        self.assertIs(self._usa('0.0.43', _cfg()), False)

    def test_fixada_pelo_painel_quando_o_stack_era_latest(self):
        cfg = _cfg(atualizacao_tag_anterior='latest', atualizacao_alvo='0.0.45')
        self.assertIs(self._usa('0.0.45', cfg), True)

    def test_fixada_pelo_painel_mas_o_stack_ja_era_numerico(self):
        cfg = _cfg(atualizacao_tag_anterior='0.0.43', atualizacao_alvo='0.0.45')
        self.assertIs(self._usa('0.0.45', cfg), False)

    def test_tag_mudou_por_outro_caminho_nao_assume_latest(self):
        cfg = _cfg(atualizacao_tag_anterior='latest', atualizacao_alvo='0.0.45')
        self.assertIs(self._usa('0.0.50', cfg), False)

    def test_sem_swarm_ou_sem_stack_e_desconhecido(self):
        self.assertIsNone(self._usa('latest', swarm=False))
        self.assertIsNone(self._usa('latest', stack=None))


@override_settings(VERSION='0.0.43')
class AvisoDoCpVersionTests(TestCase):
    """O aviso "ajuste CP_VERSION" só aparece quando o stack NÃO acompanha a latest."""

    def setUp(self):
        self.user = get_user_model().objects.create_superuser('lat_admin', 'lat@example.com', 'SenhaSegura123!')
        self.cfg = ConfiguracaoAtualizacao.objects.create(
            status_conexao='conectado', versao_disponivel='0.0.45', versoes_recentes=['0.0.45', '0.0.43'],
        )
        self.c = Client()
        self.c.force_login(self.user)
        self.change = reverse('admin:registry_configuracaoatualizacao_change', args=[1])
        self.confirmar = reverse('admin:registry_configuracaoatualizacao_atualizar', args=['0.0.45'])
        for alvo, ret in (('registry.atualizacoes.swarm_manager', True),):
            p = patch(alvo, return_value=ret)
            p.start()
            self.addCleanup(p.stop)

    def _com(self, usa_latest):
        return patch('registry.atualizacoes.stack_usa_latest', return_value=usa_latest)

    def test_painel_com_stack_em_latest(self):
        with self._com(True):
            html = self.c.get(self.change).content.decode()
        self.assertIn('usa a tag <code>latest</code>', html)
        self.assertNotIn('ajuste também <code>CP_VERSION</code>', html)

    def test_painel_com_stack_numerico_ou_desconhecido(self):
        for valor in (False, None):
            with self._com(valor):
                html = self.c.get(self.change).content.decode()
            self.assertIn('ajuste também <code>CP_VERSION</code>', html)
            self.assertNotIn('usa a tag <code>latest</code>', html)

    def test_confirmacao_com_stack_em_latest(self):
        with self._com(True):
            html = self.c.get(self.confirmar).content.decode()
        self.assertIn('usa a tag <code>latest</code>', html)
        self.assertNotIn('Depois, ajuste', html)

    def test_confirmacao_com_stack_numerico(self):
        with self._com(False):
            html = self.c.get(self.confirmar).content.decode()
        self.assertIn('Depois, ajuste <code>CP_VERSION=0.0.45</code>', html)

    def test_mensagem_de_sucesso_com_stack_em_latest(self):
        with self._com(True), patch('registry.atualizacoes.iniciar_atualizacao', return_value='cp-atualizador-x'):
            resp = self.c.post(self.confirmar, follow=True)
        self.assertContains(resp, 'Atualização para 0.0.45 iniciada')
        self.assertContains(resp, 'o stack usa a tag latest')
        self.assertNotContains(resp, 'ajuste CP_VERSION=0.0.45')

    def test_mensagem_de_sucesso_com_stack_numerico(self):
        with self._com(False), patch('registry.atualizacoes.iniciar_atualizacao', return_value='cp-atualizador-x'):
            resp = self.c.post(self.confirmar, follow=True)
        self.assertContains(resp, 'ajuste CP_VERSION=0.0.45')

    def test_andamento_nao_cobra_cp_version_quando_o_stack_era_latest(self):
        from django.utils import timezone
        ConfiguracaoAtualizacao.objects.filter(pk=1).update(
            atualizacao_alvo='0.0.45', atualizacao_container='cp-atualizador-x', atualizacao_tag_anterior='latest',
            atualizacao_iniciada_em=timezone.now(), atualizacao_iniciada_por=self.user,
        )
        with self._com(True), patch('registry.atualizacoes._docker', _docker_falso(estado='running|0')):
            html = self.c.get(self.change).content.decode()
        self.assertIn('Em andamento', html)
        self.assertNotIn('Lembre-se de ajustar', html)
        ConfiguracaoAtualizacao.objects.filter(pk=1).update(atualizacao_tag_anterior='0.0.43')
        with self._com(False), patch('registry.atualizacoes._docker', _docker_falso(estado='running|0')):
            html = self.c.get(self.change).content.decode()
        self.assertIn('Lembre-se de ajustar', html)


@override_settings(VERSION='0.0.43')
class RegistraTagAnteriorTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_superuser('tag_admin', 'tag@example.com', 'SenhaSegura123!')
        self.cfg = ConfiguracaoAtualizacao.objects.create(repositorio='emporioautomacao/ararasuite-cp')
        for alvo, kw in (
            ('registry.atualizacoes.listar_tags', {'return_value': ['0.0.43', '0.0.45']}),
        ):
            p = patch(alvo, **kw)
            p.start()
            self.addCleanup(p.stop)
        env = patch.dict('os.environ', {'HOSTNAME': 'abc123'})
        env.start()
        self.addCleanup(env.stop)

    def _iniciar(self, imagem):
        with patch('registry.atualizacoes._docker', _docker_falso(imagem=imagem)):
            atualizacoes.iniciar_atualizacao(self.cfg, '0.0.45', self.user)
        self.cfg.refresh_from_db()

    def test_guarda_latest_quando_o_stack_estava_em_latest(self):
        self._iniciar('emporioautomacao/ararasuite-cp:latest@sha256:abc')
        self.assertEqual(self.cfg.atualizacao_tag_anterior, 'latest')

    def test_guarda_a_tag_numerica_anterior(self):
        self._iniciar('emporioautomacao/ararasuite-cp:0.0.43@sha256:abc')
        self.assertEqual(self.cfg.atualizacao_tag_anterior, '0.0.43')
