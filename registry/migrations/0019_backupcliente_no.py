from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('registry', '0018_alter_cliente_tema_site'),
    ]

    operations = [
        migrations.AddField(
            model_name='backupcliente',
            name='no',
            field=models.CharField(blank=True, max_length=100, verbose_name='nó do Swarm'),
        ),
    ]
