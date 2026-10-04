import csv
from decimal import Decimal

from django.contrib.auth.decorators import login_required
from django.db.models import Count, Sum
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from . import periods
from .invoice_import import TEMPLATE_HEADERS, TEMPLATE_ROWS
from .models import IssuedInvoice, IssuedInvoiceStatusEvent, PeriodClosure

S = IssuedInvoice.Status


def _period_from(request):
    value = request.POST.get("period") or request.GET.get("period") or ""
    return value if periods.parse_period(value) else periods.current_period()


@login_required
def dashboard(request):
    owner = request.user
    period = periods.current_period()
    counts = {row["status"]: row["n"] for row in owner.issued_invoices.values("status").annotate(n=Count("id"))}
    month = owner.issued_invoices.filter(
        issue_date__year=int(period[:4]), issue_date__month=int(period[5:])
    ).exclude(status=S.CANCELLED)
    accepted_month = month.filter(status=S.ACCEPTED).aggregate(total=Sum("total"), tax=Sum("tax_total"))
    report = periods.validate_period(owner, period)
    context = {
        "period": period,
        "accepted": counts.get(S.ACCEPTED, 0),
        "rejected": counts.get(S.REJECTED, 0),
        "pending": counts.get(S.DRAFT, 0) + counts.get(S.READY, 0),
        "in_process": counts.get(S.QUEUED, 0) + counts.get(S.PROCESSING, 0),
        "month_total": accepted_month["total"] or Decimal("0"),
        "month_tax": accepted_month["tax"] or Decimal("0"),
        "month_count": month.count(),
        "report": report,
        "rejected_invoices": owner.issued_invoices.filter(status=S.REJECTED)[:5],
        "recent": owner.issued_invoices.all()[:6],
        "last_closure": PeriodClosure.objects.filter(owner=owner).first(),
    }
    return render(request, "validador/dashboard.html", context)


@login_required
@require_http_methods(["GET", "POST"])
def closures(request):
    owner = request.user
    period = _period_from(request)
    message = None
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "close":
            event, _ = periods.close_period(owner, period)
            message = (
                ("success", f"Periodo {period} cerrado.")
                if event.result == PeriodClosure.Result.CLOSED
                else ("danger", f"No se pudo cerrar {period}. Usa las acciones sugeridas en cada punto marcado o pide ayuda al asistente.")
            )
        elif action == "fix":
            try:
                result = periods.fix_period(owner, period, request.POST.get("fix", ""), request.user)
                text = periods.FIX_ACTIONS[request.POST.get("fix")] + f": {result['changed']} factura(s)."
                if result["skipped"]:
                    text += " Sin cambio: " + "; ".join(result["skipped"][:3])
                message = ("success" if result["changed"] else "warning", text)
            except ValueError as exc:
                message = ("danger", str(exc))
        elif action == "reopen":
            event = periods.reopen_period(owner, period, request.POST.get("note", ""))
            message = ("warning", f"Periodo {period} reabierto.") if event else ("danger", "El periodo no está cerrado.")
    report = periods.validate_period(owner, period)
    seen, history = set(), []
    for event in PeriodClosure.objects.filter(owner=owner):
        history.append(event)
    status_by_period = {}
    for event in history:
        if event.result != PeriodClosure.Result.BLOCKED and event.period not in status_by_period:
            status_by_period[event.period] = event
    context = {
        "period": period,
        "report": report,
        "message": message,
        "fix_actions": periods.FIX_ACTIONS,
        "history": history[:30],
        "closed_periods": [e for e in status_by_period.values() if e.result == PeriodClosure.Result.CLOSED],
    }
    return render(request, "validador/closures.html", context)


OUTCOME_LABELS = {
    S.ACCEPTED: ("accepted", "Aceptada por Hacienda"),
    S.REJECTED: ("rejected", "Rechazada por Hacienda"),
}


def _timeline(owner, kind, outcome, period):
    items = []
    if kind in ("", "facturas"):
        events = IssuedInvoiceStatusEvent.objects.filter(invoice__owner=owner).select_related("invoice")
        if period:
            events = events.filter(
                invoice__issue_date__year=int(period[:4]), invoice__issue_date__month=int(period[5:])
            )
        for event in events[:500]:
            inv = event.invoice
            tone, label = OUTCOME_LABELS.get(event.to_status, ("info", IssuedInvoice.Status(event.to_status).label))
            detail = f"{inv.receiver_name} · ₡{inv.total:,.2f}"
            if inv.clave:
                detail += f" · clave {inv.clave}"
            if tone == "rejected" and inv.hacienda_message:
                detail += f" · {inv.hacienda_message}"
            items.append({"when": event.created_at, "kind": "Factura", "tone": tone, "title": label,
                          "detail": detail, "period": inv.issue_date.strftime("%Y-%m")})
    if kind in ("", "cierres"):
        closures_qs = PeriodClosure.objects.filter(owner=owner)
        if period:
            closures_qs = closures_qs.filter(period=period)
        tones = {"closed": "accepted", "blocked": "rejected", "reopened": "info"}
        for event in closures_qs[:200]:
            detail = f"{event.invoice_count} facturas · {event.accepted_count} aceptadas · {event.rejected_count} rechazadas · ₡{event.total:,.2f}"
            if event.issues:
                detail += " · " + "; ".join(event.issues)
            items.append({"when": event.created_at, "kind": "Cierre", "tone": tones[event.result],
                          "title": f"{event.get_result_display()} {event.period}", "detail": detail, "period": event.period})
    if outcome == "aceptados":
        items = [i for i in items if i["tone"] == "accepted"]
    elif outcome == "rechazados":
        items = [i for i in items if i["tone"] == "rejected"]
    items.sort(key=lambda i: i["when"], reverse=True)
    return items


@login_required
def activity_log(request):
    kind = request.GET.get("tipo", "")
    outcome = request.GET.get("resultado", "")
    period = request.GET.get("period", "")
    period = period if periods.parse_period(period) else ""
    items = _timeline(request.user, kind if kind in ("facturas", "cierres") else "", outcome, period)
    if request.GET.get("formato") == "csv":
        response = HttpResponse(content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = 'attachment; filename="bitacora-guanabill.csv"'
        response.write("\ufeff")
        writer = csv.writer(response)
        writer.writerow(["Fecha", "Tipo", "Resultado", "Periodo", "Detalle"])
        for item in items:
            writer.writerow([timezone.localtime(item["when"]).strftime("%Y-%m-%d %H:%M"), item["kind"],
                             item["title"], item["period"], item["detail"]])
        return response
    context = {"items": items[:200], "kind": kind, "outcome": outcome, "period": period}
    return render(request, "validador/activity_log.html", context)


@login_required
def import_invoices(request):
    return render(request, "validador/import_invoices.html")


@login_required
def import_template(request):
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = 'attachment; filename="plantilla-guanabill.csv"'
    response.write("\ufeff")
    writer = csv.writer(response)
    writer.writerow(TEMPLATE_HEADERS)
    writer.writerows(TEMPLATE_ROWS)
    return response
import json as _json

from django.http import JsonResponse
from django.views.decorators.http import require_POST

from .design_themes import THEMES, recommend_themes
from .models import UserDesignPreference


@login_required
@require_POST
def theme_api(request):
    try:
        data = _json.loads(request.body or "{}")
    except ValueError:
        return JsonResponse({"detail": "JSON inválido."}, status=400)
    preference = UserDesignPreference.objects.filter(user=request.user).first()
    current = preference.theme if preference else "minimal"
    action = data.get("action")
    if action == "save":
        theme = data.get("theme")
        if theme not in THEMES:
            return JsonResponse({"detail": "Tema no disponible."}, status=400)
        UserDesignPreference.objects.update_or_create(user=request.user, defaults={"theme": theme})
        return JsonResponse({"theme": theme})
    if action == "recommend":
        suggestions, ai_used = recommend_themes(data.get("description", ""), current)
        for item in suggestions:
            item["label"] = THEMES[item["theme"]]["label"]
            item["colors"] = THEMES[item["theme"]]["colors"]
        return JsonResponse({"suggestions": suggestions, "ai_used": ai_used})
    return JsonResponse({"detail": "Acción no válida."}, status=400)


HACIENDA_STAGES = [
    ("1", "Borrador", "El proveedor captura la factura (asistente, Excel o API REST).", "draft", "ok"),
    ("2", "Validación local", "Identificaciones, totales, fecha y periodo abierto antes de enviar.", "ready", "ok"),
    ("3", "Clave y consecutivo", "Clave numérica de 50 dígitos y consecutivo de 20 (sucursal, terminal, tipo, secuencia).", "queued", "ok"),
    ("4", "XML v4.4", "Se genera FacturaElectronica con emisor, receptor, líneas e impuestos.", "queued", "ok"),
    ("5", "Firma XAdES-EPES", "Pendiente: requiere el certificado .p12 del contribuyente (solo producción).", "", "pending"),
    ("6", "Recepción (POST)", "El gateway obtiene el token OAuth2 y envía a /recepcion/v1/recepcion (202).", "processing", "ok"),
    ("7", "Consulta de estado", "GET por clave hasta recibir ind-estado: aceptado o rechazado.", "processing", "ok"),
    ("8", "MensajeHacienda", "Se guarda la respuesta, el análisis con IA y el registro en bitácora.", "accepted", "ok"),
]


@login_required
def hacienda_flow(request):
    counts = {
        row["status"]: row["n"]
        for row in IssuedInvoice.objects.filter(owner=request.user).values("status").annotate(n=Count("id"))
    }
    stages = [
        {"n": n, "title": title, "text": text, "count": counts.get(status, 0) if status else None, "state": state}
        for n, title, text, status, state in HACIENDA_STAGES
    ]
    from django.conf import settings

    mode = getattr(settings, "HACIENDA_MODE", "local")
    endpoints = [
        ("Modo", "Ambiente local de pruebas" if mode == "local" else mode),
        ("Gateway Node.js", settings.GATEWAY_URL),
        ("Token (IDP)", "/mock/auth/realms/rut-stag/protocol/openid-connect/token"),
        ("Recepción", "POST /mock/recepcion/v1/recepcion"),
        ("Estado", "GET /mock/recepcion/v1/recepcion/{clave}"),
    ]
    return render(request, "validador/hacienda_flow.html", {"stages": stages, "endpoints": endpoints, "counts": counts})
