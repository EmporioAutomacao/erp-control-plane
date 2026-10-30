"""Consultas e comandos do Swarm que só funcionam num nó **manager**.

`docker service scale/update`, `docker node ls/inspect` e `docker stack deploy` exigem o
manager; por isso estas funções só são chamadas por tasks da fila `celery`. O que depende do
daemon do nó do cliente (Postgres, volumes) fica em `registry/volumes.py`.
"""
import logging
import subprocess
import time

logger = logging.getLogger(__name__)


def _run(cmd, timeout=60):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def nos_da_regiao(regiao):
    """Nós com `node.labels.region == regiao`: lista de dicts com id, hostname, status,
    disponibilidade e arquitetura. Lista vazia se o Swarm não responder."""
    r = _run(['docker', 'node', 'ls', '--filter', f'node.label=region={regiao}',
              '--format', '{{.ID}}|{{.Hostname}}|{{.Status}}|{{.Availability}}'])
    if r.returncode != 0:
        raise RuntimeError(f'docker node ls falhou: {(r.stderr or "").strip()}')
    nos = []
    for linha in (r.stdout or '').splitlines():
        partes = linha.strip().split('|')
        if len(partes) != 4:
            continue
        no_id, hostname, status, disponibilidade = partes
        arq = _run(['docker', 'node', 'inspect', no_id, '--format', '{{.Description.Platform.Architecture}}'])
        nos.append({
            'id': no_id, 'hostname': hostname, 'status': status, 'disponibilidade': disponibilidade,
            'arquitetura': (arq.stdout or '').strip() if arq.returncode == 0 else '',
        })
    return nos


def no_apto(no):
    return no['status'] == 'Ready' and no['disponibilidade'] == 'Active' and no['arquitetura'] == 'x86_64'


def replicas(servico):
    """Réplicas configuradas do serviço, ou None se ele não existir."""
    r = _run(['docker', 'service', 'inspect', servico, '--format', '{{.Spec.Mode.Replicated.Replicas}}'])
    if r.returncode != 0:
        return None
    try:
        return int((r.stdout or '').strip())
    except ValueError:
        return None


def escalar(servico, n):
    """`docker service scale`; levanta erro se o serviço existir e o comando falhar."""
    r = _run(['docker', 'service', 'scale', '--detach', f'{servico}={n}'], timeout=120)
    if r.returncode != 0:
        raise RuntimeError(f'Falha ao escalar {servico} para {n}: {(r.stderr or "").strip()}')


def tasks_rodando(servico):
    """Quantas tasks do serviço estão de fato rodando (não só desejadas)."""
    r = _run(['docker', 'service', 'ps', servico, '--filter', 'desired-state=running',
              '--format', '{{.CurrentState}}'])
    if r.returncode != 0:
        return 0
    return sum(1 for linha in (r.stdout or '').splitlines() if linha.strip().startswith('Running'))


def aguardar_parado(servico, timeout=120, intervalo=3):
    """Espera o serviço não ter nenhuma task rodando. Levanta TimeoutError se não parar."""
    limite = time.monotonic() + timeout
    while time.monotonic() < limite:
        if tasks_rodando(servico) == 0 and not _tem_task_encerrando(servico):
            return
        time.sleep(intervalo)
    raise TimeoutError(f'{servico} ainda tem tasks rodando após {timeout}s')


def _tem_task_encerrando(servico):
    r = _run(['docker', 'service', 'ps', servico, '--format', '{{.CurrentState}}'])
    if r.returncode != 0:
        return False
    return any(linha.strip().startswith(('Running', 'Starting', 'Preparing'))
               for linha in (r.stdout or '').splitlines())


def no_do_servico(servico):
    """Hostname do nó onde a task do serviço está rodando, ou None."""
    r = _run(['docker', 'service', 'ps', servico, '--filter', 'desired-state=running',
              '--format', '{{.Node}}|{{.CurrentState}}'])
    if r.returncode != 0:
        return None
    for linha in (r.stdout or '').splitlines():
        no, _, estado = linha.strip().partition('|')
        if estado.startswith('Running'):
            return no
    return None


def aguardar_no_do_servico(servico, hostname, timeout=300, intervalo=5):
    """Espera a task do serviço estar Running no nó `hostname`."""
    limite = time.monotonic() + timeout
    atual = None
    while time.monotonic() < limite:
        atual = no_do_servico(servico)
        if atual == hostname:
            return
        time.sleep(intervalo)
    raise TimeoutError(f'{servico} não ficou rodando em {hostname} após {timeout}s (último nó: {atual or "nenhum"})')


def imagem_do_servico(servico):
    """Imagem configurada no serviço, sem o digest (`repo:tag`), ou None."""
    r = _run(['docker', 'service', 'inspect', servico, '--format', '{{.Spec.TaskTemplate.ContainerSpec.Image}}'])
    if r.returncode != 0:
        return None
    return (r.stdout or '').strip().split('@')[0] or None


def env_do_servico(servico):
    """Variáveis de ambiente configuradas no serviço, como dict."""
    r = _run(['docker', 'service', 'inspect', servico, '--format', '{{json .Spec.TaskTemplate.ContainerSpec.Env}}'])
    if r.returncode != 0:
        raise RuntimeError(f'Serviço {servico} não encontrado')
    import json
    env = {}
    for item in json.loads((r.stdout or 'null').strip() or 'null') or []:
        chave, _, valor = item.partition('=')
        env[chave] = valor
    return env


def definir_traefik(servico, habilitado):
    """Liga/desliga o roteamento do Traefik pelo label do serviço (sem reiniciar o container)."""
    valor = 'true' if habilitado else 'false'
    r = _run(['docker', 'service', 'update', '--detach', '--label-add', f'traefik.enable={valor}', servico],
             timeout=120)
    if r.returncode != 0:
        raise RuntimeError(f'Falha ao definir traefik.enable={valor} em {servico}: {(r.stderr or "").strip()}')


def traefik_habilitado(servico):
    r = _run(['docker', 'service', 'inspect', servico, '--format', '{{index .Spec.Labels "traefik.enable"}}'])
    return r.returncode == 0 and (r.stdout or '').strip() == 'true'



def swarm_ativo():
    """Se este daemon é manager de um Swarm ativo (sem Swarm, nenhuma verificação de nó faz sentido)."""
    try:
        r = _run(['docker', 'info', '--format', '{{.Swarm.LocalNodeState}}|{{.Swarm.ControlAvailable}}'], timeout=30)
    except (OSError, subprocess.SubprocessError):
        return False
    return r.returncode == 0 and (r.stdout or '').strip() == 'active|true'


def inspecionar_no(no):
    """Dados de um nó do Swarm (ID ou hostname), ou None se ele não existir."""
    import json
    r = _run(['docker', 'node', 'inspect', no, '--format', '{{json .}}'])
    if r.returncode != 0:
        return None
    d = json.loads((r.stdout or '').strip() or '{}')
    descricao = d.get('Description') or {}
    recursos = descricao.get('Resources') or {}
    status = d.get('Status') or {}
    spec = d.get('Spec') or {}
    return {
        'id': d.get('ID', ''),
        'hostname': descricao.get('Hostname', ''),
        'status': (status.get('State') or '').capitalize(),
        'endereco': status.get('Addr', ''),
        'disponibilidade': (spec.get('Availability') or '').capitalize(),
        'labels': spec.get('Labels') or {},
        'arquitetura': (descricao.get('Platform') or {}).get('Architecture', ''),
        'cpus': (recursos.get('NanoCPUs') or 0) / 1e9,
        'ram_bytes': recursos.get('MemoryBytes') or 0,
    }
