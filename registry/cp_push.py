"""
Canais de push do painel de controle (CP) para a instancia `erp` de cada
cliente. Dois canais hoje:

- SincronizadorVersoes: envia o conjunto de versoes do SyncAgent/PDV curadas
  para aquele cliente (Cliente.versoes_permitidas), pra alimentar o
  SyncPackage.allowed daquela instancia.
- SincronizadorPlano: envia o snapshot do plano contratado (nome, status,
  modulos assinados, limites comerciais/tecnicos) pra alimentar
  core.PlanoCliente daquela instancia (ver docs/infra/configuracoes-plano-cliente.md
  no repo `erp`).

Autenticacao dos dois via Cliente.integracao_secret, gerado no
provisionamento (registry.provisioning.MotorProvisionamento) e espelhado como
env var CP_SHARED_SECRET no container do erp daquele cliente.
"""

import logging
import time

from django.utils import timezone

from .models import SincronizacaoVersoesAgente

logger = logging.getLogger(__name__)

# Cliente.STATUS_CHOICES (8 valores) -> PlanoCliente.STATUS_CHOICES no erp
# (so 4: ativo/trial/suspenso/cancelado). Os tres estados transitorios de
# provisionamento so aparecem aqui como fallback defensivo -- na pratica o
# push so acontece com integracao_secret ja gerado, o que so existe depois
# do provisionamento concluir.
_STATUS_ASSINATURA_MAP = {
    'ativo': 'ativo',
    'trial': 'trial',
    'suspenso': 'suspenso',
    'cancelado': 'cancelado',
    'trial_expirado': 'suspenso',
    'aguardando_provisao': 'ativo',
    'provisionando': 'ativo',
    'erro_provisao': 'ativo',
}


def _mensagem_erro_http_amigavel(response):
    """Categoriza em pt-BR os erros conhecidos que o erp devolve nos canais
    /v1/cp/*:sync (401 unauthorized / 409 not_linked_to_cp / 409
    cliente_id_mismatch / 400 invalid_payload). Compartilhado entre
    SincronizadorVersoes e SincronizadorPlano."""
    try:
        corpo = response.json()
    except ValueError:
        corpo = {}
    erro = corpo.get('error', '')
    status = response.status_code

    if status == 401 and erro == 'unauthorized':
        return ('Segredo de integração recusado pelo ERP (401). '
                'Rode "⚙ Aplicar Configurações" para reenviar o segredo e tente de novo.')
    if status == 409 and erro == 'not_linked_to_cp':
        return 'O ERP deste cliente não está vinculado ao painel de controle (409 not_linked_to_cp).'
    if status == 409 and erro == 'cliente_id_mismatch':
        return 'O cliente_id enviado não bate com o que o ERP espera (409 cliente_id_mismatch).'
    if status == 400 and erro == 'invalid_payload':
        campo = corpo.get('field', '?')
        return f'O ERP rejeitou o pacote (400 invalid_payload, campo "{campo}").'
    return f'HTTP {status} — {response.text[:500]}'


class SincronizadorVersoes:
    """Espelha o nome/padrao de registry.provisioning.MotorProvisionamento:
    uma classe por operacao de infraestrutura, instanciada com o Cliente alvo."""

    TIMEOUT_SECONDS = 15

    def __init__(self, cliente, iniciada_por=None):
        self.cliente = cliente
        self.iniciada_por = iniciada_por
        self.registro = None
        self._linhas = []

    def _versoes_a_enviar(self):
        """Fonte das versoes que vao no push, conforme o modo do cliente:

        - modo automatico (Cliente.atualizacao_automatica_agente): todo o
          catalogo de versoes ATIVAS -- a curadoria manual e ignorada, e
          releases novos entram sozinhos (ver signals.versao_agente_atualizada).
        - modo curadoria (padrao): so o que a equipe marcou em
          Cliente.versoes_permitidas (e que ainda esteja ativo no catalogo).
        """
        if self.cliente.atualizacao_automatica_agente:
            from .models import VersaoAgente
            return VersaoAgente.objects.filter(ativo=True)
        return self.cliente.versoes_permitidas.filter(ativo=True)

    def _montar_payload(self):
        packages = [
            {
                'version': versao.versao,
                'download_url': versao.download_url,
                'sha256': versao.sha256,
                'release_notes': versao.release_notes,
                'erp_minimo': versao.erp_minimo or None,
            }
            for versao in self._versoes_a_enviar()
        ]
        return {'cliente_id': str(self.cliente.id), 'packages': packages}

    def _log(self, linha):
        """Adiciona uma linha ao log e PERSISTE na hora -- e isso que faz o
        admin conseguir mostrar o log "ao vivo" (polling no
        registry_cliente_sincronizacao_status enquanto status='enviando')."""
        ts = timezone.localtime().strftime('%H:%M:%S')
        self._linhas.append(f'[{ts}] {linha}')
        if self.registro is not None:
            SincronizacaoVersoesAgente.objects.filter(pk=self.registro.pk).update(
                log='\n'.join(self._linhas),
            )

    def sincronizar(self):
        """Envia a curadoria atual pro erp do cliente. Sempre registra um
        SincronizacaoVersoesAgente (sucesso ou erro) para auditoria, com um
        log passo a passo -- aparece na pagina do Cliente no admin (abaixo do
        botao "Sincronizar" e no inline "Sincronizacoes de Versoes")."""
        import requests

        payload = self._montar_payload()
        self.registro = SincronizacaoVersoesAgente.objects.create(
            cliente=self.cliente,
            versoes_enviadas=payload['packages'],
            status='enviando',
            iniciada_por=self.iniciada_por,
        )
        registro = self.registro

        modo = (
            'catálogo automático (todas as versões ativas)'
            if self.cliente.atualizacao_automatica_agente
            else 'curadoria manual'
        )
        self._log(f'Modo: {modo}')

        versoes = [p['version'] for p in payload['packages']]
        if versoes:
            self._log(f'Pacote montado: {len(versoes)} versão(ões) — {", ".join(versoes)}')
        else:
            self._log('Pacote montado: nenhuma versão — o ERP vai aposentar todos os pacotes deste cliente')

        host = self.cliente.dominio_custom or self.cliente.subdominio
        url = f'https://{host}/v1/cp/agent-packages:sync'
        self._log(f'Destino: POST {url}')
        self._log(f'Enviando (timeout {self.TIMEOUT_SECONDS}s)…')

        t0 = time.monotonic()
        try:
            response = requests.post(
                url,
                json=payload,
                headers={'Authorization': f'Bearer {self.cliente.integracao_secret}'},
                timeout=self.TIMEOUT_SECONDS,
            )
            dt = time.monotonic() - t0
            registro.resposta_http_status = response.status_code
            self._log(f'Resposta: HTTP {response.status_code} em {dt:.2f}s')

            if response.ok:
                registro.status = 'concluida'
                self._registrar_sucesso(response)
            else:
                registro.status = 'erro'
                registro.mensagem_erro = self._registrar_erro_http(response)[:2000]
                self._log('Terminou com erro.')
        except Exception as exc:
            dt = time.monotonic() - t0
            descricao = self._descrever_excecao(exc, host)
            registro.status = 'erro'
            registro.mensagem_erro = (str(exc) or descricao)[:2000]
            self._log(f'Falha de conexão após {dt:.2f}s: {descricao}')
            self._log('Terminou com erro.')

        registro.log = '\n'.join(self._linhas)
        registro.concluida_em = timezone.now()
        registro.save(update_fields=['status', 'resposta_http_status', 'mensagem_erro', 'concluida_em', 'log'])
        return registro

    # -- helpers de log --------------------------------------------------------

    def _registrar_sucesso(self, response):
        try:
            corpo = response.json()
        except ValueError:
            self._log(f'Corpo da resposta HTTP 200 ilegível: {response.text[:200]}')
            self._log('Concluída com sucesso.')
            return

        synced = corpo.get('synced', '?')
        retired = corpo.get('retired', '?')
        skipped = corpo.get('skipped') or []
        linha = f'ERP confirmou: {synced} sincronizada(s), {retired} aposentada(s)'
        if skipped:
            linha += f', {len(skipped)} ignorada(s) ({", ".join(map(str, skipped))})'
        self._log(linha)
        self._log('Concluída com sucesso.')

    def _registrar_erro_http(self, response):
        """Loga uma linha amigável em pt-BR pro erro do ERP e devolve o texto
        que vai pra SincronizacaoVersoesAgente.mensagem_erro (usado no toast do
        admin e na mensagem do retry da task)."""
        amigavel = _mensagem_erro_http_amigavel(response)
        self._log(amigavel)
        return amigavel

    @staticmethod
    def _descrever_excecao(exc, host):
        import requests
        if isinstance(exc, requests.exceptions.Timeout):
            return f'O ERP não respondeu em {SincronizadorVersoes.TIMEOUT_SECONDS}s (timeout).'
        if isinstance(exc, requests.exceptions.ConnectionError):
            return f'Não foi possível conectar em {host} (host fora do ar ou DNS não resolve).'
        return f'{type(exc).__name__}: {exc}'


class SincronizadorPlano:
    """Envia pro erp do cliente o snapshot do plano contratado (nome, status,
    modulos assinados, limites) -- alimenta core.PlanoCliente la (endpoint
    POST /v1/cp/plano:sync, ver sync_api.cp_push.CpPlanoSyncAPIView no repo
    `erp`).

    Ao contrario de SincronizadorVersoes, e sempre sincrono e nao tem model
    de auditoria dedicado -- o payload e pequeno e rapido, e o proprio erp ja
    guarda o timestamp da ultima sincronizacao (PlanoCliente.ultima_sincronizacao_cp).
    Quem chama decide se roda direto (botao manual, push inicial do
    provisionamento) ou via Celery task (gatilho automatico por signal)."""

    TIMEOUT_SECONDS = 15

    def __init__(self, cliente):
        self.cliente = cliente

    def _montar_payload(self):
        plano = self.cliente.plano
        return {
            'cliente_id': str(self.cliente.id),
            'plano_nome': plano.nome,
            'status_assinatura': _STATUS_ASSINATURA_MAP.get(self.cliente.status, 'ativo'),
            'modulos_assinados': list(self.cliente.modulos_ativos.values_list('slug', flat=True)),
            'limites_plano': {
                'max_usuarios': plano.max_usuarios,
                'max_empresas': plano.max_empresas,
                'recursos_cpu': plano.recursos_cpu,
                'recursos_ram_gb': plano.recursos_ram_gb,
            },
        }

    def sincronizar(self):
        """Sempre devolve um dict {'sucesso': bool, 'mensagem': str,
        'http_status': int|None} -- nunca lança, pra ser seguro de chamar
        tanto direto (view/provisioning) quanto de dentro de uma task."""
        import requests

        payload = self._montar_payload()
        host = self.cliente.dominio_custom or self.cliente.subdominio
        url = f'https://{host}/v1/cp/plano:sync'

        try:
            response = requests.post(
                url,
                json=payload,
                headers={'Authorization': f'Bearer {self.cliente.integracao_secret}'},
                timeout=self.TIMEOUT_SECONDS,
            )
        except Exception as exc:
            mensagem = self._descrever_excecao(exc, host)
            logger.warning('SincronizadorPlano: falha ao sincronizar plano de %s: %s', self.cliente.slug, mensagem)
            return {'sucesso': False, 'mensagem': mensagem, 'http_status': None}

        if response.ok:
            logger.info('SincronizadorPlano: plano sincronizado para %s', self.cliente.slug)
            return {
                'sucesso': True,
                'mensagem': 'Plano sincronizado com sucesso.',
                'http_status': response.status_code,
            }

        mensagem = _mensagem_erro_http_amigavel(response)
        logger.warning('SincronizadorPlano: erro ao sincronizar plano de %s: %s', self.cliente.slug, mensagem)
        return {'sucesso': False, 'mensagem': mensagem, 'http_status': response.status_code}

    @staticmethod
    def _descrever_excecao(exc, host):
        import requests
        if isinstance(exc, requests.exceptions.Timeout):
            return f'O ERP não respondeu em {SincronizadorPlano.TIMEOUT_SECONDS}s (timeout).'
        if isinstance(exc, requests.exceptions.ConnectionError):
            return f'Não foi possível conectar em {host} (host fora do ar ou DNS não resolve).'
        return f'{type(exc).__name__}: {exc}'
