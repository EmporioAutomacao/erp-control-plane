from django.contrib import admin
from django.shortcuts import render


def admin_ajuda(request):
    context = {
        **admin.site.each_context(request),
        'title': 'Ajuda',
    }
    return render(request, 'registry/admin_ajuda.html', context)
