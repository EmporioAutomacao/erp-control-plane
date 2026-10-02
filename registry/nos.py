"""Descoberta do nó Swarm onde os serviços de um cliente rodam.

Os comandos `docker ps/exec/cp/run` só enxergam o daemon local, então as tasks de
backup/restauração precisam rodar no worker Celery do nó que hospeda o cliente.
Cada nó consome a fila `no.<hostname>` (ver `agente-no` em stack-prod.yml).
"""
import logging
import subprocess

logger = logging.getLogger(__name__)

FILA_PADRAO = 'celery'


def hostname_do_no(slug, servico='db'):
    """Hostname Docker do nó onde `{slug}_{servico}` está rodando, ou None."""
    try:
        r = subprocess.run(
            ['docker', 'service', 'ps', f'{slug}_{servico}',
             '--filter', 'desired-state=running', '--format', '{{.Node}}'],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning('Falha ao consultar o no de %s_%s: %s', slug, servico, exc)
        return None
    if r.returncode != 0:
        return None
    linhas = [linha.strip() for linha in (r.stdout or '').splitlines() if linha.strip()]
    return linhas[0] if linhas else None


def fila_do_no(hostname):
    return f'no.{hostname}' if hostname else FILA_PADRAO


def fila_do_cliente(slug):
    """Fila Celery do worker que enxerga os containers do cliente.

    Sem Swarm (dev local) ou se o serviço não for encontrado, cai na fila padrão.
    """
    return fila_do_no(hostname_do_no(slug, 'db'))


def container_local(slug, servico):
    """ID do container `{slug}_{servico}` rodando no daemon local, ou None.

    Em Swarm o container se chama `{slug}_{servico}.<replica>.<taskid>`; o filtro
    ancorado evita casar com outros nomes que apenas contenham o texto.
    """
    r = subprocess.run(
        ['docker', 'ps', '-q', '-f', f'name=^{slug}_{servico}(\\.|$)'],
        capture_output=True, text=True,
    )
    saida = (r.stdout or '').strip()
    return saida.splitlines()[0] if saida else None
