from django.utils import timezone
from rest_framework import authentication, exceptions

from .models import ApiKey


class ApiKeyAuthentication(authentication.BaseAuthentication):
    """Acepta `Authorization: Bearer gb_...` o `X-API-Key: gb_...`."""

    def authenticate(self, request):
        raw = request.headers.get("X-API-Key", "")
        header = request.headers.get("Authorization", "")
        if not raw and header.lower().startswith("bearer "):
            raw = header[7:].strip()
        if not raw.startswith("gb_"):
            return None
        try:
            key = ApiKey.objects.select_related("owner").get(
                key_hash=ApiKey.hash_key(raw), revoked_at__isnull=True
            )
        except ApiKey.DoesNotExist:
            raise exceptions.AuthenticationFailed("API key invalida o revocada.")
        if not key.owner.is_active:
            raise exceptions.AuthenticationFailed("Usuario inactivo.")
        ApiKey.objects.filter(pk=key.pk).update(last_used_at=timezone.now())
        return key.owner, key

    def authenticate_header(self, request):
        return "Bearer"
