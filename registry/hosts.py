"""Sinalizador de saúde dos Hosts de Infraestrutura.

Duas fontes, porque o Swarm dizer que o nó está `Ready` não prova que o CP consegue trabalhar nele:

1. Swarm (manager): o nó existe, está Ready/Active, é x86_64, tem o label da região do host e o endereço
   cadastrado (`verificar_host`).
2. Worker do CP no nó: `task_pingar_host` roda na fila `no.<hostname>` e grava `agente_respondeu_em`, o disco
   livre do Docker e quantos containers de clientes rodam ali (`registrar_ping`). Sem resposta recente,
   backups, restaurações e migrações nesse nó não andam.

Tudo é gravado com `filter().update()` (seguro dentro de tasks Celery).
"""
from django.utils import timezone

from . import swarm

LIMITE_SEM_RESPOSTA_S = 15 * 60
DISCO_LIVRE_MINIMO = 0.15


def _modelo():
    from .models import HostInfraestrutura
    return HostInfraestrutura


def localizar_no(host):
    """(dados_do_no, aviso). Usa o `swarm_node_id`; sem ele, o único nó com o label da região."""
    if host.swarm_node_id:
        no = swarm.inspecionar_no(host.swarm_node_id)
        if no is None:
            return None, f'Nó {host.swarm_node_id} não existe no Swarm.'
        return no, ''
    candidatos = swarm.nos_da_regiao(host.regiao_id)
    if not candidatos:
        return None, f'Nenhum nó do Swarm tem o label region={host.regiao_id}.'
    if len(candidatos) > 1:
        nomes = ', '.join(n['hostname'] for n in candidatos)
        return None, (f'{len(candidatos)} nós têm o label region={host.regiao_id} ({nomes}): '
                      'preencha o Swarm node ID para indicar qual é este host.')
    return swarm.inspecionar_no(candidatos[0]['id']), ''


def _fmt_bytes(n):
    from .log_operacao import fmt_bytes
    return fmt_bytes(n)


def avaliar(host, no, agora=None):
    """(status, motivos) a partir do nó do Swarm e do último ping do worker gravado no host."""
    agora = agora or timezone.now()
    offline, atencao = [], []

    if no['status'] != 'Ready':
        offline.append(f'Nó {no["hostname"]} com status {no["status"] or "desconhecido"} no Swarm.')
    if no['disponibilidade'] != 'Active':
        atencao.append(f'Nó em {no["disponibilidade"] or "?"}: o Swarm não coloca serviços novos nele.')
    if no['arquitetura'] != 'x86_64':
        atencao.append(f'Arquitetura {no["arquitetura"] or "?"}: a imagem do CP só roda em x86_64.')
    label = (no['labels'] or {}).get('region')
    if label != host.regiao_id:
        atencao.append(f'Label region do nó é "{label or "ausente"}", mas o host está na região {host.regiao_id}.')
    if no['endereco'] and host.ip and no['endereco'] != host.ip:
        atencao.append(f'IP cadastrado {host.ip} difere do endereço do nó no Swarm ({no["endereco"]}).')

    if host.agente_respondeu_em is None:
        atencao.append('O worker do CP neste nó ainda não respondeu (aguarde a próxima verificação).')
    else:
        atraso = (agora - host.agente_respondeu_em).total_seconds()
        if atraso > LIMITE_SEM_RESPOSTA_S:
            offline.append(f'Worker do CP não responde há {int(atraso // 60)} min — backups, restaurações e '
                           'migrações neste nó vão falhar (confira o serviço agente-no).')
    if host.disco_total_bytes and host.disco_livre_bytes is not None:
        fracao = host.disco_livre_bytes / host.disco_total_bytes
        if fracao < DISCO_LIVRE_MINIMO:
            atencao.append(f'Disco do Docker com {_fmt_bytes(host.disco_livre_bytes)} livres ({fracao:.0%}).')

    if offline:
        return 'offline', offline + atencao
    if atencao:
        return 'atencao', atencao
    return 'online', [f'Nó {no["hostname"]} Ready/Active e worker do CP respondendo.']


def registrar(host_id, status, mensagem, **campos):
    _modelo().objects.filter(pk=host_id).update(
        status_conexao=status, status_mensagem=mensagem[:2000], verificado_em=timezone.now(), **campos,
    )


def verificar_host(host):
    """Verifica um host e grava o sinalizador. Devolve (status, mensagem)."""
    if not swarm.swarm_ativo():
        status, mensagem = 'nao_verificado', 'Sem Swarm neste ambiente: não há como consultar os nós.'
        registrar(host.pk, status, mensagem)
        return status, mensagem
    try:
        no, aviso = localizar_no(host)
    except Exception as exc:
        status, mensagem = 'nao_verificado', f'Não foi possível consultar o Swarm: {exc}'
        registrar(host.pk, status, mensagem)
        return status, mensagem
    if no is None:
        registrar(host.pk, 'offline', aviso)
        return 'offline', aviso

    campos = {'no_hostname': no['hostname'], 'cpus_swarm': no['cpus'], 'ram_bytes_swarm': no['ram_bytes']}
    if not host.swarm_node_id and no['id']:
        campos['swarm_node_id'] = no['id']
    status, motivos = avaliar(host, no)
    mensagem = ' '.join(motivos)
    registrar(host.pk, status, mensagem, **campos)
    return status, mensagem


def registrar_ping(host_id, hostname, livre, total, containers):
    """Resposta do worker do CP no nó do host."""
    _modelo().objects.filter(pk=host_id).update(
        agente_respondeu_em=timezone.now(), disco_livre_bytes=livre, disco_total_bytes=total,
        containers_clientes=containers,
    )
