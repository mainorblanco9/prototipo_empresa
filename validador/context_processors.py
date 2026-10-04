from django.conf import settings

from .design_themes import THEMES
from .models import UserDesignPreference


def branding(request):
    mode = getattr(settings, "HACIENDA_MODE", "local")
    label = "Sandbox local" if mode == "local" else "Producción"
    theme = "minimal"
    user = getattr(request, "user", None)
    if user is not None and user.is_authenticated:
        preference = UserDesignPreference.objects.filter(user=user).only("theme").first()
        if preference and preference.theme in THEMES:
            theme = preference.theme
    return {
        "gb_environment_label": label,
        "gb_hacienda_mode": mode,
        "gb_theme": theme,
        "gb_themes": [{"key": key, **info} for key, info in THEMES.items()],
    }
