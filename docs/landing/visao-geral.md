# Landing Page: Visao Geral Tecnica

- data: 2026-05-14
- componente: landing
- responsavel: Time ERP
- status: entregue

## 1. Objetivo do componente

- apresentar o produto AraraSuite ERP a potenciais clientes antes do cadastro;
- descrever funcionalidades, planos e diferenciais da plataforma de forma atrativa;
- conduzir o visitante ao formulario de signup e disparar o provisionamento automatico;
- servir de ponto de entrada publico para a plataforma SaaS em `ararasuite.com.br`.

## 2. Escopo entregue

1. Pagina inicial (`/`) com hero animado, cards de funcionalidades, secao "Como funciona" e cards de planos dinamicos.
2. Pagina de funcionalidades (`/funcionalidades/`) com descricao detalhada de cada modulo disponivel.
3. Pagina de como funciona (`/como-funciona/`) com timeline de onboarding e FAQ accordion.
4. Pagina de planos (`/planos/`) com cards dinamicos lidos do banco de dados e FAQ de planos.
5. Formulario de signup (`/comecar/`) com validacao CNPJ, geracao de slug e preview do subdominio.
6. Pagina de sucesso (`/bem-vindo/<slug>/`) exibida apos o cadastro concluido.
7. Politica de privacidade (`/privacidade/`) em conformidade com LGPD.
8. Termos de uso (`/termos/`) com foro em Anapolis-GO.

## 3. Stack tecnico

| Componente | Escolha |
|---|---|
| Framework | Django (app `landing` no `erp-control-plane`) |
| CSS | Tailwind CSS Play CDN (via `<script src="cdn.tailwindcss.com">`) |
| Animacoes de entrada | AOS (Animate on Scroll) v2.3.4 via CDN |
| Interatividade | Alpine.js v3 via CDN (navbar, formulario, FAQ) |
| Fonte | Inter via Google Fonts |
| Templates | Django template engine com `{% extends %}` e `{% block %}` |
| Dados de planos | Lidos do modelo `registry.Plano` com `prefetch_related('modulos_inclusos')` |

## 4. Estrutura de arquivos

```
landing/
├── __init__.py
├── apps.py
├── forms.py
├── urls.py
├── views.py
└── templates/
    └── landing/
        ├── base.html          # layout base: navbar, footer, CDNs
        ├── home.html          # pagina inicial
        ├── funcionalidades.html
        ├── como_funciona.html
        ├── planos.html
        ├── signup.html        # formulario de cadastro
        ├── sucesso.html       # tela pos-cadastro
        ├── privacidade.html
        └── termos.html
```

## 5. URLs e rotas

Registradas em `landing/urls.py` com `app_name = 'landing'` e incluidas em `core/urls.py` na raiz (`""`).

| URL | View | Nome |
|---|---|---|
| `/` | `home` | `landing:home` |
| `/funcionalidades/` | `funcionalidades` | `landing:funcionalidades` |
| `/como-funciona/` | `como_funciona` | `landing:como_funciona` |
| `/planos/` | `planos` | `landing:planos` |
| `/privacidade/` | `privacidade` | `landing:privacidade` |
| `/termos/` | `termos` | `landing:termos` |
| `/comecar/` | `signup` | `landing:signup` |
| `/bem-vindo/<slug>/` | `sucesso` | `landing:sucesso` |

## 6. Formulario de signup

Implementado em `landing/forms.py` como `SignupForm` (Django `Form`, nao `ModelForm`).

Campos:
- `nome` — razao social da empresa
- `cnpj` — CNPJ com mascara JS (`XX.XXX.XXX/XXXX-XX`) e validacao de digitos verificadores em Python
- `email` — e-mail do responsavel
- `telefone` — opcional
- `slug` — subdominio escolhido; gerado automaticamente a partir do nome pela funcao JS `gerarSlug()`; validado como unico no banco
- `plano` — `ModelChoiceField` filtrado por `ativo=True`

Validacoes:
- `clean_cnpj()`: formata para `XX.XXX.XXX/XXXX-XX`, executa algoritmo de digitos verificadores (`_validar_cnpj()`), rejeita se CNPJ ja existe no banco
- `clean_slug()`: lowercase, rejeita se slug ja existe no banco

## 7. Fluxo de cadastro

1. Visitante preenche o formulario em `/comecar/`.
2. `SignupForm` valida CNPJ (digitos verificadores) e slug (unicidade).
3. View `signup` cria `registry.Tenant` com `status='aguardando_provisao'`.
4. Atribui os `modulos_ativos` do plano escolhido via `tenant.modulos_ativos.set(plano.modulos_inclusos.all())`.
5. Signal `post_save` em `Tenant` dispara `task_provisionar_tenant.delay(tenant.pk)` via Celery.
6. Visitante e redirecionado para `/bem-vindo/<slug>/` exibindo credenciais e instrucoes.

## 8. Dados de planos na home e na pagina de planos

A view `home` e a view `planos` passam no contexto:

```python
planos = Plano.objects.filter(ativo=True).prefetch_related('modulos_inclusos').order_by('ordem')
```

Os cards de plano renderizam dinamicamente:
- `plano.nome`, `plano.descricao`, `plano.preco_mensal`
- `plano.max_usuarios`, `plano.max_empresas`, `plano.recursos_cpu`, `plano.recursos_ram_gb`
- `plano.destaque` — ativa borda indigo, badge "Mais popular" e scale-105 no card
- `plano.modulos_inclusos.all()` — lista de `Modulo` incluidos no plano

## 9. Base template

`base.html` centraliza:
- Tailwind config (cores `brand`, animacoes `float` e `fadeUp`, fonte Inter)
- Estilos globais: `.gradient-text`, `.gradient-bg`, `.hero-grid`, `.glow`, `.glass`, `.card-hover`
- Navbar fixa com blur ao scroll (`Alpine.js @scroll.window`) e menu mobile com transicao
- Footer com coluna de produto, coluna de contato (Anapolis e Brasilia com links WhatsApp)
- AOS inicializado com `duration: 650, once: true, offset: 60`
- Blocos: `{% block title %}`, `{% block description %}`, `{% block content %}`, `{% block extra_head %}`, `{% block extra_scripts %}`

## 10. Configuracoes relevantes em settings.py

```python
INSTALLED_APPS = [..., 'landing']

SAAS_DOMAIN = os.getenv('SAAS_DOMAIN', 'ararasuite.com.br')
# usado na view signup e na pagina de sucesso para montar o subdominio do cliente

ERP_LATEST_VERSION = os.getenv('ERP_LATEST_VERSION', '0.0.22')
```

## 11. Variaveis de ambiente

| Variavel | Padrao | Descricao |
|---|---|---|
| `SAAS_DOMAIN` | `ararasuite.com.br` | Dominio base para subdominio do cliente |
| `ERP_LATEST_VERSION` | `0.0.22` | Versao atual do ERP usada no texto de sucesso |

## 12. Pontos de atencao

- Tailwind Play CDN e adequado para desenvolvimento; em producao, substituir por build PostCSS com `tailwindcss` CLI para eliminar o overhead do compilador em-browser.
- Os dados de planos sao lidos diretamente do banco — se o banco nao tiver nenhum plano `ativo=True`, a pagina inicial nao renderiza cards de planos.
- O fixture `registry/fixtures/initial_data.json` popula os planos e modulos iniciais; deve ser carregado via `manage.py loaddata` sempre que um novo ambiente for criado.
- O fixture deve ser mantido com encoding UTF-8 sem BOM; para regerar, usar o script Python que captura stdout do `dumpdata` via `io.StringIO` (ver `docs/padroes/encoding-postgres.md`).
- Links internos entre paginas usam `{% url 'landing:nome' %}` — nunca `href="#ancora"` na navbar ou no rodape.

## 13. Documentos relacionados

- [registry/visao-geral.md](../registry/visao-geral.md) — app de provisionamento que e disparado pelo signup
- [padroes/encoding-postgres.md](../padroes/encoding-postgres.md) — problema de encoding encontrado nos dados de modulos
- [../../../erp/docs/saas/plano-saas-v2.md](../../../../erp/docs/saas/plano-saas-v2.md) — plano arquitetural da plataforma SaaS
