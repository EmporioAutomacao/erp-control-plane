import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('registry', '0001_initial'),
    ]

    operations = [
        migrations.RenameModel('Tenant', 'Cliente'),
        migrations.RenameModel('HealthCheck', 'VerificacaoSaude'),
        migrations.RenameField(
            model_name='provisionamentolog',
            old_name='tenant',
            new_name='cliente',
        ),
        migrations.RenameField(
            model_name='atualizacaoversao',
            old_name='tenant',
            new_name='cliente',
        ),
        migrations.RenameField(
            model_name='verificacaosaude',
            old_name='tenant',
            new_name='cliente',
        ),
        migrations.AlterField(
            model_name='verificacaosaude',
            name='cliente',
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name='verificacoes_saude',
                to='registry.cliente',
            ),
        ),
        migrations.AlterModelOptions(
            name='cliente',
            options={
                'ordering': ['-criado_em'],
                'verbose_name': 'Cliente',
                'verbose_name_plural': 'Clientes',
            },
        ),
        migrations.AlterModelOptions(
            name='verificacaosaude',
            options={
                'get_latest_by': 'verificado_em',
                'ordering': ['-verificado_em'],
                'verbose_name': 'Verificação de Saúde',
                'verbose_name_plural': 'Verificações de Saúde',
            },
        ),
        migrations.AlterModelTable(
            name='verificacaosaude',
            table='registry_verificacao_saude',
        ),
    ]
