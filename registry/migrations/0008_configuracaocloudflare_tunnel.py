from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('registry', '0007_configuracaocloudflare'),
    ]

    operations = [
        migrations.AddField(
            model_name='configuracaocloudflare',
            name='tunnel_name',
            field=models.CharField(blank=True, help_text='Nome do Cloudflare Tunnel (deixe vazio para usar o primeiro tunnel ativo encontrado).', max_length=200, verbose_name='Nome do Tunnel'),
        ),
        migrations.AlterField(
            model_name='configuracaocloudflare',
            name='server_ip',
            field=models.GenericIPAddressField(blank=True, null=True, verbose_name='IP do Servidor (legado)', help_text='Não utilizado — mantido apenas para referência.'),
        ),
    ]
