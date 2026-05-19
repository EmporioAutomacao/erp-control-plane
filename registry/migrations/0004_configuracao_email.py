from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('registry', '0003_cliente_isento_cobranca'),
    ]

    operations = [
        migrations.CreateModel(
            name='ConfiguracaoEmail',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('email_host', models.CharField(max_length=200, verbose_name='Servidor SMTP')),
                ('email_port', models.PositiveIntegerField(default=587, verbose_name='Porta')),
                ('email_use_tls', models.BooleanField(default=True, verbose_name='Usar TLS')),
                ('email_host_user', models.CharField(max_length=200, verbose_name='Usuário SMTP')),
                ('email_host_password', models.CharField(max_length=200, verbose_name='Senha SMTP')),
                ('default_from_email', models.EmailField(verbose_name='E-mail remetente padrão')),
                ('atualizado_em', models.DateTimeField(auto_now=True)),
            ],
            options={
                'verbose_name': 'Configuração de E-mail',
                'verbose_name_plural': 'Configuração de E-mail',
            },
        ),
    ]
