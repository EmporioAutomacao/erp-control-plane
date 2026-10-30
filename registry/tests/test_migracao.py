"""Migração de cliente entre servidores — ver docs/registry/migracao-cliente-entre-servidores.md.

Swarm, nós e despachos de tasks são simulados: aqui se prova a máquina de estados, os gates, a
volta e as garantias (nada atende tráfego antes da conferência; sem volta depois do ponto sem volta).
"""
import hashlib
import tempfile
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.test import Client, SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone

from registry import migracao
from registry.models import BackupCliente, Cliente, HostInfraestrutura, MigracaoCliente, Plano, Regiao

SEGREDOS = {'POSTGRES_PASSWORD': 'pg', 'DJANGO_SECRET_KEY': 'dj', 'EMPRESAS_CREDENCIAL_MASTER_KEY': 'mk'}
ENV_VIVO = {**SEGREDOS, 'ALLOWED_HOSTS': 'acme.ararasuite.com.br'}


def _manifesto(linhas=10, arquivos=3):
    return {
        'banco': {'tabelas': {'public.a': linhas, 'public.b': 2}, 'migrations': 5, 'migrations_hash': 'h'},
        'volumes': {
            'media': {'existe': True, 'arquivos': arquivos, 'bytes': 300, 'hash': 'm'},
            'backups': {'existe': True, 'arquivos': 0, 'bytes': 0, 'hash': ''},
        },
    }


class FakeMotor:
    """MotorProvisionamento sem Docker: registra cada implantação (host, modo)."""
    implantacoes = []
    env = dict(SEGREDOS)
    falhar_executar = False

    def __init__(self, cliente):
        self.cliente = cliente

    def executar(self, modo_migracao=False):
        if FakeMotor.falhar_executar:
            raise RuntimeError('stack deploy falhou')
        FakeMotor.implantacoes.append((self.cliente.host.regiao_id, modo_migracao))

    def ler_env_existente(self):
        return dict(FakeMotor.env)

    def montar_env(self, env_existente):
        return dict(ENV_VIVO)


class _Base(TestCase):
    """Cliente em `anapolis` (nó no-a) migrando para `hostinger` (nó no-b), com tudo aprovado."""

    def setUp(self):
        FakeMotor.implantacoes = []
        FakeMotor.env = dict(SEGREDOS)
        FakeMotor.falhar_executar = False
        self.user = get_user_model().objects.create_superuser('mig', 'mig@example.com', 'SenhaSegura123!')
        self.plano = Plano.objects.create(slug='basico-mig', nome='Basico Mig', preco_mensal='0.00')
        self.regiao_b = Regiao.objects.create(slug='hostinger', nome='Hostinger')
        self.host_a = HostInfraestrutura.objects.create(
            nome='host-a', ip='10.0.0.1', regiao_id='anapolis', cpu_total=4, ram_gb_total=8)
        self.host_b = HostInfraestrutura.objects.create(
            nome='host-b', ip='10.0.0.2', regiao=self.regiao_b, cpu_total=8, ram_gb_total=32)
        self.cliente = Cliente.objects.create(
            slug='acme', nome='Acme', cnpj='11.222.333/0001-44', email_contato='acme@example.com',
            subdominio='acme.ararasuite.com.br', plano=self.plano, host=self.host_a, status='ativo',
            versao_erp='0.0.143',
        )

        nos = {
            'anapolis': [{'id': 'A', 'hostname': 'no-a', 'status': 'Ready', 'disponibilidade': 'Active', 'arquitetura': 'x86_64'}],
            'hostinger': [{'id': 'B', 'hostname': 'no-b', 'status': 'Ready', 'disponibilidade': 'Active', 'arquitetura': 'x86_64'}],
        }
        self.nos = nos
        self.replicas = {'acme_web': 1, 'acme_cron': 1}
        self.p = {}
        alvos = {
            'nos_da_regiao': ('registry.swarm.nos_da_regiao', {'side_effect': lambda r: self.nos.get(r, [])}),
            'replicas': ('registry.swarm.replicas', {'side_effect': lambda s: self.replicas.get(s)}),
            'escalar': ('registry.swarm.escalar', {}),
            'aguardar_parado': ('registry.swarm.aguardar_parado', {}),
            'aguardar_no': ('registry.swarm.aguardar_no_do_servico', {}),
            'imagem': ('registry.swarm.imagem_do_servico', {'return_value': 'emporioautomacao/ararasuite-erp:0.0.143'}),
            'env_servico': ('registry.swarm.env_do_servico', {'return_value': dict(ENV_VIVO)}),
            'traefik': ('registry.swarm.definir_traefik', {}),
            'no_db': ('registry.nos.hostname_do_no', {'return_value': 'no-a'}),
            'storage': ('registry.storage_backup.testar_conexao', {'return_value': ('conectado', 'ok')}),
            'motor': ('registry.provisioning.MotorProvisionamento', {'new': FakeMotor}),
            'health': ('registry.migracao.health_publico', {'return_value': 'health ok'}),
            'sondar': ('registry.tasks.task_migracao_sondar.apply_async', {}),
            'manifesto': ('registry.tasks.task_migracao_manifesto.apply_async', {}),
            'iniciar_backup': ('registry.tasks.task_iniciar_backup_cliente.apply_async', {}),
            'restaurar': ('registry.tasks.task_restaurar_no.apply_async', {}),
            'teste_interno': ('registry.tasks.task_migracao_teste_interno.apply_async', {}),
            'iniciar_task': ('registry.tasks.task_migracao_iniciar.apply_async', {}),
        }
        for nome, (alvo, kw) in alvos.items():
            patcher = patch(alvo, **kw)
            self.p[nome] = patcher.start()
            self.addCleanup(patcher.stop)
        # o despacho da task inicial roda as sondas na hora (como o worker do manager faria)
        self.p['iniciar_task'].side_effect = lambda args, queue: migracao.disparar_sondas(args[0])

    # ── atalhos ──
    def iniciar(self):
        with self.captureOnCommitCallbacks(execute=True):
            self.m = migracao.iniciar(self.cliente, self.host_b, self.user)
        return self.m

    def recarregar(self):
        self.m.refresh_from_db()
        return self.m

    def evento(self, evento, dados):
        migracao.avancar(self.m.pk, evento, dados)
        return self.recarregar()

    def sondas_ok(self):
        self.evento('sonda', {'lado': 'origem', 'hostname': 'no-a', 'banco_bytes': 100, 'volumes_bytes': {'media': 300, 'backups': 0}})
        return self.evento('sonda', {'lado': 'destino', 'hostname': 'no-b', 'volumes_existentes': [], 'livre_bytes': 10 ** 9})

    def ate_backup(self):
        self.iniciar()
        self.sondas_ok()
        return self.evento('manifesto_origem', {'manifesto': _manifesto()})

    def ate_restaurando(self):
        self.ate_backup()
        return self.evento('backup_terminado', {'status': 'concluido', 'avisos': '', 'sha256': 'f' * 64})

    def ate_testando(self):
        self.ate_restaurando()
        self.evento('restauracao_terminada', {'avisos': ''})
        return self.evento('manifesto_destino', {'manifesto': _manifesto()})

    def escalas(self):
        return [c.args for c in self.p['escalar'].call_args_list]


class CaminhoFelizTests(_Base):
    def test_migracao_completa(self):
        m = self.iniciar()
        self.assertEqual((m.status, m.no_origem, m.no_destino), ('verificando', 'no-a', 'no-b'))
        filas = sorted(c.kwargs['queue'] for c in self.p['sondar'].call_args_list)
        self.assertEqual(filas, ['no.no-a', 'no.no-b'])

        m = self.sondas_ok()
        self.assertEqual(m.status, 'manifestando')
        self.assertEqual((m.replicas_web, m.replicas_cron), (1, 1))
        self.assertEqual(self.escalas(), [('acme_web', 0), ('acme_cron', 0)])
        self.assertEqual(self.p['manifesto'].call_args.kwargs['queue'], 'no.no-a')

        m = self.evento('manifesto_origem', {'manifesto': _manifesto()})
        self.assertEqual(m.status, 'backup')
        self.assertEqual(m.backup.origem, 'migracao')
        self.assertEqual(self.p['iniciar_backup'].call_args.kwargs['kwargs'], {'migracao_id': m.pk})

        m = self.evento('backup_terminado', {'status': 'concluido', 'avisos': '', 'sha256': 'f' * 64})
        self.assertEqual(m.status, 'restaurando')
        self.assertTrue(m.host_trocado)
        self.assertEqual(FakeMotor.implantacoes, [('hostinger', True)])
        self.p['aguardar_no'].assert_called_once_with('acme_db', 'no-b', timeout=migracao.TIMEOUT_DB_DESTINO_S)
        restaurar = self.p['restaurar'].call_args.kwargs
        self.assertEqual((restaurar['queue'], restaurar['kwargs']), ('no.no-b', {'migracao_id': m.pk}))

        m = self.evento('restauracao_terminada', {'avisos': ''})
        self.assertEqual(m.status, 'conferindo')
        self.assertEqual(self.p['manifesto'].call_args.kwargs['queue'], 'no.no-b')

        m = self.evento('manifesto_destino', {'manifesto': _manifesto()})
        self.assertEqual(m.status, 'testando')
        self.assertEqual(self.escalas()[-1], ('acme_web', 1))
        self.p['traefik'].assert_not_called()  # web ligado, mas fora do Traefik
        self.assertEqual(self.p['teste_interno'].call_args.kwargs['queue'], 'no.no-b')

        m = self.evento('teste_interno', {'ok': True, 'detalhe': '/health/ 200'})
        self.assertEqual(m.status, 'concluida')
        self.assertIsNotNone(m.ponto_sem_volta_em)
        self.p['traefik'].assert_called_once_with('acme_web', True)
        self.assertEqual(self.escalas()[-1], ('acme_cron', 1))
        self.assertEqual(m.volumes_residuais, {'no': 'no-a', 'volumes': ['acme_pgdata', 'acme_media', 'acme_backups']})
        self.cliente.refresh_from_db()
        self.assertEqual(self.cliente.host, self.host_b)
        self.assertEqual(self.cliente.status, 'ativo')

    def test_eventos_repetidos_ou_fora_de_hora_sao_ignorados(self):
        self.ate_restaurando()
        self.evento('backup_terminado', {'status': 'concluido', 'avisos': '', 'sha256': 'f' * 64})
        self.evento('manifesto_destino', {'manifesto': _manifesto(linhas=999)})
        self.assertEqual(self.recarregar().status, 'restaurando')
        self.assertEqual(FakeMotor.implantacoes, [('hostinger', True)])

    def test_cliente_suspenso_termina_parado(self):
        self.replicas = {'acme_web': 0, 'acme_cron': 0}
        m = self.ate_testando()
        self.assertEqual(m.status, 'concluida')
        self.p['teste_interno'].assert_not_called()
        self.p['health'].assert_not_called()
        self.assertNotIn(('acme_web', 1), self.escalas())
        self.assertEqual(self.escalas()[-1], ('acme_cron', 0))

    def test_cliente_sem_servico_cron(self):
        self.replicas = {'acme_web': 1}
        self.ate_testando()
        m = self.evento('teste_interno', {'ok': True, 'detalhe': 'ok'})
        self.assertEqual(m.status, 'concluida')
        self.assertFalse(any(s == 'acme_cron' for s, _ in self.escalas()))


class GatesTests(_Base):
    def _gates(self):
        resultados, _ = migracao.gates_manager(self.cliente, self.host_b)
        return {c: g['ok'] for c, g in resultados.items()}

    def _so_reprova(self, codigo):
        gates = self._gates()
        self.assertFalse(gates[codigo], gates)
        self.assertEqual([c for c, ok in gates.items() if not ok], [codigo])

    def test_tudo_aprovado(self):
        self.assertTrue(all(self._gates().values()))

    def test_g1_backup_em_andamento(self):
        BackupCliente.objects.create(cliente=self.cliente, status='em_andamento')
        self._so_reprova('G1')

    def test_g1_restauracao_em_andamento(self):
        BackupCliente.objects.create(cliente=self.cliente, status='concluido', restaurando=True)
        self._so_reprova('G1')

    def test_g2_mesma_regiao(self):
        self.host_b.regiao_id = 'anapolis'
        self.host_b.save()
        gates = self._gates()
        self.assertFalse(gates['G2'])

    def test_g2_regiao_inativa(self):
        Regiao.objects.filter(slug='hostinger').update(ativo=False)
        self.host_b.refresh_from_db()
        self._so_reprova('G2')

    def test_g3_dois_nos_na_regiao_de_destino(self):
        self.nos['hostinger'] = self.nos['hostinger'] * 2
        self._so_reprova('G3')

    def test_g3_no_arm(self):
        self.nos['hostinger'] = [{**self.nos['hostinger'][0], 'arquitetura': 'aarch64'}]
        self._so_reprova('G3')

    def test_g3_no_drain(self):
        self.nos['hostinger'] = [{**self.nos['hostinger'][0], 'disponibilidade': 'Drain'}]
        self._so_reprova('G3')

    def test_g4_banco_fora_do_no_da_regiao(self):
        self.p['no_db'].return_value = 'no-x'
        self._so_reprova('G4')

    def test_g4_dois_nos_na_origem(self):
        self.nos['anapolis'] = self.nos['anapolis'] + [{**self.nos['anapolis'][0], 'hostname': 'no-a2'}]
        self._so_reprova('G4')

    def test_g6_armazenamento_com_erro(self):
        self.p['storage'].return_value = ('erro', 'bucket inexistente')
        self._so_reprova('G6')

    def test_g7_imagem_diferente_da_versao_do_cliente(self):
        self.p['imagem'].return_value = 'emporioautomacao/ararasuite-erp:0.0.142'
        self._so_reprova('G7')

    def test_g7_versao_latest_nao_e_aceita(self):
        Cliente.objects.filter(pk=self.cliente.pk).update(versao_erp='latest')
        self.cliente.refresh_from_db()
        self.p['imagem'].return_value = 'emporioautomacao/ararasuite-erp:latest'
        self._so_reprova('G7')

    def test_g8_env_sem_segredo(self):
        FakeMotor.env = {k: v for k, v in SEGREDOS.items() if k != 'EMPRESAS_CREDENCIAL_MASTER_KEY'}
        gates = self._gates()
        self.assertFalse(gates['G8'])
        self.assertFalse(gates['G13'])

    def test_g12_vigia_desativado(self):
        from django_celery_beat.models import PeriodicTask
        PeriodicTask.objects.filter(name=migracao.NOME_VIGIA).update(enabled=False)
        self._so_reprova('G12')

    def test_g13_variavel_que_a_reimplantacao_perderia(self):
        self.p['env_servico'].return_value = {**ENV_VIVO, 'FEATURE_X': '1'}
        self._so_reprova('G13')

    def test_g13_nao_mostra_valores(self):
        self.p['env_servico'].return_value = {**ENV_VIVO, 'DJANGO_SECRET_KEY': 'outra-chave-secreta'}
        resultados, _ = migracao.gates_manager(self.cliente, self.host_b)
        self.assertIn('DJANGO_SECRET_KEY', resultados['G13']['detalhe'])
        self.assertNotIn('outra-chave-secreta', resultados['G13']['detalhe'])

    def test_iniciar_recusa_sem_criar_nada(self):
        self.p['imagem'].return_value = 'x:0.0.1'
        with self.assertRaises(migracao.Recusa):
            migracao.iniciar(self.cliente, self.host_b, self.user)
        self.assertFalse(MigracaoCliente.objects.exists())
        self.p['escalar'].assert_not_called()


class SondasTests(_Base):
    def _origem(self, **extra):
        return {'lado': 'origem', 'hostname': 'no-a', 'banco_bytes': 100, 'volumes_bytes': {'media': 300, 'backups': 0}, **extra}

    def _destino(self, **extra):
        return {'lado': 'destino', 'hostname': 'no-b', 'volumes_existentes': [], 'livre_bytes': 10 ** 9, **extra}

    def _rodar(self, origem, destino):
        self.iniciar()
        self.evento('sonda', origem)
        return self.evento('sonda', destino)

    def assertRecusadaSemAlterar(self, m, trecho):
        self.assertEqual(m.status, 'falhou_sem_alteracao')
        self.assertIn(trecho, m.mensagem_erro)
        self.p['escalar'].assert_not_called()
        self.assertEqual(FakeMotor.implantacoes, [])

    def test_g9_volumes_residuais_no_destino(self):
        m = self._rodar(self._origem(), self._destino(volumes_existentes=['acme_pgdata']))
        self.assertRecusadaSemAlterar(m, 'G9')

    def test_g10_espaco_insuficiente(self):
        m = self._rodar(self._origem(), self._destino(livre_bytes=1000))
        self.assertRecusadaSemAlterar(m, 'G10')

    def test_g11_banco_acima_do_limite(self):
        m = self._rodar(self._origem(banco_bytes=migracao.LIMITE_BANCO_BYTES + 1), self._destino(livre_bytes=10 ** 12))
        self.assertRecusadaSemAlterar(m, 'G11')

    def test_g5_sonda_do_destino_com_erro(self):
        m = self._rodar(self._origem(), {'lado': 'destino', 'hostname': 'outro', 'erro': 'rodou no nó errado'})
        self.assertRecusadaSemAlterar(m, 'G5')

    def test_uma_sonda_so_nao_avanca(self):
        self.iniciar()
        m = self.evento('sonda', self._origem())
        self.assertEqual(m.status, 'verificando')


class VoltaTests(_Base):
    def test_backup_com_avisos_volta_sem_reimplantar(self):
        self.ate_backup()
        m = self.evento('backup_terminado', {'status': 'concluido', 'avisos': 'Volume acme_media nao encontrado', 'sha256': 'x'})
        self.assertEqual(m.status, 'revertida')
        self.assertEqual(FakeMotor.implantacoes, [])
        self.assertEqual(self.escalas()[-2:], [('acme_cron', 1), ('acme_web', 1)])
        self.assertIsNone(m.volumes_residuais)
        self.p['traefik'].assert_not_called()

    def test_backup_sem_sha256_volta(self):
        self.ate_backup()
        m = self.evento('backup_terminado', {'status': 'concluido', 'avisos': '', 'sha256': ''})
        self.assertEqual(m.status, 'revertida')

    def test_falha_na_implantacao_volta_para_a_origem(self):
        self.ate_backup()
        FakeMotor.falhar_executar = True
        m = self.evento('backup_terminado', {'status': 'concluido', 'avisos': '', 'sha256': 'f' * 64})
        # a volta também usa o motor: com ele quebrado, sobra intervenção
        self.assertEqual(m.status, 'exige_intervencao')
        self.assertIn('stack deploy falhou', m.mensagem_erro)

    def _volta_depois_do_destino(self, m):
        self.assertEqual(m.status, 'revertendo')
        self.assertEqual(FakeMotor.implantacoes, [('hostinger', True), ('anapolis', True)])
        self.cliente.refresh_from_db()
        self.assertEqual(self.cliente.host, self.host_a)
        self.assertEqual(self.p['manifesto'].call_args.kwargs['queue'], 'no.no-a')
        self.assertEqual(self.p['manifesto'].call_args.kwargs['args'], [m.pk, 'reversao'])
        m = self.evento('manifesto_reversao', {'manifesto': _manifesto()})
        self.assertEqual(m.status, 'revertida')
        self.p['traefik'].assert_called_with('acme_web', True)
        self.assertEqual(self.escalas()[-2:], [('acme_cron', 1), ('acme_web', 1)])
        self.assertEqual(m.volumes_residuais['no'], 'no-b')
        self.assertIsNone(m.ponto_sem_volta_em)
        return m

    def test_restauracao_com_avisos_volta(self):
        self.ate_restaurando()
        m = self.evento('restauracao_terminada', {'avisos': 'psql falhou'})
        self._volta_depois_do_destino(m)

    def test_manifesto_divergente_volta(self):
        self.ate_restaurando()
        self.evento('restauracao_terminada', {'avisos': ''})
        m = self.evento('manifesto_destino', {'manifesto': _manifesto(linhas=9)})
        self.assertIn('tabela public.a: 10 ≠ 9', m.mensagem_erro)
        self._volta_depois_do_destino(m)

    def test_teste_interno_reprovado_volta(self):
        self.ate_testando()
        m = self.evento('teste_interno', {'ok': False, 'detalhe': 'status 500'})
        self.assertIn('status 500', m.mensagem_erro)
        self._volta_depois_do_destino(m)

    def test_origem_divergente_depois_da_volta_exige_intervencao(self):
        self.ate_restaurando()
        self.evento('restauracao_terminada', {'avisos': 'x'})
        m = self.evento('manifesto_reversao', {'manifesto': _manifesto(arquivos=2)})
        self.assertEqual(m.status, 'exige_intervencao')
        self.assertIn('volume media', m.mensagem_erro)


class PontoSemVoltaTests(_Base):
    def test_falha_depois_de_abrir_nao_volta(self):
        self.ate_testando()
        self.p['health'].side_effect = TimeoutError('não respondeu')
        m = self.evento('teste_interno', {'ok': True, 'detalhe': 'ok'})
        self.assertEqual(m.status, 'exige_intervencao')
        self.assertIsNotNone(m.ponto_sem_volta_em)
        self.assertEqual(FakeMotor.implantacoes, [('hostinger', True)])  # nada de reimplantar na origem
        self.cliente.refresh_from_db()
        self.assertEqual(self.cliente.host, self.host_b)

    def test_interromper_so_antes_do_ponto_sem_volta(self):
        self.ate_restaurando()
        self.assertTrue(migracao.interromper(self.m.pk, self.user))
        self.assertEqual(self.recarregar().status, 'revertendo')

    def test_interromper_depois_de_abrir_e_recusado(self):
        self.ate_testando()
        MigracaoCliente.objects.filter(pk=self.m.pk).update(status='abrindo', ponto_sem_volta_em=timezone.now())
        self.assertFalse(migracao.interromper(self.m.pk, self.user))
        self.assertEqual(self.recarregar().status, 'abrindo')


class VigiaTests(_Base):
    def _envelhecer(self, segundos):
        MigracaoCliente.objects.filter(pk=self.m.pk).update(atividade_em=timezone.now() - timedelta(seconds=segundos))

    def test_verificacao_parada_e_recusada(self):
        self.iniciar()
        self._envelhecer(migracao.LIMITE_SEM_ATIVIDADE_S['verificando'] + 1)
        migracao.vigiar()
        self.assertEqual(self.recarregar().status, 'falhou_sem_alteracao')

    def test_etapa_parada_antes_do_ponto_sem_volta_volta(self):
        self.ate_restaurando()
        self._envelhecer(migracao.LIMITE_SEM_ATIVIDADE_PADRAO_S + 1)
        BackupCliente.objects.filter(pk=self.m.backup_id).update(atividade_em=timezone.now() - timedelta(hours=1))
        migracao.vigiar()
        self.assertEqual(self.recarregar().status, 'revertendo')

    def test_atividade_do_backup_conta_como_atividade(self):
        self.ate_restaurando()
        self._envelhecer(migracao.LIMITE_SEM_ATIVIDADE_PADRAO_S + 1)
        BackupCliente.objects.filter(pk=self.m.backup_id).update(atividade_em=timezone.now())
        migracao.vigiar()
        self.assertEqual(self.recarregar().status, 'restaurando')

    def test_abrindo_parado_pede_intervencao(self):
        self.ate_testando()
        MigracaoCliente.objects.filter(pk=self.m.pk).update(status='abrindo', ponto_sem_volta_em=timezone.now())
        self._envelhecer(migracao.LIMITE_SEM_ATIVIDADE_PADRAO_S + 1)
        migracao.vigiar()
        self.assertEqual(self.recarregar().status, 'exige_intervencao')

    def test_vigia_vem_agendado_pela_migration(self):
        from django_celery_beat.models import PeriodicTask
        t = PeriodicTask.objects.get(name=migracao.NOME_VIGIA)
        self.assertEqual((t.task, t.enabled, t.interval.every, t.interval.period),
                         ('registry.tasks.task_vigiar_migracoes', True, 1, 'minutes'))


class UnicidadeTests(_Base):
    def test_so_uma_migracao_ativa_por_cliente(self):
        self.iniciar()
        with self.assertRaises(IntegrityError), transaction.atomic():
            MigracaoCliente.objects.create(
                cliente=self.cliente, host_origem=self.host_a, host_destino=self.host_b,
                regiao_origem='anapolis', regiao_destino='hostinger',
            )

    def test_terminada_libera_nova(self):
        self.iniciar()
        MigracaoCliente.objects.filter(pk=self.m.pk).update(status='revertida')
        MigracaoCliente.objects.create(
            cliente=self.cliente, host_origem=self.host_a, host_destino=self.host_b,
            regiao_origem='anapolis', regiao_destino='hostinger',
        )

    def test_segunda_migracao_reprova_g1(self):
        self.iniciar()
        with self.assertRaises(migracao.Recusa) as ctx:
            migracao.iniciar(self.cliente, self.host_b, self.user)
        self.assertIn('G1', str(ctx.exception))


class ComparacaoTests(SimpleTestCase):
    def test_identicos(self):
        self.assertEqual(migracao.comparar_manifestos(_manifesto(), _manifesto()), [])

    def test_tabela_ausente_no_destino(self):
        b = _manifesto()
        del b['banco']['tabelas']['public.b']
        self.assertEqual(migracao.comparar_manifestos(_manifesto(), b), ['tabela public.b: 2 ≠ ausente'])

    def test_migrations_e_hash_de_volume(self):
        b = _manifesto()
        b['banco']['migrations_hash'] = 'outro'
        b['volumes']['media']['hash'] = 'outro'
        diffs = migracao.comparar_manifestos(_manifesto(), b)
        self.assertIn('django_migrations (migrations_hash) diferente', diffs)
        self.assertIn('volume media (hash): m ≠ outro', diffs)

    def test_existencia_do_volume_nao_conta_so_o_conteudo(self):
        b = _manifesto()
        b['volumes']['backups']['existe'] = False
        self.assertEqual(migracao.comparar_manifestos(_manifesto(), b), [])


class AdminTests(_Base):
    def setUp(self):
        super().setUp()
        self.c = Client()
        self.c.force_login(self.user)
        self.url = reverse('admin:registry_cliente_migrar', args=[self.cliente.pk])

    def test_formulario_lista_so_hosts_de_outra_regiao_e_mostra_gates(self):
        html = self.c.get(self.url + f'?host={self.host_b.pk}').content.decode()
        self.assertIn('host-b (Hostinger)', html)
        self.assertNotIn('host-a (Anápolis)', html)
        self.assertIn('G13', html)
        self.assertIn('Iniciar migração', html)

    def test_confirmacao_errada_nao_inicia(self):
        self.c.post(self.url, {'host': self.host_b.pk, 'confirmacao': 'outro'})
        self.assertFalse(MigracaoCliente.objects.exists())

    def test_confirmacao_certa_inicia(self):
        with self.captureOnCommitCallbacks(execute=True):
            r = self.c.post(self.url, {'host': self.host_b.pk, 'confirmacao': 'acme'})
        m = MigracaoCliente.objects.get()
        self.assertRedirects(r, reverse('admin:registry_migracaocliente_change', args=[m.pk]),
                             fetch_redirect_response=False)
        self.p['iniciar_task'].assert_called_once()

    def test_gate_reprovado_nao_inicia(self):
        self.p['imagem'].return_value = 'x:0.0.1'
        self.c.post(self.url, {'host': self.host_b.pk, 'confirmacao': 'acme'})
        self.assertFalse(MigracaoCliente.objects.exists())

    def _com_migracao_ativa(self):
        with self.captureOnCommitCallbacks(execute=True):
            self.m = migracao.iniciar(self.cliente, self.host_b, self.user)

    def test_operacoes_bloqueadas_durante_a_migracao(self):
        self._com_migracao_ativa()
        backup = BackupCliente.objects.create(cliente=self.cliente, status='concluido', arquivo_path='s3://x')
        with patch('registry.tasks.task_iniciar_backup_cliente.delay') as backup_delay, \
                patch('registry.tasks.task_restaurar_cliente.delay') as restaurar_delay, \
                patch('registry.tasks.task_provisionar_cliente.delay') as reprov_delay:
            self.c.get(reverse('admin:registry_cliente_backup', args=[self.cliente.pk]))
            self.c.get(reverse('admin:registry_cliente_restaurar', args=[self.cliente.pk, backup.pk]))
            self.c.get(reverse('admin:registry_cliente_reprovisionar', args=[self.cliente.pk]))
            self.c.get(reverse('admin:registry_cliente_aplicar_modulos', args=[self.cliente.pk]))
            self.c.get(reverse('admin:registry_cliente_destruir', args=[self.cliente.pk]))
        backup_delay.assert_not_called()
        restaurar_delay.assert_not_called()
        reprov_delay.assert_not_called()
        self.assertTrue(Cliente.objects.filter(pk=self.cliente.pk).exists())

    def test_cadastro_fica_somente_leitura(self):
        self._com_migracao_ativa()
        html = self.c.get(reverse('admin:registry_cliente_change', args=[self.cliente.pk])).content.decode()
        self.assertIn('Migração em andamento', html)
        self.assertNotIn('name="host"', html)
        self.assertNotIn('name="status"', html)

    def test_interromper_backup_da_migracao_e_recusado(self):
        b = BackupCliente.objects.create(cliente=self.cliente, origem='migracao', status='em_andamento')
        r = self.c.post(reverse('admin:registry_cliente_backup_interromper', args=[self.cliente.pk, b.pk]))
        self.assertEqual(r.json()['motivo'], 'migracao')

    def test_pagina_da_migracao_e_interromper(self):
        self._com_migracao_ativa()
        url = reverse('admin:registry_migracaocliente_change', args=[self.m.pk])
        html = self.c.get(url).content.decode()
        self.assertIn('Interromper e voltar para a origem', html)
        self.c.post(reverse('admin:registry_migracaocliente_interromper', args=[self.m.pk]))
        self.assertEqual(MigracaoCliente.objects.get(pk=self.m.pk).status, 'falhou_sem_alteracao')


class TasksAdiadasTests(_Base):
    def test_suspender_durante_a_migracao_e_adiado(self):
        from registry.tasks import task_suspender_cliente
        self.iniciar()
        with patch('registry.tasks.task_suspender_cliente.apply_async') as reagendar, \
                patch('registry.provisioning.MotorProvisionamento') as motor:
            task_suspender_cliente(str(self.cliente.pk))
        reagendar.assert_called_once_with(args=[str(self.cliente.pk)], countdown=300)
        motor.assert_not_called()
        self.cliente.refresh_from_db()
        self.assertEqual(self.cliente.status, 'ativo')


class ModoMigracaoProvisionamentoTests(TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        plano = Plano.objects.create(slug='basico-prov', nome='Basico Prov', preco_mensal='0.00')
        host = HostInfraestrutura.objects.create(nome='h', ip='10.0.0.9', regiao_id='brasilia', cpu_total=1, ram_gb_total=1)
        self.cliente = Cliente.objects.create(
            slug='acme', nome='Acme', cnpj='11.222.333/0001-44', email_contato='acme@example.com',
            subdominio='acme.ararasuite.com.br', plano=plano, host=host, status='suspenso', versao_erp='0.0.143',
        )
        base = Path(self.tmp.name)
        for alvo in ('registry.provisioning.CLIENTES_BASE',):
            p = patch(alvo, base)
            p.start()
            self.addCleanup(p.stop)
        self.dir = base / 'acme'
        self.dir.mkdir()
        p = patch('registry.provisioning.docker.from_env')
        p.start()
        self.addCleanup(p.stop)
        self.run = patch('registry.provisioning.subprocess.run',
                         return_value=SimpleNamespace(returncode=0, stdout='', stderr='')).start()
        self.addCleanup(patch.stopall)

    def _motor(self):
        from registry.provisioning import MotorProvisionamento
        return MotorProvisionamento(self.cliente)

    def test_sem_segredos_no_env_recusa_antes_de_implantar(self):
        (self.dir / '.env').write_text('POSTGRES_PASSWORD=pg\n')
        with self.assertRaises(RuntimeError):
            self._motor().executar(modo_migracao=True)
        self.run.assert_not_called()
        self.assertEqual((self.dir / '.env').read_text(), 'POSTGRES_PASSWORD=pg\n')

    def test_implanta_com_app_parado_e_traefik_desligado_sem_mexer_no_cliente(self):
        (self.dir / '.env').write_text('\n'.join(f'{k}={v}' for k, v in SEGREDOS.items()))
        self._motor().executar(modo_migracao=True)

        deploy = self.run.call_args.args[0]
        self.assertEqual(deploy[:3], ['docker', 'stack', 'deploy'])
        self.assertTrue(deploy[deploy.index('-c') + 1].endswith('stack-migracao.yml'))
        migr = (self.dir / 'stack-migracao.yml').read_text()
        normal = (self.dir / 'stack.yml').read_text()
        self.assertEqual(migr.count('replicas: 0'), 2)
        self.assertIn('"traefik.enable=false"', migr)
        self.assertIn('node.labels.region == brasilia', migr)
        self.assertEqual(normal.count('replicas: 1'), 2)
        self.assertIn('"traefik.enable=true"', normal)
        env = dict(l.split('=', 1) for l in (self.dir / '.env').read_text().splitlines())
        for k, v in SEGREDOS.items():
            self.assertEqual(env[k], v)
        self.cliente.refresh_from_db()
        self.assertEqual(self.cliente.status, 'suspenso')
        self.assertIsNone(self.cliente.data_ativacao)


class TasksDoNoTests(_Base):
    def test_sonda_no_no_errado_responde_erro(self):
        from registry.tasks import task_migracao_sondar
        self.iniciar()
        with patch.dict('os.environ', {'NODE_HOSTNAME': 'intruso'}), \
                patch('registry.tasks.task_migracao_avancar.apply_async') as avancar:
            task_migracao_sondar(self.m.pk, 'destino')
        dados = avancar.call_args.kwargs['args'][2]
        self.assertIn('não em no-b', dados['erro'])

    def test_sonda_do_destino_mede_volumes_e_espaco(self):
        from registry.tasks import task_migracao_sondar
        self.iniciar()
        with patch.dict('os.environ', {'NODE_HOSTNAME': 'no-b'}), \
                patch('registry.volumes.volume_existe', side_effect=lambda s, v: v == 'media'), \
                patch('registry.volumes.espaco_livre_docker', return_value=123), \
                patch('registry.tasks.task_migracao_avancar.apply_async') as avancar:
            task_migracao_sondar(self.m.pk, 'destino')
        dados = avancar.call_args.kwargs['args'][2]
        self.assertEqual((dados['volumes_existentes'], dados['livre_bytes']), (['acme_media'], 123))

    def test_manifesto_devolve_banco_e_volumes(self):
        from registry.tasks import task_migracao_manifesto
        self.iniciar()
        with patch('registry.nos.container_local', return_value='db-ctr'), \
                patch('registry.volumes.pg_pronto', return_value=True), \
                patch('registry.volumes.manifesto_banco', return_value=_manifesto()['banco']), \
                patch('registry.volumes.manifesto_volume', side_effect=lambda s, v: _manifesto()['volumes'][v]), \
                patch('registry.tasks.task_migracao_avancar.apply_async') as avancar:
            task_migracao_manifesto(self.m.pk, 'destino')
        args = avancar.call_args.kwargs['args']
        self.assertEqual(args[1], 'manifesto_destino')
        self.assertEqual(args[2], {'manifesto': _manifesto()})
