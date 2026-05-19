from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('registry', '0004_configuracao_email'),
    ]

    operations = [
        migrations.AddField(
            model_name='configuracaoemail',
            name='email_verificar_ssl',
            field=models.BooleanField(default=True, help_text='Desative se o certificado do servidor não corresponder ao hostname.', verbose_name='Verificar SSL'),
        ),
    ]
