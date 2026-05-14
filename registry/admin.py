from django.contrib import admin
from .models import Modulo, Plano, HostInfraestrutura, Cliente, ProvisionamentoLog, AtualizacaoVersao, VerificacaoSaude


@admin.register(Modulo)
class ModuloAdmin(admin.ModelAdmin):
    list_display = ['slug', 'nome', 'preco_mensal', 'ativo']
    list_editable = ['ativo']


@admin.register(Plano)
class PlanoAdmin(admin.ModelAdmin):
    list_display = ['slug', 'nome', 'preco_mensal', 'max_usuarios', 'ativo', 'destaque', 'ordem']
    list_editable = ['ativo', 'destaque', 'ordem']
    filter_horizontal = ['modulos_inclusos']


@admin.register(HostInfraestrutura)
class HostInfraestruturaAdmin(admin.ModelAdmin):
    list_display = ['nome', 'regiao', 'tipo', 'ip', 'cpu_total', 'ram_gb_total', 'ativo']
    list_filter = ['regiao', 'tipo', 'ativo']


class ProvisionamentoLogInline(admin.TabularInline):
    model = ProvisionamentoLog
    extra = 0
    readonly_fields = ['etapa', 'status', 'mensagem', 'iniciado_em', 'concluido_em']
    can_delete = False


class AtualizacaoVersaoInline(admin.TabularInline):
    model = AtualizacaoVersao
    extra = 0
    readonly_fields = ['versao_anterior', 'versao_nova', 'status', 'iniciada_em', 'concluida_em']
    can_delete = False


@admin.register(Cliente)
class ClienteAdmin(admin.ModelAdmin):
    list_display = ['slug', 'nome', 'subdominio', 'plano', 'status', 'isento_cobranca', 'versao_erp', 'criado_em']
    list_filter = ['status', 'plano', 'host', 'isento_cobranca']
    search_fields = ['slug', 'nome', 'cnpj', 'email_contato']
    readonly_fields = ['id', 'criado_em', 'atualizado_em']
    filter_horizontal = ['modulos_ativos']
    inlines = [ProvisionamentoLogInline, AtualizacaoVersaoInline]
    fieldsets = [
        ('Identificação', {'fields': ['id', 'slug', 'nome', 'cnpj', 'email_contato', 'telefone']}),
        ('Infraestrutura', {'fields': ['host', 'versao_erp', 'stack_path', 'subdominio', 'dominio_custom']}),
        ('Plano', {'fields': ['plano', 'modulos_ativos']}),
        ('Faturamento', {'fields': ['asaas_customer_id', 'asaas_subscription_id', 'isento_cobranca', 'motivo_isencao']}),
        ('Status', {'fields': ['status', 'trial_ate', 'data_ativacao', 'data_suspensao', 'data_cancelamento']}),
        ('Observações', {'fields': ['observacoes', 'criado_em', 'atualizado_em']}),
    ]


@admin.register(VerificacaoSaude)
class VerificacaoSaudeAdmin(admin.ModelAdmin):
    list_display = ['cliente', 'online', 'status_http', 'latencia_ms', 'verificado_em']
    list_filter = ['online', 'cliente']


from . import celery_beat_pt  # noqa: E402, F401
