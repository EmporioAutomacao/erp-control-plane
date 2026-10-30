"""CPUs, memória e disco do Host de Infraestrutura deixam de ser digitados: vêm da
verificação de status (Swarm no manager + worker do CP no nó). Os campos manuais
eram só informativos e nada no CP os usava."""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('registry', '0031_poda_sincronizacoes_versoes'),
    ]

    operations = [
        migrations.RemoveField(model_name='hostinfraestrutura', name='cpu_total'),
        migrations.RemoveField(model_name='hostinfraestrutura', name='ram_gb_total'),
        migrations.AlterField(
            model_name='hostinfraestrutura',
            name='cpus_swarm',
            field=models.FloatField(blank=True, null=True, verbose_name='CPUs'),
        ),
        migrations.AlterField(
            model_name='hostinfraestrutura',
            name='ram_bytes_swarm',
            field=models.BigIntegerField(blank=True, null=True, verbose_name='Memória'),
        ),
        migrations.AlterField(
            model_name='hostinfraestrutura',
            name='disco_total_bytes',
            field=models.BigIntegerField(blank=True, null=True, verbose_name='Disco'),
        ),
    ]
