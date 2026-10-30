"""Aplica a retencao de 30 SincronizacaoVersoesAgente por cliente ao historico
ja acumulado (o push automatico reenviava o mesmo pacote a cada save do
catalogo). Daqui pra frente cp_push.podar_sincronizacoes roda a cada envio."""

from django.db import migrations

MANTER = 30


def podar(apps, schema_editor):
    Sincronizacao = apps.get_model('registry', 'SincronizacaoVersoesAgente')
    clientes = Sincronizacao.objects.values_list('cliente_id', flat=True).distinct()
    for cliente_id in list(clientes):
        antigos = list(
            Sincronizacao.objects.filter(cliente_id=cliente_id)
            .order_by('-iniciada_em', '-pk')
            .values_list('pk', flat=True)[MANTER:]
        )
        if antigos:
            Sincronizacao.objects.filter(pk__in=antigos).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('registry', '0030_agenda_verificacoes_periodicas'),
    ]

    operations = [
        migrations.RunPython(podar, migrations.RunPython.noop),
    ]
