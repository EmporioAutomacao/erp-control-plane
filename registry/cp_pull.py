"""
Canal de LEITURA do painel de controle (CP) para a instancia `erp` de cada
cliente: le a versao do ERP em execucao e a lista de instalacoes do
SyncAgent/PDV (com a versao instalada de cada uma).

Contraparte do registry.cp_push (que ESCREVE a curadoria de versoes). Mesma
autenticacao: Cliente.integracao_secret como Bearer, espelhado como env var
CP_SHARED_SECRET no container do erp daquele cliente.

Lado erp: sync_api.cp_status.CpStatusAPIView (GET /v1/cp/status), repo `erp`.
Clientes com ERP antigo (sem essa rota) respondem 404 -> fallback pra GET
/health/, que so tem a versao do ERP.
"""

import json
import time
import urllib.request

from django.utils import timezone

from .models import Cliente, InstalacaoAgente


class ColetorVersoes:
    """Espelha registry.cp_push.SincronizadorVersoes: uma classe por operacao
    de infraestrutura, instanciada com o Cliente alvo."""

    TIMEOUT_SECONDS = 15

    def __init__(self, cliente):
        self.cliente = cliente

    def coletar(self):
        """Consulta o erp do cliente e grava:
        - Cliente.versao_erp_detectada / versoes_detectadas_em / deteccao_versoes_erro
        - InstalacaoAgente (full-sync: upsert das presentes, apaga as ausentes)

        Devolve um dict {'ok': bool, 'erro': str, 'erp_version': str,
        'instalacoes': int} — usado pelo admin/testes.
        """
        host = self.cliente.dominio_custom or self.cliente.subdominio
        agora = timezone.now()

        if not self.cliente.integracao_secret:
            erp_version = self._tentar_health(host)
            erro = ('Sem segredo de integração — clique em "⚙ Aplicar Configurações" '
                    'primeiro (gera o segredo e o envia pro ERP). Só a versão do ERP '
                    'foi lida via /health/.')
            self._gravar_cliente(erp_version, erro, agora)
            return {'ok': False, 'erro': erro, 'erp_version': erp_version, 'instalacoes': 0}

        url = f'https://{host}/v1/cp/status'
        try:
            import requests
            resp = requests.get(
                url,
                params={'cliente_id': str(self.cliente.id)},
                headers={'Authorization': f'Bearer {self.cliente.integracao_secret}'},
                timeout=self.TIMEOUT_SECONDS,
            )
        except Exception as exc:
            erro = self._descrever_excecao(exc, host)
            self._gravar_cliente(self.cliente.versao_erp_detectada, erro, agora)
            return {'ok': False, 'erro': erro, 'erp_version': self.cliente.versao_erp_detectada, 'instalacoes': 0}

        if resp.status_code == 404:
            erp_version = self._tentar_health(host)
            erro = 'ERP sem /v1/cp/status (versão antiga) — só a versão do ERP foi lida via /health/.'
            self._gravar_cliente(erp_version, erro, agora)
            return {'ok': False, 'erro': erro, 'erp_version': erp_version, 'instalacoes': 0}

        if not resp.ok:
            erro = self._descrever_erro_http(resp)
            self._gravar_cliente(self.cliente.versao_erp_detectada, erro, agora)
            return {'ok': False, 'erro': erro, 'erp_version': self.cliente.versao_erp_detectada, 'instalacoes': 0}

        try:
            corpo = resp.json()
        except ValueError:
            erro = f'Resposta HTTP 200 ilegível do ERP: {resp.text[:200]}'
            self._gravar_cliente(self.cliente.versao_erp_detectada, erro, agora)
            return {'ok': False, 'erro': erro, 'erp_version': self.cliente.versao_erp_detectada, 'instalacoes': 0}

        erp_version = str(corpo.get('erp_version') or '')[:20]
        instalacoes = corpo.get('instalacoes') or []
        n = self._reconciliar_instalacoes(instalacoes)
        self._gravar_cliente(erp_version, '', agora)
        return {'ok': True, 'erro': '', 'erp_version': erp_version, 'instalacoes': n}

    # -- helpers -------------------------------------------------------------

    def _gravar_cliente(self, erp_version, erro, quando):
        Cliente.objects.filter(pk=self.cliente.pk).update(
            versao_erp_detectada=erp_version or '',
            deteccao_versoes_erro=(erro or '')[:300],
            versoes_detectadas_em=quando,
        )

    def _reconciliar_instalacoes(self, instalacoes):
        """Full-sync: cria/atualiza as do payload, apaga as que sumiram —
        mesma filosofia do cp_push (SyncPackage.allowed)."""
        vistos = []
        for item in instalacoes:
            if not isinstance(item, dict):
                continue
            instance_id = str(item.get('instance_id') or '').strip()
            if not instance_id:
                continue
            vistos.append(instance_id)
            InstalacaoAgente.objects.update_or_create(
                cliente=self.cliente,
                instance_id=instance_id,
                defaults={
                    'installation_label': str(item.get('installation_label') or '')[:128],
                    'agent_version': str(item.get('agent_version') or '')[:80],
                    'last_seen_at': self._parse_dt(item.get('last_seen_at')),
                    'active': bool(item.get('active', True)),
                    'connectivity': str(item.get('connectivity') or '')[:20],
                },
            )
        self.cliente.instalacoes_agente.exclude(instance_id__in=vistos).delete()
        return len(vistos)

    def _tentar_health(self, host):
        """Fallback: GET /health/ (publico) só pra versao do ERP."""
        try:
            with urllib.request.urlopen(f'https://{host}/health/', timeout=10) as r:
                corpo = json.loads(r.read().decode('utf-8'))
            return str(corpo.get('version') or '')[:20]
        except Exception:
            return self.cliente.versao_erp_detectada or ''

    @staticmethod
    def _parse_dt(valor):
        if not valor:
            return None
        from django.utils.dateparse import parse_datetime
        try:
            return parse_datetime(valor)
        except (TypeError, ValueError):
            return None

    def _descrever_erro_http(self, resp):
        try:
            erro = (resp.json() or {}).get('error', '')
        except ValueError:
            erro = ''
        status = resp.status_code
        if status == 401 and erro == 'unauthorized':
            return ('Segredo de integração recusado pelo ERP (401). Rode '
                    '"⚙ Aplicar Configurações" para reenviar o segredo.')
        if status == 409 and erro == 'not_linked_to_cp':
            return 'O ERP deste cliente não está vinculado ao painel de controle (409 not_linked_to_cp).'
        if status == 409 and erro == 'cliente_id_mismatch':
            return 'O cliente_id enviado não bate com o que o ERP espera (409 cliente_id_mismatch).'
        return f'HTTP {status} — {resp.text[:300]}'

    @staticmethod
    def _descrever_excecao(exc, host):
        import requests
        if isinstance(exc, requests.exceptions.Timeout):
            return f'O ERP não respondeu em {ColetorVersoes.TIMEOUT_SECONDS}s (timeout).'
        if isinstance(exc, requests.exceptions.ConnectionError):
            return f'Não foi possível conectar em {host} (host fora do ar ou DNS não resolve).'
        return f'{type(exc).__name__}: {exc}'
