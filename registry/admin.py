import os
from django import forms
from django.contrib import admin
from django.http import FileResponse, Http404, JsonResponse
from django.urls import path
from django.shortcuts import get_object_or_404, redirect
from django.utils.html import format_html, mark_safe
from unfold.admin import ModelAdmin
from .models import Modulo, Plano, HostInfraestrutura, Cliente, ProvisionamentoLog, AtualizacaoVersao, VerificacaoSaude, ConfiguracaoEmail, ConfiguracaoCloudflare, BackupCliente


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
    readonly_fields = ['id', 'criado_em', 'atualizado_em', 'status_badge', 'painel_acesso', 'acoes_provisionamento', 'badge_isencao', 'lista_backups']
    filter_horizontal = ['modulos_ativos']
    actions = [acao_reprovisionar, acao_destruir]
    inlines = [ProvisionamentoLogInline, AtualizacaoVersaoInline]
    fieldsets = [
        ('Identificação', {'fields': ['id', 'slug', 'nome', 'cnpj', 'email_contato', 'telefone']}),
        ('Acesso', {'fields': ['painel_acesso']}),
        ('Infraestrutura', {'fields': ['host', 'versao_erp', 'stack_path', 'subdominio', 'dominio_custom']}),
        ('Plano', {'fields': ['plano', 'modulos_ativos', 'tema_site']}),
        ('Faturamento', {'fields': ['asaas_customer_id', 'asaas_subscription_id', 'badge_isencao', 'isento_cobranca', 'motivo_isencao']}),
        ('Status', {'fields': ['status_badge', 'status', 'trial_ate', 'data_ativacao', 'data_suspensao', 'data_cancelamento']}),
        ('Ações', {'fields': ['acoes_provisionamento']}),
        ('Backups', {'fields': ['lista_backups']}),
        ('Observações', {'fields': ['observacoes', 'criado_em', 'atualizado_em']}),
    ]

    def get_urls(self):
        urls = super().get_urls()
        return [
            path('<pk>/reprovisionar/', self.admin_site.admin_view(self._view_reprovisionar), name='registry_cliente_reprovisionar'),
            path('<pk>/destruir/', self.admin_site.admin_view(self._view_destruir), name='registry_cliente_destruir'),
            path('<pk>/reenviar-boas-vindas/', self.admin_site.admin_view(self._view_reenviar_boas_vindas), name='registry_cliente_reenviar_boas_vindas'),
            path('<pk>/aplicar-modulos/', self.admin_site.admin_view(self._view_aplicar_modulos), name='registry_cliente_aplicar_modulos'),
            path('<pk>/configurar-cloudflare/', self.admin_site.admin_view(self._view_configurar_cloudflare), name='registry_cliente_configurar_cloudflare'),
            path('<pk>/backup/', self.admin_site.admin_view(self._view_backup), name='registry_cliente_backup'),
            path('<pk>/backups/<int:backup_id>/download/', self.admin_site.admin_view(self._view_download_backup), name='registry_cliente_backup_download'),
            path('<pk>/backups/<int:backup_id>/restaurar/', self.admin_site.admin_view(self._view_restaurar), name='registry_cliente_restaurar'),
            path('<pk>/backups/<int:backup_id>/status/', self.admin_site.admin_view(self._view_status_backup), name='registry_cliente_backup_status'),
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
        from django.contrib import messages

        cliente = get_object_or_404(Cliente, pk=pk)
        slug = cliente.slug
        modulos = ','.join(m.slug for m in cliente.modulos_ativos.all()) or 'financeiro,tarefas'
        tema = cliente.tema_site or 'padrao'
        dominio_custom = cliente.dominio_custom or ''
        subdominio = cliente.subdominio

        allowed_hosts = f'{subdominio},{dominio_custom}' if dominio_custom else subdominio
        csrf_origins = (
            f'https://{subdominio},https://{dominio_custom}' if dominio_custom
            else f'https://{subdominio}'
        )

        base_path = Path(os.getenv('CLIENTES_BASE_PATH', '/opt/clientes'))
        env_file = base_path / slug / '.env'
        if env_file.exists():
            campos = {
                'MODULOS_ATIVOS': modulos,
                'TEMA_SITE': tema,
                'ALLOWED_HOSTS': allowed_hosts,
                'CSRF_TRUSTED_ORIGINS': csrf_origins,
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

        # Remove registro A conflitante, se existir
        r_a = _requests.get(f'{base}/zones/{zone_id}/dns_records', params={'type': 'A', 'name': dominio}, headers=auth_headers, timeout=15)
        if r_a.ok:
            for rec in r_a.json().get('result', []):
                _requests.delete(f'{base}/zones/{zone_id}/dns_records/{rec["id"]}', headers=auth_headers, timeout=15)

        # Cria ou atualiza CNAME → tunnel
        r = _cf_raise(_requests.get(f'{base}/zones/{zone_id}/dns_records', params={'type': 'CNAME', 'name': dominio}, headers=auth_headers, timeout=15))
        records = r.json()['result']

        if records:
            record_id = records[0]['id']
            if records[0]['content'] != tunnel_cname or not records[0].get('proxied'):
                _cf_raise(_requests.put(
                    f'{base}/zones/{zone_id}/dns_records/{record_id}',
                    json={'type': 'CNAME', 'name': '@', 'content': tunnel_cname, 'proxied': True, 'ttl': 1},
                    headers=json_headers, timeout=15,
                ))
            record_acao = 'atualizado'
        else:
            _cf_raise(_requests.post(
                f'{base}/zones/{zone_id}/dns_records',
                json={'type': 'CNAME', 'name': '@', 'content': tunnel_cname, 'proxied': True, 'ttl': 1},
                headers=json_headers, timeout=15,
            ))
            record_acao = 'criado'

        # Remove A record de www conflitante, se existir
        r_a_www = _requests.get(
            f'{base}/zones/{zone_id}/dns_records',
            params={'type': 'A', 'name': f'www.{dominio}'},
            headers=auth_headers, timeout=15,
        )
        if r_a_www.ok:
            for rec in r_a_www.json().get('result', []):
                _requests.delete(
                    f'{base}/zones/{zone_id}/dns_records/{rec["id"]}',
                    headers=auth_headers, timeout=15,
                )

        # Cria ou atualiza CNAME www → tunnel
        r_www = _cf_raise(_requests.get(
            f'{base}/zones/{zone_id}/dns_records',
            params={'type': 'CNAME', 'name': 'www'},
            headers=auth_headers, timeout=15,
        ))
        records_www = r_www.json()['result']
        if records_www:
            if records_www[0]['content'] != tunnel_cname or not records_www[0].get('proxied'):
                _cf_raise(_requests.put(
                    f'{base}/zones/{zone_id}/dns_records/{records_www[0]["id"]}',
                    json={'type': 'CNAME', 'name': 'www', 'content': tunnel_cname, 'proxied': True, 'ttl': 1},
                    headers=json_headers, timeout=15,
                ))
        else:
            _cf_raise(_requests.post(
                f'{base}/zones/{zone_id}/dns_records',
                json={'type': 'CNAME', 'name': 'www', 'content': tunnel_cname, 'proxied': True, 'ttl': 1},
                headers=json_headers, timeout=15,
            ))

        return {'ok': True, 'zona_criada': zona_criada, 'nameservers': nameservers, 'record': record_acao, 'tunnel_name': tunnel_name}

    def _view_backup(self, request, pk):
        from django.contrib import messages
        from .tasks import task_backup_cliente
        cliente = get_object_or_404(Cliente, pk=pk)
        backup = BackupCliente.objects.create(cliente=cliente)
        task_backup_cliente.delay(str(cliente.pk), backup.pk)
        messages.success(request, f'Backup iniciado para "{cliente.slug}". Recarregue a página para ver o status.')
        return redirect('admin:registry_cliente_change', pk)

    def _view_download_backup(self, request, pk, backup_id):
        from pathlib import Path
        backup = get_object_or_404(BackupCliente, pk=backup_id, cliente__pk=pk)
        if backup.status != 'concluido' or not backup.arquivo_path:
            raise Http404
        arquivo = Path(backup.arquivo_path)
        if not arquivo.exists():
            raise Http404
        return FileResponse(open(arquivo, 'rb'), as_attachment=True, filename=arquivo.name)

    def _view_restaurar(self, request, pk, backup_id):
        from django.contrib import messages
        from .tasks import task_restaurar_cliente
        cliente = get_object_or_404(Cliente, pk=pk)
        backup = get_object_or_404(BackupCliente, pk=backup_id, cliente=cliente)
        if backup.status != 'concluido':
            messages.error(request, 'Apenas backups concluídos podem ser restaurados.')
            return redirect('admin:registry_cliente_change', pk)
        task_restaurar_cliente.delay(str(cliente.pk), backup.pk)
        messages.warning(
            request,
            f'Restauração iniciada para "{cliente.slug}". O ERP ficará indisponível por alguns minutos. '
            f'Acompanhe o progresso na seção Backups abaixo.',
        )
        return redirect('admin:registry_cliente_change', pk)

    def _view_status_backup(self, request, pk, backup_id):
        backup = get_object_or_404(BackupCliente, pk=backup_id, cliente__pk=pk)
        ativo = backup.status == 'em_andamento' or backup.restaurando
        return JsonResponse({
            'status': backup.status,
            'progresso': backup.progresso,
            'restaurando': backup.restaurando,
            'ativo': ativo,
            'mensagem': backup.mensagem[:120] if backup.mensagem else '',
        })

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

    def lista_backups(self, obj):
        if not obj.pk:
            return '—'
        from django.urls import reverse
        backups = list(obj.backups.all()[:5])
        url_novo = reverse('admin:registry_cliente_backup', args=[obj.pk])

        botao_novo = (
            f'<a href="{url_novo}" style="display:inline-block;padding:6px 14px;background:#2e7d32;'
            f'color:#fff;border-radius:4px;text-decoration:none;font-size:13px;" '
            f'onclick="return confirm(\'Iniciar backup completo (banco de dados + mídia + configuração) para este cliente? O processo roda em segundo plano.\')">💾 Novo Backup</a>'
        )

        if not backups:
            return format_html(
                '{}<p style="margin-top:12px;color:#999;font-size:13px;">Nenhum backup registrado ainda.</p>',
                mark_safe(botao_novo),
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

            if ativo:
                pct = b.progresso
                barra = (
                    f'<div style="margin-top:6px;background:#e0e0e0;border-radius:6px;height:14px;width:100%;max-width:320px;overflow:hidden;">'
                    f'  <div id="bpbar-{b.pk}" style="height:14px;width:{pct}%;background:linear-gradient(90deg,#1565c0,#42a5f5);border-radius:6px;transition:width .4s ease;"></div>'
                    f'</div>'
                    f'<div style="font-size:11px;color:#555;margin-top:3px;">'
                    f'  <span id="bppct-{b.pk}">{pct}%</span>'
                    f'  <span style="margin-left:8px;color:#999;">· recarregando automaticamente…</span>'
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
                    f'      if(!d.ativo){{clearInterval(iv);location.reload();}}'
                    f'    }}).catch(function(){{clearInterval(iv);}});'
                    f'  }},2000);'
                    f'}})();'
                )
                acoes = barra
            elif b.status == 'concluido':
                url_dl = reverse('admin:registry_cliente_backup_download', args=[obj.pk, b.pk])
                url_rst = reverse('admin:registry_cliente_restaurar', args=[obj.pk, b.pk])
                acoes = (
                    f'<span style="color:#666;font-size:12px;margin-right:8px;">{b.tamanho_fmt}</span>'
                    f'<a href="{url_dl}" style="padding:3px 10px;background:#1565c0;color:#fff;border-radius:4px;font-size:12px;text-decoration:none;margin-right:4px;">⬇ Baixar</a>'
                    f'<a href="{url_rst}" style="padding:3px 10px;background:#e65100;color:#fff;border-radius:4px;font-size:12px;text-decoration:none;" '
                    f'onclick="return confirm(\'ATENÇÃO: O ERP ficará indisponível por alguns minutos durante a restauração.\\n\\nO banco de dados e a pasta mídia serão sobrescritos pelo backup de {data}.\\n\\nConfirma?\')">↩ Restaurar</a>'
                )
            else:
                acoes = f'<span style="color:#b71c1c;font-size:12px;">{b.mensagem[:100]}</span>'

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
        return format_html('{}{}{}', mark_safe(botao_novo), mark_safe(tabela), mark_safe(script_tag))

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


from . import celery_beat_pt  # noqa: E402, F401
