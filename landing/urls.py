from django.urls import path
from . import views

app_name = 'landing'

urlpatterns = [
    path('', views.inicio, name='inicio'),
    path('funcionalidades/', views.funcionalidades, name='funcionalidades'),
    path('como-funciona/', views.como_funciona, name='como_funciona'),
    path('planos/', views.planos, name='planos'),
    path('privacidade/', views.privacidade, name='privacidade'),
    path('termos/', views.termos, name='termos'),
    path('comecar/', views.cadastro, name='cadastro'),
    path('bem-vindo/<slug:slug>/', views.sucesso, name='sucesso'),
]
