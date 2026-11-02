"""Trava anti-downgrade do CP: nenhuma operação do painel baixa a versão do ERP de um cliente.

A referência é a maior versão já vista: `Cliente.versao_erp_maxima` (só sobe), a detectada e a
tag em execução no Swarm. `Cliente.versao_erp` fica de fora porque é só o alvo pedido: um número
digitado errado, ainda não implantado, não pode virar piso.

Falha fechada: sem como comparar, recusa. Não existe desvio; para corrigir uma versão ruim,
publica-se um número maior. A última barreira é o `guarda_versao` no boot da imagem do ERP.
"""
import logging
import re
import subprocess

from django.conf import settings
from django.db import transaction

logger = logging.getLogger(__name__)

_SEMVER = re.compile(r'^\d+\.\d+\.\d+$')
_IMAGEM_NO_STACK = re.compile(r'ararasuite-erp:(\S+)')


class DowngradeRecusado(Exception):
    """A operação baixaria a versão, ou não dá para garantir que não baixa (mensagem em português)."""


def eh_semver(tag):
    return bool(_SEMVER.match(str(tag)))


def _tupla(versao):
    return tuple(int(p) for p in str(versao).split('.'))


def maior(*versoes):
    """A maior versão `X.Y.Z` entre as informadas; o que não for semver é ignorado. '' se não houver."""
    validas = [v for v in versoes if eh_semver(v)]
    return max(validas, key=_tupla) if validas else ''


def tag_em_execucao(slug):
    """Tag da imagem de `{slug}_web`; '' se o serviço não existe. Levanta se não der para saber."""
    servico = f'{slug}_web'
    try:
        r = subprocess.run(
            ['docker', 'service', 'inspect', '--format', '{{.Spec.TaskTemplate.ContainerSpec.Image}}', servico],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        if settings.DEBUG:
            return ''
        raise DowngradeRecusado(f'Não foi possível consultar {servico} no Swarm: {exc}') from exc
    if r.returncode != 0:
        erro = str(r.stderr).lower()
        if 'no such service' in erro or 'not found' in erro or settings.DEBUG:
            return ''
        raise DowngradeRecusado(f'Não foi possível consultar {servico} no Swarm: {str(r.stderr).strip()}')
    imagem = str(r.stdout).strip().split('@')[0].rsplit('/', 1)[-1]
    return imagem.split(':', 1)[1] if ':' in imagem else 'latest'


def _referencia(cliente):
    """(maior versão já vista, tag em execução)."""
    tag = tag_em_execucao(cliente.slug)
    referencia = maior(
        getattr(cliente, 'versao_erp_maxima', ''), getattr(cliente, 'versao_erp_detectada', ''), tag,
    )
    return referencia, tag


def _comparar(alvo, referencia, slug):
    if referencia and _tupla(alvo) < _tupla(referencia):
        raise DowngradeRecusado(
            f'Downgrade recusado: {slug} já está na versão {referencia} e o alvo é {alvo}. '
            f'Use {referencia} ou maior.'
        )


def exigir_sem_downgrade(cliente, versao):
    """Levanta `DowngradeRecusado` se `versao` não for `X.Y.Z` ou for menor que a referência."""
    if not eh_semver(versao):
        if settings.DEBUG:
            return
        raise DowngradeRecusado(f'Versão "{versao}" recusada: informe uma versão X.Y.Z.')
    _comparar(versao, _referencia(cliente)[0], cliente.slug)


def resolver_versao(cliente, alvo=None):
    """A tag a implantar para `cliente`, ou levanta `DowngradeRecusado`.

    `alvo` (padrão: `cliente.versao_erp`) que não seja `X.Y.Z`, como `latest` ou vazio:
    - cliente que já tem versão conhecida fica fixado nela: `latest` poderia apontar para qualquer uma;
    - primeira implantação (nem versão conhecida, nem ativação) continua aceita, inclusive em
      retentativa: o banco é novo, não há de onde baixar. É o caso do `ERP_LATEST_VERSION=latest`.
    """
    alvo = str(cliente.versao_erp if alvo is None else alvo).strip()
    referencia, tag = _referencia(cliente)
    if eh_semver(alvo):
        _comparar(alvo, referencia, cliente.slug)
        return alvo
    tag_pedida = alvo or 'latest'
    if settings.DEBUG:
        return tag_pedida
    if referencia:
        return referencia
    if not getattr(cliente, 'data_ativacao', None) and tag in ('', tag_pedida):
        return tag_pedida
    raise DowngradeRecusado(
        f'{cliente.slug} já foi implantado, mas nenhuma versão X.Y.Z é conhecida (alvo "{alvo}", '
        f'em execução "{tag}"). Verifique as versões do cliente antes de implantar.'
    )


def registrar_versao_vista(cliente_id, versao):
    """Sobe `versao_erp_maxima`, nunca reduz. Versão menor que a máxima fica registrada no log de erro."""
    from .models import Cliente

    if not eh_semver(versao):
        return
    with transaction.atomic():
        cliente = Cliente.objects.select_for_update().filter(pk=cliente_id).first()
        if cliente is None:
            return
        maxima = cliente.versao_erp_maxima
        if eh_semver(maxima) and _tupla(versao) < _tupla(maxima):
            logger.error('Versao do ERP regrediu em %s: maxima %s, detectada %s', cliente.slug, maxima, versao)
        elif versao != maxima:
            Cliente.objects.filter(pk=cliente_id).update(versao_erp_maxima=versao)


def regrediu(cliente):
    """A versão em execução é menor que a maior já vista? Só acontece por fora do painel."""
    detectada, maxima = cliente.versao_erp_detectada, cliente.versao_erp_maxima
    return eh_semver(detectada) and eh_semver(maxima) and _tupla(detectada) < _tupla(maxima)


def versao_implantada(cliente):
    """Versão a gravar no backup: a tag em execução, ou a detectada. '' se nenhuma for conhecida."""
    try:
        tag = tag_em_execucao(cliente.slug)
    except DowngradeRecusado:
        tag = ''
    return maior(tag) or maior(cliente.versao_erp_detectada)


def versao_do_stack(conteudo):
    """Versão da imagem do ERP num `stack.yml`; '' se não houver ou não for `X.Y.Z`."""
    achado = _IMAGEM_NO_STACK.search(conteudo or '')
    return maior(achado.group(1)) if achado else ''


def exigir_restauracao_compativel(cliente, versao_backup):
    """Recusa backup de versão mais nova que a imagem atual: o banco ficaria à frente do código.

    Backup mais antigo é permitido (o `migrate` leva o banco para frente). Backup sem versão
    conhecida também: a barreira dele é o `guarda_versao`, no boot da imagem.
    """
    if not eh_semver(versao_backup):
        return
    atual = maior(tag_em_execucao(cliente.slug)) or maior(cliente.versao_erp_detectada)
    if not atual:
        if settings.DEBUG:
            return
        raise DowngradeRecusado(
            f'Restauração recusada: o backup é da versão {versao_backup} e a versão atual '
            f'de {cliente.slug} não pôde ser confirmada.'
        )
    if _tupla(versao_backup) > _tupla(atual):
        raise DowngradeRecusado(
            f'Restauração recusada: o backup é da versão {versao_backup}, mais nova que a atual ({atual}). '
            f'Atualize o cliente para {versao_backup} ou maior antes de restaurar.'
        )
