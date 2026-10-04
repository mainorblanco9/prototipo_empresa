import re
from decimal import Decimal

from django.utils import timezone

from .models import IssuedInvoice, PeriodClosure

PERIOD_RE = re.compile(r"^(\d{4})-(0[1-9]|1[0-2])$")
S = IssuedInvoice.Status


def current_period():
    return timezone.localdate().strftime("%Y-%m")


def parse_period(value):
    match = PERIOD_RE.match(value or "")
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


def period_of(date):
    return date.strftime("%Y-%m")


def latest_event(owner, period):
    return (
        PeriodClosure.objects.filter(owner=owner, period=period)
        .exclude(result=PeriodClosure.Result.BLOCKED)
        .first()
    )


def is_closed(owner, period):
    event = latest_event(owner, period)
    return bool(event and event.result == PeriodClosure.Result.CLOSED)


def validate_period(owner, period):
    year, month = parse_period(period)
    invoices = list(
        IssuedInvoice.objects.filter(
            owner=owner, issue_date__year=year, issue_date__month=month
        ).order_by("consecutive", "created_at")
    )
    active = [i for i in invoices if i.status != S.CANCELLED]
    count = lambda *states: sum(1 for i in active if i.status in states)

    accepted = [i for i in active if i.status == S.ACCEPTED]
    summary = {
        "period": period,
        "invoice_count": len(active),
        "cancelled_count": len(invoices) - len(active),
        "accepted_count": len(accepted),
        "rejected_count": count(S.REJECTED),
        "pending_count": count(S.DRAFT, S.READY),
        "in_process_count": count(S.QUEUED, S.PROCESSING),
        "total": sum((i.total for i in accepted), Decimal("0")),
        "tax_total": sum((i.tax_total for i in accepted), Decimal("0")),
    }

    checks = []

    def add(label, ok, detail, blocking=True):
        checks.append({"label": label, "ok": ok, "detail": detail, "blocking": blocking})

    add("El periodo tiene facturas", bool(active), "Sin facturas emitidas en el mes." if not active else f"{len(active)} facturas.")
    add("Sin borradores ni facturas por enviar", summary["pending_count"] == 0,
        f"{summary['pending_count']} factura(s) aún no se envían a Hacienda." if summary["pending_count"] else "Todo fue enviado.")
    add("Sin facturas en proceso", summary["in_process_count"] == 0,
        f"{summary['in_process_count']} factura(s) esperan respuesta de Hacienda." if summary["in_process_count"] else "Hacienda respondió todo.")
    add("Sin facturas rechazadas", summary["rejected_count"] == 0,
        f"{summary['rejected_count']} rechazada(s): corrígelas y reénvíalas, o anúlalas." if summary["rejected_count"] else "Ninguna rechazada.")
    bad = [i for i in active if i.subtotal + i.tax_total != i.total]
    add("Subtotal + IVA = total en cada factura", not bad, f"{len(bad)} factura(s) no cuadran." if bad else "Los importes cuadran.")
    claves = [i.clave for i in active if i.clave]
    add("Sin claves duplicadas", len(claves) == len(set(claves)), "Hay claves repetidas." if len(claves) != len(set(claves)) else "Claves únicas.")
    numbers = sorted(i.consecutive for i in active if i.consecutive)
    gaps = [n for a, b in zip(numbers, numbers[1:]) for n in range(a + 1, b)]
    add("Consecutivos sin saltos", not gaps, f"Faltan consecutivos: {', '.join(map(str, gaps[:10]))}." if gaps else "Consecutivos continuos.", blocking=False)
    add("El periodo ya terminó", period <= current_period(), "El mes aún no existe." if period > current_period() else
        ("Mes en curso: puedes cerrarlo, pero no aceptará nuevas facturas." if period == current_period() else "Mes completo."),
        blocking=period > current_period())

    already = is_closed(owner, period)
    blockers = [c for c in checks if c["blocking"] and not c["ok"]]
    return {
        "summary": summary,
        "checks": checks,
        "issues": [f"{c['label']}: {c['detail']}" for c in blockers],
        "already_closed": already,
        "can_close": not blockers and not already,
    }


def close_period(owner, period):
    report = validate_period(owner, period)
    s = report["summary"]
    result = PeriodClosure.Result.CLOSED if report["can_close"] else PeriodClosure.Result.BLOCKED
    event = PeriodClosure.objects.create(
        owner=owner, period=period, result=result,
        invoice_count=s["invoice_count"], accepted_count=s["accepted_count"],
        rejected_count=s["rejected_count"], total=s["total"], tax_total=s["tax_total"],
        issues=report["issues"],
    )
    return event, report


def reopen_period(owner, period, note=""):
    if not is_closed(owner, period):
        return None
    s = validate_period(owner, period)["summary"]
    return PeriodClosure.objects.create(
        owner=owner, period=period, result=PeriodClosure.Result.REOPENED,
        invoice_count=s["invoice_count"], accepted_count=s["accepted_count"],
        rejected_count=s["rejected_count"], total=s["total"], tax_total=s["tax_total"],
        note=note[:250],
    )
