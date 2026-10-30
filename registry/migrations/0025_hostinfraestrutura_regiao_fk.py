import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    """Converte HostInfraestrutura.regiao em chave estrangeira mantendo a coluna ``regiao``
    (os valores gravados já são os slugs das regiões criadas na 0024)."""

    dependencies = [
        ('registry', '0024_regiao'),
    ]

    operations = [
        migrations.AlterField(
            model_name='hostinfraestrutura',
            name='regiao',
            field=models.ForeignKey(db_column='regiao', on_delete=django.db.models.deletion.PROTECT, related_name='hosts', to='registry.regiao', verbose_name='Região'),
        ),
    ]
