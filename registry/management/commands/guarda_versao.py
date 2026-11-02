"""Trava anti-downgrade no boot: nenhuma imagem mais antiga sobe num banco que já viu versão maior.

É a última barreira: cobre o que passa por fora do painel de controle (redeploy no Portainer,
`docker service update`, `docker service rollback`). Não existe variável nem flag de desvio;
para corrigir uma versão ruim, publica-se um número maior.

- `verificar` roda antes do `migrate` (e no cron): encerra com erro se o código for mais antigo
  que a versão máxima registrada ou se o banco tiver migration à frente do código.
- `registrar` roda antes do `migrate`, porque o schema pode mudar a partir dali, mesmo se o
  `migrate` falhar no meio.

A tabela é criada por SQL, e não por migration, porque o comando roda antes do `migrate`.
"""
import re

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction
from django.db.migrations.loader import MigrationLoader
from django.db.migrations.recorder import MigrationRecorder
from django.utils import timezone

TABELA = 'guarda_versao'
_SEMVER = re.compile(r'^\d+\.\d+\.\d+$')
_NUMERO_MIGRATION = re.compile(r'^(\d+)_')


def _tupla(versao):
    return tuple(int(p) for p in versao.split('.'))


def _numero(nome_migration):
    achado = _NUMERO_MIGRATION.match(nome_migration)
    return int(achado.group(1)) if achado else None


def migrations_a_frente():
    """Migrations aplicadas com número maior que o último em disco do app: o banco é mais novo que o código.

    Só o número conta, e só em apps que ainda têm migrations numeradas em disco. Migration
    apagada do repositório não é sinal de downgrade: bancos antigos ainda guardam, por exemplo,
    as `0002`–`0009` do `mercadolivre`, removidas há tempos, e não podem ser recusados por isso.
    """
    maior_em_disco = {}
    for app, nome in MigrationLoader(None).disk_migrations:
        numero = _numero(nome)
        if numero is not None:
            maior_em_disco[app] = max(numero, maior_em_disco.get(app, 0))

    a_frente = []
    for app, nome in MigrationRecorder(connection).applied_migrations():
        numero = _numero(nome)
        if numero is not None and app in maior_em_disco and numero > maior_em_disco[app]:
            a_frente.append(f'{app}.{nome}')
    return sorted(a_frente)


def _ler_maxima():
    if TABELA not in connection.introspection.table_names():
        return ''
    with connection.cursor() as cursor:
        cursor.execute(f'SELECT versao_maxima FROM {TABELA} WHERE id = 1')
        linha = cursor.fetchone()
    return linha[0] if linha else ''


def _erro_downgrade(versao, maxima):
    return CommandError(
        f'[guarda_versao] DOWNGRADE RECUSADO: esta imagem é a versão {versao}, mas este banco já '
        f'rodou a versão {maxima}. Suba a imagem {maxima} ou maior; voltar de versão não é permitido.'
    )


class Command(BaseCommand):
    help = 'Recusa subir código mais antigo que a versão máxima já registrada neste banco.'

    def add_arguments(self, parser):
        parser.add_argument('acao', choices=['verificar', 'registrar'])

    def handle(self, *args, **options):
        versao = str(settings.VERSION)
        if not _SEMVER.match(versao):
            if settings.DEBUG:
                self.stdout.write(f'[guarda_versao] versão "{versao}" ignorada (DEBUG).')
                return
            raise CommandError(f'[guarda_versao] settings.VERSION "{versao}" não é uma versão X.Y.Z.')
        if options['acao'] == 'verificar':
            self._verificar(versao)
        else:
            self._registrar(versao)

    def _verificar(self, versao):
        maxima = _ler_maxima()
        if maxima and _tupla(versao) < _tupla(maxima):
            raise _erro_downgrade(versao, maxima)
        a_frente = migrations_a_frente()
        if a_frente:
            raise CommandError(
                f'[guarda_versao] DOWNGRADE RECUSADO: o banco tem migrations que a versão {versao} '
                f'não conhece: {", ".join(a_frente)}. Suba uma imagem mais nova.'
            )
        self.stdout.write(f'[guarda_versao] versão {versao} ok (máxima registrada: {maxima or "nenhuma"}).')

    def _registrar(self, versao):
        agora = timezone.now().isoformat()
        with transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(
                f'CREATE TABLE IF NOT EXISTS {TABELA} ('
                'id INTEGER PRIMARY KEY, versao_maxima VARCHAR(20) NOT NULL, atualizada_em VARCHAR(40) NOT NULL)'
            )
            cursor.execute(
                f'INSERT INTO {TABELA} (id, versao_maxima, atualizada_em) VALUES (1, %s, %s) '
                'ON CONFLICT (id) DO NOTHING',
                [versao, agora],
            )
            # Trava a linha: dois containers subindo juntos não podem baixar o valor.
            cursor.execute(f'SELECT versao_maxima FROM {TABELA} WHERE id = 1 FOR UPDATE')
            maxima = cursor.fetchone()[0]
            if _tupla(versao) < _tupla(maxima):
                raise _erro_downgrade(versao, maxima)
            if _tupla(versao) > _tupla(maxima):
                cursor.execute(
                    f'UPDATE {TABELA} SET versao_maxima = %s, atualizada_em = %s WHERE id = 1',
                    [versao, agora],
                )
