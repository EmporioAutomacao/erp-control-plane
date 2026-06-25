from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('registry', '0009_backupcliente'),
    ]

    operations = [
        migrations.AddField(
            model_name='backupcliente',
            name='progresso',
            field=models.PositiveSmallIntegerField(default=0),
        ),
        migrations.AddField(
            model_name='backupcliente',
            name='restaurando',
            field=models.BooleanField(default=False),
        ),
    ]
