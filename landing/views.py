from django.conf import settings
from django.shortcuts import redirect, render

from registry.models import Plano, Cliente, HostInfraestrutura

from .forms import FormCadastro

SAAS_DOMAIN = getattr(settings, 'SAAS_DOMAIN', 'ararasuite.com.br')


def funcionalidades(request):
    return render(request, 'landing/funcionalidades.html')


def como_funciona(request):
    return render(request, 'landing/como_funciona.html')


def planos(request):
    planos_qs = Plano.objects.filter(ativo=True).prefetch_related('modulos_inclusos').order_by('ordem')
    return render(request, 'landing/planos.html', {'planos': planos_qs, 'saas_domain': SAAS_DOMAIN})


def privacidade(request):
    return render(request, 'landing/privacidade.html')


def termos(request):
    return render(request, 'landing/termos.html')


def inicio(request):
    planos = Plano.objects.filter(ativo=True).prefetch_related('modulos_inclusos').order_by('ordem')
    return render(request, 'landing/home.html', {'planos': planos, 'saas_domain': SAAS_DOMAIN})


def cadastro(request):
    plano_inicial = request.GET.get('plano')
    if request.method == 'POST':
        form = FormCadastro(request.POST)
        if form.is_valid():
            slug = form.cleaned_data['slug']
            plano = form.cleaned_data['plano']
            host = HostInfraestrutura.objects.filter(ativo=True).first()
            versao = getattr(settings, 'ERP_LATEST_VERSION', 'latest')
            cliente = Cliente.objects.create(
                nome=form.cleaned_data['nome'],
                cnpj=form.cleaned_data['cnpj'],
                email_contato=form.cleaned_data['email'],
                telefone=form.cleaned_data.get('telefone', ''),
                slug=slug,
                subdominio=f'{slug}.{SAAS_DOMAIN}',
                plano=plano,
                host=host,
                versao_erp=versao,
                status='aguardando_provisao',
            )
            cliente.modulos_ativos.set(plano.modulos_inclusos.all())
            from registry.tasks import task_enviar_email_boas_vindas
            task_enviar_email_boas_vindas.delay(str(cliente.pk))
            return redirect('landing:sucesso', slug=slug)
    else:
        initial = {}
        if plano_inicial:
            try:
                initial['plano'] = Plano.objects.get(slug=plano_inicial)
            except Plano.DoesNotExist:
                pass
        form = FormCadastro(initial=initial)

    planos = Plano.objects.filter(ativo=True).order_by('ordem')
    return render(request, 'landing/signup.html', {
        'form': form,
        'planos': planos,
        'saas_domain': SAAS_DOMAIN,
    })


def sucesso(request, slug):
    try:
        cliente = Cliente.objects.get(slug=slug)
    except Cliente.DoesNotExist:
        return redirect('landing:inicio')
    return render(request, 'landing/sucesso.html', {'cliente': cliente, 'saas_domain': SAAS_DOMAIN})
