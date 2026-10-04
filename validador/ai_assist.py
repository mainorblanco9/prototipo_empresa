"""Asistente de IA para facturas. Usa solo Ollama en localhost.

Los importes los calcula Django (determinista); el modelo solo explica y sugiere.
Si Ollama no responde, se devuelve el cálculo y un análisis básico sin IA.
"""
from decimal import Decimal

from .ai_client import AIProviderError, generate_local_text

SYSTEM = (
    "Eres un asistente de facturación para Costa Rica. Responde en español, breve "
    "y práctico, con viñetas. Usa solo los datos dados; no inventes reglas fiscales, "
    "montos ni garantías de cumplimiento. Si dudas, recomienda confirmar con el contador."
)


def _fmt(value):
    return f"₡{Decimal(value):,.2f}"


def estimate_cost(document):
    """Desglose determinista del costo para el receptor."""
    lines = []
    by_rate = {}
    for line in document["lines"]:
        rate = line["tax_rate"]
        entry = by_rate.setdefault(rate, {"base": Decimal("0"), "tax": Decimal("0")})
        entry["base"] += Decimal(line["subtotal"])
        entry["tax"] += Decimal(line["tax_amount"])
        lines.append(
            {
                "description": line["description"],
                "total": line["total"],
                "tax_amount": line["tax_amount"],
            }
        )
    return {
        "currency": "CRC",
        "subtotal": document["totals"]["subtotal"],
        "tax": document["totals"]["tax"],
        "total": document["totals"]["total"],
        "by_rate": {
            rate: {"base": str(v["base"]), "tax": str(v["tax"])}
            for rate, v in sorted(by_rate.items())
        },
        "lines": lines,
    }


def _document_summary(document):
    rows = "\n".join(
        f"{l['line_number']}. {l['description']} | cant {l['quantity']} | "
        f"precio c/IVA {l['unit_price_including_tax']} | tarifa {l['tax_rate']} | "
        f"IVA {l['tax_amount']} | total {l['total']}"
        for l in document["lines"]
    )
    t = document["totals"]
    return (
        f"Fecha: {document['issue_date']}\nLíneas:\n{rows}\n"
        f"Subtotal {t['subtotal']} · IVA {t['tax']} · Total {t['total']} CRC"
    )


def review_draft(document):
    """Costo exacto + sugerencias de IA sobre descripciones y tarifas."""
    cost = estimate_cost(document)
    result = {"cost": cost, "ai_available": True, "advice": "", "error": ""}
    prompt = (
        "Para esta factura en colones: 1) di en una frase cuánto pagará el cliente "
        "(usa exactamente el total dado); 2) revisa descripciones ambiguas o muy cortas, "
        "cantidades o precios atípicos y tarifas de IVA que convenga confirmar; "
        "3) propone descripciones corregidas si aplica.\n\n" + _document_summary(document)
    )
    try:
        result["advice"] = generate_local_text(SYSTEM, prompt, max_tokens=500)
    except AIProviderError as exc:
        result["ai_available"] = False
        result["error"] = str(exc)
    return result


def fallback_hacienda_analysis(status, message):
    if status == "accepted":
        return (
            "Hacienda aceptó el comprobante. Conserva la clave y el XML de respuesta "
            "y entrega la factura al cliente."
        )
    if status == "rejected":
        return (
            "Hacienda rechazó el comprobante. Revisa el mensaje de rechazo"
            + (f" («{message}»)" if message else "")
            + ", corrige los datos, vuelve a borrador y reenvía."
        )
    return "El comprobante sigue en proceso. Vuelve a consultar el estado en unos minutos."


def analyze_hacienda_result(invoice):
    """Explica la respuesta de Hacienda. Devuelve (texto, usó_ia)."""
    document = invoice.document
    prompt = (
        f"Estado devuelto por Hacienda: {invoice.get_status_display()} ({invoice.status}).\n"
        f"Mensaje: {invoice.hacienda_message or '(sin detalle)'}\n"
        f"Clave: {invoice.clave}\n\n{_document_summary(document)}\n\n"
        "Explica en lenguaje simple qué significa el resultado. Si fue aceptada, indica "
        "qué hacer a continuación. Si fue rechazada, enumera las causas probables según "
        "el mensaje y los datos, y los pasos concretos para corregir y reenviar. "
        "Responde como asesor tributario de Costa Rica: no menciones simuladores ni entornos de prueba."
    )
    try:
        return generate_local_text(SYSTEM, prompt, max_tokens=500), True
    except AIProviderError:
        return fallback_hacienda_analysis(invoice.status, invoice.hacienda_message), False
