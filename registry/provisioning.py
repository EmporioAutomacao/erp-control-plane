import os
import secrets
import subprocess
import tempfile
import time
from pathlib import Path

import docker
from django.utils import timezone

CLIENTES_BASE = Path(os.getenv('CLIENTES_BASE_PATH', '/opt/clientes'))
ERP_IMAGE = 'emporioautomacao/ararasuite-erp'
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
        versao = self.cliente.versao_erp or 'latest'
        subdominio = self.cliente.subdominio
        modulos = ','.join(m.slug for m in self.cliente.modulos_ativos.all()) or 'financeiro,tarefas'
        tema = self.cliente.tema_site or 'padrao'
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
            'DEBUG': 'false',
            'MODULOS_ATIVOS': modulos,
            'TEMA_SITE': tema,
            'CP_CLIENTE_ID': str(self.cliente.id),
            'CP_CLIENTE_NOME': self.cliente.nome,
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
