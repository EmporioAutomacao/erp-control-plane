"""Migração de um cliente para outro nó do Swarm.

Especificação: `docs/registry/migracao-cliente-entre-servidores.md`. Resumo das garantias:

- o ERP do destino nunca atende tráfego antes de restaurado e conferido (sobe com web/cron em 0
  e o Traefik desligado) — um agente que renovasse o token num ERP vazio exigiria reativação;
- ponto sem volta = habilitar o Traefik no destino; antes dele qualquer falha volta para a origem,
  depois dele nunca há volta automática (`exige_intervencao`);
- nada é apagado na origem; a integridade é provada por manifesto (contagem exata de linhas por
  tabela, `django_migrations`, arquivos/bytes/hash de cada volume) e pelo SHA-256 do pacote.

Este módulo roda no **manager** (fila `celery`). O que precisa do daemon do nó do cliente roda em
tasks na fila `no.<hostname>` e devolve o resultado por `task_migracao_avancar`.
"""
import hashlib
import json
import logging
import os
import time
import urllib.request

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from . import swarm
from .log_operacao import LogOperacao
from .volumes import VOLUMES_STACK

logger = logging.getLogger(__name__)

LIMITE_SEM_ATIVIDADE_S = {'verificando': 180}
LIMITE_SEM_ATIVIDADE_PADRAO_S = 600
TIMEOUT_PARAR_S = 180
TIMEOUT_DB_DESTINO_S = 300
TIMEOUT_HEALTH_S = 300
FATOR_ESPACO_LIVRE = 3
LIMITE_BANCO_BYTES = int(os.getenv('MIGRACAO_LIMITE_BANCO_BYTES', str(2 * 1024 ** 3)))
LIMITE_VOLUMES_BYTES = int(os.getenv('MIGRACAO_LIMITE_VOLUMES_BYTES', str(20 * 1024 ** 3)))
NOME_VIGIA = 'Vigia das migrações de servidor'


class Recusa(Exception):
    """Gate reprovado: nada foi alterado."""


def _modelo():
    from .models import MigracaoCliente
    return MigracaoCliente


def log(migracao_id, texto):
    LogOperacao(migracao_id, modelo=_modelo()).linha(texto)


def transicionar(migracao_id, de, para, **campos):
    """Avança o status só se ele ainda for um dos de `de` (reentrada/eventos atrasados viram no-op)."""
    de = (de,) if isinstance(de, str) else tuple(de)
    n = _modelo().objects.filter(pk=migracao_id, status__in=de).update(
        status=para, atividade_em=timezone.now(), **campos,
    )
    if n:
        log(migracao_id, f'Etapa: {dict(_modelo().STATUS_CHOICES)[para]}.')
    return n == 1


def _fila(hostname):
    from .nos import fila_do_no
    return fila_do_no(hostname)


def _versao_concreta(versao):
    partes = (versao or '').split('.')
    return len(partes) == 3 and all(p.isdigit() for p in partes)


def _hash_segredos(env):
    from .provisioning import SEGREDOS_ENV
    return hashlib.sha256('\n'.join(env.get(k, '') for k in SEGREDOS_ENV).encode()).hexdigest()


# ── Gates do manager ─────────────────────────────────────────────────────────

def operacao_ativa(cliente, ignorar_migracao=None):
    """Descrição da operação em andamento no cliente (migração, backup ou restauração), ou ''."""
    from .models import BackupCliente
    migracoes = _modelo().objects.filter(cliente=cliente).exclude(status__in=_modelo().STATUS_FINAIS)
    if ignorar_migracao is not None:
        migracoes = migracoes.exclude(pk=ignorar_migracao)
    if migracoes.exists():
        return 'há uma migração de servidor em andamento'
    if BackupCliente.objects.filter(cliente=cliente).filter(Q(status='em_andamento') | Q(restaurando=True)).exists():
        return 'há um backup ou uma restauração em andamento'
    return ''


def gates_manager(cliente, host_destino, ignorar_migracao=None):
    """Gates que o manager responde sozinho. Devolve (resultados, contexto).

    resultados: {codigo: {'ok': bool, 'detalhe': str}}; contexto: nós, réplicas e segredos.
    """
    from . import storage_backup
    from .nos import hostname_do_no
    from .provisioning import SEGREDOS_ENV, MotorProvisionamento

    r = {}
    ctx = {}
    slug = cliente.slug

    def marca(codigo, ok, detalhe):
        r[codigo] = {'ok': bool(ok), 'detalhe': detalhe}

    em_andamento = operacao_ativa(cliente, ignorar_migracao)
    marca('G1', not em_andamento, em_andamento or 'Nenhuma operação em andamento no cliente.')

    host_origem = cliente.host
    regiao_origem = host_origem.regiao_id if host_origem else ''
    regiao_destino = host_destino.regiao_id
    ctx.update(regiao_origem=regiao_origem, regiao_destino=regiao_destino)
    if host_origem is None:
        marca('G2', False, 'O cliente não tem host de origem cadastrado.')
    elif not host_destino.ativo:
        marca('G2', False, f'O host de destino {host_destino.nome} está inativo.')
    elif not host_destino.regiao.ativo:
        marca('G2', False, f'A região {regiao_destino} está inativa.')
    elif regiao_destino == regiao_origem:
        marca('G2', False, f'O destino está na mesma região da origem ({regiao_origem}).')
    else:
        marca('G2', True, f'{host_origem.nome} ({regiao_origem}) → {host_destino.nome} ({regiao_destino}).')

    try:
        nos_destino = swarm.nos_da_regiao(regiao_destino)
        aptos = [n for n in nos_destino if swarm.no_apto(n)]
        if len(nos_destino) == 1 and aptos:
            ctx['no_destino'] = aptos[0]['hostname']
            marca('G3', True, f'Nó de destino: {aptos[0]["hostname"]} (Ready, Active, x86_64).')
        else:
            marca('G3', False, f'A região {regiao_destino} precisa de exatamente 1 nó Ready/Active/x86_64; '
                               f'encontrados: {nos_destino or "nenhum"}.')
    except Exception as exc:
        marca('G3', False, f'Não foi possível consultar os nós do Swarm: {exc}')

    try:
        nos_origem = swarm.nos_da_regiao(regiao_origem) if regiao_origem else []
        no_db = hostname_do_no(slug, 'db')
        if len(nos_origem) == 1 and no_db == nos_origem[0]['hostname'] and nos_origem[0]['status'] == 'Ready':
            ctx['no_origem'] = no_db
            marca('G4', True, f'Nó de origem: {no_db}, onde roda {slug}_db.')
        else:
            marca('G4', False, f'A região {regiao_origem or "?"} precisa de exatamente 1 nó Ready e ele deve '
                               f'rodar {slug}_db; nós: {nos_origem or "nenhum"}, {slug}_db em: {no_db or "nenhum nó"}.')
    except Exception as exc:
        marca('G4', False, f'Não foi possível consultar o nó de origem: {exc}')

    status, msg = storage_backup.testar_conexao()
    marca('G6', status in ('conectado', 'atencao'), f'Armazenamento: {status} — {msg}')

    imagem = swarm.imagem_do_servico(f'{slug}_web') or ''
    tag = imagem.rpartition(':')[2] if ':' in imagem else ''
    if not _versao_concreta(cliente.versao_erp):
        marca('G7', False, f'versao_erp do cliente "{cliente.versao_erp}" não é uma versão X.Y.Z.')
    elif tag != cliente.versao_erp:
        marca('G7', False, f'{slug}_web roda "{imagem or "nada"}", mas o cliente está na versão {cliente.versao_erp}.')
    else:
        marca('G7', True, f'{slug}_web na versão {tag}.')
    ctx['versao_erp'] = cliente.versao_erp

    motor = MotorProvisionamento.__new__(MotorProvisionamento)
    motor.cliente = cliente
    env_arquivo = motor.ler_env_existente()
    faltando = [k for k in SEGREDOS_ENV if not env_arquivo.get(k)]
    marca('G8', not faltando, f'.env sem {", ".join(faltando)}.' if faltando else '.env com os segredos obrigatórios.')
    ctx['hash_segredos'] = _hash_segredos(env_arquivo)

    from django_celery_beat.models import PeriodicTask
    vigia_ok = PeriodicTask.objects.filter(name=NOME_VIGIA, enabled=True).exists()
    marca('G12', vigia_ok, 'Vigia agendado.' if vigia_ok else f'Tarefa periódica "{NOME_VIGIA}" ausente ou desativada.')

    if not faltando:
        try:
            vivo = swarm.env_do_servico(f'{slug}_web')
            esperado = motor.montar_env(env_arquivo)
            divergentes = sorted(k for k, v in vivo.items() if esperado.get(k) != v)
            # Só os nomes: os valores podem ser segredos.
            marca('G13', not divergentes,
                  f'Variáveis do {slug}_web que a reimplantação mudaria ou perderia: {", ".join(divergentes)}. '
                  'Use "Aplicar Configurações" ou ajuste o cadastro antes de migrar.' if divergentes
                  else f'Ambiente do {slug}_web igual ao que a reimplantação gera.')
        except Exception as exc:
            marca('G13', False, f'Não foi possível comparar o ambiente de {slug}_web: {exc}')
    else:
        marca('G13', False, 'Sem .env completo não há como comparar o ambiente.')

    return r, ctx


def avaliar_sondas(sondas):
    """Gates que dependem das sondas dos nós (G5, G9, G10, G11)."""
    r = {}
    origem, destino = sondas.get('origem') or {}, sondas.get('destino') or {}
    if origem.get('erro'):
        r['G5o'] = {'ok': False, 'detalhe': f'Sonda da origem falhou: {origem["erro"]}'}
    if destino.get('erro'):
        r['G5'] = {'ok': False, 'detalhe': f'Sonda do destino falhou: {destino["erro"]}'}
        return r
    r['G5'] = {'ok': True, 'detalhe': f'Worker do destino {destino.get("hostname")} respondeu.'}
    if origem.get('erro'):
        return r
    existentes = destino.get('volumes_existentes') or []
    r['G9'] = {'ok': not existentes, 'detalhe': (
        f'O destino já tem os volumes {", ".join(existentes)} (resíduo de outra migração?). Remova antes.'
        if existentes else 'Destino sem volumes do cliente.')}
    banco = int(origem.get('banco_bytes') or 0)
    volumes = sum(int(v or 0) for v in (origem.get('volumes_bytes') or {}).values())
    necessario = FATOR_ESPACO_LIVRE * (banco + volumes)
    livre = int(destino.get('livre_bytes') or 0)
    r['G10'] = {'ok': livre >= necessario, 'detalhe': (
        f'Livre no destino: {livre} bytes; necessário: {necessario} ({FATOR_ESPACO_LIVRE}× banco+volumes).')}
    r['G11'] = {'ok': banco <= LIMITE_BANCO_BYTES and volumes <= LIMITE_VOLUMES_BYTES, 'detalhe': (
        f'Banco {banco} bytes (limite {LIMITE_BANCO_BYTES}); volumes {volumes} bytes (limite {LIMITE_VOLUMES_BYTES}).')}
    return r


def reprovados(gates):
    return [f'{c}: {g["detalhe"]}' for c, g in sorted(gates.items()) if not g['ok']]


# ── Início ──────────────────────────────────────────────────────────────────

def iniciar(cliente, host_destino, usuario=None):
    """Cria a migração e dispara as verificações. Levanta Recusa se um gate do manager reprovar."""
    from .models import Cliente
    from .tasks import task_migracao_iniciar

    with transaction.atomic():
        cliente = Cliente.objects.select_for_update().get(pk=cliente.pk)
        gates, ctx = gates_manager(cliente, host_destino)
        falhas = reprovados(gates)
        if falhas:
            raise Recusa('\n'.join(falhas))
        m = _modelo().objects.create(
            cliente=cliente, host_origem=cliente.host, host_destino=host_destino,
            regiao_origem=ctx['regiao_origem'], regiao_destino=ctx['regiao_destino'],
            no_origem=ctx['no_origem'], no_destino=ctx['no_destino'], versao_erp=ctx['versao_erp'],
            verificacoes={'gates': gates, 'hash_segredos': ctx['hash_segredos']},
            iniciada_por=usuario, atividade_em=timezone.now(),
        )
        transaction.on_commit(lambda: task_migracao_iniciar.apply_async(args=[m.pk], queue='celery'))
    log(m.pk, f'Migração solicitada: {m.regiao_origem} ({m.no_origem}) → {m.regiao_destino} ({m.no_destino}). '
              'Gates do manager aprovados; sondando os nós…')
    return m


def disparar_sondas(migracao_id):
    from .tasks import task_migracao_sondar
    m = _modelo().objects.get(pk=migracao_id)
    for lado, no in (('origem', m.no_origem), ('destino', m.no_destino)):
        task_migracao_sondar.apply_async(args=[migracao_id, lado], queue=_fila(no))
    log(migracao_id, f'Sondas enviadas às filas {_fila(m.no_origem)} e {_fila(m.no_destino)}.')


# ── Eventos ─────────────────────────────────────────────────────────────────

def avancar(migracao_id, evento, dados):
    """Recebe o resultado de uma etapa e executa a próxima. Eventos fora de hora são ignorados."""
    m = _modelo().objects.get(pk=migracao_id)
    dados = dados or {}
    tratadores = {
        ('verificando', 'sonda'): _ao_receber_sonda,
        ('manifestando', 'manifesto_origem'): _ao_manifestar_origem,
        ('backup', 'backup_terminado'): _ao_terminar_backup,
        ('restaurando', 'restauracao_terminada'): _ao_terminar_restauracao,
        ('conferindo', 'manifesto_destino'): _ao_manifestar_destino,
        ('testando', 'teste_interno'): _ao_testar,
        ('revertendo', 'manifesto_reversao'): _ao_manifestar_reversao,
    }
    tratador = tratadores.get((m.status, evento))
    if tratador is None:
        logger.info('Migracao %s: evento %s ignorado no status %s', migracao_id, evento, m.status)
        return
    m.atividade_em = timezone.now()
    _modelo().objects.filter(pk=migracao_id).update(atividade_em=m.atividade_em)
    try:
        tratador(m, dados)
    except Exception as exc:
        logger.exception('Migracao %s: falha ao tratar %s', migracao_id, evento)
        falhar(migracao_id, f'Erro inesperado em {evento}: {exc}')


def _ao_receber_sonda(m, dados):
    lado = dados.get('lado')
    with transaction.atomic():
        m = _modelo().objects.select_for_update().get(pk=m.pk)
        if m.status != 'verificando':
            return
        verificacoes = dict(m.verificacoes or {})
        sondas = dict(verificacoes.get('sondas') or {})
        sondas[lado] = dados
        verificacoes['sondas'] = sondas
        _modelo().objects.filter(pk=m.pk).update(verificacoes=verificacoes)
        completas = 'origem' in sondas and 'destino' in sondas
    log(m.pk, f'Sonda da {lado} recebida ({dados.get("hostname") or "?"}).')
    if not completas:
        return
    gates = avaliar_sondas(sondas)
    verificacoes['gates'] = {**verificacoes.get('gates', {}), **gates}
    _modelo().objects.filter(pk=m.pk).update(verificacoes=verificacoes)
    falhas = reprovados(gates)
    if falhas:
        falhar(m.pk, 'Verificações dos nós reprovadas:\n' + '\n'.join(falhas))
        return
    log(m.pk, 'Todos os gates aprovados.')
    congelar(m.pk)


def congelar(migracao_id):
    from .tasks import task_migracao_manifesto
    if not transicionar(migracao_id, 'verificando', 'congelando'):
        return
    m = _modelo().objects.get(pk=migracao_id)
    slug = m.cliente.slug
    web, cron = swarm.replicas(f'{slug}_web'), swarm.replicas(f'{slug}_cron')
    _modelo().objects.filter(pk=migracao_id).update(replicas_web=web or 0, replicas_cron=cron)
    log(migracao_id, f'Réplicas originais: web={web}, cron={cron}. Parando o ERP na origem…')
    swarm.escalar(f'{slug}_web', 0)
    if cron is not None:
        swarm.escalar(f'{slug}_cron', 0)
    swarm.aguardar_parado(f'{slug}_web', timeout=TIMEOUT_PARAR_S)
    if cron is not None:
        swarm.aguardar_parado(f'{slug}_cron', timeout=TIMEOUT_PARAR_S)
    log(migracao_id, 'Web e cron parados: o banco e os volumes não recebem mais escritas.')
    if transicionar(migracao_id, 'congelando', 'manifestando'):
        task_migracao_manifesto.apply_async(args=[migracao_id, 'origem'], queue=_fila(m.no_origem))


def _ao_manifestar_origem(m, dados):
    from .models import BackupCliente
    from .tasks import task_iniciar_backup_cliente
    if dados.get('erro'):
        raise RuntimeError(f'Manifesto da origem: {dados["erro"]}')
    _modelo().objects.filter(pk=m.pk).update(manifesto_origem=dados['manifesto'])
    log(m.pk, f'Manifesto da origem: {_resumo_manifesto(dados["manifesto"])}.')
    backup = BackupCliente.objects.create(cliente=m.cliente, origem='migracao', status='em_andamento')
    if transicionar(m.pk, 'manifestando', 'backup', backup=backup):
        task_iniciar_backup_cliente.apply_async(
            args=[str(m.cliente_id), backup.pk], kwargs={'migracao_id': m.pk}, queue='celery',
        )
        log(m.pk, f'Backup #{backup.pk} solicitado.')


def _ao_terminar_backup(m, dados):
    if dados.get('status') != 'concluido' or dados.get('avisos') or not dados.get('sha256'):
        raise RuntimeError(f'Backup não aproveitável (status {dados.get("status")}, '
                           f'avisos: {dados.get("avisos") or "nenhum"}, sha256: {dados.get("sha256") or "ausente"}).')
    log(m.pk, f'Backup concluído sem avisos (SHA-256 {dados["sha256"][:16]}…).')
    if transicionar(m.pk, 'backup', 'implantando_destino'):
        implantar_destino(m.pk)


def _implantar(migracao_id, host, no_esperado):
    """Aponta o cliente para `host` e implanta o stack em modo migração; espera o banco no nó certo."""
    from .models import Cliente
    from .provisioning import MotorProvisionamento
    m = _modelo().objects.get(pk=migracao_id)
    Cliente.objects.filter(pk=m.cliente_id).update(host=host)
    cliente = Cliente.objects.get(pk=m.cliente_id)
    motor = MotorProvisionamento(cliente)
    motor.executar(modo_migracao=True)
    if _hash_segredos(motor.ler_env_existente()) != (m.verificacoes or {}).get('hash_segredos'):
        raise RuntimeError('Os segredos do .env mudaram durante a implantação.')
    log(migracao_id, f'Stack implantado na região {host.regiao_id}; aguardando {cliente.slug}_db em {no_esperado}…')
    swarm.aguardar_no_do_servico(f'{cliente.slug}_db', no_esperado, timeout=TIMEOUT_DB_DESTINO_S)
    log(migracao_id, f'{cliente.slug}_db rodando em {no_esperado}.')


def implantar_destino(migracao_id):
    from .models import BackupCliente
    from .tasks import task_restaurar_no
    m = _modelo().objects.get(pk=migracao_id)
    _modelo().objects.filter(pk=migracao_id).update(host_trocado=True)
    _implantar(migracao_id, m.host_destino, m.no_destino)
    LogOperacao(m.backup_id, 'log_restauracao').reiniciar()
    BackupCliente.objects.filter(pk=m.backup_id).update(restaurando=True, progresso=0)
    if transicionar(migracao_id, 'implantando_destino', 'restaurando'):
        task_restaurar_no.apply_async(
            args=[str(m.cliente_id), m.backup_id], kwargs={'migracao_id': migracao_id}, queue=_fila(m.no_destino),
        )
        log(migracao_id, f'Restauração enviada à fila {_fila(m.no_destino)}.')


def _ao_terminar_restauracao(m, dados):
    from .tasks import task_migracao_manifesto
    if dados.get('avisos'):
        raise RuntimeError(f'Restauração com avisos: {dados["avisos"]}')
    log(m.pk, 'Restauração concluída sem avisos.')
    if transicionar(m.pk, 'restaurando', 'conferindo'):
        task_migracao_manifesto.apply_async(args=[m.pk, 'destino'], queue=_fila(m.no_destino))


def comparar_manifestos(a, b):
    """Diferenças entre dois manifestos (lista vazia = idênticos)."""
    diffs = []
    ta, tb = a['banco']['tabelas'], b['banco']['tabelas']
    for tabela in sorted(set(ta) | set(tb)):
        if ta.get(tabela) != tb.get(tabela):
            diffs.append(f'tabela {tabela}: {ta.get(tabela, "ausente")} ≠ {tb.get(tabela, "ausente")}')
    for campo in ('migrations', 'migrations_hash'):
        if a['banco'][campo] != b['banco'][campo]:
            diffs.append(f'django_migrations ({campo}) diferente')
    for volume in sorted(set(a['volumes']) | set(b['volumes'])):
        va, vb = a['volumes'].get(volume) or {}, b['volumes'].get(volume) or {}
        for campo in ('arquivos', 'bytes', 'hash'):
            if va.get(campo) != vb.get(campo):
                diffs.append(f'volume {volume} ({campo}): {va.get(campo)} ≠ {vb.get(campo)}')
    return diffs


def _resumo_manifesto(man):
    linhas = sum(man['banco']['tabelas'].values())
    vols = ', '.join(f'{v} {d.get("arquivos", 0)} arq.' for v, d in sorted(man['volumes'].items()))
    return f'{len(man["banco"]["tabelas"])} tabelas, {linhas} linhas, {man["banco"]["migrations"]} migrations; {vols}'


def _ao_manifestar_destino(m, dados):
    from .tasks import task_migracao_teste_interno
    if dados.get('erro'):
        raise RuntimeError(f'Manifesto do destino: {dados["erro"]}')
    _modelo().objects.filter(pk=m.pk).update(manifesto_destino=dados['manifesto'])
    diffs = comparar_manifestos(m.manifesto_origem, dados['manifesto'])
    if diffs:
        raise RuntimeError('Destino diferente da origem:\n' + '\n'.join(diffs[:50]))
    log(m.pk, f'Destino idêntico à origem: {_resumo_manifesto(dados["manifesto"])}.')
    if not transicionar(m.pk, 'conferindo', 'testando'):
        return
    if m.replicas_web:
        swarm.escalar(f'{m.cliente.slug}_web', m.replicas_web)
        log(m.pk, f'Web ligado no destino com {m.replicas_web} réplica(s), ainda fora do Traefik. Testando…')
        task_migracao_teste_interno.apply_async(args=[m.pk], queue=_fila(m.no_destino))
    else:
        log(m.pk, 'Cliente estava com o web parado: sem teste interno.')
        abrir(m.pk)


def _ao_testar(m, dados):
    if not dados.get('ok'):
        raise RuntimeError(f'Teste interno do destino falhou: {dados.get("detalhe")}')
    log(m.pk, f'Teste interno aprovado: {dados.get("detalhe")}.')
    abrir(m.pk)


def health_publico(cliente, versao, timeout=TIMEOUT_HEALTH_S):
    """Espera `https://<subdominio>/health/` responder 200 com a versão esperada."""
    url = f'https://{cliente.subdominio}/health/'
    limite = time.monotonic() + timeout
    ultimo = ''
    while time.monotonic() < limite:
        try:
            resp = urllib.request.urlopen(url, timeout=10)
            corpo = json.loads(resp.read().decode('utf-8') or '{}')
            if resp.status == 200 and str(corpo.get('version')) == versao:
                return f'{url} → 200, versão {versao}'
            ultimo = f'status {resp.status}, versão {corpo.get("version")}'
        except Exception as exc:
            ultimo = str(exc)
        time.sleep(10)
    raise TimeoutError(f'{url} não respondeu 200 com a versão {versao} em {timeout}s (último: {ultimo})')


def abrir(migracao_id):
    """Ponto sem volta: liga o Traefik no destino. Daqui em diante não há volta automática."""
    if not transicionar(migracao_id, 'testando', 'abrindo', ponto_sem_volta_em=timezone.now()):
        return
    m = _modelo().objects.get(pk=migracao_id)
    slug = m.cliente.slug
    log(migracao_id, 'PONTO SEM VOLTA: habilitando o Traefik no destino.')
    try:
        swarm.definir_traefik(f'{slug}_web', True)
        if m.replicas_cron is not None:
            swarm.escalar(f'{slug}_cron', m.replicas_cron)
        if m.replicas_web:
            log(migracao_id, health_publico(m.cliente, m.versao_erp))
    except Exception as exc:
        intervencao(migracao_id, f'Falha depois do ponto sem volta: {exc}')
        return
    concluir(migracao_id)


def concluir(migracao_id):
    m = _modelo().objects.get(pk=migracao_id)
    _remover_stack_migracao(m.cliente.slug)
    residuais = {'no': m.no_origem, 'volumes': [f'{m.cliente.slug}_{v}' for v in VOLUMES_STACK]}
    if transicionar(migracao_id, 'abrindo', 'concluida', concluida_em=timezone.now(), volumes_residuais=residuais):
        log(migracao_id, f'CONCLUÍDA. Volumes antigos mantidos em {m.no_origem}: {", ".join(residuais["volumes"])}.')


def _remover_stack_migracao(slug):
    from .provisioning import CLIENTES_BASE
    try:
        (CLIENTES_BASE / slug / 'stack-migracao.yml').unlink(missing_ok=True)
    except OSError:
        pass


# ── Falha, volta e intervenção ──────────────────────────────────────────────

def falhar(migracao_id, motivo):
    """Decide o destino de uma falha conforme a etapa."""
    m = _modelo().objects.get(pk=migracao_id)
    if m.status in _modelo().STATUS_FINAIS:
        return
    if m.status == 'verificando':
        if transicionar(migracao_id, 'verificando', 'falhou_sem_alteracao',
                        mensagem_erro=motivo, concluida_em=timezone.now()):
            log(migracao_id, f'RECUSADA (nada foi alterado): {motivo}')
        return
    if m.status in _modelo().STATUS_ANTES_DO_PONTO_SEM_VOLTA:
        reverter(migracao_id, motivo)
        return
    intervencao(migracao_id, motivo)


def intervencao(migracao_id, motivo):
    m = _modelo().objects.get(pk=migracao_id)
    if m.status in _modelo().STATUS_FINAIS:
        return
    _modelo().objects.filter(pk=migracao_id).exclude(status__in=_modelo().STATUS_FINAIS).update(
        status='exige_intervencao', mensagem_erro=motivo, atividade_em=timezone.now(), concluida_em=timezone.now(),
    )
    log(migracao_id, f'EXIGE INTERVENÇÃO: {motivo} — veja o runbook em docs/registry/migracao-cliente-entre-servidores.md.')
    logger.error('Migracao %s exige intervencao: %s', migracao_id, motivo)


def reverter(migracao_id, motivo):
    """Volta para a origem. Só antes do ponto sem volta."""
    from .tasks import task_migracao_manifesto
    if not transicionar(migracao_id, _modelo().STATUS_ANTES_DO_PONTO_SEM_VOLTA, 'revertendo', mensagem_erro=motivo):
        m = _modelo().objects.get(pk=migracao_id)
        if m.status not in _modelo().STATUS_FINAIS and m.status != 'revertendo':
            intervencao(migracao_id, motivo)
        return
    m = _modelo().objects.get(pk=migracao_id)
    slug = m.cliente.slug
    log(migracao_id, f'FALHA: {motivo}')
    log(migracao_id, 'Voltando para a origem (o banco e os volumes da origem não foram alterados).')
    try:
        if m.host_trocado:
            for servico in ('web', 'cron'):
                if swarm.replicas(f'{slug}_{servico}') is not None:
                    swarm.escalar(f'{slug}_{servico}', 0)
            _implantar(migracao_id, m.host_origem, m.no_origem)
            task_migracao_manifesto.apply_async(args=[migracao_id, 'reversao'], queue=_fila(m.no_origem))
            log(migracao_id, 'Conferindo se os dados da origem estão intactos…')
        else:
            finalizar_reversao(migracao_id)
    except Exception as exc:
        logger.exception('Migracao %s: falha na volta', migracao_id)
        intervencao(migracao_id, f'A volta para a origem falhou: {exc} (motivo original: {motivo})')


def _ao_manifestar_reversao(m, dados):
    if dados.get('erro'):
        raise RuntimeError(f'Manifesto da origem após a volta: {dados["erro"]}')
    diffs = comparar_manifestos(m.manifesto_origem, dados['manifesto']) if m.manifesto_origem else []
    if diffs:
        intervencao(m.pk, 'Depois da volta, a origem não confere com o manifesto inicial:\n' + '\n'.join(diffs[:50]))
        return
    log(m.pk, 'Origem confere com o manifesto inicial.')
    finalizar_reversao(m.pk)


def finalizar_reversao(migracao_id):
    m = _modelo().objects.get(pk=migracao_id)
    slug = m.cliente.slug
    try:
        if m.host_trocado:
            swarm.definir_traefik(f'{slug}_web', True)
            _remover_stack_migracao(slug)
        if m.replicas_cron is not None:
            swarm.escalar(f'{slug}_cron', m.replicas_cron)
        if m.replicas_web is not None:
            swarm.escalar(f'{slug}_web', m.replicas_web)
        if m.replicas_web:
            log(migracao_id, health_publico(m.cliente, m.versao_erp))
    except Exception as exc:
        intervencao(migracao_id, f'A volta para a origem falhou ao religar: {exc}')
        return
    residuais = (
        {'no': m.no_destino, 'volumes': [f'{slug}_{v}' for v in VOLUMES_STACK]} if m.host_trocado else None
    )
    if transicionar(migracao_id, 'revertendo', 'revertida', concluida_em=timezone.now(), volumes_residuais=residuais):
        extra = f' Volumes criados em {m.no_destino} ficaram para limpeza.' if residuais else ''
        log(migracao_id, f'REVERTIDA: o cliente continua na origem, com os dados de antes.{extra}')


def interromper(migracao_id, usuario=None):
    """Pedido manual de parada: só antes do ponto sem volta (vira volta para a origem)."""
    m = _modelo().objects.get(pk=migracao_id)
    if m.status not in _modelo().STATUS_ANTES_DO_PONTO_SEM_VOLTA:
        return False
    quem = getattr(usuario, 'username', '') or 'usuário'
    falhar(migracao_id, f'Interrompida manualmente por {quem}.')
    return True


def vigiar():
    """Etapas sem atividade além do limite: antes do ponto sem volta, volta; depois, intervenção."""
    from .models import BackupCliente
    agora = timezone.now()
    for m in _modelo().objects.exclude(status__in=_modelo().STATUS_FINAIS):
        ultimo = m.atividade_em or m.iniciada_em
        if m.backup_id and m.status in ('backup', 'restaurando'):
            b = BackupCliente.objects.filter(pk=m.backup_id).values_list('atividade_em', flat=True).first()
            if b and b > ultimo:
                ultimo = b
        limite = LIMITE_SEM_ATIVIDADE_S.get(m.status, LIMITE_SEM_ATIVIDADE_PADRAO_S)
        parado = (agora - ultimo).total_seconds()
        if parado < limite:
            continue
        motivo = f'Etapa "{m.get_status_display()}" sem atividade há {int(parado)}s (vigia).'
        if m.status == 'revertendo':
            intervencao(m.pk, motivo)
        else:
            falhar(m.pk, motivo)
