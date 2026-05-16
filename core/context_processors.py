def app_metadata(request):
    from django.conf import settings

    return {"app_version": getattr(settings, "VERSION", "")}
