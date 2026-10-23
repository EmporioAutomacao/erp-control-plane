from django.core import serializers
from django.core.management.base import BaseCommand
from django.db import transaction

from registry.models import Modulo, Plano

FIXTURE = "registry/fixtures/initial_data.json"


class Command(BaseCommand):
    help = (
        "Carrega a fixture inicial de Modulos e Planos SEM sobrescrever o que ja "
        "existe no banco. Diferente do loaddata (que roda a cada boot e desfazia "
        "edicoes do admin, ex.: precos dos planos), aqui so os registros ausentes "
        "sao criados. Um Modulo novo tambem e incluido nos Planos ja existentes "
        "que o listam na fixture."
    )

    def handle(self, *args, **options):
        with open(FIXTURE, encoding="utf-8") as f:
            objetos = list(serializers.deserialize("json", f))

        modulos_novos = set()
        planos_fixture = []
        criados = 0

        with transaction.atomic():
            for obj in objetos:
                instancia = obj.object
                if isinstance(instancia, Modulo):
                    if not Modulo.objects.filter(pk=instancia.pk).exists():
                        obj.save()
                        modulos_novos.add(instancia.pk)
                        criados += 1
                elif isinstance(instancia, Plano):
                    planos_fixture.append(obj)

            for obj in planos_fixture:
                instancia = obj.object
                if not Plano.objects.filter(pk=instancia.pk).exists():
                    obj.save()  # salva tambem o M2M modulos_inclusos
                    criados += 1
                    continue
                inclusos = set(obj.m2m_data.get("modulos_inclusos", []))
                adicionar = inclusos & modulos_novos
                if adicionar:
                    Plano.objects.get(pk=instancia.pk).modulos_inclusos.add(*adicionar)

        self.stdout.write(f"[CP] Dados iniciais: {criados} registro(s) criado(s); existentes preservados.")
