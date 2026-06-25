import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('registry', '0008_configuracaocloudflare_tunnel'),
    ]

    operations = [
        migrations.CreateModel(
            name='BackupCliente',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('criado_em', models.DateTimeField(auto_now_add=True)),
                ('arquivo_path', models.CharField(blank=True, max_length=500)),
                ('tamanho_bytes', models.BigIntegerField(default=0)),
                ('status', models.CharField(
                    choices=[('em_andamento', 'Em andamento'), ('concluido', 'Concluído'), ('erro', 'Erro')],
                    default='em_andamento', max_length=20,
                )),
                ('mensagem', models.TextField(blank=True)),
                ('cliente', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='backups',
                    to='registry.cliente',
                )),
            ],
            options={
                'verbose_name': 'Backup',
                'verbose_name_plural': 'Backups',
                'ordering': ['-criado_em'],
            },
        ),
    ]
