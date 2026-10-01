import re

from .ai_client import generate_text

THEME_KEYS = {"minimal", "abstract", "corporate"}


def recommend_design(description):
    recommendation = generate_text(
        system_prompt=(
            "Eres un asesor de diseño de interfaces empresariales. Elige la opción "
            "que mejor se ajusta a la preferencia del usuario y responde con una "
            "sola palabra, exactamente una de estas opciones: minimal, abstract, corporate. "
            "No devuelvas explicaciones ni HTML, CSS o código."
        ),
        user_prompt=description,
        max_tokens=10,
    )
    recommendation = re.sub(r"[^a-z]", "", recommendation.strip().lower())
    if recommendation not in THEME_KEYS:
        raise ValueError("La IA devolvió una recomendación de tema no admitida.")
    return recommendation
