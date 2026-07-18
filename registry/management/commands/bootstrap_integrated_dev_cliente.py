import json
import os

import psycopg2
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from registry.models import Cliente, Modulo, Plano


class Command(BaseCommand):
    help = "Cria/atualiza o cliente desenvolvimento no CP e sincroniza o snapshot do plano no ERP dev."

    def add_arguments(self, parser):
        parser.add_argument("--slug", default="desenvolvimento")
        parser.add_argument("--nome", default="Desenvolvimento")
        parser.add_argument("--cnpj", default="00.000.000/0001-00")
        parser.add_argument("--email", default="dev@localhost")
        parser.add_argument("--plano", default="pro")
        parser.add_argument("--erp-db-host", default=os.getenv("ERP_CLIENTE_DB_HOST", "erp_cliente_db"))
        parser.add_argument("--erp-db-port", default=os.getenv("ERP_CLIENTE_DB_PORT", "5432"))
        parser.add_argument("--erp-db-name", default=os.getenv("ERP_CLIENTE_DB_NAME", "erp_desenvolvimento"))
        parser.add_argument("--erp-db-user", default=os.getenv("ERP_CLIENTE_DB_USER", "erp_desenvolvimento"))
        parser.add_argument("--erp-db-password", default=os.getenv("ERP_CLIENTE_DB_PASSWORD", "erp_desenvolvimento_dev"))
        parser.add_argument(
            "--skip-erp-sync",
            action="store_true",
            help="Apenas cria/atualiza o cliente no CP, sem escrever o snapshot no ERP.",
        )

    def handle(self, *args, **options):
        slug = options["slug"].strip()
        plano_slug = options["plano"].strip()

        try:
            plano = Plano.objects.get(slug=plano_slug, ativo=True)
        except Plano.DoesNotExist as exc:
            raise CommandError(f'Plano ativo "{plano_slug}" nao encontrado no CP.') from exc

        modulos = list(plano.modulos_inclusos.filter(ativo=True).order_by("slug"))
        if not modulos:
            modulos = list(Modulo.objects.filter(ativo=True).order_by("slug"))

        cliente, created = Cliente.objects.update_or_create(
            slug=slug,
            defaults={
                "nome": options["nome"].strip(),
                "cnpj": options["cnpj"].strip(),
                "email_contato": options["email"].strip(),
                "subdominio": f"{slug}.localhost",
                "plano": plano,
                "status": "ativo",
                "versao_erp": "dev",
                "stack_path": f"docker-compose.integrated-dev.yml#{slug}",
                "isento_cobranca": True,
                "motivo_isencao": "Ambiente de desenvolvimento integrado.",
                "observacoes": "Cliente criado para desenvolvimento local CP + ERP + SyncAgent.",
            },
        )
        cliente.modulos_ativos.set(modulos)

        if not options["skip_erp_sync"]:
            self._sync_erp_snapshot(cliente, plano, modulos, options)

        result = {
            "created": created,
            "cliente_id": str(cliente.id),
            "slug": cliente.slug,
            "nome": cliente.nome,
            "plano": plano.slug,
            "modulos": [modulo.slug for modulo in modulos],
        }
        self.stdout.write(json.dumps(result, ensure_ascii=False))

    def _sync_erp_snapshot(self, cliente, plano, modulos, options):
        limites = {
            "max_usuarios": plano.max_usuarios,
            "max_empresas": plano.max_empresas,
            "recursos_cpu": plano.recursos_cpu,
            "recursos_ram_gb": plano.recursos_ram_gb,
        }
        now = timezone.now()

        conn = psycopg2.connect(
            dbname=options["erp_db_name"],
            user=options["erp_db_user"],
            password=options["erp_db_password"],
            host=options["erp_db_host"],
            port=int(options["erp_db_port"]),
            connect_timeout=10,
        )
        try:
            with conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        """
                        INSERT INTO core_planocliente (
                            id,
                            cp_cliente_id,
                            cp_cliente_nome,
                            plano_nome,
                            status_assinatura,
                            modulos_assinados,
                            limites_plano,
                            ultima_sincronizacao_cp,
                            observacoes,
                            atualizado_em
                        )
                        VALUES (
                            1,
                            %s,
                            %s,
                            %s,
                            %s,
                            %s::jsonb,
                            %s::jsonb,
                            %s,
                            %s,
                            %s
                        )
                        ON CONFLICT (id) DO UPDATE SET
                            cp_cliente_id = EXCLUDED.cp_cliente_id,
                            cp_cliente_nome = EXCLUDED.cp_cliente_nome,
                            plano_nome = EXCLUDED.plano_nome,
                            status_assinatura = EXCLUDED.status_assinatura,
                            modulos_assinados = EXCLUDED.modulos_assinados,
                            limites_plano = EXCLUDED.limites_plano,
                            ultima_sincronizacao_cp = EXCLUDED.ultima_sincronizacao_cp,
                            observacoes = EXCLUDED.observacoes,
                            atualizado_em = EXCLUDED.atualizado_em
                        """,
                        [
                            str(cliente.id),
                            cliente.nome,
                            plano.nome,
                            "ativo",
                            json.dumps([modulo.slug for modulo in modulos]),
                            json.dumps(limites),
                            now,
                            "Snapshot sincronizado pelo CP no ambiente Docker de desenvolvimento.",
                            now,
                        ],
                    )
        finally:
            conn.close()
