from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError

from registry.release_api import upsert_versao_agente


class Command(BaseCommand):
    help = (
        "Registra ou atualiza uma versao do SyncAgent/PDV no catalogo mestre "
        "(VersaoAgente). So popula o catalogo -- nao permite a versao pra "
        "nenhum cliente (isso continua manual, por cliente, em Cliente > "
        "Versoes do SyncAgent/PDV). Mesma logica do endpoint "
        "POST /v1/releases/pdv-local:register, usado pelo CI do pdv-local."
    )

    def add_arguments(self, parser):
        parser.add_argument("--versao", required=True, help="Versao do pacote (ex: 1.6.0)")
        parser.add_argument("--download-url", required=True, dest="download_url", help="URL publica do ZIP")
        parser.add_argument("--sha256", required=True, help="Hash SHA256 do ZIP (64 hex chars)")
        parser.add_argument("--release-notes", default="", dest="release_notes", help="Notas de versao (opcional)")
        parser.add_argument(
            "--erp-minimo", default=None, dest="erp_minimo",
            help="Versao minima do ERP exigida por esta versao (opcional; se omitido, preserva o valor ja cadastrado)",
        )

    def handle(self, *args, **options):
        try:
            versao_agente, created = upsert_versao_agente(
                versao=options["versao"],
                download_url=options["download_url"],
                sha256=options["sha256"],
                release_notes=options["release_notes"],
                erp_minimo=options["erp_minimo"],
            )
        except (ValueError, ValidationError) as exc:
            raise CommandError(str(exc)) from exc

        action = "Criada" if created else "Atualizada"
        self.stdout.write(self.style.SUCCESS(f"{action}: VersaoAgente v{versao_agente.versao} no catalogo."))
        self.stdout.write(f"  URL:    {versao_agente.download_url}")
        self.stdout.write(f"  SHA256: {versao_agente.sha256[:16]}…")
        self.stdout.write(
            "  Ainda nao esta permitida pra nenhum cliente -- adicione em "
            "Cliente > Versoes do SyncAgent/PDV pra quem deve recebe-la."
        )
