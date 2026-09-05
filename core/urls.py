"""
URL configuration for core project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/6.0/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.contrib import admin
from django.urls import path, include

from registry.views import admin_ajuda
from registry.release_api import register_pdv_local_release

urlpatterns = [
    path('', include('landing.urls')),
    # Precisa vir antes de 'admin/' — o catch_all_view do django.contrib.admin
    # intercepta qualquer sub-path desconhecido sob 'admin/' antes que esta
    # rota especifica seja tentada, se ficar depois.
    path('admin/ajuda/', admin.site.admin_view(admin_ajuda), name='admin_ajuda'),
    path('admin/', admin.site.urls),
    # Chamado pelo workflow de release do repo pdv-local (GitHub Actions) —
    # ver registry/release_api.py.
    path('v1/releases/pdv-local:register', register_pdv_local_release, name='register_pdv_local_release'),
]
