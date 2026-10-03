"""Verificação de atualizações do próprio CP (Docker Hub) e atualização pelo painel.

A versão em execução é `settings.VERSION`. A versão disponível é a maior tag semver
(`X.Y.Z`) do repositório da imagem no Docker Hub, consultada pela API do registry.

A atualização não pode ser feita pelo processo que está sendo atualizado (o `web`/`celery`
seriam encerrados no meio). Por isso um **container auxiliar destacado** roda os
`docker service update` na ordem certa (`web` por último) e guarda o log.
"""
import logging
import os
import re
import subprocess
import time
from urllib.parse import urljoin

import requests
from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)

# `web` por último: é a tela que está disparando a atualização. db/redis nunca são tocados.
SERVICOS_ORDEM = ['celery', 'beat', 'agente-no', 'web']
CACHE_SEGUNDOS = 60
_SEMVER = re.compile(r'^\d+\.\d+\.\d+$')
_REPOSITORIO = re.compile(r'^[a-z0-9][a-z0-9._-]*/[a-z0-9][a-z0-9._-]*$')
_AUTH_URL = 'https://auth.docker.io/token'
_REGISTRY_URL = 'https://registry-1.docker.io'
_TIMEOUT = (5, 10)


class ErroRegistry(Exception):
    """Falha ao consultar o Docker Hub (mensagem já em português)."""


class ErroAtualizacao(Exception):
    """A atualização foi recusada ou não pôde ser iniciada (mensagem já em português)."""


# ── Versões ──────────────────────────────────────────────────────────────────

def versao_atual():
    return settings.VERSION


def eh_semver(tag):
    return bool(_SEMVER.match(str(tag)))


def _tupla(versao):
    return tuple(int(p) for p in str(versao).split('.'))


def tags_semver(tags):
    """Tags `X.Y.Z` únicas, da mais antiga para a mais nova (ordem numérica, não alfabética)."""
    return sorted({t for t in tags if eh_semver(t)}, key=_tupla)


def maior_versao(tags):
    validas = tags_semver(tags)
    return validas[-1] if validas else ''


def ha_atualizacao(atual, disponivel):
    return bool(disponivel) and eh_semver(atual) and eh_semver(disponivel) and _tupla(disponivel) > _tupla(atual)


def situacao(cfg):
    """`atualizado` | `disponivel` | `indisponivel` | `nao_verificado` — o que a tela mostra."""
    if cfg is None or cfg.status_conexao == 'nao_testado':
        return 'nao_verificado'
    if cfg.status_conexao == 'erro':
        return 'indisponivel'
    if not cfg.versao_disponivel:
        return 'nao_verificado'
    return 'disponivel' if ha_atualizacao(versao_atual(), cfg.versao_disponivel) else 'atualizado'


# ── Docker Hub ───────────────────────────────────────────────────────────────

def repositorio_valido(repositorio):
    return bool(_REPOSITORIO.match((repositorio or '').strip()))


def _traduzir_erro(exc):
    if isinstance(exc, ErroRegistry):
        return str(exc)
    if isinstance(exc, requests.exceptions.Timeout):
        return 'O Docker Hub demorou demais para responder (timeout).'
    if isinstance(exc, requests.exceptions.ConnectionError):
        return 'Não foi possível conectar ao Docker Hub. Confira a rede do servidor.'
    return f'{type(exc).__name__}: {exc}'


def listar_tags(cfg):
    """Todas as tags do repositório (segue a paginação). Levanta `ErroRegistry`."""
    repo = (cfg.repositorio or '').strip().strip('/')
    if not repositorio_valido(repo):
        raise ErroRegistry('Repositório inválido. Use o formato usuario/repositorio.')

    usuario = (cfg.dockerhub_usuario or '').strip()
    auth = (usuario, cfg.dockerhub_token) if usuario and cfg.dockerhub_token_cifrado else None

    r = requests.get(
        _AUTH_URL,
        params={'service': 'registry.docker.io', 'scope': f'repository:{repo}:pull'},
        auth=auth, timeout=_TIMEOUT,
    )
    if r.status_code == 401:
        raise ErroRegistry('Credenciais do Docker Hub recusadas: confira o usuário e o token.')
    r.raise_for_status()
    corpo = r.json()
    token = corpo.get('token') or corpo.get('access_token')
    if not token:
        raise ErroRegistry('O Docker Hub não devolveu um token de acesso.')

    headers = {'Authorization': f'Bearer {token}'}
    url, params, tags = f'{_REGISTRY_URL}/v2/{repo}/tags/list', {'n': 1000}, []
    for _ in range(30):  # trava de segurança contra paginação infinita
        r = requests.get(url, params=params, headers=headers, timeout=_TIMEOUT)
        if r.status_code in (401, 403, 404):
            raise ErroRegistry(
                f'Repositório "{repo}" não encontrado ou sem permissão. '
                'Se for privado, informe o usuário e um token de leitura do Docker Hub.'
            )
        r.raise_for_status()
        tags.extend(r.json().get('tags') or [])
        proximo = r.links.get('next', {}).get('url')
        if not proximo:
            break
        url, params = urljoin(_REGISTRY_URL, proximo), None
    return tags


def verificar(cfg, usar_cache=False):
    """Consulta o Docker Hub e devolve `{status, mensagem, disponivel, recentes}` (sem gravar)."""
    from .cripto import SegredoIlegivel

    if usar_cache and cfg.verificado_em and cfg.status_conexao == 'conectado' \
            and (timezone.now() - cfg.verificado_em).total_seconds() < CACHE_SEGUNDOS:
        return {
            'status': cfg.status_conexao, 'mensagem': cfg.status_mensagem,
            'disponivel': cfg.versao_disponivel, 'recentes': cfg.versoes_recentes,
        }
    try:
        tags = listar_tags(cfg)
    except SegredoIlegivel as exc:
        return {'status': 'erro', 'mensagem': f'{exc} Informe o token novamente e salve.', 'disponivel': '', 'recentes': []}
    except Exception as exc:
        return {'status': 'erro', 'mensagem': _traduzir_erro(exc), 'disponivel': '', 'recentes': []}

    validas = tags_semver(tags)
    if not validas:
        return {
            'status': 'erro', 'disponivel': '', 'recentes': [],
            'mensagem': f'Nenhuma tag de versão (X.Y.Z) encontrada em "{cfg.repositorio}".',
        }
    disponivel = validas[-1]
    return {
        'status': 'conectado', 'disponivel': disponivel, 'recentes': list(reversed(validas))[:10],
        'mensagem': f'Docker Hub acessível: {len(validas)} versões em "{cfg.repositorio}", a mais recente é {disponivel}.',
    }


def registrar(resultado):
    from .models import ConfiguracaoAtualizacao
    ConfiguracaoAtualizacao.objects.filter(pk=1).update(
        status_conexao=resultado['status'], status_mensagem=resultado['mensagem'][:1000],
        verificado_em=timezone.now(), versao_disponivel=resultado['disponivel'],
        versoes_recentes=resultado['recentes'],
    )


def verificar_e_registrar(usar_cache=False):
    from .models import ConfiguracaoAtualizacao
    cfg = ConfiguracaoAtualizacao.obter()
    if not cfg:
        return {'status': 'nao_testado', 'mensagem': 'Configuração ainda não salva.', 'disponivel': '', 'recentes': []}
    resultado = verificar(cfg, usar_cache=usar_cache)
    if not (usar_cache and resultado['mensagem'] == cfg.status_mensagem and resultado['status'] == cfg.status_conexao):
        registrar(resultado)
    return resultado


# ── Docker / Swarm ───────────────────────────────────────────────────────────

def _docker(*args, timeout=30):
    return subprocess.run(['docker', *args], capture_output=True, text=True, timeout=timeout)


def swarm_manager():
    try:
        r = _docker('info', '--format', '{{.Swarm.ControlAvailable}}', timeout=15)
    except (OSError, subprocess.SubprocessError):
        return False
    return r.returncode == 0 and r.stdout.strip() == 'true'


def descobrir_stack():
    """Nome do stack do CP, lido do label do próprio container (ou None)."""
    host = os.getenv('HOSTNAME', '').strip()
    if not host:
        return None
    try:
        r = _docker('inspect', host, '--format', '{{index .Config.Labels "com.docker.stack.namespace"}}', timeout=15)
    except (OSError, subprocess.SubprocessError):
        return None
    nome = r.stdout.strip() if r.returncode == 0 else ''
    return nome if re.match(r'^[A-Za-z0-9][A-Za-z0-9_.-]*$', nome) else None


def servicos_do_stack(stack):
    """Serviços do CP a atualizar, na ordem segura (só os da lista permitida que existem)."""
    r = _docker('service', 'ls', '--filter', f'label=com.docker.stack.namespace={stack}', '--format', '{{.Name}}')
    if r.returncode != 0:
        return []
    existentes = {linha.strip() for linha in r.stdout.splitlines()}
    return [f'{stack}_{s}' for s in SERVICOS_ORDEM if f'{stack}_{s}' in existentes]


def imagem_do_servico(servico):
    r = _docker('service', 'inspect', servico, '--format', '{{.Spec.TaskTemplate.ContainerSpec.Image}}')
    imagem = r.stdout.strip().split('@')[0] if r.returncode == 0 else ''
    return imagem or None


def montar_script(repositorio, versao, servicos):
    """Script (sh) do container auxiliar. Só recebe valores já validados (semver, repo, nomes do Docker)."""
    assert eh_semver(versao) and repositorio_valido(repositorio)
    linhas = ['set -e', f'echo "Atualizando o CP para {versao}"']
    for servico in servicos:
        linhas += [
            f'echo "==> {servico}"',
            f'docker service update --with-registry-auth --update-failure-action rollback '
            f'--detach=false --image {repositorio}:{versao} {servico}',
        ]
    linhas.append('echo "Atualização concluída"')
    return '\n'.join(linhas)


def estado_atualizacao(cfg):
    """`{estado, log}`; estado: nenhuma | em_andamento | concluida | falhou | desconhecido."""
    if not cfg or not cfg.atualizacao_container:
        return {'estado': 'nenhuma', 'log': ''}
    try:
        r = _docker('inspect', cfg.atualizacao_container, '--format', '{{.State.Status}}|{{.State.ExitCode}}', timeout=15)
    except (OSError, subprocess.SubprocessError):
        return {'estado': 'desconhecido', 'log': ''}
    if r.returncode != 0:
        # auxiliar removido: se a versão em execução já é a alvo, deu certo
        concluida = cfg.atualizacao_alvo and cfg.atualizacao_alvo == versao_atual()
        return {'estado': 'concluida' if concluida else 'desconhecido', 'log': ''}
    status, _, codigo = r.stdout.strip().partition('|')
    try:
        log = _docker('logs', '--tail', '30', cfg.atualizacao_container, timeout=15)
        texto = (log.stdout + log.stderr).strip()
    except (OSError, subprocess.SubprocessError):
        texto = ''
    if status in ('running', 'created', 'restarting'):
        return {'estado': 'em_andamento', 'log': texto}
    return {'estado': 'concluida' if codigo == '0' else 'falhou', 'log': texto}


def iniciar_atualizacao(cfg, versao, usuario):
    """Valida e dispara o container auxiliar. Devolve o nome dele. Levanta `ErroAtualizacao`."""
    from .models import ConfiguracaoAtualizacao

    if not eh_semver(versao):
        raise ErroAtualizacao('Versão inválida.')
    if not repositorio_valido(cfg.repositorio):
        raise ErroAtualizacao('Repositório inválido. Corrija a configuração.')
    if not ha_atualizacao(versao_atual(), versao):
        raise ErroAtualizacao(f'A versão {versao} não é mais nova que a atual ({versao_atual()}); downgrade não é permitido.')
    if estado_atualizacao(cfg)['estado'] == 'em_andamento':
        raise ErroAtualizacao('Já existe uma atualização em andamento.')

    try:
        existentes = tags_semver(listar_tags(cfg))
    except Exception as exc:
        raise ErroAtualizacao(f'Não foi possível confirmar as versões no Docker Hub: {_traduzir_erro(exc)}') from exc
    if versao not in existentes:
        raise ErroAtualizacao(f'A versão {versao} não existe no Docker Hub.')

    if not swarm_manager():
        raise ErroAtualizacao('Atualização pelo painel só funciona em um Swarm manager (não disponível neste ambiente).')
    stack = descobrir_stack()
    if not stack:
        raise ErroAtualizacao('Não foi possível identificar o stack do CP (label do container ausente).')
    servicos = servicos_do_stack(stack)
    if f'{stack}_web' not in servicos:
        raise ErroAtualizacao(f'Serviço {stack}_web não encontrado no Swarm.')
    imagem = imagem_do_servico(f'{stack}_web')
    if not imagem:
        raise ErroAtualizacao('Não foi possível descobrir a imagem atual do CP.')

    if cfg.atualizacao_container:  # remove o auxiliar da execução anterior
        _docker('rm', '-f', cfg.atualizacao_container, timeout=30)

    nome = f'cp-atualizador-{versao.replace(".", "-")}-{int(time.time())}'
    script = montar_script(cfg.repositorio.strip(), versao, servicos)
    r = _docker(
        'run', '-d', '--name', nome,
        '-v', '/var/run/docker.sock:/var/run/docker.sock',
        '-v', '/root/.docker:/root/.docker:ro',
        imagem, 'sh', '-c', script,
        timeout=120,
    )
    if r.returncode != 0:
        raise ErroAtualizacao(f'Falha ao iniciar o container de atualização: {(r.stderr or r.stdout).strip()[:300]}')

    ConfiguracaoAtualizacao.objects.filter(pk=1).update(
        atualizacao_alvo=versao, atualizacao_iniciada_em=timezone.now(),
        atualizacao_iniciada_por=usuario, atualizacao_container=nome,
    )
    logger.info('Atualizacao do CP para %s iniciada por %s (container %s)', versao, usuario, nome)
    return nome
