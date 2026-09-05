"""
Canal de push do painel de controle (CP) para a instancia `erp` de cada
cliente: envia o conjunto de versoes do SyncAgent/PDV curadas para aquele
cliente (Cliente.versoes_permitidas), pra alimentar o SyncPackage.allowed
daquela instancia (ver sync_api.cp_push no repo `erp`).

Nao existe nenhum outro canal HTTPS do CP para uma instancia de cliente hoje
-- este e o primeiro. Autenticacao via Cliente.integracao_secret, gerado no
provisionamento (registry.provisioning.MotorProvisionamento) e espelhado como
env var CP_SHARED_SECRET no container do erp daquele cliente.
"""

from django.utils import timezone

from .models import SincronizacaoVersoesAgente


class SincronizadorVersoes:
    """Espelha o nome/padrao de registry.provisioning.MotorProvisionamento:
    uma classe por operacao de infraestrutura, instanciada com o Cliente alvo."""

    TIMEOUT_SECONDS = 15

    def __init__(self, cliente):
        self.cliente = cliente

    def _montar_payload(self):
        packages = [
            {
                'version': versao.versao,
                'download_url': versao.download_url,
                'sha256': versao.sha256,
                'release_notes': versao.release_notes,
                'erp_minimo': versao.erp_minimo or None,
            }
            for versao in self.cliente.versoes_permitidas.filter(ativo=True)
        ]
        return {'cliente_id': str(self.cliente.id), 'packages': packages}

    def sincronizar(self):
        """Envia a curadoria atual pro erp do cliente. Sempre registra um
        SincronizacaoVersoesAgente (sucesso ou erro) para auditoria --
        aparece como inline na pagina do Cliente no admin."""
        import requests

        payload = self._montar_payload()
        registro = SincronizacaoVersoesAgente.objects.create(
            cliente=self.cliente,
            versoes_enviadas=payload['packages'],
            status='enviando',
        )

        host = self.cliente.dominio_custom or self.cliente.subdominio
        url = f'https://{host}/v1/cp/agent-packages:sync'

        try:
            response = requests.post(
                url,
                json=payload,
                headers={'Authorization': f'Bearer {self.cliente.integracao_secret}'},
                timeout=self.TIMEOUT_SECONDS,
            )
            registro.resposta_http_status = response.status_code
            if response.ok:
                registro.status = 'concluida'
            else:
                registro.status = 'erro'
                registro.mensagem_erro = response.text[:2000]
        except Exception as exc:
            registro.status = 'erro'
            registro.mensagem_erro = str(exc)[:2000]

        registro.concluida_em = timezone.now()
        registro.save(update_fields=['status', 'resposta_http_status', 'mensagem_erro', 'concluida_em'])
        return registro
