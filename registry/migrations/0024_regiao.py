import django.core.validators
from django.db import migrations, models

# Regiões que existiam fixas em HostInfraestrutura.REGIAO_CHOICES.
REGIOES_INICIAIS = {'anapolis': 'Anápolis', 'brasilia': 'Brasília'}


def criar_regioes(apps, schema_editor):
    Regiao = apps.get_model('registry', 'Regiao')
    HostInfraestrutura = apps.get_model('registry', 'HostInfraestrutura')
    slugs = dict(REGIOES_INICIAIS)
    # Qualquer valor já gravado nos hosts precisa existir antes de virar chave estrangeira.
    for slug in HostInfraestrutura.objects.values_list('regiao', flat=True).distinct():
        slugs.setdefault(slug, slug)
    for slug, nome in slugs.items():
        Regiao.objects.get_or_create(slug=slug, defaults={'nome': nome})


class Migration(migrations.Migration):

    dependencies = [
        ('registry', '0023_atualizacao_tag_anterior'),
    ]

    operations = [
        migrations.CreateModel(
            name='Regiao',
            fields=[
                ('slug', models.CharField(help_text='Valor do label do nó no Swarm (node.labels.region). Não pode ser alterado depois de salvo.', max_length=20, primary_key=True, serialize=False, validators=[django.core.validators.RegexValidator(message='Use só letras minúsculas sem acento, números e hífen (ex.: hostinger, sao-paulo).', regex='^[a-z0-9][a-z0-9-]*$')], verbose_name='Código')),
                ('nome', models.CharField(max_length=100, unique=True)),
                ('ativo', models.BooleanField(default=True, help_text='Regiões inativas não aparecem no cadastro de hosts.')),
                ('observacoes', models.TextField(blank=True)),
            ],
            options={
                'verbose_name': 'Região',
                'verbose_name_plural': 'Regiões',
                'ordering': ['slug'],
            },
        ),
        migrations.RunPython(criar_regioes, migrations.RunPython.noop),
    ]
