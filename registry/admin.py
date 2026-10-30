import os
from django import forms
from django.contrib import admin
from django.db.models import Count, Q
from django.http import FileResponse, Http404, JsonResponse
from django.urls import path
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.html import escape, format_html, format_html_join, mark_safe
from unfold.admin import ModelAdmin
from .models import Modulo, Plano, Regiao, HostInfraestrutura, Cliente, ProvisionamentoLog, AtualizacaoVersao, VerificacaoSaude, ConfiguracaoEmail, ConfiguracaoCloudflare, ConfiguracaoArmazenamento, ConfiguracaoAtualizacao, BackupCliente, VersaoAgente, SincronizacaoVersoesAgente, InstalacaoAgente, MigracaoCliente


@admin.register(Modulo)
class ModuloAdmin(ModelAdmin):
    list_display = ['slug', 'nome', 'preco_mensal', 'ativo']
    list_editable = ['ativo']


@admin.register(Plano)
class PlanoAdmin(ModelAdmin):
    list_display = ['slug', 'nome', 'preco_mensal', 'max_usuarios', 'ativo', 'destaque', 'ordem']
    list_editable = ['ativo', 'destaque', 'ordem']
    filter_horizontal = ['modulos_inclusos']


@admin.register(Regiao)
class RegiaoAdmin(ModelAdmin):
    list_display = ['nome', 'slug', 'qtd_hosts', 'qtd_clientes', 'ativo']
    list_editable = ['ativo']
    fields = ['nome', 'slug', 'ativo', 'observacoes', 'comando_label']
    readonly_fields = ['comando_label']

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(
            _qtd_hosts=Count('hosts', distinct=True),
            _qtd_clientes=Count('hosts__cliente', distinct=True),
        )

    def get_readonly_fields(self, request, obj=None):
        # O slug é a chave primária e o label dos nós no Swarm: trocar criaria outra região.
        if obj is not None:
            return ['slug', *self.readonly_fields]
        return self.readonly_fields

    @admin.display(description='Hosts', ordering='_qtd_hosts')
    def qtd_hosts(self, obj):
        return obj._qtd_hosts

    @admin.display(description='Clientes', ordering='_qtd_clientes')
    def qtd_clientes(self, obj):
        return obj._qtd_clientes

    @admin.display(description='Label no Swarm')
    def comando_label(self, obj):
        if obj is None or not obj.slug:
            return 'Salve a região para ver o comando.'
        return format_html(
            '<p>Rode no manager do Swarm para cada nó desta região:</p>'
            '<pre style="margin-top:6px;padding:8px;background:#263238;color:#eceff1;border-radius:4px;">'
            'docker node update --label-add region={} &lt;node-id&gt;</pre>'
            '<p style="margin-top:6px;">Clientes cujo host é desta região só sobem em nós com esse label.</p>',
            obj.slug,
        )


_HOST_CORES = {'online': '#16a34a', 'atencao': '#d97706', 'offline': '#dc2626', 'nao_verificado': '#6b7280'}
_HOST_ROTULOS = dict(HostInfraestrutura.STATUS_CHOICES)


def _host_pill(status, elemento_id=''):
    return format_html(
        '<span {} style="display:inline-block;padding:3px 12px;border-radius:12px;background:{};color:#fff;'
        'font-size:12px;font-weight:600;white-space:nowrap;">● {}</span>',
        mark_safe(f'id="{elemento_id}"') if elemento_id else '', _HOST_CORES.get(status, '#6b7280'),
        _HOST_ROTULOS.get(status, status),
    )


@admin.register(HostInfraestrutura)
class HostInfraestruturaAdmin(ModelAdmin):
    list_display = ['nome', 'status_lista', 'regiao', 'tipo', 'ip', 'cpu_total', 'ram_gb_total', 'verificado_quando', 'ativo']
    list_filter = ['status_conexao', 'regiao', 'tipo', 'ativo']
    readonly_fields = ['status_painel', 'botao_verificar']
    fieldsets = [
        ('Cadastro', {'fields': ['nome', 'regiao', 'tipo', 'ip', 'swarm_node_id', 'cpu_total', 'ram_gb_total', 'ativo', 'observacoes']}),
        ('Status', {'fields': ['status_painel', 'botao_verificar']}),
    ]

    def get_urls(self):
        return [
            path('<pk>/verificar/', self.admin_site.admin_view(self._view_verificar),
                 name='registry_hostinfraestrutura_verificar'),
            path('<pk>/verificar-agora/', self.admin_site.admin_view(self._view_verificar_agora),
                 name='registry_hostinfraestrutura_verificar_agora'),
        ] + super().get_urls()

    def _verificar(self, host):
        from .hosts import verificar_host
        from .tasks import _pedir_ping_host
        status, mensagem = verificar_host(host)
        try:
            _pedir_ping_host(host.pk)  # a resposta do worker reavalia o status em segundos
        except Exception:
            pass
        return status, mensagem

    def _view_verificar(self, request, pk):
        """JSON usado pelo formulário para atualizar o sinalizador ao abrir a tela."""
        host = get_object_or_404(HostInfraestrutura, pk=pk)
        if not self.has_view_permission(request, host):
            raise Http404
        status, mensagem = self._verificar(host)
        return JsonResponse({'status': status, 'rotulo': _HOST_ROTULOS.get(status, status), 'mensagem': mensagem})

    def _view_verificar_agora(self, request, pk):
        from django.contrib import messages
        host = get_object_or_404(HostInfraestrutura, pk=pk)
        if not self.has_change_permission(request, host):
            raise Http404
        status, mensagem = self._verificar(host)
        nivel = {'online': messages.SUCCESS, 'atencao': messages.WARNING}.get(status, messages.ERROR)
        messages.add_message(request, nivel, f'{_HOST_ROTULOS.get(status, status)}: {mensagem}')
        return redirect('admin:registry_hostinfraestrutura_change', pk)

    @admin.display(description='Status', ordering='status_conexao')
    def status_lista(self, obj):
        return format_html('<span title="{}">{}</span>', obj.status_mensagem or '', _host_pill(obj.status_conexao))

    @admin.display(description='Verificado', ordering='verificado_em')
    def verificado_quando(self, obj):
        from django.utils.timesince import timesince
        return f'há {timesince(obj.verificado_em)}' if obj.verificado_em else '—'

    @admin.display(description='Status')
    def status_painel(self, obj):
        if not obj or not obj.pk:
            return 'Salve o host para verificar o status.'
        import json
        from django.urls import reverse
        from django.utils.timesince import timesince
        from .log_operacao import fmt_bytes

        def quando(dt):
            return f'há {timesince(dt)}' if dt else 'nunca'

        linhas = [
            ('Nó no Swarm', f'{obj.no_hostname or "—"} ({obj.swarm_node_id or "sem ID"})'),
            ('CPU / RAM no Swarm', (f'{obj.cpus_swarm:g} CPUs / {fmt_bytes(obj.ram_bytes_swarm)}'
                                    if obj.cpus_swarm else '—') + f' (cadastrado: {obj.cpu_total} CPUs / {obj.ram_gb_total} GB)'),
            ('Disco do Docker', f'{fmt_bytes(obj.disco_livre_bytes)} livres de {fmt_bytes(obj.disco_total_bytes)}'
                                if obj.disco_total_bytes else '—'),
            ('Clientes neste nó', '—' if obj.containers_clientes is None else str(obj.containers_clientes)),
            ('Worker do CP respondeu', quando(obj.agente_respondeu_em)),
        ]
        tabela = format_html_join('', '<tr><td style="padding:2px 12px 2px 0;color:#6b7280;">{}</td><td>{}</td></tr>', linhas)
        url = reverse('admin:registry_hostinfraestrutura_verificar', args=[obj.pk])
        script = (
            '(function(){'
            'var cores=' + json.dumps(_HOST_CORES) + ';'
            'var rot=document.getElementById("host-badge");var msg=document.getElementById("host-msg");'
            'var q=document.getElementById("host-quando");if(!rot)return;q.textContent="verificando agora…";'
            'fetch(' + json.dumps(url) + ',{credentials:"same-origin"}).then(function(r){return r.json();}).then(function(d){'
            'rot.style.background=cores[d.status]||"#6b7280";rot.textContent="● "+d.rotulo;'
            'msg.textContent=d.mensagem;q.textContent="verificado agora";'
            '}).catch(function(){q.textContent="não foi possível verificar agora";});'
            '})();'
        )
        return format_html(
            '{} <span id="host-quando" style="margin-left:8px;color:#6b7280;font-size:12px;">verificado {}</span>'
            '<div id="host-msg" style="margin-top:6px;font-size:13px;">{}</div>'
            '<table style="margin-top:10px;font-size:13px;">{}</table><script>{}</script>',
            _host_pill(obj.status_conexao, 'host-badge'), quando(obj.verificado_em), obj.status_mensagem or '',
            tabela, mark_safe(script),
        )

    @admin.display(description='')
    def botao_verificar(self, obj):
        if not obj or not obj.pk:
            return '—'
        from django.urls import reverse
        return format_html(
            '<a href="{}" style="display:inline-block;padding:6px 14px;background:#0369a1;color:#fff;border-radius:4px;'
            'text-decoration:none;font-size:13px;">🔌 Verificar agora</a>'
            '<p style="margin-top:6px;font-size:12px;color:#6b7280;">Também é verificado automaticamente a cada 5 minutos. '
            'Online exige o nó Ready/Active no Swarm e o worker do CP respondendo nele.</p>',
            reverse('admin:registry_hostinfraestrutura_verificar_agora', args=[obj.pk]),
        )

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        if db_field.name == 'regiao':
            # Regiões inativas somem da lista, mas a atual do host continua selecionável.
            object_id = request.resolver_match.kwargs.get('object_id') if request.resolver_match else None
            filtro = Q(ativo=True)
            if object_id:
                filtro |= Q(hosts__pk=object_id)
            kwargs['queryset'] = Regiao.objects.filter(filtro).distinct()
        return super().formfield_for_foreignkey(db_field, request, **kwargs)


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

# Status de SincronizacaoVersoesAgente (enviando/concluida/erro) — chaves
# proprias, nao batem com _LOG_CORES.
_SYNC_CORES = {
    'enviando':  ('#fff8e1', '#f57f17', 'Em andamento'),
    'concluida': ('#e8f5e9', '#2e7d32', 'Concluída'),
    'erro':      ('#ffebee', '#b71c1c', 'Erro'),
}


def _sync_badge(status):
    bg, fg, label = _SYNC_CORES.get(status, ('#f5f5f5', '#333', status))
    return (
        f'<span style="background:{bg};color:{fg};padding:2px 8px;border-radius:12px;'
        f'font-size:11px;font-weight:600;">{label}</span>'
    )


class ProvisionamentoLogInline(admin.TabularInline):  # noqa: keep django TabularInline (unfold doesn't override it)
    tab = True
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
    tab = True
    model = AtualizacaoVersao
    extra = 0
    readonly_fields = ['versao_anterior', 'versao_nova', 'status', 'iniciada_em', 'concluida_em']
    can_delete = False


class SincronizacaoVersoesAgenteInline(admin.TabularInline):
    tab = True
    model = SincronizacaoVersoesAgente
    extra = 0
    fields = ['iniciada_em', 'status_badge', 'iniciada_por', 'versoes_fmt', 'resposta_http_status', 'log_fmt', 'concluida_em']
    readonly_fields = fields
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False

    def status_badge(self, obj):
        return mark_safe(_sync_badge(obj.status))
    status_badge.short_description = 'Status'

    def versoes_fmt(self, obj):
        return ', '.join(p.get('version', '?') for p in (obj.versoes_enviadas or [])) or '—'
    versoes_fmt.short_description = 'Versões enviadas'

    def log_fmt(self, obj):
        return format_html(
            '<pre style="white-space:pre-wrap;max-width:640px;max-height:200px;overflow:auto;'
            'font-size:11px;margin:0;">{}</pre>',
            obj.log or '—',
        )
    log_fmt.short_description = 'Log'


@admin.register(VersaoAgente)
class VersaoAgenteAdmin(ModelAdmin):
    change_list_template = 'admin/registry/versaoagente/change_list.html'
    list_display = ['versao', 'erp_minimo', 'ativo', 'criado_em']
    list_filter = ['ativo']
    search_fields = ['versao']
    readonly_fields = ['criado_em']

    def get_urls(self):
        urls = super().get_urls()
        return [
            path('verificar-github/', self.admin_site.admin_view(self._view_verificar_github), name='registry_versaoagente_verificar_github'),
        ] + urls

    def _view_verificar_github(self, request):
        from django.contrib import messages
        from .github_releases import sync_versoes_from_github

        try:
            resultado = sync_versoes_from_github()
        except Exception as exc:
            self.message_user(request, f'Falha ao consultar o GitHub: {exc}', level=messages.ERROR)
            return redirect('admin:registry_versaoagente_changelist')

        partes = []
        if resultado['criadas']:
            partes.append(f"criadas: {', '.join(resultado['criadas'])}")
        if resultado['atualizadas']:
            partes.append(f"atualizadas: {', '.join(resultado['atualizadas'])}")
        if not partes:
            self.message_user(request, 'Nenhuma versao nova ou alterada encontrada nos Releases do GitHub.', level=messages.INFO)
        else:
            self.message_user(request, 'Catalogo sincronizado com o GitHub — ' + '; '.join(partes) + '.', level=messages.SUCCESS)

        for versao, motivo in resultado['ignoradas']:
            self.message_user(request, f'Release v{versao} ignorado: {motivo}', level=messages.WARNING)

        return redirect('admin:registry_versaoagente_changelist')


def _migracao_ativa(cliente):
    return MigracaoCliente.objects.filter(cliente=cliente).exclude(status__in=MigracaoCliente.STATUS_FINAIS).first()


def _bloqueado_por_migracao(request, cliente):
    """Recusa operações que mexem no stack enquanto o cliente está em migração de servidor."""
    from django.contrib import messages
    if _migracao_ativa(cliente):
        messages.error(request, f'"{cliente.slug}" está em migração de servidor: aguarde ela terminar.')
        return True
    return False


def acao_reprovisionar(modeladmin, request, queryset):
    from .tasks import task_provisionar_cliente
    for cliente in queryset:
        if _bloqueado_por_migracao(request, cliente):
            continue
        cliente.logs.all().delete()
        task_provisionar_cliente.delay(str(cliente.pk))
    modeladmin.message_user(request, f'{queryset.count()} cliente(s) enviado(s) para re-provisionamento.')

acao_reprovisionar.short_description = 'Re-provisionar cliente(s)'


def acao_destruir(modeladmin, request, queryset):
    from .provisioning import MotorProvisionamento
    for cliente in queryset:
        if _bloqueado_por_migracao(request, cliente):
            continue
        MotorProvisionamento(cliente).destruir()
    modeladmin.message_user(request, f'{queryset.count()} cliente(s) destruído(s) com sucesso.')

acao_destruir.short_description = 'Destruir cliente(s) — remove stack, volumes e arquivos'


def _detalhes_log(b):
    """📜 Log expansível de um backup (e da última restauração), para diagnosticar falhas passadas."""
    partes = []
    if b.log:
        partes.append(('Log do backup', b.log))
    if b.log_restauracao:
        partes.append(('Log da última restauração', b.log_restauracao))
    if not partes:
        return ''
    corpo = ''.join(
        f'<div style="font-size:11px;color:#666;margin-top:6px;">{titulo}</div>'
        f'<pre style="margin:2px 0 0;padding:8px 10px;max-height:220px;overflow:auto;background:#0b1220;color:#cbd5e1;'
        f'border-radius:6px;font-size:11px;line-height:1.5;white-space:pre-wrap;">{escape(texto)}</pre>'
        for titulo, texto in partes
    )
    return (
        '<details style="margin-top:6px;"><summary style="cursor:pointer;font-size:12px;color:#455a64;">📜 Log</summary>'
        f'{corpo}</details>'
    )


def format_html_join_migracoes(migracoes):
    from django.urls import reverse
    itens = [
        format_html(
            '<li><a href="{}">{:%d/%m/%Y %H:%M}</a> — {} → {}: {}</li>',
            reverse('admin:registry_migracaocliente_change', args=[m.pk]), m.iniciada_em,
            m.regiao_origem, m.regiao_destino, m.get_status_display(),
        )
        for m in migracoes
    ]
    if not itens:
        return ''
    return format_html('<ul style="margin:8px 0 0 18px;font-size:12px;">{}</ul>', mark_safe(''.join(itens)))


_MIGRACAO_CORES = {
    'concluida': '#2e7d32', 'revertida': '#1565c0', 'falhou_sem_alteracao': '#757575', 'exige_intervencao': '#b71c1c',
}


@admin.register(MigracaoCliente)
class MigracaoClienteAdmin(ModelAdmin):
    list_display = ['cliente', 'regiao_origem', 'regiao_destino', 'status_colorido', 'iniciada_em', 'concluida_em']
    list_filter = ['status', 'regiao_destino']
    search_fields = ['cliente__slug', 'cliente__nome']
    fields = ['painel']
    readonly_fields = ['painel']

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def get_urls(self):
        return [
            path('<pk>/interromper/', self.admin_site.admin_view(self._view_interromper),
                 name='registry_migracaocliente_interromper'),
        ] + super().get_urls()

    def _view_interromper(self, request, pk):
        from django.contrib import messages
        from . import migracao
        if request.method != 'POST':
            return redirect('admin:registry_migracaocliente_change', pk)
        m = get_object_or_404(MigracaoCliente, pk=pk)
        if migracao.interromper(m.pk, request.user):
            messages.warning(request, 'Interrupção pedida: o cliente está voltando para a origem.')
        else:
            messages.error(request, 'Esta migração não pode mais ser interrompida (já passou do ponto sem volta ou terminou).')
        return redirect('admin:registry_migracaocliente_change', pk)

    @admin.display(description='Status')
    def status_colorido(self, obj):
        cor = _MIGRACAO_CORES.get(obj.status, '#6a1b9a')
        return format_html('<strong style="color:{};">{}</strong>', cor, obj.get_status_display())

    @admin.display(description='Migração')
    def painel(self, obj):
        from django.urls import reverse
        gates = (obj.verificacoes or {}).get('gates') or {}
        linhas_gates = ''.join(
            format_html('<tr><td><code>{}</code></td><td>{}</td><td>{}</td></tr>', codigo,
                        '✓' if g.get('ok') else '✗', g.get('detalhe', ''))
            for codigo, g in sorted(gates.items())
        )
        interromper = ''
        if obj.status in MigracaoCliente.STATUS_ANTES_DO_PONTO_SEM_VOLTA:
            # A página já está dentro do <form> do admin: o POST é montado por JS (CSRF do cookie).
            interromper = format_html(
                '<button type="button" data-url="{}" style="margin-top:12px;padding:6px 14px;background:#ba2121;'
                'color:#fff;border:0;border-radius:4px;" onclick="interromperMigracao(this.dataset.url)">'
                '⏹ Interromper e voltar para a origem</button>{}',
                reverse('admin:registry_migracaocliente_interromper', args=[obj.pk]),
                mark_safe(
                    '<script>function interromperMigracao(url) {'
                    ' if (!confirm("Interromper e voltar o cliente para a origem?")) return;'
                    ' var f = document.createElement("form"); f.method = "post"; f.action = url;'
                    ' var t = document.createElement("input"); t.type = "hidden"; t.name = "csrfmiddlewaretoken";'
                    ' t.value = (document.cookie.match(/csrftoken=([^;]+)/) || [])[1] || "";'
                    ' f.appendChild(t); document.body.appendChild(f); f.submit(); }</script>'
                ),
            )
        recarregar = mark_safe('<script>setTimeout(function(){location.reload();}, 5000);</script>') if obj.ativa else ''
        residuais = ''
        if obj.volumes_residuais:
            residuais = format_html(
                '<p><strong>Volumes residuais</strong> em <code>{}</code> (remover à mão quando quiser): <code>{}</code></p>',
                obj.volumes_residuais.get('no'), ' '.join(obj.volumes_residuais.get('volumes') or []),
            )
        return format_html(
            '<div style="font-size:13px;line-height:1.6;">'
            '<p><strong>{}</strong>: {} ({}) → {} ({}) · status <strong style="color:{};">{}</strong>'
            '{}</p>'
            '{}'
            '{}'
            '<p style="margin-top:10px;"><strong>Log</strong></p>'
            '<pre style="max-height:420px;overflow:auto;padding:10px;background:#0b1220;color:#cbd5e1;border-radius:6px;'
            'font-size:12px;white-space:pre-wrap;">{}</pre>'
            '<p style="margin-top:10px;"><strong>Verificações</strong></p>'
            '<table style="font-size:12px;">{}</table>'
            '{}{}</div>',
            obj.cliente.slug, obj.regiao_origem, obj.no_origem, obj.regiao_destino, obj.no_destino,
            _MIGRACAO_CORES.get(obj.status, '#6a1b9a'), obj.get_status_display(),
            format_html(' · ponto sem volta em {:%d/%m %H:%M:%S}', obj.ponto_sem_volta_em) if obj.ponto_sem_volta_em else '',
            format_html('<p style="color:#b71c1c;white-space:pre-wrap;"><strong>Motivo:</strong> {}</p>', obj.mensagem_erro)
            if obj.mensagem_erro else '',
            residuais,
            obj.log or '—',
            mark_safe(linhas_gates),
            interromper, recarregar,
        )


@admin.register(Cliente)
class ClienteAdmin(ModelAdmin):
    list_display = ['slug', 'nome', 'subdominio', 'plano', 'status_badge', 'isento_cobranca', 'col_versao_erp', 'criado_em']
    list_filter = ['status', 'plano', 'host', 'isento_cobranca', 'atualizacao_automatica_agente']
    search_fields = ['slug', 'nome', 'cnpj', 'email_contato']
    readonly_fields = ['id', 'criado_em', 'atualizado_em', 'status_badge', 'painel_acesso', 'acoes_provisionamento', 'badge_isencao', 'lista_backups', 'acoes_versoes', 'acoes_plano_sync', 'versao_atual', 'migracao_servidor']
    filter_horizontal = ['modulos_ativos', 'versoes_permitidas']
    actions = [acao_reprovisionar, acao_destruir]
    inlines = [ProvisionamentoLogInline, AtualizacaoVersaoInline, SincronizacaoVersoesAgenteInline]
    # Página dividida em abas (steps) pelo Unfold: cada fieldset com classes=['tab'] vira uma aba.
    fieldsets = [
        ('Cadastro', {'classes': ['tab'], 'fields': [
            'id', 'slug', 'nome', 'cnpj', 'email_contato', 'telefone', 'observacoes', 'criado_em', 'atualizado_em',
        ]}),
        ('Acesso e Infraestrutura', {'classes': ['tab'], 'fields': [
            'painel_acesso', 'host', 'migracao_servidor', 'versao_erp', 'stack_path', 'subdominio', 'dominio_custom',
        ]}),
        ('Plano e Faturamento', {'classes': ['tab'], 'fields': [
            'plano', 'modulos_ativos', 'tema_site', 'acoes_plano_sync',
            'asaas_customer_id', 'asaas_subscription_id', 'badge_isencao', 'isento_cobranca', 'motivo_isencao',
        ]}),
        ('Versões', {'classes': ['tab'], 'fields': [
            'versao_atual', 'atualizacao_automatica_agente', 'versoes_permitidas', 'acoes_versoes',
        ]}),
        ('Status e Ações', {'classes': ['tab'], 'fields': [
            'status_badge', 'status', 'trial_ate', 'data_ativacao', 'data_suspensao', 'data_cancelamento',
            'acoes_provisionamento',
        ]}),
        ('Backups', {'classes': ['tab'], 'fields': ['lista_backups']}),
    ]

    class Media:
        js = ('registry/abas_cliente.js',)

    def get_readonly_fields(self, request, obj=None):
        # Durante a migração de servidor o cadastro inteiro fica somente leitura: trocar host,
        # status, plano ou módulos dispararia operações no stack que a migração está movendo.
        if obj is not None and _migracao_ativa(obj):
            return [f.name for f in self.model._meta.fields] + [
                f.name for f in self.model._meta.many_to_many
            ] + list(self.readonly_fields)
        return super().get_readonly_fields(request, obj)

    def get_urls(self):
        urls = super().get_urls()
        return [
            path('<pk>/migrar/', self.admin_site.admin_view(self._view_migrar), name='registry_cliente_migrar'),
            path('<pk>/reprovisionar/', self.admin_site.admin_view(self._view_reprovisionar), name='registry_cliente_reprovisionar'),
            path('<pk>/destruir/', self.admin_site.admin_view(self._view_destruir), name='registry_cliente_destruir'),
            path('<pk>/reenviar-boas-vindas/', self.admin_site.admin_view(self._view_reenviar_boas_vindas), name='registry_cliente_reenviar_boas_vindas'),
            path('<pk>/aplicar-modulos/', self.admin_site.admin_view(self._view_aplicar_modulos), name='registry_cliente_aplicar_modulos'),
            path('<pk>/configurar-cloudflare/', self.admin_site.admin_view(self._view_configurar_cloudflare), name='registry_cliente_configurar_cloudflare'),
            path('<pk>/sincronizar-versoes/', self.admin_site.admin_view(self._view_sincronizar_versoes), name='registry_cliente_sincronizar_versoes'),
            path('<pk>/sincronizar-plano/', self.admin_site.admin_view(self._view_sincronizar_plano), name='registry_cliente_sincronizar_plano'),
            path('<pk>/sincronizacoes/<int:sync_id>/status/', self.admin_site.admin_view(self._view_status_sincronizacao), name='registry_cliente_sincronizacao_status'),
            path('<pk>/coletar-versoes/', self.admin_site.admin_view(self._view_coletar_versoes), name='registry_cliente_coletar_versoes'),
            path('<pk>/backup/', self.admin_site.admin_view(self._view_backup), name='registry_cliente_backup'),
            path('<pk>/backup/upload/', self.admin_site.admin_view(self._view_upload_backup), name='registry_cliente_backup_upload'),
            path('<pk>/backups/<int:backup_id>/download/', self.admin_site.admin_view(self._view_download_backup), name='registry_cliente_backup_download'),
            path('<pk>/backups/<int:backup_id>/restaurar/', self.admin_site.admin_view(self._view_restaurar), name='registry_cliente_restaurar'),
            path('<pk>/backups/<int:backup_id>/status/', self.admin_site.admin_view(self._view_status_backup), name='registry_cliente_backup_status'),
            path('<pk>/backups/<int:backup_id>/interromper/', self.admin_site.admin_view(self._view_interromper_backup), name='registry_cliente_backup_interromper'),
        ] + urls

    def _view_migrar(self, request, pk):
        from django.contrib import messages
        from . import migracao
        cliente = get_object_or_404(Cliente.objects.select_related('host'), pk=pk)
        regiao_atual = cliente.host.regiao_id if cliente.host else None
        hosts = HostInfraestrutura.objects.filter(ativo=True, regiao__ativo=True).exclude(
            regiao_id=regiao_atual).select_related('regiao').order_by('regiao__nome', 'nome')
        if request.method == 'POST':
            destino = get_object_or_404(hosts, pk=request.POST.get('host'))
            if (request.POST.get('confirmacao') or '').strip() != cliente.slug:
                messages.error(request, 'Confirmação incorreta: digite exatamente o slug do cliente.')
                return redirect(f"{request.path}?host={destino.pk}")
            try:
                m = migracao.iniciar(cliente, destino, request.user)
            except migracao.Recusa as exc:
                messages.error(request, f'Migração recusada (nada foi alterado): {exc}')
                return redirect(f"{request.path}?host={destino.pk}")
            except Exception as exc:
                messages.error(request, f'Não foi possível iniciar a migração: {exc}')
                return redirect(f"{request.path}?host={destino.pk}")
            messages.success(request, f'Migração de "{cliente.slug}" iniciada. Acompanhe abaixo.')
            return redirect('admin:registry_migracaocliente_change', m.pk)

        destino = hosts.filter(pk=request.GET.get('host')).first() if request.GET.get('host') else None
        gates = []
        aprovado = False
        if destino is not None:
            resultados, _ = migracao.gates_manager(cliente, destino)
            gates = sorted(resultados.items())
            aprovado = all(g['ok'] for _, g in gates)
        context = {
            **self.admin_site.each_context(request),
            'title': 'Migrar servidor', 'cliente': cliente, 'hosts': hosts,
            'destino': destino, 'gates': gates, 'aprovado': aprovado,
        }
        return render(request, 'registry/admin_migrar.html', context)

    def migracao_servidor(self, obj):
        if not obj.pk:
            return '—'
        from django.urls import reverse
        ativa = _migracao_ativa(obj)
        if ativa:
            url = reverse('admin:registry_migracaocliente_change', args=[ativa.pk])
            return format_html(
                '<p style="margin:0;padding:8px 12px;background:#ede7f6;border-left:4px solid #6a1b9a;font-size:13px;">'
                '🚚 <strong>Migração em andamento</strong> ({}): {} → {}. O cadastro está somente leitura. '
                '<a href="{}">Acompanhar</a></p>',
                ativa.get_status_display(), ativa.regiao_origem, ativa.regiao_destino, url,
            )
        historico = MigracaoCliente.objects.filter(cliente=obj)[:5]
        linhas = format_html_join_migracoes(historico)
        url = reverse('admin:registry_cliente_migrar', args=[obj.pk])
        return format_html(
            '<a href="{}" style="display:inline-block;padding:6px 14px;background:#6a1b9a;color:#fff;border-radius:4px;'
            'text-decoration:none;font-size:13px;">🚚 Migrar servidor</a>{}',
            url, linhas,
        )
    migracao_servidor.short_description = 'Migração de servidor'

    def _view_reprovisionar(self, request, pk):
        from django.contrib import messages
        from .tasks import task_provisionar_cliente
        cliente = get_object_or_404(Cliente, pk=pk)
        if _bloqueado_por_migracao(request, cliente):
            return redirect('admin:registry_cliente_change', pk)
        cliente.logs.all().delete()
        task_provisionar_cliente.delay(str(cliente.pk))
        messages.success(request, f'Re-provisionamento disparado para "{cliente.slug}".')
        return redirect('admin:registry_cliente_change', pk)

    def _view_destruir(self, request, pk):
        from django.contrib import messages
        from .provisioning import MotorProvisionamento
        cliente = get_object_or_404(Cliente, pk=pk)
        if _bloqueado_por_migracao(request, cliente):
            return redirect('admin:registry_cliente_change', pk)
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
        from django.contrib import messages

        cliente = get_object_or_404(Cliente, pk=pk)
        if _bloqueado_por_migracao(request, cliente):
            return redirect('admin:registry_cliente_change', pk)
        slug = cliente.slug
        modulos = ','.join(m.slug for m in cliente.modulos_ativos.all()) or 'financeiro,tarefas'
        tema = cliente.tema_site or 'loja'
        dominio_custom = cliente.dominio_custom or ''
        subdominio = cliente.subdominio

        allowed_hosts = f'{subdominio},{dominio_custom}' if dominio_custom else subdominio
        csrf_origins = (
            f'https://{subdominio},https://{dominio_custom}' if dominio_custom
            else f'https://{subdominio}'
        )

        import secrets

        from .provisioning import site_url_cliente

        site_url = site_url_cliente(cliente)

        # Backfill do segredo de integracao CP -> erp (sincronizar-versoes) pra
        # clientes provisionados antes desta feature. Reusa o que ja estiver
        # salvo -- nunca troca um segredo existente por baixo do pano.
        cp_shared_secret = cliente.integracao_secret
        if not cp_shared_secret:
            cp_shared_secret = secrets.token_hex(32)
            Cliente.objects.filter(pk=cliente.pk).update(integracao_secret=cp_shared_secret)

        base_path = Path(os.getenv('CLIENTES_BASE_PATH', '/opt/clientes'))
        env_file = base_path / slug / '.env'
        if env_file.exists():
            campos = {
                'MODULOS_ATIVOS': modulos,
                'TEMA_SITE': tema,
                'ALLOWED_HOSTS': allowed_hosts,
                'CSRF_TRUSTED_ORIGINS': csrf_origins,
                'SITE_URL': site_url,
                'CP_CLIENTE_ID': str(cliente.id),
                'CP_CLIENTE_NOME': cliente.nome,
                'CP_CLIENTE_CNPJ': cliente.cnpj,
                'CP_SHARED_SECRET': cp_shared_secret,
            }
            linhas = env_file.read_text().splitlines()
            novas = []
            encontrados = set()
            for linha in linhas:
                chave = linha.split('=', 1)[0] if '=' in linha else ''
                if chave in campos:
                    novas.append(f'{chave}={campos[chave]}')
                    encontrados.add(chave)
                else:
                    novas.append(linha)
            for chave, valor in campos.items():
                if chave not in encontrados:
                    novas.append(f'{chave}={valor}')
            env_file.write_text('\n'.join(novas))

        service_name = f'{slug}_web'

        # Monta o comando docker service update
        cmd = [
            'docker', 'service', 'update', '--detach',
            '--env-add', f'MODULOS_ATIVOS={modulos}',
            '--env-add', f'TEMA_SITE={tema}',
            '--env-add', f'ALLOWED_HOSTS={allowed_hosts}',
            '--env-add', f'CSRF_TRUSTED_ORIGINS={csrf_origins}',
            '--env-add', f'SITE_URL={site_url}',
            '--env-add', f'CP_CLIENTE_ID={cliente.id}',
            '--env-add', f'CP_CLIENTE_NOME={cliente.nome}',
            '--env-add', f'CP_CLIENTE_CNPJ={cliente.cnpj}',
            '--env-add', f'CP_SHARED_SECRET={cp_shared_secret}',
            # Atualiza a label do router principal (subdomínio padrão) no Traefik
            '--label-add', f'traefik.http.routers.{slug}-erp.rule=Host(`{subdominio}`)',
        ]
        if dominio_custom:
            cmd += [
                # Router apex
                '--label-add', f'traefik.http.routers.{slug}-erp-custom.rule=Host(`{dominio_custom}`)',
                '--label-add', f'traefik.http.routers.{slug}-erp-custom.entrypoints=web',
                '--label-add', f'traefik.http.routers.{slug}-erp-custom.service={slug}-erp',
                '--label-rm', f'traefik.http.routers.{slug}-erp-custom.tls',
                # Router www → redirect 301 para o apex
                '--label-add', f'traefik.http.routers.{slug}-erp-custom-www.rule=Host(`www.{dominio_custom}`)',
                '--label-add', f'traefik.http.routers.{slug}-erp-custom-www.entrypoints=web',
                '--label-add', f'traefik.http.routers.{slug}-erp-custom-www.middlewares={slug}-www-redirect',
                '--label-add', f'traefik.http.middlewares.{slug}-www-redirect.redirectregex.regex=^https?://www\\.(.+)',
                '--label-add', f'traefik.http.middlewares.{slug}-www-redirect.redirectregex.replacement=https://${{1}}',
                '--label-add', f'traefik.http.middlewares.{slug}-www-redirect.redirectregex.permanent=true',
            ]
        cmd.append(service_name)

        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if result.returncode == 0:
                desc = f'módulos={modulos}, tema={tema}'
                if dominio_custom:
                    desc += f', domínio={dominio_custom} (+ {subdominio})'
                messages.success(
                    request,
                    f'Configurações aplicadas para "{slug}": {desc}. '
                    f'O serviço está reiniciando — aguarde ~30s e recarregue o ERP.',
                )
                # Cria o serviço de crons em clientes antigos e mantém o env dele
                # igual ao do web (Reposição, filas de WhatsApp etc. dependem dele).
                try:
                    from .provisioning import MotorProvisionamento

                    MotorProvisionamento(cliente).sincronizar_servico_cron()
                except Exception as exc:
                    messages.warning(request, f'Falha ao sincronizar o serviço de crons ({slug}_cron): {exc}')
                if dominio_custom:
                    try:
                        cf = self._configurar_dominio_cloudflare(dominio_custom)
                        if cf['ok']:
                            if cf['zona_criada']:
                                ns = ', '.join(cf['nameservers'])
                                messages.info(
                                    request,
                                    f'Zona "{dominio_custom}" criada no Cloudflare. '
                                    f'Informe ao cliente para trocar os nameservers no registrador para: {ns}',
                                )
                            else:
                                messages.success(request, f'CNAME de "{dominio_custom}" {cf["record"]} no Cloudflare → tunnel "{cf["tunnel_name"]}".')
                        else:
                            messages.warning(request, f'Cloudflare: {cf["erro"]}')
                    except Exception as exc:
                        messages.warning(request, f'Falha ao configurar Cloudflare: {exc}')
            elif 'not found' in result.stderr:
                messages.info(request, 'Configurações salvas no .env. O cliente ainda não está provisionado — serão aplicadas no próximo provisionamento.')
            else:
                messages.warning(request, f'.env atualizado, mas falha ao atualizar serviço Docker: {result.stderr.strip()}')
        except Exception as exc:
            messages.warning(request, f'.env atualizado, mas falha ao atualizar serviço Docker: {exc}')

        return redirect('admin:registry_cliente_change', pk)

    def _view_sincronizar_versoes(self, request, pk):
        from django.contrib import messages
        from .tasks import task_sincronizar_versoes_agente

        cliente = get_object_or_404(Cliente, pk=pk)

        if not cliente.integracao_secret:
            messages.warning(
                request,
                'Este cliente ainda não tem um segredo de integração gerado. '
                'Clique em "⚙ Aplicar Configurações" primeiro (gera o segredo e o envia pro ERP), '
                'depois tente sincronizar as versões de novo.',
            )
            return redirect('admin:registry_cliente_change', pk)

        # Passa por task (nao mais sincrono) so pra gravar o log linha a linha e
        # o admin mostrar "ao vivo" via polling. com_retry=False: disparo manual
        # nao retenta em background.
        task_sincronizar_versoes_agente.delay(
            str(pk), iniciada_por_id=request.user.id, com_retry=False,
        )
        messages.info(
            request,
            f'Sincronização iniciada para "{cliente.slug}" — o log aparece abaixo do botão e atualiza sozinho.',
        )
        return redirect('admin:registry_cliente_change', pk)

    def _view_sincronizar_plano(self, request, pk):
        from django.contrib import messages
        from .cp_push import SincronizadorPlano

        cliente = get_object_or_404(Cliente, pk=pk)

        if not cliente.integracao_secret:
            messages.warning(
                request,
                'Este cliente ainda não tem um segredo de integração gerado. '
                'Clique em "⚙ Aplicar Configurações" primeiro (gera o segredo e o envia pro ERP), '
                'depois tente sincronizar o plano de novo.',
            )
            return redirect('admin:registry_cliente_change', pk)

        # Payload pequeno e rapido -- sincrono, sem passar por task (diferente
        # de "Sincronizar Versoes", que so usa task pra ter log ao vivo).
        resultado = SincronizadorPlano(cliente).sincronizar()
        if resultado['sucesso']:
            messages.success(request, f'Plano sincronizado com "{cliente.slug}".')
        else:
            messages.error(request, f'Falha ao sincronizar plano: {resultado["mensagem"]}')
        return redirect('admin:registry_cliente_change', pk)

    def _view_status_sincronizacao(self, request, pk, sync_id):
        reg = get_object_or_404(SincronizacaoVersoesAgente, pk=sync_id, cliente__pk=pk)
        return JsonResponse({
            'status': reg.status,
            'log': reg.log,
            'ativo': reg.status == 'enviando',
            'http': reg.resposta_http_status,
        })

    def _view_coletar_versoes(self, request, pk):
        from django.contrib import messages
        from django.urls import reverse
        from .tasks import task_coletar_versoes_cliente

        cliente = get_object_or_404(Cliente, pk=pk)
        task_coletar_versoes_cliente.delay(str(pk))
        messages.info(
            request,
            f'Coleta de versões disparada para "{cliente.slug}" — a página recarrega em alguns segundos.',
        )
        return redirect(reverse('admin:registry_cliente_change', args=[pk]) + '?coletando=1')

    def _view_configurar_cloudflare(self, request, pk):
        from django.contrib import messages
        cliente = get_object_or_404(Cliente, pk=pk)
        if not cliente.dominio_custom:
            messages.warning(request, 'Nenhum domínio custom configurado para este cliente.')
            return redirect('admin:registry_cliente_change', pk)
        try:
            cf = self._configurar_dominio_cloudflare(cliente.dominio_custom)
            if cf['ok']:
                if cf['zona_criada']:
                    ns = ', '.join(cf['nameservers'])
                    messages.success(request, f'Zona "{cliente.dominio_custom}" criada no Cloudflare.')
                    messages.info(request, f'Informe ao cliente para trocar os nameservers no registrador para: {ns}')
                else:
                    messages.success(request, f'CNAME de "{cliente.dominio_custom}" {cf["record"]} no Cloudflare → tunnel "{cf["tunnel_name"]}".')
            else:
                messages.error(request, f'Cloudflare: {cf["erro"]}')
        except Exception as exc:
            messages.error(request, f'Falha ao configurar Cloudflare: {exc}')
        return redirect('admin:registry_cliente_change', pk)

    def _configurar_dominio_cloudflare(self, dominio):
        import requests as _requests

        cfg = ConfiguracaoCloudflare.obter()
        token = (cfg.cf_api_token if cfg else None) or os.getenv('CF_API_TOKEN', '')
        token = token.strip()
        if not token:
            return {'ok': False, 'erro': 'Token Cloudflare não configurado. Acesse Configurações → Cloudflare no admin do CP.'}

        tunnel_name_cfg = (cfg.tunnel_name if cfg else None) or os.getenv('CF_TUNNEL_NAME', '')

        def _cf_raise(r):
            if not r.ok:
                try:
                    erros = r.json().get('errors', [])
                    msg = erros[0].get('message', r.text) if erros else r.text
                except Exception:
                    msg = r.text
                raise Exception(f'Cloudflare {r.status_code}: {msg}')
            return r

        auth_headers = {'Authorization': f'Bearer {token}'}
        json_headers = {**auth_headers, 'Content-Type': 'application/json'}
        base = 'https://api.cloudflare.com/client/v4'

        r_verify = _requests.get(f'{base}/user/tokens/verify', headers=auth_headers, timeout=10)
        if not r_verify.ok:
            return {
                'ok': False,
                'erro': (
                    'CF_API_TOKEN inválido. Verifique: (1) é um API Token, não a Global API Key; '
                    '(2) tem permissões Zone:Read + DNS:Edit + Account:Cloudflare Tunnel:Edit. '
                    f'Cloudflare retornou {r_verify.status_code}.'
                ),
            }

        # Obtém account ID a partir de uma zona existente (não requer Account:Read)
        saas_domain = os.getenv('SAAS_DOMAIN', 'ararasuite.com.br')
        r_zone = _requests.get(f'{base}/zones', params={'name': saas_domain}, headers=auth_headers, timeout=15)
        if r_zone.ok and r_zone.json().get('result'):
            account_id = r_zone.json()['result'][0]['account']['id']
        else:
            r = _cf_raise(_requests.get(f'{base}/accounts', headers=auth_headers, timeout=15))
            accounts = r.json()['result']
            if not accounts:
                return {'ok': False, 'erro': f'Não foi possível obter o Account ID. Adicione "Account Settings:Read" ao token ou certifique que SAAS_DOMAIN ({saas_domain}) está na conta Cloudflare.'}
            account_id = accounts[0]['id']

        # Encontra o tunnel ativo
        params = {'is_deleted': 'false'}
        if tunnel_name_cfg:
            params['name'] = tunnel_name_cfg
        r = _cf_raise(_requests.get(f'{base}/accounts/{account_id}/cfd_tunnel', params=params, headers=auth_headers, timeout=15))
        tunnels = r.json()['result']
        if not tunnels:
            return {'ok': False, 'erro': 'Nenhum Cloudflare Tunnel encontrado. Configure o nome do tunnel em Configurações → Cloudflare.'}
        tunnel = tunnels[0]
        tunnel_id = tunnel['id']
        tunnel_name = tunnel['name']

        # Atualiza ingress do tunnel para incluir o domínio custom
        r = _cf_raise(_requests.get(f'{base}/accounts/{account_id}/cfd_tunnel/{tunnel_id}/configurations', headers=auth_headers, timeout=15))
        config_data = r.json()['result'].get('config', {})
        ingress = config_data.get('ingress', [])

        www_dominio = f'www.{dominio}'
        ingress_others = [
            rule for rule in ingress
            if rule.get('hostname') and rule['hostname'] not in {dominio, www_dominio}
        ]
        catch_all = next((rule for rule in ingress if not rule.get('hostname')), {'service': 'http_status:404'})
        new_ingress = [
            {'hostname': dominio,     'service': 'http://traefik_traefik:80'},
            {'hostname': www_dominio, 'service': 'http://traefik_traefik:80'},
        ] + ingress_others + [catch_all]

        _cf_raise(_requests.put(
            f'{base}/accounts/{account_id}/cfd_tunnel/{tunnel_id}/configurations',
            json={'config': {**config_data, 'ingress': new_ingress}},
            headers=json_headers, timeout=15,
        ))

        # Cria/encontra zona e cria CNAME apontando para o tunnel
        r = _cf_raise(_requests.get(f'{base}/zones', params={'name': dominio}, headers=auth_headers, timeout=15))
        zonas = r.json()['result']
        nameservers = []
        zona_criada = False

        if zonas:
            zone_id = zonas[0]['id']
        else:
            r = _cf_raise(_requests.post(f'{base}/zones', json={'name': dominio, 'jump_start': False}, headers=json_headers, timeout=15))
            data = r.json()['result']
            zone_id = data['id']
            nameservers = data.get('name_servers', [])
            zona_criada = True

        tunnel_cname = f'{tunnel_id}.cfargotunnel.com'

        def _upsert_cname(nome_label, nome_fqdn):
            """Cria/atualiza um CNAME proxied → tunnel, removendo A/AAAA/CNAME
            conflitantes no mesmo host. A busca é sempre pelo FQDN — a API do
            Cloudflare ignora labels curtas como 'www'."""
            # Remove A/AAAA conflitantes (CNAME não coexiste com eles)
            for tipo in ('A', 'AAAA'):
                rr = _requests.get(
                    f'{base}/zones/{zone_id}/dns_records',
                    params={'type': tipo, 'name': nome_fqdn},
                    headers=auth_headers, timeout=15,
                )
                if rr.ok:
                    for rec in rr.json().get('result', []):
                        _cf_raise(_requests.delete(
                            f'{base}/zones/{zone_id}/dns_records/{rec["id"]}',
                            headers=auth_headers, timeout=15,
                        ))

            rr = _cf_raise(_requests.get(
                f'{base}/zones/{zone_id}/dns_records',
                params={'type': 'CNAME', 'name': nome_fqdn},
                headers=auth_headers, timeout=15,
            ))
            recs = rr.json()['result']
            payload = {'type': 'CNAME', 'name': nome_label, 'content': tunnel_cname, 'proxied': True, 'ttl': 1}
            if recs:
                if recs[0]['content'] != tunnel_cname or not recs[0].get('proxied'):
                    _cf_raise(_requests.put(
                        f'{base}/zones/{zone_id}/dns_records/{recs[0]["id"]}',
                        json=payload, headers=json_headers, timeout=15,
                    ))
                return 'atualizado'
            _cf_raise(_requests.post(
                f'{base}/zones/{zone_id}/dns_records',
                json=payload, headers=json_headers, timeout=15,
            ))
            return 'criado'

        record_acao = _upsert_cname('@', dominio)
        _upsert_cname('www', f'www.{dominio}')

        return {'ok': True, 'zona_criada': zona_criada, 'nameservers': nameservers, 'record': record_acao, 'tunnel_name': tunnel_name}

    def _view_backup(self, request, pk):
        from django.contrib import messages
        from .tasks import task_iniciar_backup_cliente
        cliente = get_object_or_404(Cliente, pk=pk)
        if _bloqueado_por_migracao(request, cliente):
            return redirect('admin:registry_cliente_change', pk)
        backup = BackupCliente.objects.create(cliente=cliente)
        task_iniciar_backup_cliente.delay(str(cliente.pk), backup.pk)
        messages.success(request, f'Backup iniciado para "{cliente.slug}". Acompanhe o console abaixo.')
        from django.urls import reverse
        return redirect(f"{reverse('admin:registry_cliente_change', args=[pk])}?acompanhar={backup.pk}")

    def _view_upload_backup(self, request, pk):
        import tarfile
        from datetime import datetime
        from pathlib import Path
        from django.contrib import messages
        from . import storage_backup

        cliente = get_object_or_404(Cliente, pk=pk)

        if request.method == 'POST':
            arquivo = request.FILES.get('arquivo')
            if not arquivo or not arquivo.name.lower().endswith(('.tar.gz', '.tgz')):
                messages.error(request, 'Envie um arquivo .tar.gz ou .tgz válido.')
                return redirect('admin:registry_cliente_backup_upload', pk)

            backup_base = Path(os.getenv('CP_BACKUP_DIR', '/opt/backups/cp')) / 'clientes' / cliente.slug
            backup_base.mkdir(parents=True, exist_ok=True)
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            destino = backup_base / f'manual_{cliente.slug}_{timestamp}.tar.gz'

            with open(destino, 'wb') as f:
                for chunk in arquivo.chunks():
                    f.write(chunk)

            avisos = []
            try:
                with tarfile.open(destino, 'r:gz') as tar:
                    nomes = tar.getnames()
                if not any(n == 'db.sql' or n.startswith('db.sql') for n in nomes):
                    avisos.append('Não contém db.sql — a restauração não vai recuperar o banco.')
                if not any(n.startswith('media') for n in nomes):
                    avisos.append('Não contém media/ — a restauração não vai recuperar arquivos de mídia.')
            except tarfile.TarError:
                destino.unlink()
                messages.error(request, 'Arquivo inválido: não é um .tar.gz legível.')
                return redirect('admin:registry_cliente_backup_upload', pk)

            tamanho = destino.stat().st_size
            # o web roda no manager; o worker de outro nó só enxerga o bucket
            try:
                caminho = storage_backup.enviar(destino, f'clientes/{cliente.slug}/{destino.name}')
            except storage_backup.ArmazenamentoNaoConfigurado:
                messages.error(request, storage_backup.MSG_NAO_CONFIGURADO)
                return redirect('admin:registry_cliente_backup_upload', pk)
            except Exception as exc:
                messages.error(request, f'Falha ao enviar o backup para o armazenamento: {storage_backup._traduzir_erro(exc)}')
                return redirect('admin:registry_cliente_backup_upload', pk)
            finally:
                destino.unlink(missing_ok=True)

            BackupCliente.objects.create(
                cliente=cliente,
                status='concluido',
                progresso=100,
                arquivo_path=caminho,
                tamanho_bytes=tamanho,
                origem='manual',
                mensagem=' '.join(avisos),
            )
            messages.success(request, f'Backup importado para "{cliente.slug}".')
            return redirect('admin:registry_cliente_change', pk)

        context = {
            **self.admin_site.each_context(request),
            'title': 'Importar backup',
            'cliente': cliente,
        }
        return render(request, 'registry/admin_upload_backup.html', context)

    def _view_download_backup(self, request, pk, backup_id):
        from pathlib import Path
        from . import storage_backup
        backup = get_object_or_404(BackupCliente, pk=backup_id, cliente__pk=pk)
        if backup.status != 'concluido' or not backup.arquivo_path:
            raise Http404
        if storage_backup.eh_remoto(backup.arquivo_path):
            from django.contrib import messages
            nome = backup.arquivo_path.rsplit('/', 1)[-1]
            try:
                return redirect(storage_backup.url_download(backup.arquivo_path, nome))
            except storage_backup.ArmazenamentoNaoConfigurado:
                messages.error(request, storage_backup.MSG_NAO_CONFIGURADO)
                return redirect('admin:registry_cliente_change', pk)
        arquivo = Path(backup.arquivo_path)
        if not arquivo.exists():
            raise Http404
        return FileResponse(open(arquivo, 'rb'), as_attachment=True, filename=arquivo.name)

    def _view_restaurar(self, request, pk, backup_id):
        from django.contrib import messages
        from .tasks import task_restaurar_cliente
        cliente = get_object_or_404(Cliente, pk=pk)
        backup = get_object_or_404(BackupCliente, pk=backup_id, cliente=cliente)
        if _bloqueado_por_migracao(request, cliente):
            return redirect('admin:registry_cliente_change', pk)
        if backup.status != 'concluido':
            messages.error(request, 'Apenas backups concluídos podem ser restaurados.')
            return redirect('admin:registry_cliente_change', pk)
        task_restaurar_cliente.delay(str(cliente.pk), backup.pk)
        messages.warning(
            request,
            f'Restauração iniciada para "{cliente.slug}". O ERP ficará indisponível por alguns minutos. '
            f'Acompanhe o console abaixo.',
        )
        from django.urls import reverse
        return redirect(f"{reverse('admin:registry_cliente_change', args=[pk])}?acompanhar={backup.pk}&op=restauracao")

    def _view_status_backup(self, request, pk, backup_id):
        from django.utils import timezone
        backup = get_object_or_404(BackupCliente, pk=backup_id, cliente__pk=pk)
        ativo = backup.status == 'em_andamento' or backup.restaurando
        op = 'restauracao' if (backup.restaurando or request.GET.get('op') == 'restauracao') else 'backup'
        agora = timezone.now()
        inicio = backup.restauracao_iniciada_em if (op == 'restauracao' and backup.restauracao_iniciada_em) else backup.criado_em
        referencia = backup.atividade_em or inicio
        log = backup.log_restauracao if op == 'restauracao' else backup.log
        ativo_operacao = backup.restaurando if op == 'restauracao' else backup.status == 'em_andamento'
        mensagem = backup.mensagem_restauracao if op == 'restauracao' else backup.mensagem
        return JsonResponse({
            'status': backup.status,
            'progresso': backup.progresso,
            'restaurando': backup.restaurando,
            'ativo': ativo,
            'ativo_operacao': ativo_operacao,
            'operacao': op,
            'mensagem': mensagem[:300] if mensagem else '',
            'log': log,
            'decorrido_s': max(0, int((agora - inicio).total_seconds())),
            'sem_atividade_s': max(0, int((agora - referencia).total_seconds())),
            'aguardando_worker': bool(ativo_operacao and 'Aguardando um worker' in log and 'iniciou' not in log),
            'sem_inicio': bool(ativo_operacao and not log.strip()),  # nenhuma tarefa chegou a começar (nenhum worker?)
        })

    def _view_interromper_backup(self, request, pk, backup_id):
        """Saída para backup/restauração travado (worker morto, tarefa perdida): só após 120 s sem atividade."""
        from django.utils import timezone
        from .console_backup import LIMITE_TRAVADO_S
        from .log_operacao import LogOperacao
        if request.method != 'POST':
            return JsonResponse({'ok': False, 'motivo': 'metodo'}, status=405)
        backup = get_object_or_404(BackupCliente, pk=backup_id, cliente__pk=pk)
        if backup.origem == 'migracao':
            return JsonResponse({'ok': False, 'motivo': 'migracao'}, status=409)
        restaurando = backup.restaurando
        if not (backup.status == 'em_andamento' or restaurando):
            return JsonResponse({'ok': False, 'motivo': 'nao_ativa'}, status=409)
        inicio = backup.restauracao_iniciada_em if (restaurando and backup.restauracao_iniciada_em) else backup.criado_em
        sem_atividade = int((timezone.now() - (backup.atividade_em or inicio)).total_seconds())
        if sem_atividade < LIMITE_TRAVADO_S:
            return JsonResponse({'ok': False, 'motivo': 'recente', 'sem_atividade_s': sem_atividade}, status=409)
        motivo = f'Interrompida manualmente: sem atividade há {sem_atividade}s.'
        if restaurando:
            from .tasks import _religar_app
            LogOperacao(backup.pk, 'log_restauracao').linha(f'ERRO: {motivo} Religando o web e o cron…')
            _religar_app(backup.cliente.slug, backup.replicas_restauracao)
            BackupCliente.objects.filter(pk=backup.pk).update(
                restaurando=False, progresso=100, mensagem_restauracao=motivo,
            )
        else:
            LogOperacao(backup.pk).linha(f'ERRO: {motivo}')
            BackupCliente.objects.filter(pk=backup.pk).update(status='erro', progresso=100, mensagem=motivo)
        return JsonResponse({'ok': True})

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
        url_cloudflare = reverse('admin:registry_cliente_configurar_cloudflare', args=[obj.pk])
        return format_html(
            '<p style="margin:0 0 10px;padding:8px 12px;background:#fff8e1;border-left:4px solid #f9a825;font-size:12px;color:#5d4037;">'
            '⚠️ <strong>Salve o formulário antes</strong> de aplicar — o botão lê os módulos, tema e domínio custom já gravados no banco.'
            '</p>'
            '<a href="{}" style="display:inline-block;padding:6px 14px;background:#2e7d32;color:#fff;border-radius:4px;text-decoration:none;font-size:13px;" '
            'onclick="return confirm(\'O serviço web do cliente será reiniciado para aplicar as configurações (módulos, tema, domínio custom) — leva ~30s. Continuar?\')">⚙ Aplicar Configurações</a>'
            '&nbsp;&nbsp;'
            '<a href="{}" style="display:inline-block;padding:6px 14px;background:#0369a1;color:#fff;border-radius:4px;text-decoration:none;font-size:13px;" '
            'onclick="return confirm(\'Adicionar domínio custom ao Cloudflare Tunnel e criar CNAME no DNS. Continuar?\')">☁ Configurar Cloudflare</a>'
            '&nbsp;&nbsp;'
            '<a href="{}" style="display:inline-block;padding:6px 14px;background:#417690;color:#fff;border-radius:4px;text-decoration:none;font-size:13px;" '
            'onclick="return confirm(\'ATENÇÃO: Faça backup do banco do cliente ANTES de re-provisionar. Dados podem ser perdidos se houver conflito de volume.\\n\\nConfirma que o backup foi realizado e deseja continuar?\')">Re-provisionar</a>'
            '&nbsp;&nbsp;'
            '<a href="{}" style="display:inline-block;padding:6px 14px;background:#ba2121;color:#fff;border-radius:4px;text-decoration:none;font-size:13px;" '
            'onclick="return confirm(\'Tem certeza? Isso vai apagar o stack, volumes e todos os dados do cliente.\')">Destruir</a>'
            '&nbsp;&nbsp;'
            '<a href="{}" style="display:inline-block;padding:6px 14px;background:#1565c0;color:#fff;border-radius:4px;text-decoration:none;font-size:13px;">✉ Reenviar Boas-vindas</a>',
            url_modulos, url_cloudflare, url_reprov, url_destruir, url_boas_vindas,
        )
    acoes_provisionamento.short_description = 'Provisionamento'

    def acoes_versoes(self, obj):
        if not obj.pk:
            return '—'
        from django.urls import reverse
        url_sync = reverse('admin:registry_cliente_sincronizar_versoes', args=[obj.pk])
        aviso_secret = ''
        if not obj.integracao_secret:
            aviso_secret = (
                '<p style="margin:0 0 10px;padding:8px 12px;background:#fff8e1;border-left:4px solid #f9a825;'
                'font-size:12px;color:#5d4037;">⚠️ Sem segredo de integração ainda — clique em '
                '"⚙ Aplicar Configurações" (aba Ações) uma vez antes de sincronizar.</p>'
            )

        if obj.atualizacao_automatica_agente:
            banner = (
                '<p style="margin:0 0 10px;padding:8px 12px;background:#e8f5e9;border-left:4px solid #2e7d32;'
                'font-size:12px;color:#1b5e20;">🔄 <strong>Atualização automática LIGADA</strong> — o catálogo '
                'inteiro de versões ativas é enviado pro ERP deste cliente; a lista "Versões permitidas" acima '
                'fica ignorada enquanto isso, e cada release novo entra sozinho. O botão abaixo força o reenvio '
                'na hora (útil logo depois de ligar o modo).</p>'
            )
        else:
            banner = (
                '<p style="margin:0 0 10px;padding:8px 12px;background:#f5f5f5;border-left:4px solid #9e9e9e;'
                'font-size:12px;color:#424242;">Envia o conjunto de versões marcadas acima pro ERP deste cliente '
                '(<code>SyncPackage.allowed</code>) — não reinicia o serviço, é só uma chamada HTTPS.</p>'
            )

        botao = (
            f'<a href="{url_sync}" style="display:inline-block;padding:6px 14px;background:#2e7d32;color:#fff;'
            f'border-radius:4px;text-decoration:none;font-size:13px;" '
            f'onclick="return confirm(\'Salve o formulário antes (se mudou as versões permitidas ou o modo '
            f'automático). Enviar a lista atual para o ERP deste cliente agora?\')">⇪ Sincronizar Versões com o ERP</a>'
        )

        bloco_log, script = self._bloco_log_sincronizacao(obj)

        return format_html(
            '{}{}{}{}{}',
            mark_safe(aviso_secret), mark_safe(banner), mark_safe(botao),
            mark_safe(bloco_log), mark_safe(script),
        )
    acoes_versoes.short_description = 'Sincronizar com o ERP'

    def acoes_plano_sync(self, obj):
        if not obj.pk:
            return '—'
        from django.urls import reverse
        url = reverse('admin:registry_cliente_sincronizar_plano', args=[obj.pk])
        aviso_secret = ''
        if not obj.integracao_secret:
            aviso_secret = (
                '<p style="margin:0 0 10px;padding:8px 12px;background:#fff8e1;border-left:4px solid #f9a825;'
                'font-size:12px;color:#5d4037;">⚠️ Sem segredo de integração ainda — clique em '
                '"⚙ Aplicar Configurações" (aba Ações) uma vez antes de sincronizar.</p>'
            )
        botao = (
            f'<a href="{url}" style="display:inline-block;padding:6px 14px;background:#2e7d32;color:#fff;'
            f'border-radius:4px;text-decoration:none;font-size:13px;">🔄 Sincronizar Plano com o ERP</a>'
        )
        return format_html('{}{}', mark_safe(aviso_secret), mark_safe(botao))
    acoes_plano_sync.short_description = 'Enviar plano/limites pro ERP'

    def _bloco_log_sincronizacao(self, obj):
        """Renderiza o log da ULTIMA sincronizacao deste cliente abaixo do
        botao. Se ainda esta 'enviando', devolve tambem um <script> que faz
        polling no endpoint de status e recarrega a pagina ao terminar
        (mesmo padrao do lista_backups)."""
        from django.urls import reverse
        from django.utils import timezone

        ultima = obj.sincronizacoes_versoes.first()
        if ultima is None:
            return '', ''

        quem = ultima.iniciada_por.get_username() if ultima.iniciada_por else 'automático'
        quando = timezone.localtime(ultima.iniciada_em).strftime('%d/%m/%Y %H:%M:%S')
        cabecalho = (
            f'<div style="margin-top:14px;font-size:12px;color:#555;">'
            f'<strong>Última sincronização</strong> &nbsp; {_sync_badge(ultima.status)} '
            f'&nbsp; <span style="color:#888;">{quando} · por {escape(quem)}</span></div>'
        )
        pre = (
            f'<pre id="synclog-{ultima.pk}" style="white-space:pre-wrap;max-height:280px;overflow:auto;'
            f'font-size:12px;line-height:1.5;background:#1e1e1e;color:#d4d4d4;padding:10px;'
            f'border-radius:4px;margin-top:6px;">{escape(ultima.log or "(sem log)")}</pre>'
        )

        script = ''
        if ultima.status == 'enviando':
            url_status = reverse('admin:registry_cliente_sincronizacao_status', args=[obj.pk, ultima.pk])
            idade = (timezone.now() - ultima.iniciada_em).total_seconds()
            hint = (
                'se o log não avançar, confirme que o worker Celery está rodando'
                if idade > 30 else 'atualizando…'
            )
            cabecalho += f'<div style="font-size:11px;color:#999;margin-top:3px;">· {hint}</div>'
            script = (
                f'<script>(function(){{'
                f'  var pre=document.getElementById("synclog-{ultima.pk}");'
                f'  var iv=setInterval(function(){{'
                f'    fetch("{url_status}").then(function(r){{return r.json();}}).then(function(d){{'
                f'      if(pre){{pre.textContent=d.log||"(sem log)";}}'
                f'      if(!d.ativo){{clearInterval(iv);setTimeout(function(){{location.reload();}},800);}}'
                f'    }}).catch(function(){{clearInterval(iv);}});'
                f'  }},1500);'
                f'}})();</script>'
            )

        return cabecalho + pre, script

    def col_versao_erp(self, obj):
        det = obj.versao_erp_detectada or '?'
        alvo = obj.versao_erp or '—'
        if obj.versao_erp_detectada and obj.versao_erp and obj.versao_erp_detectada != obj.versao_erp:
            return format_html('<span style="color:#e65100;">{} <small>(alvo {})</small></span>', det, alvo)
        return format_html('{} <small style="color:#999;">(alvo {})</small>', det, alvo)
    col_versao_erp.short_description = 'Versão ERP'

    _CONN_CORES = {
        'online':   ('#e8f5e9', '#2e7d32', 'online'),
        'degraded': ('#fff8e1', '#f57f17', 'instável'),
        'offline':  ('#ffebee', '#b71c1c', 'offline'),
        'unknown':  ('#f5f5f5', '#757575', '—'),
    }

    def versao_atual(self, obj):
        if not obj.pk:
            return '—'
        from django.urls import reverse
        from django.utils import timezone

        instalacoes = list(obj.instalacoes_agente.all())
        versoes_agente = sorted({i.agent_version for i in instalacoes if i.agent_version})
        resumo_agente = (
            ', '.join(versoes_agente) if versoes_agente
            else ('(nenhuma versão reportada)' if instalacoes else '(sem instalações)')
        )
        n = len(instalacoes)
        sufixo_n = f' · {n} instalação(ões)' if n else ''

        if obj.versoes_detectadas_em:
            quando = timezone.localtime(obj.versoes_detectadas_em).strftime('%d/%m/%Y %H:%M:%S')
            cab = f'<div style="font-size:12px;color:#555;">Verificado em <strong>{quando}</strong></div>'
        else:
            cab = '<div style="font-size:12px;color:#999;">Nunca verificado — clique no botão abaixo.</div>'

        if obj.deteccao_versoes_erro:
            cab += (
                f'<p style="margin:6px 0 0;padding:8px 12px;background:#fff8e1;border-left:4px solid #f9a825;'
                f'font-size:12px;color:#5d4037;">⚠ {escape(obj.deteccao_versoes_erro)}</p>'
            )

        erp_det = obj.versao_erp_detectada or '—'
        erp_alvo = obj.versao_erp or '—'
        erp_cor = '#e65100' if (obj.versao_erp_detectada and obj.versao_erp
                                and obj.versao_erp_detectada != obj.versao_erp) else '#111'
        resumo = (
            '<table style="margin-top:10px;border-collapse:collapse;font-size:13px;">'
            '<tr><td style="padding:4px 14px 4px 0;color:#666;">ERP</td>'
            f'<td style="padding:4px 0;font-weight:600;color:{erp_cor};">{escape(erp_det)}</td>'
            f'<td style="padding:4px 0 4px 14px;color:#999;">alvo: {escape(erp_alvo)}</td></tr>'
            '<tr><td style="padding:4px 14px 4px 0;color:#666;">SyncAgent</td>'
            f'<td style="padding:4px 0;font-weight:600;">{escape(resumo_agente)}</td>'
            f'<td style="padding:4px 0 4px 14px;color:#999;">{escape(sufixo_n.lstrip(" ·"))}</td></tr>'
            '<tr><td style="padding:4px 14px 4px 0;color:#666;">PDV Local</td>'
            f'<td style="padding:4px 0;font-weight:600;">{escape(resumo_agente)}</td>'
            '<td style="padding:4px 0 4px 14px;color:#999;">mesma release do SyncAgent (pdv-local)</td></tr>'
            '</table>'
        )

        tabela = ''
        if instalacoes:
            linhas = ''
            for i in instalacoes:
                bg, fg, lbl = self._CONN_CORES.get(i.connectivity, self._CONN_CORES['unknown'])
                visto = (timezone.localtime(i.last_seen_at).strftime('%d/%m %H:%M')
                         if i.last_seen_at else '—')
                ver = escape(i.agent_version or '?')
                marca_inativa = '' if i.active else " <small style=\"color:#b71c1c;\">inativa</small>"
                linhas += (
                    '<tr>'
                    f'<td style="padding:4px 10px;border-top:1px solid #eee;">{escape(i.installation_label or i.instance_id)}</td>'
                    f'<td style="padding:4px 10px;border-top:1px solid #eee;">{ver}</td>'
                    f'<td style="padding:4px 10px;border-top:1px solid #eee;">{ver}</td>'
                    f'<td style="padding:4px 10px;border-top:1px solid #eee;color:#666;">{visto}</td>'
                    f'<td style="padding:4px 10px;border-top:1px solid #eee;">'
                    f'<span style="background:{bg};color:{fg};padding:2px 8px;border-radius:12px;font-size:11px;font-weight:600;">{lbl}</span>'
                    f'{marca_inativa}</td>'
                    '</tr>'
                )
            tabela = (
                '<table style="margin-top:12px;border-collapse:collapse;font-size:12px;min-width:520px;">'
                '<tr style="text-align:left;color:#888;">'
                '<th style="padding:4px 10px;">Instalação</th><th style="padding:4px 10px;">SyncAgent</th>'
                '<th style="padding:4px 10px;">PDV Local</th><th style="padding:4px 10px;">Visto por último</th>'
                '<th style="padding:4px 10px;">Conectividade</th></tr>'
                f'{linhas}</table>'
            )

        url = reverse('admin:registry_cliente_coletar_versoes', args=[obj.pk])
        botao = (
            f'<a href="{url}" style="display:inline-block;margin-top:12px;padding:6px 14px;background:#417690;'
            f'color:#fff;border-radius:4px;text-decoration:none;font-size:13px;">🔄 Verificar versões agora</a>'
        )

        # ?coletando=1 -> a task acabou de ser disparada; recarrega uma vez.
        script = (
            '<script>(function(){'
            '  if(new URLSearchParams(location.search).get("coletando")!=="1")return;'
            '  setTimeout(function(){'
            '    var u=location.pathname;history.replaceState(null,"",u);location.href=u;'
            '  },5000);'
            '})();</script>'
        )

        return format_html('{}{}{}{}{}', mark_safe(cab), mark_safe(resumo),
                           mark_safe(tabela), mark_safe(botao), mark_safe(script))
    versao_atual.short_description = 'ERP / SyncAgent / PDV em execução'

    def lista_backups(self, obj):
        if not obj.pk:
            return '—'
        from django.urls import reverse
        backups = list(obj.backups.all()[:5])
        url_novo = reverse('admin:registry_cliente_backup', args=[obj.pk])
        url_upload = reverse('admin:registry_cliente_backup_upload', args=[obj.pk])

        botao_novo = (
            f'<a href="{url_novo}" style="display:inline-block;padding:6px 14px;background:#2e7d32;'
            f'color:#fff;border-radius:4px;text-decoration:none;font-size:13px;" '
            f'onclick="return confirm(\'Iniciar backup completo (banco de dados + mídia + configuração) para este cliente? O processo roda em segundo plano.\')">💾 Novo Backup</a>'
        )
        botao_upload = (
            f'<a href="{url_upload}" style="display:inline-block;padding:6px 14px;background:#1565c0;'
            f'color:#fff;border-radius:4px;text-decoration:none;font-size:13px;margin-left:8px;">⬆ Importar Backup</a>'
        )
        botoes = botao_novo + botao_upload

        if not backups:
            return format_html(
                '{}<p style="margin-top:12px;color:#999;font-size:13px;">Nenhum backup registrado ainda.</p>',
                mark_safe(botoes),
            )

        _BACKUP_CORES = {
            'concluido':   ('#e8f5e9', '#2e7d32', 'Concluído'),
            'em_andamento': ('#fff8e1', '#f57f17', 'Em andamento'),
            'erro':        ('#ffebee', '#b71c1c', 'Erro'),
        }

        _BACKUP_CORES = {
            'concluido':    ('#e8f5e9', '#2e7d32', 'Concluído'),
            'em_andamento': ('#fff8e1', '#f57f17', 'Em andamento'),
            'erro':         ('#ffebee', '#b71c1c', 'Erro'),
        }

        ativa = next((x for x in backups if x.status == 'em_andamento' or x.restaurando), None)
        from .console_backup import renderizar as _renderizar_console
        console = _renderizar_console(
            obj.pk, ativa.pk if ativa else None, 'restauracao' if (ativa and ativa.restaurando) else 'backup',
        )

        linhas = []
        scripts = []
        for b in backups:
            ativo = b.status == 'em_andamento' or b.restaurando
            data = b.criado_em.strftime('%d/%m/%Y %H:%M')

            if b.restaurando:
                bg, fg, label = '#fff3e0', '#e65100', 'Restaurando...'
            else:
                bg, fg, label = _BACKUP_CORES.get(b.status, ('#f5f5f5', '#333', b.status))

            badge = f'<span style="background:{bg};color:{fg};padding:2px 8px;border-radius:12px;font-size:11px;font-weight:600;">{label}</span>'
            if b.origem == 'manual':
                badge += ' <span style="background:#eceff1;color:#455a64;padding:2px 8px;border-radius:12px;font-size:11px;font-weight:600;">manual</span>'

            if ativo:
                pct = b.progresso
                barra = (
                    f'<div style="margin-top:6px;background:#e0e0e0;border-radius:6px;height:14px;width:100%;max-width:320px;overflow:hidden;">'
                    f'  <div id="bpbar-{b.pk}" style="height:14px;width:{pct}%;background:linear-gradient(90deg,#1565c0,#42a5f5);border-radius:6px;transition:width .4s ease;"></div>'
                    f'</div>'
                    f'<div style="font-size:11px;color:#555;margin-top:3px;">'
                    f'  <span id="bppct-{b.pk}">{pct}%</span>'
                    f'  <span style="margin-left:8px;color:#999;">· acompanhe no console acima</span>'
                    f'</div>'
                )
                url_status = reverse('admin:registry_cliente_backup_status', args=[obj.pk, b.pk])
                scripts.append(
                    f'(function(){{'
                    f'  var bar=document.getElementById("bpbar-{b.pk}");'
                    f'  var pct=document.getElementById("bppct-{b.pk}");'
                    f'  var iv=setInterval(function(){{'
                    f'    fetch("{url_status}").then(function(r){{return r.json();}}).then(function(d){{'
                    f'      if(bar){{bar.style.width=d.progresso+"%";}} '
                    f'      if(pct){{pct.textContent=d.progresso+"%";}} '
                    f'      if(!d.ativo){{clearInterval(iv);}}'
                    f'    }}).catch(function(){{clearInterval(iv);}});'
                    f'  }},2000);'
                    f'}})();'
                )
                acoes = barra
            elif b.status == 'concluido':
                url_dl = reverse('admin:registry_cliente_backup_download', args=[obj.pk, b.pk])
                url_rst = reverse('admin:registry_cliente_restaurar', args=[obj.pk, b.pk])
                aviso_html = (
                    f'<div style="color:#e65100;font-size:11px;margin-top:2px;">⚠ {escape(b.mensagem[:100])}</div>'
                    if b.mensagem else ''
                )
                aviso_restauracao_html = (
                    f'<div style="color:#b71c1c;font-size:11px;margin-top:2px;">⚠ Última restauração: {escape(b.mensagem_restauracao[:150])}</div>'
                    if b.mensagem_restauracao else ''
                )
                acoes = (
                    f'<span style="color:#666;font-size:12px;margin-right:8px;">{b.tamanho_fmt}</span>'
                    f'<a href="{url_dl}" style="padding:3px 10px;background:#1565c0;color:#fff;border-radius:4px;font-size:12px;text-decoration:none;margin-right:4px;">⬇ Baixar</a>'
                    f'<a href="{url_rst}" style="padding:3px 10px;background:#e65100;color:#fff;border-radius:4px;font-size:12px;text-decoration:none;" '
                    f'onclick="return confirm(\'ATENÇÃO: O ERP ficará indisponível por alguns minutos durante a restauração.\\n\\nO banco de dados e a pasta mídia serão sobrescritos pelo backup de {data}.\\n\\nConfirma?\')">↩ Restaurar</a>'
                    f'{aviso_html}'
                    f'{aviso_restauracao_html}'
                )
            else:
                acoes = f'<span style="color:#b71c1c;font-size:12px;">{escape(b.mensagem[:100])}</span>'
            acoes += _detalhes_log(b)

            linhas.append(
                f'<tr>'
                f'<td style="padding:6px 10px;border-bottom:1px solid #eee;font-size:13px;white-space:nowrap;">{data}</td>'
                f'<td style="padding:6px 10px;border-bottom:1px solid #eee;">{badge}</td>'
                f'<td style="padding:6px 10px;border-bottom:1px solid #eee;">{acoes}</td>'
                f'</tr>'
            )

        tabela = (
            '<table style="width:100%;border-collapse:collapse;margin-top:12px;">'
            '<thead><tr>'
            '<th style="text-align:left;padding:6px 10px;background:#f5f5f5;font-size:12px;font-weight:600;white-space:nowrap;">Data</th>'
            '<th style="text-align:left;padding:6px 10px;background:#f5f5f5;font-size:12px;font-weight:600;">Status</th>'
            '<th style="text-align:left;padding:6px 10px;background:#f5f5f5;font-size:12px;font-weight:600;">Ações</th>'
            '</tr></thead><tbody>' + ''.join(linhas) + '</tbody></table>'
        )

        script_tag = f'<script>{" ".join(scripts)}</script>' if scripts else ''
        tabela = f'<div id="bk-tabela">{tabela}</div>'
        return format_html('{}{}{}{}', mark_safe(botoes), mark_safe(console), mark_safe(tabela), mark_safe(script_tag))

    lista_backups.short_description = 'Backups'


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


class _ConfiguracaoCloudflareForm(forms.ModelForm):
    cf_api_token = forms.CharField(
        label='API Token',
        widget=forms.PasswordInput(render_value=True),
        help_text='Token com permissões Zone:Read + DNS:Edit + Account:Cloudflare Tunnel:Edit em All Accounts. Criado em Cloudflare → My Profile → API Tokens.',
    )

    class Meta:
        model = ConfiguracaoCloudflare
        fields = ['cf_api_token', 'tunnel_name']


@admin.register(ConfiguracaoCloudflare)
class ConfiguracaoCloudflareAdmin(ModelAdmin):
    form = _ConfiguracaoCloudflareForm

    fieldsets = [
        ('Autenticação', {'fields': ['cf_api_token']}),
        ('Tunnel', {'fields': ['tunnel_name']}),
        ('Verificar token', {'fields': ['botao_verificar_token']}),
    ]
    readonly_fields = ['atualizado_em', 'botao_verificar_token']

    def get_urls(self):
        urls = super().get_urls()
        return [
            path('verificar-token/', self.admin_site.admin_view(self._view_verificar_token), name='registry_configuracaocloudflare_verificar'),
        ] + urls

    def _view_verificar_token(self, request):
        from django.contrib import messages
        import requests as _requests
        cfg = ConfiguracaoCloudflare.obter()
        token = (cfg.cf_api_token if cfg else '').strip() or os.getenv('CF_API_TOKEN', '').strip()
        if not token:
            messages.error(request, 'Token não configurado.')
            return redirect('admin:registry_configuracaocloudflare_changelist')
        try:
            r = _requests.get(
                'https://api.cloudflare.com/client/v4/user/tokens/verify',
                headers={'Authorization': f'Bearer {token}'},
                timeout=10,
            )
            if r.ok:
                status = r.json().get('result', {}).get('status', 'ativo')
                messages.success(request, f'Token válido — status Cloudflare: {status}.')
            else:
                try:
                    msg = r.json()['errors'][0]['message']
                except Exception:
                    msg = r.text
                messages.error(request, f'Token inválido ({r.status_code}): {msg}')
        except Exception as exc:
            messages.error(request, f'Erro ao verificar token: {exc}')
        return redirect('admin:registry_configuracaocloudflare_changelist')

    def botao_verificar_token(self, obj):
        if not obj.pk:
            return '—'
        from django.urls import reverse
        url = reverse('admin:registry_configuracaocloudflare_verificar')
        return format_html(
            '<a href="{}" style="display:inline-block;padding:6px 14px;background:#0369a1;color:#fff;'
            'border-radius:4px;text-decoration:none;font-size:13px;">☁ Verificar token no Cloudflare</a>',
            url,
        )
    botao_verificar_token.short_description = 'Verificar token'

    def has_add_permission(self, request):
        return not ConfiguracaoCloudflare.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False


_ARMAZ_CORES = {
    'conectado': '#16a34a',
    'atencao': '#d97706',
    'erro': '#dc2626',
    'nao_testado': '#6b7280',
}
_ARMAZ_ROTULOS = dict(ConfiguracaoArmazenamento.STATUS_CHOICES)


def _armaz_pill(status, elemento_id=''):
    cor = _ARMAZ_CORES.get(status, '#6b7280')
    return format_html(
        '<span {} style="display:inline-block;padding:3px 12px;border-radius:12px;background:{};color:#fff;'
        'font-size:12px;font-weight:600;">● {}</span>',
        mark_safe(f'id="{elemento_id}"') if elemento_id else '', cor, _ARMAZ_ROTULOS.get(status, status),
    )


class _ConfiguracaoArmazenamentoForm(forms.ModelForm):
    secret_key = forms.CharField(
        label='Secret key',
        required=False,
        widget=forms.PasswordInput(render_value=False, attrs={'autocomplete': 'new-password'}),
    )

    class Meta:
        model = ConfiguracaoArmazenamento
        fields = ['endpoint', 'endpoint_publico', 'bucket', 'access_key', 'regiao']

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance and self.instance.secret_key_cifrada:
            self.fields['secret_key'].help_text = '•••••••• mantida. Deixe em branco para não alterar.'
        else:
            self.fields['secret_key'].help_text = 'Chave secreta da access key. Fica criptografada no banco e não é exibida depois de salva.'

    def clean(self):
        dados = super().clean()
        if not dados.get('secret_key') and not (self.instance and self.instance.secret_key_cifrada):
            self.add_error('secret_key', 'Informe a secret key.')
        return dados


@admin.register(ConfiguracaoArmazenamento)
class ConfiguracaoArmazenamentoAdmin(ModelAdmin):
    form = _ConfiguracaoArmazenamentoForm
    list_display = ['__str__', 'status_badge_lista', 'verificado_em']

    fieldsets = [
        ('Conexão', {'fields': ['endpoint', 'bucket', 'access_key', 'secret_key', 'regiao']}),
        ('Endereço público (downloads)', {'fields': ['endpoint_publico']}),
        ('Status da conexão', {'fields': ['status_badge', 'botao_testar']}),
    ]
    readonly_fields = ['status_badge', 'botao_testar']

    def get_changeform_initial_data(self, request):
        return {'endpoint': 'http://rustfs-s3:9000', 'bucket': 'cp-backups', 'regiao': 'us-east-1'}

    def get_urls(self):
        urls = super().get_urls()
        return [
            path('testar-conexao/', self.admin_site.admin_view(self._view_testar), name='registry_configuracaoarmazenamento_testar'),
            path('verificar/', self.admin_site.admin_view(self._view_verificar), name='registry_configuracaoarmazenamento_verificar'),
        ] + urls

    def save_model(self, request, obj, form, change):
        from django.contrib import messages
        from . import storage_backup
        nova = form.cleaned_data.get('secret_key')
        if nova:
            obj.definir_secret(nova)
        super().save_model(request, obj, form, change)
        status, mensagem = storage_backup.verificar_e_registrar()
        nivel = {'conectado': messages.SUCCESS, 'atencao': messages.WARNING}.get(status, messages.ERROR)
        messages.add_message(request, nivel, f'Conexão testada após salvar: {mensagem}')

    def _view_testar(self, request):
        from django.contrib import messages
        from . import storage_backup
        if not self.has_change_permission(request):
            raise Http404
        status, mensagem = storage_backup.verificar_e_registrar()
        nivel = {'conectado': messages.SUCCESS, 'atencao': messages.WARNING}.get(status, messages.ERROR)
        messages.add_message(request, nivel, mensagem)
        if ConfiguracaoArmazenamento.obter():
            return redirect('admin:registry_configuracaoarmazenamento_change', 1)
        return redirect('admin:registry_configuracaoarmazenamento_changelist')

    def _view_verificar(self, request):
        """JSON usado pelo change form para atualizar o sinalizador ao abrir a tela."""
        from . import storage_backup
        if not self.has_view_permission(request):
            raise Http404
        status, mensagem = storage_backup.verificar_e_registrar()
        return JsonResponse({'status': status, 'rotulo': _ARMAZ_ROTULOS.get(status, status), 'mensagem': mensagem})

    def status_badge_lista(self, obj):
        return _armaz_pill(obj.status_conexao)
    status_badge_lista.short_description = 'Status'

    def status_badge(self, obj):
        if not obj or not obj.pk:
            return '—'
        import json
        from django.urls import reverse
        from django.utils.timesince import timesince
        quando = f'verificado há {timesince(obj.verificado_em)}' if obj.verificado_em else 'ainda não verificado'
        url = reverse('admin:registry_configuracaoarmazenamento_verificar')
        script = (
            '(function(){'
            'var cores=' + json.dumps(_ARMAZ_CORES) + ';'
            'var rot=document.getElementById("armaz-badge");'
            'var msg=document.getElementById("armaz-msg");var q=document.getElementById("armaz-quando");'
            'if(!rot)return;q.textContent="verificando agora…";'
            'fetch(' + json.dumps(url) + ',{credentials:"same-origin"}).then(function(r){return r.json();}).then(function(d){'
            'rot.style.background=cores[d.status]||"#6b7280";rot.textContent="● "+d.rotulo;'
            'msg.textContent=d.mensagem;q.textContent="verificado agora";'
            '}).catch(function(){q.textContent="não foi possível verificar agora";});'
            '})();'
        )
        return format_html(
            '{} <span id="armaz-quando" style="margin-left:8px;color:#6b7280;font-size:12px;">{}</span>'
            '<div id="armaz-msg" style="margin-top:6px;font-size:13px;">{}</div><script>{}</script>',
            _armaz_pill(obj.status_conexao, 'armaz-badge'), quando, obj.status_mensagem or '', mark_safe(script),
        )
    status_badge.short_description = 'Status'

    def botao_testar(self, obj):
        if not obj or not obj.pk:
            return '—'
        from django.urls import reverse
        return format_html(
            '<a href="{}" style="display:inline-block;padding:6px 14px;background:#417690;color:#fff;'
            'border-radius:4px;text-decoration:none;font-size:13px;">🔌 Testar conexão agora</a>',
            reverse('admin:registry_configuracaoarmazenamento_testar'),
        )
    botao_testar.short_description = 'Testar'

    def has_add_permission(self, request):
        return not ConfiguracaoArmazenamento.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False


_ATZ_SITUACAO_CORES = {
    'atualizado': '#16a34a',
    'disponivel': '#2563eb',
    'indisponivel': '#dc2626',
    'nao_verificado': '#6b7280',
}
_ATZ_SITUACAO_ROTULOS = {
    'atualizado': 'Atualizado',
    'disponivel': 'Atualização disponível',
    'indisponivel': 'Não foi possível verificar',
    'nao_verificado': 'Não verificado',
}
_ATZ_ESTADO_ROTULOS = {
    'em_andamento': ('Em andamento', '#d97706'),
    'concluida': ('Concluída', '#16a34a'),
    'falhou': ('Falhou', '#dc2626'),
    'desconhecido': ('Sem informação', '#6b7280'),
}


def _atz_pill(texto, cor, elemento_id=''):
    return format_html(
        '<span {} style="display:inline-block;padding:3px 12px;border-radius:12px;background:{};color:#fff;'
        'font-size:12px;font-weight:600;">{}</span>',
        mark_safe(f'id="{elemento_id}"') if elemento_id else '', cor, texto,
    )


class _ConfiguracaoAtualizacaoForm(forms.ModelForm):
    dockerhub_token = forms.CharField(
        label='Token do Docker Hub',
        required=False,
        widget=forms.PasswordInput(render_value=False, attrs={'autocomplete': 'new-password'}),
    )

    class Meta:
        model = ConfiguracaoAtualizacao
        fields = ['repositorio', 'dockerhub_usuario']

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance and self.instance.dockerhub_token_cifrado:
            self.fields['dockerhub_token'].help_text = '•••••••• mantido. Deixe em branco para não alterar.'
        else:
            self.fields['dockerhub_token'].help_text = (
                'Personal Access Token do Docker Hub com permissão somente leitura. '
                'Fica criptografado no banco e não é exibido depois de salvo.'
            )

    def clean_repositorio(self):
        from . import atualizacoes
        repo = (self.cleaned_data.get('repositorio') or '').strip().strip('/')
        if not atualizacoes.repositorio_valido(repo):
            raise forms.ValidationError('Use o formato usuario/repositorio (minúsculas), ex.: emporioautomacao/ararasuite-cp.')
        return repo


@admin.register(ConfiguracaoAtualizacao)
class ConfiguracaoAtualizacaoAdmin(ModelAdmin):
    form = _ConfiguracaoAtualizacaoForm

    fieldsets = [
        ('Versão', {'fields': ['painel_versao']}),
        ('Docker Hub', {'fields': ['repositorio', 'dockerhub_usuario', 'dockerhub_token']}),
        ('Status da conexão', {'fields': ['status_badge']}),
        ('Última atualização iniciada pelo painel', {'fields': ['info_atualizacao']}),
    ]
    readonly_fields = ['painel_versao', 'status_badge', 'info_atualizacao']

    def get_urls(self):
        urls = super().get_urls()
        return [
            path('verificar/', self.admin_site.admin_view(self._view_verificar), name='registry_configuracaoatualizacao_verificar'),
            path('verificar-agora/', self.admin_site.admin_view(self._view_verificar_agora), name='registry_configuracaoatualizacao_verificar_agora'),
            path('atualizar/<str:versao>/', self.admin_site.admin_view(self._view_atualizar), name='registry_configuracaoatualizacao_atualizar'),
        ] + urls

    def changelist_view(self, request, extra_context=None):
        # singleton: o item da sidebar leva direto ao formulário
        if ConfiguracaoAtualizacao.obter():
            return redirect('admin:registry_configuracaoatualizacao_change', 1)
        return redirect('admin:registry_configuracaoatualizacao_add')

    def _voltar(self):
        if ConfiguracaoAtualizacao.obter():
            return redirect('admin:registry_configuracaoatualizacao_change', 1)
        return redirect('admin:registry_configuracaoatualizacao_add')

    def save_model(self, request, obj, form, change):
        from django.contrib import messages
        from . import atualizacoes
        novo = form.cleaned_data.get('dockerhub_token')
        if novo:
            obj.definir_token(novo)
        super().save_model(request, obj, form, change)
        resultado = atualizacoes.verificar_e_registrar()
        nivel = messages.SUCCESS if resultado['status'] == 'conectado' else messages.ERROR
        messages.add_message(request, nivel, f'Consulta ao Docker Hub após salvar: {resultado["mensagem"]}')

    # ── views ────────────────────────────────────────────────────────────────

    def _json(self, resultado):
        from . import atualizacoes
        cfg = ConfiguracaoAtualizacao.obter()
        sit = atualizacoes.situacao(cfg) if cfg else 'nao_verificado'
        return {
            'status': resultado['status'], 'rotulo': _ARMAZ_ROTULOS.get(resultado['status'], resultado['status']),
            'mensagem': resultado['mensagem'], 'disponivel': resultado['disponivel'],
            'recentes': resultado['recentes'], 'situacao': sit, 'situacao_rotulo': _ATZ_SITUACAO_ROTULOS[sit],
            'atual': atualizacoes.versao_atual(),
        }

    def _view_verificar(self, request):
        """JSON usado pelo formulário para atualizar versão disponível e status ao abrir a tela."""
        from . import atualizacoes
        if not self.has_view_permission(request):
            raise Http404
        return JsonResponse(self._json(atualizacoes.verificar_e_registrar(usar_cache=True)))

    def _view_verificar_agora(self, request):
        from django.contrib import messages
        from . import atualizacoes
        if not self.has_change_permission(request):
            raise Http404
        resultado = atualizacoes.verificar_e_registrar()
        nivel = messages.SUCCESS if resultado['status'] == 'conectado' else messages.ERROR
        messages.add_message(request, nivel, resultado['mensagem'])
        return self._voltar()

    def _view_atualizar(self, request, versao):
        from django.contrib import messages
        from django.core.exceptions import PermissionDenied
        from . import atualizacoes
        if not request.user.is_superuser:
            raise PermissionDenied
        cfg = ConfiguracaoAtualizacao.obter()
        if not cfg:
            return self._voltar()
        if not atualizacoes.ha_atualizacao(atualizacoes.versao_atual(), versao):
            messages.error(request, f'A versão {versao} não é mais nova que a atual ({atualizacoes.versao_atual()}).')
            return self._voltar()
        usa_latest = atualizacoes.stack_usa_latest(cfg)
        if request.method == 'POST':
            try:
                atualizacoes.iniciar_atualizacao(cfg, versao, request.user)
            except atualizacoes.ErroAtualizacao as exc:
                messages.error(request, str(exc))
            else:
                depois = (
                    'Como o stack usa a tag latest, um redeploy do stack também traz a versão mais recente.'
                    if usa_latest else
                    f'Depois, ajuste CP_VERSION={versao} no stack do Portainer, senão um redeploy volta a versão antiga.'
                )
                messages.warning(
                    request,
                    f'Atualização para {versao} iniciada. O painel pode ficar indisponível por instantes. {depois}',
                )
            return self._voltar()
        from django.urls import reverse
        context = {
            **self.admin_site.each_context(request),
            'title': f'Atualizar o CP para {versao}',
            'versao': versao, 'versao_atual': atualizacoes.versao_atual(),
            'repositorio': cfg.repositorio, 'usa_latest': usa_latest,
            'voltar_url': reverse('admin:registry_configuracaoatualizacao_change', args=[1]),
        }
        return render(request, 'registry/admin_confirmar_atualizacao.html', context)

    # ── campos somente leitura ───────────────────────────────────────────────

    def painel_versao(self, obj):
        if not obj or not obj.pk:
            return 'Salve a configuração para verificar as atualizações.'
        import json
        from django.urls import reverse
        from django.utils.timesince import timesince
        from . import atualizacoes

        sit = atualizacoes.situacao(obj)
        swarm = atualizacoes.swarm_manager()
        url_json = reverse('admin:registry_configuracaoatualizacao_verificar')
        url_agora = reverse('admin:registry_configuracaoatualizacao_verificar_agora')
        alvo = obj.versao_disponivel if sit == 'disponivel' else ''
        base_atualizar = reverse('admin:registry_configuracaoatualizacao_atualizar', args=['9.9.9']).replace('9.9.9/', '')
        url_atualizar = f'{base_atualizar}{alvo}/' if alvo else ''
        recentes = ', '.join(obj.versoes_recentes[:6]) or '—'
        usa_latest = atualizacoes.stack_usa_latest(obj) if swarm else None
        if usa_latest:
            nota_stack = mark_safe(
                '<div style="margin-top:10px;font-size:12px;color:#475569;">O stack do CP usa a tag <code>latest</code>: '
                'um redeploy do stack (com <em>Re-pull image</em>) também traz a versão mais recente.</div>'
            )
        else:
            nota_stack = mark_safe(
                '<div style="margin-top:10px;font-size:12px;color:#92400e;">Ao atualizar pelo painel, ajuste também <code>CP_VERSION</code> '
                'no stack do Portainer — senão um redeploy volta a versão antiga.</div>'
            )
        estilo_btn = ('display:inline-block;padding:6px 14px;color:#fff;border-radius:4px;'
                      'text-decoration:none;font-size:13px;margin-right:8px;')
        if swarm:
            botao_atualizar = format_html(
                '<a id="atz-btn" href="{}" style="{}background:#2563eb;{}">⬆ Atualizar para <span id="atz-btn-versao">{}</span></a>',
                url_atualizar, mark_safe(estilo_btn), mark_safe('' if alvo else 'display:none;'), alvo,
            )
            aviso_swarm = ''
        else:
            botao_atualizar = ''
            aviso_swarm = mark_safe(
                '<div style="margin-top:8px;color:#6b7280;font-size:12px;">Atualizar pelo painel só está disponível em um '
                'Swarm manager (indisponível neste ambiente).</div>'
            )
        script = (
            '(function(){'
            'var cores=' + json.dumps(_ATZ_SITUACAO_CORES) + ';var cs=' + json.dumps(_ARMAZ_CORES) + ';'
            'function $(i){return document.getElementById(i);}'
            'if(!$("atz-situacao"))return;$("atz-quando").textContent="verificando agora…";'
            'fetch(' + json.dumps(url_json) + ',{credentials:"same-origin"}).then(function(r){return r.json();}).then(function(d){'
            '$("atz-disponivel").textContent=d.disponivel||"—";'
            '$("atz-recentes").textContent=(d.recentes||[]).slice(0,6).join(", ")||"—";'
            '$("atz-situacao").style.background=cores[d.situacao]||"#6b7280";$("atz-situacao").textContent=d.situacao_rotulo;'
            'var b=$("atz-btn");if(b){if(d.situacao==="disponivel"){'
            'b.style.display="inline-block";$("atz-btn-versao").textContent=d.disponivel;'
            'b.href=' + json.dumps(base_atualizar) + '+d.disponivel+"/";}else{b.style.display="none";}}'
            'var s=$("armaz-badge");if(s){s.style.background=cs[d.status]||"#6b7280";s.textContent="● "+d.rotulo;}'
            'if($("armaz-msg"))$("armaz-msg").textContent=d.mensagem;'
            '$("atz-quando").textContent="verificado agora";'
            '}).catch(function(){$("atz-quando").textContent="não foi possível verificar agora";});'
            '})();'
        )
        return format_html(
            '<table style="border-collapse:collapse;font-size:14px;">'
            '<tr><td style="padding:4px 18px 4px 0;color:#6b7280;">Versão atual</td><td><strong>{}</strong></td></tr>'
            '<tr><td style="padding:4px 18px 4px 0;color:#6b7280;">Versão disponível</td><td><strong id="atz-disponivel">{}</strong></td></tr>'
            '<tr><td style="padding:4px 18px 4px 0;color:#6b7280;">Situação</td><td>{} <span id="atz-quando" style="margin-left:8px;color:#6b7280;font-size:12px;">{}</span></td></tr>'
            '<tr><td style="padding:4px 18px 4px 0;color:#6b7280;">Versões recentes</td><td id="atz-recentes">{}</td></tr>'
            '</table>'
            '<div style="margin-top:12px;">{}<a href="{}" style="{}background:#417690;">🔄 Verificar agora</a></div>{}'
            '{}<script>{}</script>',
            atualizacoes.versao_atual(), obj.versao_disponivel or '—',
            _atz_pill(_ATZ_SITUACAO_ROTULOS[sit], _ATZ_SITUACAO_CORES[sit], 'atz-situacao'),
            f'verificado há {timesince(obj.verificado_em)}' if obj.verificado_em else 'ainda não verificado',
            recentes, botao_atualizar, url_agora, mark_safe(estilo_btn), aviso_swarm, nota_stack, mark_safe(script),
        )
    painel_versao.short_description = 'Versões'

    def status_badge(self, obj):
        if not obj or not obj.pk:
            return '—'
        return format_html(
            '{}<div id="armaz-msg" style="margin-top:6px;font-size:13px;">{}</div>',
            _armaz_pill(obj.status_conexao, 'armaz-badge'), obj.status_mensagem or '',
        )
    status_badge.short_description = 'Docker Hub'

    def info_atualizacao(self, obj):
        from django.utils import timezone
        from . import atualizacoes
        if not obj or not obj.atualizacao_container:
            return 'Nenhuma atualização foi iniciada pelo painel.'
        estado = atualizacoes.estado_atualizacao(obj)
        rotulo, cor = _ATZ_ESTADO_ROTULOS.get(estado['estado'], _ATZ_ESTADO_ROTULOS['desconhecido'])
        quem = obj.atualizacao_iniciada_por.get_username() if obj.atualizacao_iniciada_por_id else '—'
        quando = timezone.localtime(obj.atualizacao_iniciada_em).strftime('%d/%m/%Y %H:%M') if obj.atualizacao_iniciada_em else '—'
        log = format_html(
            '<pre style="margin-top:8px;max-height:220px;overflow:auto;background:#0f172a;color:#e2e8f0;padding:10px;'
            'border-radius:6px;font-size:12px;white-space:pre-wrap;">{}</pre>', estado['log'],
        ) if estado['log'] else ''
        aviso = format_html(
            '<div style="margin-top:6px;font-size:12px;color:#92400e;">Lembre-se de ajustar <code>CP_VERSION={}</code> no stack do Portainer.</div>',
            obj.atualizacao_alvo,
        ) if estado['estado'] in ('em_andamento', 'concluida') and obj.atualizacao_tag_anterior != 'latest' else ''
        return format_html(
            'Para a versão <strong>{}</strong>, iniciada em {} por {}: {}{}{}',
            obj.atualizacao_alvo, quando, quem, _atz_pill(rotulo, cor), aviso, log,
        )
    info_atualizacao.short_description = 'Andamento'

    def has_add_permission(self, request):
        return not ConfiguracaoAtualizacao.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False


from . import celery_beat_pt  # noqa: E402, F401
