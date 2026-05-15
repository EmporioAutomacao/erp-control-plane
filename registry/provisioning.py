import os
import secrets
import subprocess
import tempfile
import time
from pathlib import Path

import docker
from django.utils import timezone

CLIENTES_BASE = Path(os.getenv('CLIENTES_BASE_PATH', '/opt/clientes'))
ERP_IMAGE = 'emporioautomacao/erp'
DOMAIN = os.getenv('ERP_DOMAIN', 'ararasuite.com.br')
CHECK_HTTP_HEALTH = os.getenv('PROVISIONING_CHECK_HTTP', 'true').lower() == 'true'


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
        db_password = secrets.token_hex(24)
        secret_key = secrets.token_hex(50)
        master_key = secrets.token_hex(32)
        senha_temp = secrets.token_urlsafe(12)
        versao = self.cliente.versao_erp or 'latest'
        subdominio = self.cliente.subdominio
        modulos = ','.join(m.slug for m in self.cliente.modulos_ativos.all()) or 'financeiro,tarefas'

        # .env salvo no manager como referência — não é lido pelo container
        env_vars = {
            'SLUG': slug,
            'POSTGRES_DB': f'erp_{slug}',
            'POSTGRES_USER': f'erp_{slug}',
            'POSTGRES_PASSWORD': db_password,
            'POSTGRES_HOST': 'db',
            'POSTGRES_PORT': '5432',
            'REDIS_URL': 'redis://redis:6379/0',
            'DJANGO_SECRET_KEY': secret_key,
            'ALLOWED_HOSTS': subdominio,
            'CSRF_TRUSTED_ORIGINS': f'https://{subdominio}',
            'DEBUG': 'false',
            'MODULOS_ATIVOS': modulos,
            'EMPRESAS_CREDENCIAL_MASTER_KEY': master_key,
            'BACKUP_DIR': '/app/backups',
            'DJANGO_SUPERUSER_USERNAME': 'admin',
            'DJANGO_SUPERUSER_EMAIL': self.cliente.email_contato,
            'DJANGO_SUPERUSER_PASSWORD': senha_temp,
        }
        env_file = cliente_dir / '.env'
        env_file.write_text('\n'.join(f'{k}={v}' for k, v in env_vars.items()))
        env_file.chmod(0o600)

        # senha_temp logged before deploy so it's preserved even if deploy fails
        self._log('criar_superuser', 'pendente', f'senha_temp:{senha_temp}')

        stack_content = self._gerar_stack_yaml(slug, subdominio, versao, regiao, env_vars)
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
        from .models import Cliente
        Cliente.objects.filter(pk=self.cliente.pk).update(versao_erp=versao_nova)

    def suspender(self):
        subprocess.run(['docker', 'service', 'scale', f'{self.cliente.slug}_web=0'], check=True)

    def reativar(self):
        subprocess.run(['docker', 'service', 'scale', f'{self.cliente.slug}_web=1'], check=True)

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

    def _gerar_stack_yaml(self, slug, subdominio, versao, regiao, env_vars):
        env_block = '\n'.join(f'      {k}: "{v}"' for k, v in env_vars.items() if k != 'SLUG')
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
        condition: on-failure
        delay: 10s
        max_attempts: 3
      labels:
        - "traefik.enable=true"
        - "traefik.http.routers.{slug}-erp.rule=Host(`{subdominio}`)"
        - "traefik.http.routers.{slug}-erp.entrypoints=websecure"
        - "traefik.http.routers.{slug}-erp.tls.certresolver=le"
        - "traefik.http.services.{slug}-erp.loadbalancer.server.port=8000"
        - "traefik.http.middlewares.{slug}-redirect.redirectscheme.scheme=https"
        - "traefik.http.routers.{slug}-erp-http.rule=Host(`{subdominio}`)"
        - "traefik.http.routers.{slug}-erp-http.entrypoints=web"
        - "traefik.http.routers.{slug}-erp-http.middlewares={slug}-redirect"

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
