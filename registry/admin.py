import os
from django import forms
from django.contrib import admin
from django.http import JsonResponse
from django.urls import path
from django.shortcuts import get_object_or_404, redirect
from django.utils.html import format_html, mark_safe
from unfold.admin import ModelAdmin
from .models import Modulo, Plano, HostInfraestrutura, Cliente, ProvisionamentoLog, AtualizacaoVersao, VerificacaoSaude, ConfiguracaoEmail


@admin.register(Modulo)
class ModuloAdmin(ModelAdmin):
    list_display = ['slug', 'nome', 'preco_mensal', 'ativo']
    list_editable = ['ativo']


@admin.register(Plano)
class PlanoAdmin(ModelAdmin):
    list_display = ['slug', 'nome', 'preco_mensal', 'max_usuarios', 'ativo', 'destaque', 'ordem']
    list_editable = ['ativo', 'destaque', 'ordem']
    filter_horizontal = ['modulos_inclusos']


@admin.register(HostInfraestrutura)
class HostInfraestruturaAdmin(ModelAdmin):
    list_display = ['nome', 'regiao', 'tipo', 'ip', 'cpu_total', 'ram_gb_total', 'ativo']
    list_filter = ['regiao', 'tipo', 'ativo']


_STATUS_CORES = {
    'ativo':               '#2e7d32',
    'trial':               '#1565c0',
    'aguardando_provisao': '#e65100',
    'provisionando':       '#f57f17',
    'atualizando':         '#6a1b9a',
    'suspenso':            '#757575',
    'cancelado':           '#424242',
    'trial_expirado':      '#bf360c',
    'erro_provisao':       '#b71c1c',
}

_LOG_CORES = {
    'concluido':  ('#e8f5e9', '#2e7d32'),
    'executando': ('#fff8e1', '#f57f17'),
    'erro':       ('#ffebee', '#b71c1c'),
    'pendente':   ('#f5f5f5', '#757575'),
}


class ProvisionamentoLogInline(admin.TabularInline):  # noqa: keep django TabularInline (unfold doesn't override it)
    model = ProvisionamentoLog
    extra = 0
    readonly_fields = ['etapa', 'status_colorido', 'mensagem', 'iniciado_em', 'concluido_em']
    fields = ['etapa', 'status_colorido', 'mensagem', 'iniciado_em', 'concluido_em']
    can_delete = False

    def status_colorido(self, obj):
        bg, fg = _LOG_CORES.get(obj.status, ('#f5f5f5', '#333'))
        label = obj.get_status_display()
        return format_html(
            '<span style="background:{};color:{};padding:2px 8px;border-radius:12px;font-size:11px;font-weight:600;">{}</span>',
            bg, fg, label,
        )
    status_colorido.short_description = 'Status'


class AtualizacaoVersaoInline(admin.TabularInline):
    model = AtualizacaoVersao
    extra = 0
    readonly_fields = ['versao_anterior', 'versao_nova', 'status', 'iniciada_em', 'concluida_em']
    can_delete = False


def acao_reprovisionar(modeladmin, request, queryset):
    from .tasks import task_provisionar_cliente
    for cliente in queryset:
        cliente.logs.all().delete()
        task_provisionar_cliente.delay(str(cliente.pk))
    modeladmin.message_user(request, f'{queryset.count()} cliente(s) enviado(s) para re-provisionamento.')

acao_reprovisionar.short_description = 'Re-provisionar cliente(s)'


def acao_destruir(modeladmin, request, queryset):
    from .provisioning import MotorProvisionamento
    for cliente in queryset:
        MotorProvisionamento(cliente).destruir()
    modeladmin.message_user(request, f'{queryset.count()} cliente(s) destruído(s) com sucesso.')

acao_destruir.short_description = 'Destruir cliente(s) — remove stack, volumes e arquivos'


@admin.register(Cliente)
class ClienteAdmin(ModelAdmin):
    list_display = ['slug', 'nome', 'subdominio', 'plano', 'status_badge', 'isento_cobranca', 'versao_erp', 'criado_em']
    list_filter = ['status', 'plano', 'host', 'isento_cobranca']
    search_fields = ['slug', 'nome', 'cnpj', 'email_contato']
    readonly_fields = ['id', 'criado_em', 'atualizado_em', 'status_badge', 'painel_acesso', 'acoes_provisionamento', 'badge_isencao']
    filter_horizontal = ['modulos_ativos']
    actions = [acao_reprovisionar, acao_destruir]
    inlines = [ProvisionamentoLogInline, AtualizacaoVersaoInline]
    fieldsets = [
        ('Identificação', {'fields': ['id', 'slug', 'nome', 'cnpj', 'email_contato', 'telefone']}),
        ('Acesso', {'fields': ['painel_acesso']}),
        ('Infraestrutura', {'fields': ['host', 'versao_erp', 'stack_path', 'subdominio', 'dominio_custom']}),
        ('Plano', {'fields': ['plano', 'modulos_ativos']}),
        ('Faturamento', {'fields': ['asaas_customer_id', 'asaas_subscription_id', 'badge_isencao', 'isento_cobranca', 'motivo_isencao']}),
        ('Status', {'fields': ['status_badge', 'status', 'trial_ate', 'data_ativacao', 'data_suspensao', 'data_cancelamento']}),
        ('Ações', {'fields': ['acoes_provisionamento']}),
        ('Observações', {'fields': ['observacoes', 'criado_em', 'atualizado_em']}),
    ]

    def get_urls(self):
        urls = super().get_urls()
        return [
            path('<pk>/reprovisionar/', self.admin_site.admin_view(self._view_reprovisionar), name='registry_cliente_reprovisionar'),
            path('<pk>/destruir/', self.admin_site.admin_view(self._view_destruir), name='registry_cliente_destruir'),
            path('<pk>/reenviar-boas-vindas/', self.admin_site.admin_view(self._view_reenviar_boas_vindas), name='registry_cliente_reenviar_boas_vindas'),
            path('<pk>/aplicar-modulos/', self.admin_site.admin_view(self._view_aplicar_modulos), name='registry_cliente_aplicar_modulos'),
        ] + urls

    def _view_reprovisionar(self, request, pk):
        from django.contrib import messages
        from .tasks import task_provisionar_cliente
        cliente = get_object_or_404(Cliente, pk=pk)
        cliente.logs.all().delete()
        task_provisionar_cliente.delay(str(cliente.pk))
        messages.success(request, f'Re-provisionamento disparado para "{cliente.slug}".')
        return redirect('admin:registry_cliente_change', pk)

    def _view_destruir(self, request, pk):
        from django.contrib import messages
        from .provisioning import MotorProvisionamento
        cliente = get_object_or_404(Cliente, pk=pk)
        slug = cliente.slug
        MotorProvisionamento(cliente).destruir()
        messages.success(request, f'Cliente "{slug}" destruído — stack, volumes e arquivos removidos.')
        return redirect('admin:registry_cliente_changelist')

    def _view_reenviar_boas_vindas(self, request, pk):
        from django.contrib import messages
        from .tasks import task_enviar_email_boas_vindas
        cliente = get_object_or_404(Cliente, pk=pk)
        task_enviar_email_boas_vindas.delay(str(cliente.pk))
        messages.success(request, f'E-mail de boas-vindas enfileirado para "{cliente.email_contato}".')
        return redirect('admin:registry_cliente_change', pk)

    def _view_aplicar_modulos(self, request, pk):
        import subprocess
        from pathlib import Path
        from django.conf import settings
        from django.contrib import messages

        cliente = get_object_or_404(Cliente, pk=pk)
        modulos = ','.join(m.slug for m in cliente.modulos_ativos.all()) or 'financeiro,tarefas'

        base_path = Path(os.getenv('CLIENTES_BASE_PATH', '/opt/clientes'))
        env_file = base_path / cliente.slug / '.env'
        if env_file.exists():
            linhas = env_file.read_text().splitlines()
            novas = []
            encontrou = False
            for linha in linhas:
                if linha.startswith('MODULOS_ATIVOS='):
                    novas.append(f'MODULOS_ATIVOS={modulos}')
                    encontrou = True
                else:
                    novas.append(linha)
            if not encontrou:
                novas.append(f'MODULOS_ATIVOS={modulos}')
            env_file.write_text('\n'.join(novas))

        service_name = f'{cliente.slug}_web'
        try:
            result = subprocess.run(
                ['docker', 'service', 'update', '--detach', '--env-add', f'MODULOS_ATIVOS={modulos}', service_name],
                capture_output=True, text=True, timeout=30,
            )
            if result.returncode == 0:
                messages.success(
                    request,
                    f'Módulos enviados para "{cliente.slug}": {modulos}. '
                    f'O serviço está reiniciando — aguarde ~30s e recarregue o ERP.',
                )
            elif 'not found' in result.stderr:
                messages.info(request, 'Módulos salvos no .env. O cliente ainda não está provisionado — serão aplicados no próximo provisionamento.')
            else:
                messages.warning(request, f'.env atualizado, mas falha ao atualizar serviço Docker: {result.stderr.strip()}')
        except Exception as exc:
            messages.warning(request, f'.env atualizado, mas falha ao atualizar serviço Docker: {exc}')

        return redirect('admin:registry_cliente_change', pk)

    def status_badge(self, obj):
        cor = _STATUS_CORES.get(obj.status, '#757575')
        label = obj.get_status_display()
        return format_html(
            '<span style="background:{};color:#fff;padding:3px 10px;border-radius:12px;font-size:12px;font-weight:600;">{}</span>',
            cor, label,
        )
    status_badge.short_description = 'Status'

    def painel_acesso(self, obj):
        if not obj.pk:
            return '—'
        url = obj.url
        log = obj.logs.filter(etapa='criar_superuser').first()
        senha = '—'
        if log and 'senha_temp:' in log.mensagem:
            senha = log.mensagem.split('senha_temp:')[-1].strip()
        return format_html(
            '<table style="border-collapse:collapse;width:100%">'
            '<tr><td style="padding:4px 8px;font-weight:600;width:120px">URL</td>'
            '<td style="padding:4px 8px"><a href="{url}" target="_blank">{url}</a></td></tr>'
            '<tr><td style="padding:4px 8px;font-weight:600">Usuário</td>'
            '<td style="padding:4px 8px"><code>admin</code></td></tr>'
            '<tr><td style="padding:4px 8px;font-weight:600">Senha inicial</td>'
            '<td style="padding:4px 8px"><code>{senha}</code></td></tr>'
            '</table>',
            url=url, senha=senha,
        )
    painel_acesso.short_description = 'Acesso ao ERP'

    def badge_isencao(self, obj):
        if not obj.pk:
            return '—'
        if obj.isento_cobranca:
            motivo = f' — {obj.motivo_isencao}' if obj.motivo_isencao else ''
            return format_html(
                '<span style="display:inline-flex;align-items:center;gap:8px;background:#fff8e1;border:1px solid #f9a825;'
                'border-radius:6px;padding:6px 12px;">'
                '<span style="font-size:16px;">⚠️</span>'
                '<span style="color:#e65100;font-weight:600;">Isento de cobrança{}</span>'
                '</span>',
                motivo,
            )
        return mark_safe(
            '<span style="display:inline-flex;align-items:center;gap:8px;background:#e8f5e9;border:1px solid #a5d6a7;'
            'border-radius:6px;padding:6px 12px;">'
            '<span style="color:#2e7d32;font-weight:600;">✓ Cobrança ativa</span>'
            '</span>'
        )
    badge_isencao.short_description = 'Situação de cobrança'

    def acoes_provisionamento(self, obj):
        if not obj.pk:
            return '—'
        from django.urls import reverse
        url_reprov = reverse('admin:registry_cliente_reprovisionar', args=[obj.pk])
        url_destruir = reverse('admin:registry_cliente_destruir', args=[obj.pk])
        url_boas_vindas = reverse('admin:registry_cliente_reenviar_boas_vindas', args=[obj.pk])
        url_modulos = reverse('admin:registry_cliente_aplicar_modulos', args=[obj.pk])
        return format_html(
            '<p style="margin:0 0 10px;padding:8px 12px;background:#fff8e1;border-left:4px solid #f9a825;font-size:12px;color:#5d4037;">'
            '⚠️ <strong>Salve o formulário antes</strong> de aplicar — o botão lê os módulos já gravados no banco.'
            '</p>'
            '<a href="{}" style="display:inline-block;padding:6px 14px;background:#2e7d32;color:#fff;border-radius:4px;text-decoration:none;font-size:13px;" '
            'onclick="return confirm(\'O serviço web do cliente será reiniciado para aplicar os módulos selecionados (leva ~30s). Continuar?\')">⚙ Aplicar Módulos</a>'
            '&nbsp;&nbsp;'
            '<a href="{}" style="display:inline-block;padding:6px 14px;background:#417690;color:#fff;border-radius:4px;text-decoration:none;font-size:13px;" '
            'onclick="return confirm(\'ATENÇÃO: Faça backup do banco do cliente ANTES de re-provisionar. Dados podem ser perdidos se houver conflito de volume.\\n\\nConfirma que o backup foi realizado e deseja continuar?\')">Re-provisionar</a>'
            '&nbsp;&nbsp;'
            '<a href="{}" style="display:inline-block;padding:6px 14px;background:#ba2121;color:#fff;border-radius:4px;text-decoration:none;font-size:13px;" '
            'onclick="return confirm(\'Tem certeza? Isso vai apagar o stack, volumes e todos os dados do cliente.\')">Destruir</a>'
            '&nbsp;&nbsp;'
            '<a href="{}" style="display:inline-block;padding:6px 14px;background:#1565c0;color:#fff;border-radius:4px;text-decoration:none;font-size:13px;">✉ Reenviar Boas-vindas</a>',
            url_modulos, url_reprov, url_destruir, url_boas_vindas,
        )
    acoes_provisionamento.short_description = 'Provisionamento'


@admin.register(VerificacaoSaude)
class VerificacaoSaudeAdmin(ModelAdmin):
    list_display = ['cliente', 'online', 'status_http', 'latencia_ms', 'verificado_em']
    list_filter = ['online', 'cliente']


class _ConfiguracaoEmailForm(forms.ModelForm):
    email_host_password = forms.CharField(
        label='Senha SMTP',
        widget=forms.PasswordInput(render_value=True),
        required=False,
    )

    class Meta:
        model = ConfiguracaoEmail
        fields = '__all__'


@admin.register(ConfiguracaoEmail)
class ConfiguracaoEmailAdmin(ModelAdmin):
    form = _ConfiguracaoEmailForm

    fieldsets = [
        ('Servidor SMTP', {'fields': ['email_host', 'email_port', 'email_use_tls', 'email_verificar_ssl', 'email_host_user', 'email_host_password']}),
        ('Remetente', {'fields': ['default_from_email']}),
        ('Teste', {'fields': ['botao_teste_email']}),
    ]
    readonly_fields = ['atualizado_em', 'botao_teste_email']

    def get_urls(self):
        urls = super().get_urls()
        return [
            path('testar-email/', self.admin_site.admin_view(self._view_testar_email), name='registry_configuracaoemail_testar'),
        ] + urls

    def _view_testar_email(self, request):
        from django.contrib import messages
        from .email import enviar_email
        destinatario = request.GET.get('para') or request.user.email
        try:
            enviar_email(
                assunto='Teste de e-mail — AraraSuite CP',
                corpo='Este é um e-mail de teste enviado pelo painel de controle AraraSuite.',
                destinatarios=destinatario,
            )
            messages.success(request, f'E-mail de teste enviado para {destinatario}.')
        except Exception as exc:
            messages.error(request, f'Falha ao enviar: {exc}')
        return redirect('admin:registry_configuracaoemail_changelist')

    def botao_teste_email(self, obj):
        if not obj.pk:
            return '—'
        from django.urls import reverse
        url = reverse('admin:registry_configuracaoemail_testar')
        return format_html(
            '<a href="{}?para={}" style="display:inline-block;padding:6px 14px;background:#417690;color:#fff;'
            'border-radius:4px;text-decoration:none;font-size:13px;">Enviar e-mail de teste para mim</a>',
            url, obj.default_from_email or '',
        )
    botao_teste_email.short_description = 'Enviar teste'

    def has_add_permission(self, request):
        return not ConfiguracaoEmail.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False


from . import celery_beat_pt  # noqa: E402, F401
