from pathlib import Path
import os
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent

load_dotenv(BASE_DIR / '.env')

VERSION = '0.0.25'

SECRET_KEY = os.getenv('SECRET_KEY', 'django-insecure-change-me')
DEBUG = os.getenv('DEBUG', 'true').lower() in ('1', 'true', 'yes')
ALLOWED_HOSTS = [h.strip() for h in os.getenv('ALLOWED_HOSTS', 'localhost').split(',')]
CSRF_TRUSTED_ORIGINS = [o.strip() for o in os.getenv('CSRF_TRUSTED_ORIGINS', 'http://localhost').split(',')]

INSTALLED_APPS = [
    'unfold',
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'django_celery_beat',
    'registry',
    'landing',
]

from django.templatetags.static import static
from django.urls import reverse_lazy

UNFOLD = {
    "SITE_TITLE": "Painel de Controle AraraSuite",
    "SITE_HEADER": "AraraSuite",
    "SITE_SYMBOL": "hub",
    "SHOW_HISTORY": True,
    "SHOW_VIEW_ON_SITE": False,
    "STYLES": [
        lambda request: static("registry/admin_custom.css"),
    ],
    "SIDEBAR": {
        "show_search": True,
        "show_all_applications": False,
        "navigation": [
            {
                "title": "Clientes",
                "items": [
                    {"title": "Clientes", "icon": "groups", "link": reverse_lazy("admin:registry_cliente_changelist")},
                    {"title": "Planos", "icon": "workspace_premium", "link": reverse_lazy("admin:registry_plano_changelist")},
                    {"title": "Módulos", "icon": "extension", "link": reverse_lazy("admin:registry_modulo_changelist")},
                    {"title": "Hosts de Infraestrutura", "icon": "dns", "link": reverse_lazy("admin:registry_hostinfraestrutura_changelist")},
                    {"title": "Verificações de Saúde", "icon": "monitor_heart", "link": reverse_lazy("admin:registry_verificacaosaude_changelist")},
                ],
            },
            {
                "title": "Agendamentos",
                "items": [
                    {"title": "Tarefas Periódicas", "icon": "schedule", "link": reverse_lazy("admin:django_celery_beat_periodictask_changelist")},
                    {"title": "Intervalos", "icon": "timelapse", "link": reverse_lazy("admin:django_celery_beat_intervalschedule_changelist")},
                    {"title": "Crontabs", "icon": "event_repeat", "link": reverse_lazy("admin:django_celery_beat_crontabschedule_changelist")},
                ],
            },
            {
                "title": "Configurações",
                "items": [
                    {"title": "E-Mail", "icon": "mail", "link": reverse_lazy("admin:registry_configuracaoemail_changelist")},
                    {"title": "Cloudflare", "icon": "cloud", "link": reverse_lazy("admin:registry_configuracaocloudflare_changelist")},
                    {"title": "Ajuda", "icon": "help", "link": reverse_lazy("admin_ajuda")},
                ],
            },
        ],
    },
}

SAAS_DOMAIN = os.getenv('SAAS_DOMAIN', 'ararasuite.com.br')
ERP_LATEST_VERSION = os.getenv('ERP_LATEST_VERSION', '0.0.26')

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'core.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
                'core.context_processors.app_metadata',
            ],
        },
    },
]

WSGI_APPLICATION = 'core.wsgi.application'

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.postgresql',
        'NAME': os.getenv('POSTGRES_DB', 'erp_cp'),
        'USER': os.getenv('POSTGRES_USER', 'erp_cp'),
        'PASSWORD': os.getenv('POSTGRES_PASSWORD', ''),
        'HOST': os.getenv('POSTGRES_HOST', 'localhost'),
        'PORT': os.getenv('POSTGRES_PORT', '5432'),
    }
}

LANGUAGE_CODE = 'pt-br'
TIME_ZONE = 'America/Sao_Paulo'
USE_I18N = True
USE_TZ = True

STATIC_URL = '/static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'
STATICFILES_STORAGE = 'whitenoise.storage.CompressedManifestStaticFilesStorage'

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# Celery
REDIS_URL = os.getenv('REDIS_URL', 'redis://localhost:6379/0')
CELERY_BROKER_URL = REDIS_URL
CELERY_RESULT_BACKEND = REDIS_URL
CELERY_TIMEZONE = TIME_ZONE
CELERY_BEAT_SCHEDULER = 'django_celery_beat.schedulers:DatabaseScheduler'

# E-mail
EMAIL_BACKEND = 'django.core.mail.backends.smtp.EmailBackend'
EMAIL_HOST = os.getenv('EMAIL_HOST', '')
EMAIL_PORT = int(os.getenv('EMAIL_PORT', 587))
EMAIL_HOST_USER = os.getenv('EMAIL_HOST_USER', '')
EMAIL_HOST_PASSWORD = os.getenv('EMAIL_HOST_PASSWORD', '')
EMAIL_USE_TLS = True
DEFAULT_FROM_EMAIL = os.getenv('DEFAULT_FROM_EMAIL', 'noreply@ararasuite.com.br')
