from django.conf import settings


def branding(request):
    mode = getattr(settings, "HACIENDA_MODE", "local")
    label = "Sandbox local" if mode == "local" else "Produccion"
    return {"gb_environment_label": label, "gb_hacienda_mode": mode}
