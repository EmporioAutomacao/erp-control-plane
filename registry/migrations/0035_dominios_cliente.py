"""Vários domínios próprios por cliente: `Cliente.dominio_custom` vira `DominioCliente` (principal).

Preserva o que está publicado hoje: o domínio antigo vira o principal, no modo "abre o ERP",
com www (era sempre criado), e `dominios_aplicados` registra os dois hostnames que o
"Configurar Cloudflare" antigo já publicou — para a remoção funcionar se ele sair da lista.
"""
import django.db.models.deletion
from django.db import migrations, models


def _copiar_para_dominios(apps, schema_editor):
    Cliente = apps.get_model('registry', 'Cliente')
    DominioCliente = apps.get_model('registry', 'DominioCliente')
    for cliente in Cliente.objects.exclude(dominio_custom='').iterator():
        dominio = (cliente.dominio_custom or '').strip().lower().rstrip('.')
        if not dominio or DominioCliente.objects.filter(dominio=dominio).exists():
            continue
        DominioCliente.objects.create(
            cliente_id=cliente.pk, dominio=dominio, modo='servir', principal=True, incluir_www=True,
        )
        Cliente.objects.filter(pk=cliente.pk).update(dominios_aplicados=[dominio, f'www.{dominio}'])


def _voltar_para_dominio_custom(apps, schema_editor):
    Cliente = apps.get_model('registry', 'Cliente')
    DominioCliente = apps.get_model('registry', 'DominioCliente')
    for d in DominioCliente.objects.filter(principal=True):
        Cliente.objects.filter(pk=d.cliente_id).update(dominio_custom=d.dominio[:200])


class Migration(migrations.Migration):

    dependencies = [
        ('registry', '0034_trava_anti_downgrade'),
    ]

    operations = [
        migrations.CreateModel(
            name='DominioCliente',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('dominio', models.CharField(help_text='Ex.: loja.com.br ou erp.loja.com.br (sem https:// nem caminho).', max_length=253, unique=True, verbose_name='Domínio')),
                ('modo', models.CharField(choices=[('servir', 'Abre o ERP'), ('redirecionar', 'Redireciona (301) para o principal')], default='servir', help_text='Redireciona: o Traefik responde 301/308 para o principal. Não use em domínio onde um SyncAgent/PDV foi ativado: o redirect descarta o token e a sincronização para (401).', max_length=20, verbose_name='Modo')),
                ('principal', models.BooleanField(default=False, help_text='Endereço de acesso do cliente (link, e-mail, health check). No máximo um; sem nenhum marcado, vale o subdomínio.', verbose_name='Principal')),
                ('incluir_www', models.BooleanField(default=True, help_text='Publica também www.<domínio>, redirecionando (301). Desligue em subdomínios (erp.loja.com.br) se não quiser www.erp.loja.com.br.', verbose_name='Incluir www')),
                ('criado_em', models.DateTimeField(auto_now_add=True)),
                ('cliente', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='dominios', to='registry.cliente')),
            ],
            options={
                'verbose_name': 'Domínio',
                'verbose_name_plural': 'Domínios',
                'db_table': 'registry_dominiocliente',
                'ordering': ['-principal', 'dominio'],
                'constraints': [models.UniqueConstraint(condition=models.Q(('principal', True)), fields=('cliente',), name='dominio_principal_unico_por_cliente')],
            },
        ),
        migrations.AddField(
            model_name='cliente',
            name='dominios_aplicados',
            field=models.JSONField(blank=True, default=list, editable=False, help_text='Hostnames publicados no Cloudflare (DNS + ingress do tunnel) no último "Aplicar Configurações". É por ele que o CP sabe o que remover quando um domínio sai da lista.'),
        ),
        migrations.RunPython(_copiar_para_dominios, _voltar_para_dominio_custom),
        migrations.RemoveField(
            model_name='cliente',
            name='dominio_custom',
        ),
    ]
