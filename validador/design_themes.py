import json
import re

from .ai_client import AIProviderError, ai_is_configured, generate_text

THEMES = {
    "minimal": {"label": "Minimalista", "colors": ["#f5f7fa", "#003087", "#0070e0"], "mood": "limpio, claro, corporativo azul"},
    "abstract": {"label": "Abstracto", "colors": ["#17162a", "#8b5cf6", "#a78bfa"], "mood": "oscuro, violeta, creativo"},
    "corporate": {"label": "Corporativo", "colors": ["#f2f7f4", "#10563f", "#176b52"], "mood": "verde sobrio, financiero"},
    "ocean": {"label": "Océano", "colors": ["#f0f9fb", "#0e5a73", "#0891b2"], "mood": "turquesa fresco, calmado"},
    "sunset": {"label": "Atardecer", "colors": ["#fff7f2", "#9a3412", "#ea580c"], "mood": "cálido, naranja, energético"},
    "midnight": {"label": "Medianoche", "colors": ["#0f172a", "#0ea5e9", "#38bdf8"], "mood": "oscuro, azul, descanso visual nocturno"},
}

KEYWORDS = {
    "minimal": ("minimal", "limpio", "simple", "claro", "blanco", "sobrio"),
    "abstract": ("abstract", "creativ", "violeta", "morado", "arte", "moderno"),
    "corporate": ("corporat", "verde", "finan", "formal", "serio", "naturaleza"),
    "ocean": ("oc[eé]ano", "mar", "azul", "fresc", "calm", "turquesa", "playa"),
    "sunset": ("atardecer", "calid", "naranja", "energ", "rojo", "amanecer"),
    "midnight": ("noche", "oscur", "dark", "nocturn", "descans", "ojos"),
}


def _fallback(description):
    text = (description or "").lower()
    scored = sorted(
        THEMES,
        key=lambda key: -sum(bool(re.search(word, text)) for word in KEYWORDS[key]),
    )
    return [
        {"theme": key, "reason": f"Estilo {THEMES[key]['label'].lower()}: {THEMES[key]['mood']}."}
        for key in scored[:3]
    ]


def recommend_themes(description, current):
    """Devuelve hasta 3 temas sugeridos y si la IA local participó."""
    description = (description or "").strip()[:300] or "un diseño minimalista, agradable y cómodo para trabajar facturas"
    if not ai_is_configured():
        return _fallback(description), False
    catalog = "; ".join(f"{key}: {info['mood']}" for key, info in THEMES.items())
    try:
        raw = generate_text(
            system_prompt=(
                "Eres asesor de diseño de interfaces. Elige exactamente 3 temas distintos del catálogo "
                f"({catalog}) que mejor encajen con lo que pide el usuario; el tema actual es {current}. "
                'Responde solo JSON: {"themes":[{"theme":"clave","reason":"motivo en español, máximo 15 palabras"}]}.'
            ),
            user_prompt=description,
            max_tokens=300,
            json_mode=True,
        )
        data = json.loads(raw[raw.index("{"): raw.rindex("}") + 1])
        result, seen = [], set()
        for item in data.get("themes", []):
            key = str(item.get("theme", "")).strip().lower()
            if key in THEMES and key not in seen:
                seen.add(key)
                result.append({"theme": key, "reason": str(item.get("reason", ""))[:140]})
        if result:
            return result[:3], True
    except (AIProviderError, ValueError, KeyError, TypeError):
        pass
    return _fallback(description), False
