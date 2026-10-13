import json
import logging
import os
import secrets
import subprocess
import tempfile
import time
from pathlib import Path

import docker
from django.utils import timezone

logger = logging.getLogger(__name__)

CLIENTES_BASE = Path(os.getenv('CLIENTES_BASE_PATH', '/opt/clientes'))
ERP_IMAGE = 'emporioautomacao/ararasuite-erp'
DOMAIN = os.getenv('ERP_DOMAIN', 'ararasuite.com.br')
CHECK_HTTP_HEALTH = os.getenv('PROVISIONING_CHECK_HTTP', 'true').lower() == 'true'

# Executor central de crons do ERP (Reposição Automática, Mercado Livre, Yampi,
# filas de WhatsApp/Comunicações, Tarefas recorrentes, NF-e...). O web só sobe o
# Daphne: sem o serviço <slug>_cron nenhuma rotina automática do cliente roda.
# Substitui o entrypoint da imagem — migrations ficam só com o web.
CRON_COMANDO = ['python', 'manage.py', 'executar_crons', '--loop', '--sleep', '60']


def site_url_cliente(cliente) -> str:
    """URL pública base da instância (``SITE_URL`` do ERP).

    O ERP usa em tudo que sai para fora sem request: links de peça de campanha
    no WhatsApp, imagens do HTML no e-mail, "abra no navegador", QR do portal de
    O.S., PDFs gerados por cron. Usa sempre o **subdomínio**: ele funciona desde o
    provisionamento, enquanto o domínio próprio depende de o cliente trocar os
    nameservers no Cloudflare — um link já enviado com DNS pendente quebraria.
    """
    return f"https://{cliente.subdominio}"


class MotorProvisionamento:
    def __init__(self, cliente):
        self.cliente = cliente
        self.client = docker.from_env()

    def _log(self, etapa, status, mensagem=''):
        from .models import ProvisionamentoLog
        log, _ = ProvisionamentoLog.objects.get_or_create(
            cliente=self.cliente, etapa=etapa,
            defaults={'status': 'pendente'},
        )
        log.status = status
        log.mensagem = mensagem
        if status == 'concluido':
            log.concluido_em = timezone.now()
        log.save()

    def executar(self):
        slug = self.cliente.slug
        cliente_dir = CLIENTES_BASE / slug
        cliente_dir.mkdir(parents=True, exist_ok=True)

        self._log('escolher_host', 'executando')
        host = self.cliente.host
        regiao = host.regiao if host else 'anapolis'
        self._log('escolher_host', 'concluido', f'Região: {regiao}')

        self._log('gerar_stack', 'executando')
        versao = self.cliente.versao_erp or 'latest'
        subdominio = self.cliente.subdominio
        modulos = ','.join(m.slug for m in self.cliente.modulos_ativos.all()) or 'financeiro,tarefas'
        tema = self.cliente.tema_site or 'loja'
        dominio_custom = self.cliente.dominio_custom or ''

        allowed_hosts = f'{subdominio},{dominio_custom}' if dominio_custom else subdominio
        csrf_origins = (
            f'https://{subdominio},https://{dominio_custom}' if dominio_custom
            else f'https://{subdominio}'
        )

        # Reusar credenciais existentes para não conflitar com o volume pgdata
        env_file = cliente_dir / '.env'
        env_existente = {}
        if env_file.exists():
            for linha in env_file.read_text().splitlines():
                if '=' in linha:
                    k, v = linha.split('=', 1)
                    env_existente[k.strip()] = v.strip()

        db_password = env_existente.get('POSTGRES_PASSWORD') or secrets.token_hex(24)
        secret_key = env_existente.get('DJANGO_SECRET_KEY') or secrets.token_hex(50)
        master_key = env_existente.get('EMPRESAS_CREDENCIAL_MASTER_KEY') or secrets.token_hex(32)
        senha_temp = env_existente.get('DJANGO_SUPERUSER_PASSWORD') or secrets.token_urlsafe(12)
        # Segredo usado pelo CP para autenticar chamadas HTTPS PARA esta
        # instancia (ex.: sincronizar versoes permitidas do SyncAgent/PDV) —
        # reusa o que ja estiver salvo no Cliente antes de gerar um novo, pra
        # nao invalidar integracoes existentes a cada reprovisionamento.
        cp_shared_secret = self.cliente.integracao_secret or env_existente.get('CP_SHARED_SECRET') or secrets.token_hex(32)

        env_vars = {
            'SLUG': slug,
            'POSTGRES_DB': f'erp_{slug}',
            'POSTGRES_USER': f'erp_{slug}',
            'POSTGRES_PASSWORD': db_password,
            'POSTGRES_HOST': 'db',
            'POSTGRES_PORT': '5432',
            'REDIS_URL': 'redis://redis:6379/0',
            'DJANGO_SECRET_KEY': secret_key,
            'ALLOWED_HOSTS': allowed_hosts,
            'CSRF_TRUSTED_ORIGINS': csrf_origins,
            'SITE_URL': site_url_cliente(self.cliente),
            'DEBUG': 'false',
            'MODULOS_ATIVOS': modulos,
            'TEMA_SITE': tema,
            'CP_CLIENTE_ID': str(self.cliente.id),
            'CP_CLIENTE_NOME': self.cliente.nome,
            'CP_CLIENTE_CNPJ': self.cliente.cnpj,
            'CP_SHARED_SECRET': cp_shared_secret,
            'EMPRESAS_CREDENCIAL_MASTER_KEY': master_key,
            'BACKUP_DIR': '/app/backups',
            'DJANGO_SUPERUSER_USERNAME': 'admin',
            'DJANGO_SUPERUSER_EMAIL': self.cliente.email_contato,
            'DJANGO_SUPERUSER_PASSWORD': senha_temp,
        }
        env_file.write_text('\n'.join(f'{k}={v}' for k, v in env_vars.items()))
        env_file.chmod(0o600)

        # senha_temp logged before deploy so it's preserved even if deploy fails
        self._log('criar_superuser', 'pendente', f'senha_temp:{senha_temp}')

        stack_content = self._gerar_stack_yaml(slug, subdominio, versao, regiao, env_vars, dominio_custom)
        stack_file = cliente_dir / 'stack.yml'
        stack_file.write_text(stack_content)
        self._log('gerar_stack', 'concluido')

        self._log('subir_stack', 'executando')
        result = subprocess.run(
            ['docker', 'stack', 'deploy', '--with-registry-auth', '-c', str(stack_file), slug],
            capture_output=True, text=True,
        )
        if result.returncode != 0 and 'AlreadyExists' not in result.stderr:
            raise subprocess.CalledProcessError(result.returncode, result.args, result.stderr)
        # Entrypoint handles: pgvector, migrations, superuser creation
        self._aguardar_servico(f'{slug}_web', timeout=180)
        if CHECK_HTTP_HEALTH:
            self._aguardar_http(f'https://{subdominio}/health/', timeout=180)
        self._log('subir_stack', 'concluido')
        self._log('criar_superuser', 'concluido', f'senha_temp:{senha_temp}')

        from .models import Cliente
        Cliente.objects.filter(pk=self.cliente.pk).update(
            status='ativo',
            versao_erp=versao,
            stack_path=str(cliente_dir),
            data_ativacao=timezone.now().date(),
            integracao_secret=cp_shared_secret,
        )
        self.cliente.integracao_secret = cp_shared_secret
        self.cliente.status = 'ativo'

        # Push inicial do snapshot de plano -- garante que a instancia recem
        # provisionada ja nasce com limites_plano real no erp, sem depender
        # do proximo signal de mudanca de plano/status/modulos. Nao-fatal:
        # falhar aqui nao pode derrubar o provisionamento.
        try:
            from .cp_push import SincronizadorPlano
            SincronizadorPlano(self.cliente).sincronizar()
        except Exception:
            logger.warning(
                'Falha ao enviar snapshot de plano inicial para %s', slug, exc_info=True,
            )

    def atualizar_versao(self, versao_nova):
        slug = self.cliente.slug
        subdominio = self.cliente.subdominio
        subprocess.run(
            ['docker', 'service', 'update', '--with-registry-auth',
             '--image', f'{ERP_IMAGE}:{versao_nova}', f'{slug}_web'],
            check=True,
        )
        self._aguardar_servico(f'{slug}_web', timeout=120)
        if CHECK_HTTP_HEALTH:
            self._aguardar_http(f'https://{subdominio}/health/', timeout=120)
        # Só depois do web, que roda as migrations no entrypoint: o cron nunca
        # fica com código antigo contra o schema novo (era o que matava o loop).
        self.sincronizar_servico_cron(versao_nova)
        from .models import Cliente
        Cliente.objects.filter(pk=self.cliente.pk).update(versao_erp=versao_nova)

    def sincronizar_servico_cron(self, versao=None):
        """Garante o serviço ``<slug>_cron`` com a imagem e o ambiente atuais do web.

        Clientes provisionados antes deste serviço não o têm: ele é criado a partir
        da spec do web — e não do .env/stack.yml, que não refletem o que o
        "Aplicar Configurações" injeta via ``--env-add``. Se já existe, recebe a
        imagem e o env do web.
        """
        slug = self.cliente.slug
        nome = f'{slug}_cron'
        spec_web = self._spec_servico(f'{slug}_web')
        container = spec_web['TaskTemplate']['ContainerSpec']
        imagem = f'{ERP_IMAGE}:{versao}' if versao else container['Image'].split('@')[0]
        env = container.get('Env') or []

        if self._spec_servico(nome, obrigatorio=False) is None:
            cmd = [
                'docker', 'service', 'create', '--detach', '--with-registry-auth',
                '--name', nome,
                '--label', f'com.docker.stack.namespace={slug}',
                '--container-label', f'com.docker.stack.namespace={slug}',
                '--network', f'{slug}_internal',
                '--replicas', '1',
                '--restart-condition', 'any', '--restart-delay', '60s', '--restart-window', '120s',
                '--entrypoint', ' '.join(CRON_COMANDO),
            ]
            for mount in container.get('Mounts') or []:
                cmd += ['--mount', f"type={mount.get('Type', 'volume')},source={mount['Source']},target={mount['Target']}"]
            for constraint in (spec_web['TaskTemplate'].get('Placement') or {}).get('Constraints') or []:
                cmd += ['--constraint', constraint]
            for item in env:
                cmd += ['--env', item]
            cmd.append(imagem)
        else:
            cmd = ['docker', 'service', 'update', '--detach', '--with-registry-auth', '--image', imagem]
            for item in env:
                cmd += ['--env-add', item]
            cmd.append(nome)

        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f'Falha ao sincronizar o serviço {nome}: {result.stderr.strip()}')

    def _spec_servico(self, nome, obrigatorio=True):
        result = subprocess.run(
            ['docker', 'service', 'inspect', '--format', '{{json .Spec}}', nome],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            if obrigatorio:
                raise RuntimeError(f'Serviço {nome} não encontrado: {result.stderr.strip()}')
            return None
        return json.loads(result.stdout)

    def destruir(self):
        import shutil
        slug = self.cliente.slug
        subprocess.run(['docker', 'stack', 'rm', slug], capture_output=True)
        time.sleep(15)
        for vol in [f'{slug}_pgdata', f'{slug}_media', f'{slug}_backups']:
            subprocess.run(['docker', 'volume', 'rm', vol], capture_output=True)
        cliente_dir = CLIENTES_BASE / slug
        if cliente_dir.exists():
            shutil.rmtree(cliente_dir)
        self.cliente.delete()

    def suspender(self):
        subprocess.run(['docker', 'service', 'scale', f'{self.cliente.slug}_web=0'], check=True)
        # Cliente suspenso não pode continuar disparando WhatsApp/e-mail pelo cron.
        subprocess.run(['docker', 'service', 'scale', f'{self.cliente.slug}_cron=0'], capture_output=True)

    def reativar(self):
        subprocess.run(['docker', 'service', 'scale', f'{self.cliente.slug}_web=1'], check=True)
        if self._spec_servico(f'{self.cliente.slug}_cron', obrigatorio=False) is None:
            self.sincronizar_servico_cron()
        else:
            subprocess.run(['docker', 'service', 'scale', f'{self.cliente.slug}_cron=1'], check=True)

    def _aguardar_servico(self, service_name, timeout=120):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = subprocess.run(
                ['docker', 'service', 'ps', '--filter', 'desired-state=running',
                 '--format', '{{.CurrentState}}', service_name],
                capture_output=True, text=True,
            )
            if 'Running' in result.stdout:
                return
            time.sleep(5)
        raise TimeoutError(f'Serviço {service_name} não ficou Running em {timeout}s')

    def _aguardar_http(self, url, timeout=180):
        import urllib.request
        import urllib.error
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                resp = urllib.request.urlopen(url, timeout=10)
                if resp.status == 200:
                    return
            except Exception:
                pass
            time.sleep(10)
        raise TimeoutError(f'Endpoint {url} não respondeu 200 em {timeout}s')

    def _carregar_env(self, slug):
        env_file = TENANTS_BASE / slug / '.env'
        env_vars = {}
        for line in env_file.read_text().splitlines():
            if '=' in line:
                k, v = line.split('=', 1)
                env_vars[k.strip()] = v.strip()
        return env_vars

    def _run_manage(self, slug, versao, env_vars, args):
        network = f'{slug}_internal'
        env = {**os.environ, **env_vars}
        subprocess.run(
            ['docker', 'run', '--rm', '--network', network,
             f'{ERP_IMAGE}:{versao}', 'python', 'manage.py'] + args,
            check=True, env=env,
        )

    def _gerar_stack_yaml(self, slug, subdominio, versao, regiao, env_vars, dominio_custom=''):
        env_block = '\n'.join(f'      {k}: "{v}"' for k, v in env_vars.items() if k != 'SLUG')
        labels_custom = ''
        if dominio_custom:
            labels_custom = (
                # Router apex
                f'\n        - "traefik.http.routers.{slug}-erp-custom.rule=Host(`{dominio_custom}`)"'
                f'\n        - "traefik.http.routers.{slug}-erp-custom.entrypoints=web"'
                f'\n        - "traefik.http.routers.{slug}-erp-custom.service={slug}-erp"'
                # Router www → redireciona 301 para o apex
                f'\n        - "traefik.http.routers.{slug}-erp-custom-www.rule=Host(`www.{dominio_custom}`)"'
                f'\n        - "traefik.http.routers.{slug}-erp-custom-www.entrypoints=web"'
                f'\n        - "traefik.http.routers.{slug}-erp-custom-www.middlewares={slug}-www-redirect"'
                f'\n        - "traefik.http.middlewares.{slug}-www-redirect.redirectregex.regex=^https?://www\\\\.(.+)"'
                f'\n        - "traefik.http.middlewares.{slug}-www-redirect.redirectregex.replacement=https://$${{1}}"'
                f'\n        - "traefik.http.middlewares.{slug}-www-redirect.redirectregex.permanent=true"'
            )
        return f"""version: "3.8"
services:
  web:
    image: {ERP_IMAGE}:{versao}
    environment:
{env_block}
    volumes:
      - media:/app/media
      - backups:/app/backups
    networks:
      - traefik-public
      - internal
    deploy:
      replicas: 1
      placement:
        constraints:
          - node.labels.region == {regiao}
      restart_policy:
        condition: any
        delay: 60s
        window: 120s
      labels:
        - "traefik.enable=true"
        - "traefik.http.routers.{slug}-erp.rule=Host(`{subdominio}`)"
        - "traefik.http.routers.{slug}-erp.entrypoints=web"
        - "traefik.http.services.{slug}-erp.loadbalancer.server.port=8000"{labels_custom}

  cron:
    image: {ERP_IMAGE}:{versao}
    entrypoint: {json.dumps(CRON_COMANDO)}
    environment:
{env_block}
    volumes:
      - media:/app/media
      - backups:/app/backups
    networks:
      - internal
    deploy:
      replicas: 1
      placement:
        constraints:
          - node.labels.region == {regiao}
      restart_policy:
        condition: any
        delay: 60s
        window: 120s

  db:
    image: pgvector/pgvector:0.8.0-pg17
    environment:
      POSTGRES_DB: "erp_{slug}"
      POSTGRES_USER: "erp_{slug}"
      POSTGRES_PASSWORD: "{env_vars['POSTGRES_PASSWORD']}"
    volumes:
      - pgdata:/var/lib/postgresql/data
    networks:
      - internal
    deploy:
      placement:
        constraints:
          - node.labels.region == {regiao}

  redis:
    image: redis:7-alpine
    networks:
      - internal
    deploy:
      placement:
        constraints:
          - node.labels.region == {regiao}

volumes:
  pgdata:
  media:
  backups:

networks:
  traefik-public:
    external: true
  internal:
    driver: overlay
    attachable: true
"""
