from django.conf import settings
from django.db import models


class UserDesignPreference(models.Model):
    class Theme(models.TextChoices):
        MINIMAL = "minimal", "Minimalista"
        ABSTRACT = "abstract", "Abstracto"
        CORPORATE = "corporate", "Corporativo"

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="design_preference",
    )
    theme = models.CharField(
        max_length=20,
        choices=Theme.choices,
        default=Theme.MINIMAL,
    )
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.user} — {self.get_theme_display()}"
