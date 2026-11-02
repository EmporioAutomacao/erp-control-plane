from django.db import migrations, models


def _inicializar_maxima(apps, schema_editor):
    from registry.versoes import maior

    Cliente = apps.get_model('registry', 'Cliente')
    for cliente in Cliente.objects.all().iterator():
        # O alvo só conta em cliente já ativado: nos demais pode nunca ter sido implantado.
        alvo = cliente.versao_erp if cliente.data_ativacao else ''
        maxima = maior(alvo, cliente.versao_erp_detectada)
        if maxima:
            Cliente.objects.filter(pk=cliente.pk).update(versao_erp_maxima=maxima)


class Migration(migrations.Migration):

    dependencies = [
        ('registry', '0033_saude_a_cada_15_min'),
    ]

    operations = [
        migrations.AddField(
            model_name='cliente',
            name='versao_erp_maxima',
            field=models.CharField(blank=True, editable=False, help_text='Piso da trava anti-downgrade (registry/versoes.py). Só sobe.', max_length=20, verbose_name='Maior versão ERP já vista'),
        ),
        migrations.AddField(
            model_name='backupcliente',
            name='versao_erp',
            field=models.CharField(blank=True, help_text='Vazio = desconhecida (backups antigos ou importados sem stack.yml).', max_length=20, verbose_name='Versão do ERP no backup'),
        ),
        migrations.RunPython(_inicializar_maxima, migrations.RunPython.noop),
    ]
