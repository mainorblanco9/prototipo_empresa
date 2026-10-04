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


PROCESS = {
    "manual": (
        "Canal manual: el usuario digita la factura en el asistente de 5 pasos "
        "(emisor, receptor y fecha, líneas con precio final con IVA, revisión del total, crear). "
        "Se guarda como borrador, se valida localmente, se marca lista y se envía a Hacienda, "
        "que responde aceptado o rechazado."
    ),
    "excel": (
        "Canal Excel: se sube un .xlsx o .csv con una fila por línea (columnas factura, emisor, "
        "cedula_emisor, receptor, cedula_receptor, fecha, descripcion, cantidad, precio_con_iva, iva). "
        "Se valida el archivo, se ven los errores por fila y se crean como borradores solo las facturas válidas."
    ),
    "api": (
        "Canal API REST: el proveedor usa una API key (Authorization: Bearer gb_...) y llama "
        "POST /api/v1/invoices/ (con client_id para evitar duplicados), luego /validate/, "
        "transition a ready y /submit/. La documentación está en /api/docs/."
    ),
    "cierre": (
        "Cierre de periodo: antes de cerrar el mes no deben quedar borradores, facturas en proceso ni "
        "rechazadas. Las rechazadas se corrigen y reenvían, o se anulan. Un periodo cerrado no admite "
        "cambios hasta reabrirlo."
    ),
    "facturas": (
        "Facturas: Borrador -> Listo -> Recibido -> Aceptado o Rechazado. Una factura rechazada se puede "
        "editar y reenviar (recibe nueva clave) o anular. Una aceptada ya no se modifica."
    ),
    "general": (
        "GuanaBill recibe facturas por tres canales (manual, Excel y API REST), las valida, las envía a "
        "Hacienda, guarda cada respuesta en la bitácora y permite cerrar el mes."
    ),
}

GUIDE_STEPS = {
    "manual": [
        "Escribe emisor y receptor; puedes guardar favoritos para no repetirlos.",
        "Agrega cada línea con el precio final (IVA incluido) y su tarifa.",
        "Revisa el total y pide la revisión con IA antes de crear.",
        "Crea el borrador o envíalo directo a Hacienda.",
    ],
    "excel": [
        "Descarga la plantilla o usa los archivos de la carpeta samples.",
        "Sube el archivo y revisa el resultado fila por fila.",
        "Corrige las filas con error y vuelve a validar.",
        "Crea las facturas válidas y envíalas desde Facturas.",
    ],
    "api": [
        "Crea una API key en Integraciones.",
        "Envía POST /api/v1/invoices/ con un client_id único.",
        "Valida con /validate/ y envía con /submit/.",
        "Consulta /history/ y /ai-analysis/ para ver la respuesta de Hacienda.",
    ],
    "cierre": [
        "Envía las facturas pendientes.",
        "Corrige o anula las rechazadas.",
        "Espera las que están en proceso.",
        "Valida y cierra el periodo.",
    ],
    "facturas": [
        "Valida y marca lista cada borrador.",
        "Envía a Hacienda y revisa la respuesta.",
        "Si fue rechazada, usa Editar con ayuda de la IA y reenvía.",
    ],
    "general": ["Elige un canal de ingreso: manual, Excel o API.", "Envía a Hacienda.", "Revisa la bitácora y cierra el mes."],
}

REJECTION_HINTS = {
    "-37": "Verifica la actividad económica del emisor.",
    "-43": "Revisa que el código CABYS de cada línea exista en el catálogo vigente.",
    "-52": "Recalcula el IVA: el total de impuesto debe coincidir con la suma de las líneas.",
    "-64": "Confirma que la identificación del receptor esté inscrita en Hacienda.",
    "-79": "Revisa que la tarifa de IVA de cada línea sea la correcta.",
}

CONTEXT_LABELS = {k: k for k in PROCESS}


def rejection_code(message):
    import re

    found = re.search(r"\[(-\d+)\]", message or "")
    return found.group(1) if found else ""


def facts_for(owner, period=None):
    from django.db.models import Count

    from . import periods
    from .models import IssuedInvoice

    counts = {r["status"]: r["n"] for r in owner.issued_invoices.values("status").annotate(n=Count("id"))}
    labels = dict(IssuedInvoice.Status.choices)
    text = "Facturas por estado: " + (", ".join(f"{labels.get(k, k)}={v}" for k, v in counts.items()) or "ninguna")
    if period and periods.parse_period(period):
        report = periods.validate_period(owner, period)
        text += f"\nCierre {period}: " + (" | ".join(report["issues"]) or "sin bloqueos, listo para cerrar")
    return text


def fix_guidance(invoice, issues):
    """Pasos concretos para corregir una factura rechazada o con errores locales."""
    code = rejection_code(invoice.hacienda_message)
    steps = [i["message"] if isinstance(i, dict) else str(i) for i in issues]
    if code in REJECTION_HINTS:
        steps.append(REJECTION_HINTS[code])
    steps += [
        "Pulsa Editar, corrige los datos y guarda: la factura vuelve a borrador.",
        "Valida, marca lista y reenvía; recibirá una nueva clave.",
        "Si la factura no corresponde, anúlala.",
    ]
    fallback = "Cómo corregirla:\n" + "\n".join(f"- {s}" for s in steps)
    prompt = (
        f"La factura fue rechazada por Hacienda. Mensaje: {invoice.hacienda_message or '(sin detalle)'}\n"
        f"Problemas de validación local: {'; '.join(steps[:len(issues)]) or 'ninguno'}\n\n"
        f"{_document_summary(invoice.document)}\n\n"
        "Indica en 3 a 5 viñetas qué campos concretos de la factura conviene cambiar y por qué, "
        "y qué hacer después (editar, validar, reenviar o anular). No menciones simuladores."
    )
    try:
        return generate_local_text(SYSTEM, prompt, max_tokens=450), True, steps
    except AIProviderError:
        return fallback, False, steps


def assistant_answer(context, question, facts="", extra=""):
    context = context if context in PROCESS else "general"
    steps = GUIDE_STEPS[context]
    question = (question or "").strip()[:600] or "Guíame paso a paso en lo que estoy haciendo ahora."
    prompt = (
        f"Proceso: {PROCESS[context]}\n{facts}\n"
        + (f"Datos de la pantalla: {extra[:1500]}\n" if extra else "")
        + f"\nPregunta del usuario: {question}\n"
        "Responde como guía de GuanaBill: indica el siguiente paso concreto y avisa de errores comunes."
    )
    try:
        return generate_local_text(SYSTEM, prompt, max_tokens=450), True, steps
    except AIProviderError:
        return "Pasos recomendados:\n" + "\n".join(f"{i}. {s}" for i, s in enumerate(steps, 1)), False, steps
