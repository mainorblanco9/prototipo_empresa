from django.conf import settings
from openai import OpenAI, OpenAIError
import requests


class AIProviderError(RuntimeError):
    pass


def ai_is_configured():
    provider = settings.AI_PROVIDER.lower()
    if provider == "ollama":
        return True
    if provider == "openai":
        return bool(settings.OPENAI_API_KEY)
    return False


def ai_provider_label():
    provider = settings.AI_PROVIDER.lower()
    if provider == "ollama":
        return "Ollama local"
    if provider == "openai":
        return "OpenAI"
    return "proveedor de IA no válido"


def generate_text(system_prompt, user_prompt, max_tokens=None):
    provider = settings.AI_PROVIDER.lower()
    if provider == "ollama":
        return _generate_with_ollama(system_prompt, user_prompt, max_tokens)
    if provider == "openai":
        return _generate_with_openai(system_prompt, user_prompt, max_tokens)
    raise AIProviderError(
        "AI_PROVIDER debe ser 'ollama' o 'openai'."
    )


def _generate_with_ollama(system_prompt, user_prompt, max_tokens):
    url = f"{settings.OLLAMA_BASE_URL.rstrip('/')}/api/chat"
    payload = {
        "model": settings.OLLAMA_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "stream": False,
    }
    if max_tokens is not None:
        payload["options"] = {"num_predict": max_tokens}

    try:
        response = requests.post(
            url,
            json=payload,
            timeout=settings.OLLAMA_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        content = response.json()["message"]["content"]
    except requests.RequestException as exc:
        raise AIProviderError(
            "No se pudo conectar con Ollama. Comprueba que Ollama esté abierto "
            "y que el modelo configurado esté descargado."
        ) from exc
    except (KeyError, TypeError, ValueError) as exc:
        raise AIProviderError(
            "Ollama devolvió una respuesta vacía o con formato no válido."
        ) from exc

    if not isinstance(content, str) or not content.strip():
        raise AIProviderError("Ollama devolvió una respuesta vacía.")
    return content.strip()


def _generate_with_openai(system_prompt, user_prompt, max_tokens):
    if not settings.OPENAI_API_KEY:
        raise AIProviderError(
            "Configura OPENAI_API_KEY o selecciona AI_PROVIDER=ollama."
        )

    try:
        client = OpenAI(api_key=settings.OPENAI_API_KEY)
        options = {
            "model": settings.OPENAI_MODEL,
            "instructions": system_prompt,
            "input": user_prompt,
        }
        if max_tokens is not None:
            options["max_output_tokens"] = max_tokens
        response = client.responses.create(**options)
    except OpenAIError as exc:
        raise AIProviderError(
            "No se pudo completar la solicitud al proveedor OpenAI."
        ) from exc

    content = response.output_text
    if not isinstance(content, str) or not content.strip():
        raise AIProviderError("OpenAI devolvió una respuesta vacía.")
    return content.strip()
