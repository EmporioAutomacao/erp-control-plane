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

from . import dominios, mcp, versoes

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

# Segredos que precisam sobreviver a qualquer reimplantação: trocar qualquer um deles quebra o
# acesso ao volume do banco, as sessões ou as credenciais de empresas cifradas no ERP.
SEGREDOS_ENV = ('POSTGRES_PASSWORD', 'DJANGO_SECRET_KEY', 'EMPRESAS_CREDENCIAL_MASTER_KEY')


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

    def executar(self, modo_migracao=False):
        """Gera o .env/stack.yml e implanta o stack do cliente.

        `modo_migracao=True` (usado pela migração de servidor): exige o `.env` com os segredos,
        implanta com web/cron em 0 réplicas e o Traefik desligado (o ERP não atende ninguém antes de
        restaurado), não espera o web, não altera status/datas do cliente e não envia o plano.
        O `stack.yml` gravado em disco continua sendo o normal.
        """
        slug = self.cliente.slug
        # Antes de qualquer escrita: uma recusa deixa o .env, o stack.yml e o stack intocados.
        try:
            versao = versoes.resolver_versao(self.cliente)
        except versoes.DowngradeRecusado as exc:
            self._log('gerar_stack', 'erro', str(exc))
            raise
        cliente_dir = CLIENTES_BASE / slug
        cliente_dir.mkdir(parents=True, exist_ok=True)

        self._log('escolher_host', 'executando')
        host = self.cliente.host
        regiao = host.regiao_id if host else 'anapolis'
        self._log('escolher_host', 'concluido', f'Região: {regiao}')

        self._log('gerar_stack', 'executando')
        subdominio = self.cliente.subdominio
        labels_dominios = dominios.labels_traefik(self.cliente)

        # Reusar credenciais existentes para não conflitar com o volume pgdata
        env_file = cliente_dir / '.env'
        env_existente = self.ler_env_existente()

        if modo_migracao:
            faltando = [k for k in SEGREDOS_ENV if not env_existente.get(k)]
            if faltando:
                raise RuntimeError(
                    f'.env do cliente sem {", ".join(faltando)} em {env_file}: a migração não pode gerar segredos novos.'
                )

        env_vars = self.montar_env(env_existente)
        senha_temp = env_vars['DJANGO_SUPERUSER_PASSWORD']
        cp_shared_secret = env_vars['CP_SHARED_SECRET']
        env_file.write_text('\n'.join(f'{k}={v}' for k, v in env_vars.items()))
        env_file.chmod(0o600)

        if not modo_migracao:
            # senha_temp logged before deploy so it's preserved even if deploy fails
            self._log('criar_superuser', 'pendente', f'senha_temp:{senha_temp}')

        servico_mcp = self.spec_mcp(versao)
        stack_content = self._gerar_stack_yaml(
            slug, subdominio, versao, regiao, env_vars, labels_dominios, servico_mcp=servico_mcp,
        )
        stack_file = cliente_dir / 'stack.yml'
        stack_file.write_text(stack_content)
        if modo_migracao:
            stack_file = cliente_dir / 'stack-migracao.yml'
            stack_file.write_text(self._gerar_stack_yaml(
                slug, subdominio, versao, regiao, env_vars, labels_dominios, replicas_app=0, traefik=False,
                servico_mcp=servico_mcp,
            ))
        self._log('gerar_stack', 'concluido')

        self._log('subir_stack', 'executando')
        result = subprocess.run(
            ['docker', 'stack', 'deploy', '--with-registry-auth', '-c', str(stack_file), slug],
            capture_output=True, text=True,
        )
        if result.returncode != 0 and 'AlreadyExists' not in result.stderr:
            raise subprocess.CalledProcessError(result.returncode, result.args, result.stderr)
        versoes.registrar_versao_vista(self.cliente.pk, versao)
        if modo_migracao:
            self._log('subir_stack', 'concluido', f'Modo migração: região {regiao}, web/cron em 0, Traefik desligado')
            return
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
        versoes.exigir_sem_downgrade(self.cliente, versao_nova)
        subprocess.run(
            ['docker', 'service', 'update', '--with-registry-auth',
             '--image', f'{ERP_IMAGE}:{versao_nova}', f'{slug}_web'],
            check=True,
        )
        versoes.registrar_versao_vista(self.cliente.pk, versao_nova)
        # Fora do ar o web fica em 0 e continua assim: não há o que esperar. As migrations rodam ao religar.
        if not self._fora_do_ar():
            self._aguardar_servico(f'{slug}_web', timeout=120)
            if CHECK_HTTP_HEALTH:
                self._aguardar_http(f'https://{subdominio}/health/', timeout=120)
        # Só depois do web, que roda as migrations no entrypoint: o cron nunca
        # fica com código antigo contra o schema novo (era o que matava o loop).
        self.sincronizar_servico_cron(versao_nova)
        self.sincronizar_servico_mcp(versao_nova)
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
        if versao:
            versoes.exigir_sem_downgrade(self.cliente, versao)
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
                '--replicas', '0' if self._fora_do_ar() else '1',
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

    def spec_mcp(self, versao):
        """Env, labels e réplicas do ``<slug>_mcp`` (ver ``registry/mcp.py``), sem tocar no Docker."""
        lista = dominios.do_cliente(self.cliente)
        return {
            'env': mcp.env(self.cliente, versao, lista),
            'labels': mcp.labels_traefik(self.cliente, lista),
            'replicas': mcp.replicas_desejadas(self.cliente),
        }

    def sincronizar_servico_mcp(self, versao=None):
        """Garante o serviço ``<slug>_mcp`` com a imagem do web, o env próprio e as réplicas do módulo.

        Diferente do cron, **não** copia env nem volumes do web (segredos). Cria o serviço em clientes
        provisionados antes dele; se já existe, atualiza imagem, env, labels (removendo routers de
        domínio que saiu) e réplicas. ``traefik.enable`` segue o do web, que a migração entre
        servidores liga e desliga.
        """
        slug = self.cliente.slug
        nome = mcp.nome_servico(slug)
        if versao:
            versoes.exigir_sem_downgrade(self.cliente, versao)
        spec_web = self._spec_servico(f'{slug}_web')
        container = spec_web['TaskTemplate']['ContainerSpec']
        imagem = f'{ERP_IMAGE}:{versao}' if versao else container['Image'].split('@')[0]
        lista = dominios.do_cliente(self.cliente)
        env = mcp.env(self.cliente, imagem.rsplit(':', 1)[-1], lista)
        habilitado = (spec_web.get('Labels') or {}).get('traefik.enable', 'true') == 'true'
        labels = mcp.labels_traefik(self.cliente, lista, habilitado=habilitado)
        replicas = str(mcp.replicas_desejadas(self.cliente))

        spec_mcp = self._spec_servico(nome, obrigatorio=False)
        if spec_mcp is None:
            cmd = [
                'docker', 'service', 'create', '--detach', '--with-registry-auth',
                '--name', nome,
                '--label', f'com.docker.stack.namespace={slug}',
                '--container-label', f'com.docker.stack.namespace={slug}',
                '--network', f'{slug}_internal',
                '--network', 'traefik-public',
                '--replicas', replicas,
                '--restart-condition', 'any', '--restart-delay', '60s', '--restart-window', '120s',
                '--entrypoint', ' '.join(mcp.COMANDO),
            ]
            for constraint in (spec_web['TaskTemplate'].get('Placement') or {}).get('Constraints') or []:
                cmd += ['--constraint', constraint]
            for chave, valor in env.items():
                cmd += ['--env', f'{chave}={valor}']
            for chave, valor in labels.items():
                cmd += ['--label', f'{chave}={valor}']
            cmd.append(imagem)
        else:
            cmd = ['docker', 'service', 'update', '--detach', '--with-registry-auth', '--image', imagem,
                   '--replicas', replicas]
            for chave, valor in env.items():
                cmd += ['--env-add', f'{chave}={valor}']
            cmd += mcp.args_reconciliar(slug, spec_mcp.get('Labels') or {}, labels)
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

    def _fora_do_ar(self):
        from .models import Cliente
        return self.cliente.status in Cliente.STATUS_FORA_DO_AR

    def suspender(self):
        subprocess.run(['docker', 'service', 'scale', f'{self.cliente.slug}_web=0'], check=True)
        # Cliente suspenso não pode continuar disparando WhatsApp/e-mail pelo cron.
        if self._spec_servico(f'{self.cliente.slug}_cron', obrigatorio=False) is not None:
            subprocess.run(['docker', 'service', 'scale', f'{self.cliente.slug}_cron=0'], check=True)
        # Nem o MCP: cliente fora do ar não expõe /mcp.
        nome_mcp = mcp.nome_servico(self.cliente.slug)
        if self._spec_servico(nome_mcp, obrigatorio=False) is not None:
            subprocess.run(['docker', 'service', 'scale', f'{nome_mcp}=0'], check=True)

    def reativar(self):
        subprocess.run(['docker', 'service', 'scale', f'{self.cliente.slug}_web=1'], check=True)
        if self._spec_servico(f'{self.cliente.slug}_cron', obrigatorio=False) is None:
            self.sincronizar_servico_cron()
        else:
            subprocess.run(['docker', 'service', 'scale', f'{self.cliente.slug}_cron=1'], check=True)
        # Volta a seguir o módulo `agents` (e cria o serviço em cliente antigo).
        self.sincronizar_servico_mcp()

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
        from . import http_publico
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                resp = http_publico.abrir(url, timeout=10)
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
        versoes.exigir_sem_downgrade(self.cliente, versao)
        network = f'{slug}_internal'
        env = {**os.environ, **env_vars}
        subprocess.run(
            ['docker', 'run', '--rm', '--network', network,
             f'{ERP_IMAGE}:{versao}', 'python', 'manage.py'] + args,
            check=True, env=env,
        )

    def ler_env_existente(self):
        """Conteúdo do `.env` atual do cliente (dict vazio se não existir)."""
        env_file = CLIENTES_BASE / self.cliente.slug / '.env'
        env_existente = {}
        if env_file.exists():
            for linha in env_file.read_text().splitlines():
                if '=' in linha:
                    k, v = linha.split('=', 1)
                    env_existente[k.strip()] = v.strip()
        return env_existente

    def montar_env(self, env_existente):
        """Ambiente do web/cron que um provisionamento gravaria agora (sem gravar nada).

        Reaproveita os segredos de `env_existente`; só gera um valor novo quando ele não existe.
        """
        slug = self.cliente.slug
        subdominio = self.cliente.subdominio
        modulos = ','.join(m.slug for m in self.cliente.modulos_ativos.all()) or 'financeiro,tarefas'
        tema = self.cliente.tema_site or 'loja'
        lista = dominios.do_cliente(self.cliente)
        allowed_hosts = dominios.allowed_hosts(self.cliente, lista)
        csrf_origins = dominios.csrf_origins(self.cliente, lista)

        db_password = env_existente.get('POSTGRES_PASSWORD') or secrets.token_hex(24)
        secret_key = env_existente.get('DJANGO_SECRET_KEY') or secrets.token_hex(50)
        master_key = env_existente.get('EMPRESAS_CREDENCIAL_MASTER_KEY') or secrets.token_hex(32)
        senha_temp = env_existente.get('DJANGO_SUPERUSER_PASSWORD') or secrets.token_urlsafe(12)
        # Segredo usado pelo CP para autenticar chamadas HTTPS PARA esta
        # instancia (ex.: sincronizar versoes permitidas do SyncAgent/PDV) —
        # reusa o que ja estiver salvo no Cliente antes de gerar um novo, pra
        # nao invalidar integracoes existentes a cada reprovisionamento.
        cp_shared_secret = self.cliente.integracao_secret or env_existente.get('CP_SHARED_SECRET') or secrets.token_hex(32)

        return {
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

    def _gerar_stack_yaml(self, slug, subdominio, versao, regiao, env_vars, labels_dominios=None,
                          replicas_app=1, traefik=True, servico_mcp=None):
        """`labels_dominios`: labels dos domínios próprios (`dominios.labels_traefik`), valor bruto.

        `servico_mcp`: `spec_mcp()` (env, labels, réplicas). O bloco `mcp` usa só esse env, nunca o do
        web, não tem volumes e segue `replicas_app`/`traefik` como o web (modo migração).
        """
        env_block = '\n'.join(f'      {k}: "{v}"' for k, v in env_vars.items() if k != 'SLUG')
        labels_custom = dominios.labels_yaml(labels_dominios or {})
        bloco_mcp = self._bloco_mcp_yaml(versao, regiao, servico_mcp, replicas_app, traefik) if servico_mcp else ''
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
      replicas: {replicas_app}
      placement:
        constraints:
          - node.labels.region == {regiao}
      restart_policy:
        condition: any
        delay: 60s
        window: 120s
      labels:
        - "traefik.enable={'true' if traefik else 'false'}"
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
      replicas: {replicas_app}
      placement:
        constraints:
          - node.labels.region == {regiao}
      restart_policy:
        condition: any
        delay: 60s
        window: 120s
{bloco_mcp}
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

    @staticmethod
    def _bloco_mcp_yaml(versao, regiao, servico_mcp, replicas_app, traefik):
        replicas = min(replicas_app, servico_mcp['replicas'])
        labels = dict(servico_mcp['labels'])
        if not traefik:
            labels['traefik.enable'] = 'false'
        env_block = '\n'.join(f'      {k}: "{v}"' for k, v in servico_mcp['env'].items())
        return f"""
  mcp:
    image: {ERP_IMAGE}:{versao}
    entrypoint: {json.dumps(mcp.COMANDO)}
    environment:
{env_block}
    networks:
      - traefik-public
      - internal
    deploy:
      replicas: {replicas}
      placement:
        constraints:
          - node.labels.region == {regiao}
      restart_policy:
        condition: any
        delay: 60s
        window: 120s
      labels:{dominios.labels_yaml(labels)}
"""
