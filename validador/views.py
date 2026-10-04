import csv
import json
import secrets
import time
import uuid
import zipfile
from decimal import Decimal

from django.conf import settings
from django.contrib.auth import get_user_model, login, logout
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.core.cache import cache
from django.core.paginator import Paginator
from django.http import HttpResponse, HttpResponseNotFound, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme

from .ai_explanation import explain_validation
from .ai_client import (
    AIProviderError,
    ai_is_configured,
    ai_provider_label,
    generate_local_text,
)
from .design_advisor import recommend_design
from .invoice_documents import (
    InvoiceAIUnavailable,
    InvoiceMappingError,
    InvoiceTableTypeError,
    build_invoice_batch,
)
from .invoice_profiles import INVOICE_DOCUMENT_PROFILES
from .issued_invoice import (
    IssuedInvoiceDraftError,
    build_issued_invoice_draft,
)
from .local_simulation import simulate_local_invoice_submission
from .models import InvoiceAuditEvent, UserDesignPreference
from .sharepoint import (
    SharePointError,
    SharePointNotConfigured,
    is_sharepoint_configured,
    upload_to_sharepoint,
)
from .table_analysis import analyze_table, build_dynamic_csv_report
from .validation import (
    COSTA_RICAN_CURRENCY,
    MAX_ROWS,
    FileValidationError,
    validate_upload,
)

MAX_UPLOAD_BYTES = 10 * 1024 * 1024
REPORT_TTL_SECONDS = 600
INVOICE_BATCH_TTL_SECONDS = 600


def login_view(request):
    if not settings.DEBUG or not settings.DEMO_LOGIN_ENABLED:
        return HttpResponseNotFound("El inicio de sesión de demostración está desactivado.")

    if request.user.is_authenticated:
        return redirect("index")

    context = {"demo_username": settings.DEMO_USERNAME}
    if request.method == "POST":
        username = request.POST.get("username", "")
        password = request.POST.get("password", "")
        valid_username = secrets.compare_digest(username, settings.DEMO_USERNAME)
        valid_password = secrets.compare_digest(password, settings.DEMO_PASSWORD)
        if valid_username and valid_password:
            user_model = get_user_model()
            user, _ = user_model.objects.get_or_create(
                username=settings.DEMO_USERNAME,
                defaults={"is_staff": False, "is_superuser": False},
            )
            if not user.is_active or user.is_staff or user.is_superuser:
                context["error"] = "La cuenta de demostración no está habilitada."
                return render(request, "validador/login.html", context, status=403)
            if not user.has_usable_password():
                user.set_unusable_password()
                user.save(update_fields=["password"])
            login(request, user, backend="django.contrib.auth.backends.ModelBackend")
            next_url = request.POST.get("next") or request.GET.get("next", "")
            try:
                user.design_preference
            except UserDesignPreference.DoesNotExist:
                if next_url and url_has_allowed_host_and_scheme(
                    next_url, allowed_hosts={request.get_host()}
                ):
                    request.session["post_login_next"] = next_url
                return redirect("design_preferences")
            if next_url and url_has_allowed_host_and_scheme(
                next_url, allowed_hosts={request.get_host()}
            ):
                return redirect(next_url)
            return redirect("index")
        context["error"] = "Usuario o contraseña incorrectos."
        return render(request, "validador/login.html", context, status=401)

    return render(request, "validador/login.html", context)


@login_required
def design_preferences(request):
    preference = UserDesignPreference.objects.filter(user=request.user).first()
    themes = UserDesignPreference.Theme.choices
    context = {
        "themes": themes,
        "selected_theme": preference.theme if preference else UserDesignPreference.Theme.MINIMAL,
        "ai_enabled": ai_is_configured(),
        "ai_provider_label": ai_provider_label(),
    }

    if request.method == "POST":
        action = request.POST.get("action", "save")
        description = request.POST.get("design_description", "").strip()

        if action == "recommend":
            if not ai_is_configured():
                context["error"] = (
                    f"La recomendación con IA requiere configurar {ai_provider_label()}. "
                    "Puedes elegir un estilo manualmente."
                )
            elif not description:
                context["error"] = "Describe brevemente el estilo o ambiente que buscas."
            elif len(description) > 500:
                context["error"] = "La descripción no puede superar los 500 caracteres."
            else:
                try:
                    selected_theme = recommend_design(description)
                    preference = _save_design_preference(
                        request.user,
                        preference,
                        selected_theme,
                    )
                    messages.success(
                        request,
                        "Ollama recomendó y aplicó automáticamente el diseño "
                        f"{preference.get_theme_display()}.",
                    )
                    return _redirect_after_design_save(request)
                except (AIProviderError, ValueError) as exc:
                    context["error"] = (
                        f"No se pudo recomendar un estilo: {exc} "
                        "Elige una opción manualmente o inténtalo de nuevo."
                    )
            context["design_description"] = description
            return render(request, "validador/design_preferences.html", context)

        selected_theme = request.POST.get("theme", "")
        allowed_themes = {choice for choice, _label in themes}
        if selected_theme not in allowed_themes:
            context["error"] = "Selecciona uno de los estilos disponibles."
            context["selected_theme"] = selected_theme
            return render(request, "validador/design_preferences.html", context, status=400)

        _save_design_preference(request.user, preference, selected_theme)
        return _redirect_after_design_save(request)

    return render(request, "validador/design_preferences.html", context)


def _save_design_preference(user, preference, theme):
    if preference is None:
        return UserDesignPreference.objects.create(user=user, theme=theme)
    preference.theme = theme
    preference.save(update_fields=["theme", "updated_at"])
    return preference


def _redirect_after_design_save(request):
    next_url = request.session.pop("post_login_next", "")
    if next_url and url_has_allowed_host_and_scheme(
        next_url, allowed_hosts={request.get_host()}
    ):
        return redirect(next_url)
    return redirect("index")


def logout_view(request):
    if request.method != "POST":
        return HttpResponse("Usa el formulario para cerrar sesión.", status=405)
    logout(request)
    return redirect("login")


def _load_invoice_batch(request):
    batch_id = request.session.get("invoice_workspace_batch_id")
    batch = cache.get(f"invoice-workspace-batch:{batch_id}") if batch_id else None
    if batch is None:
        saved_batch = request.session.get("invoice_workspace_batch")
        if (
            not isinstance(saved_batch, dict)
            or saved_batch.get("expires_at", 0) <= time.time()
            or not isinstance(saved_batch.get("batch"), dict)
        ):
            return None

        batch = saved_batch["batch"]
        if batch_id:
            cache.set(
                f"invoice-workspace-batch:{batch_id}",
                batch,
                timeout=max(1, int(saved_batch["expires_at"] - time.time())),
            )

    if not isinstance(batch, dict):
        batch_is_supported = False
    else:
        batch_is_supported = batch.get("currency_scope") == COSTA_RICAN_CURRENCY
        if batch_is_supported and batch.get("status") == "ready":
            invoices = batch.get("invoices")
            batch_is_supported = bool(invoices) and all(
                isinstance(invoice, dict)
                and invoice.get("currency") == COSTA_RICAN_CURRENCY
                and invoice.get("status") == "validated"
                and not invoice.get("errors")
                for invoice in invoices
            )

    if not batch_is_supported:
        if batch_id:
            cache.delete(f"invoice-workspace-batch:{batch_id}")
        request.session.pop("invoice_workspace_batch_id", None)
        request.session.pop("invoice_workspace_batch", None)
        request.session.pop("invoice_workspace_audit_batch_id", None)
        messages.warning(
            request,
            "El lote activo se creó con reglas anteriores a la validación CRC. "
            "Vuelve a cargar el archivo para continuar con las reglas actuales.",
        )
        return None
    return batch


def _invoice_audit_batch_id(request, batch=None):
    batch_id = (batch or {}).get("batch_id") or request.session.get(
        "invoice_workspace_audit_batch_id"
    )
    try:
        batch_id = uuid.UUID(str(batch_id))
    except (TypeError, ValueError, AttributeError):
        batch_id = uuid.uuid4()
    request.session["invoice_workspace_audit_batch_id"] = str(batch_id)
    if batch is not None:
        batch["batch_id"] = str(batch_id)
    return batch_id


def _record_invoice_event(
    request,
    event_type,
    result,
    batch=None,
    batch_id=None,
    once_per_batch=False,
):
    batch_id = batch_id or _invoice_audit_batch_id(request, batch)
    summary = (batch or {}).get("summary", {})
    event_data = {
        "user": request.user,
        "batch_id": batch_id,
        "event_type": event_type,
        "result": result,
        "invoice_count": summary.get("invoice_count", 0),
        "validated_count": summary.get("validated_count", 0),
        "needs_review_count": summary.get("needs_review_count", 0),
    }
    if once_per_batch:
        event, _ = InvoiceAuditEvent.objects.get_or_create(
            user=request.user,
            batch_id=batch_id,
            event_type=event_type,
            defaults=event_data,
        )
        return event
    return InvoiceAuditEvent.objects.create(**event_data)


def _record_failed_invoice_analysis(request):
    _record_invoice_event(
        request,
        InvoiceAuditEvent.EventType.ANALYSIS,
        "failed",
        batch_id=uuid.uuid4(),
    )


@login_required
def parse_invoice_documents(request):
    if request.method != "POST":
        return JsonResponse(
            {"error": "Usa POST multipart/form-data con el campo 'archivo'."},
            status=405,
        )

    upload = request.FILES.get("archivo")
    if not upload:
        return JsonResponse(
            {"error": "Selecciona un archivo con el campo 'archivo'."},
            status=400,
        )
    if upload.size > MAX_UPLOAD_BYTES:
        return JsonResponse(
            {
                "error": (
                    f"El archivo supera el límite de "
                    f"{MAX_UPLOAD_BYTES // (1024 * 1024)} MB."
                )
            },
            status=400,
        )

    try:
        batch = build_invoice_batch(upload)
    except InvoiceAIUnavailable as exc:
        _record_failed_invoice_analysis(request)
        return JsonResponse({"error": str(exc)}, status=503)
    except InvoiceMappingError as exc:
        _record_failed_invoice_analysis(request)
        return JsonResponse({"error": str(exc)}, status=422)
    except FileValidationError as exc:
        _record_failed_invoice_analysis(request)
        return JsonResponse({"error": str(exc)}, status=422)
    except (csv.Error, UnicodeError, OSError, ValueError, zipfile.BadZipFile) as exc:
        _record_failed_invoice_analysis(request)
        return JsonResponse(
            {"error": f"No se pudo leer el archivo: {exc}"},
            status=400,
        )

    status_code = 200 if batch["status"] == "ready" else 422
    _record_invoice_event(
        request,
        InvoiceAuditEvent.EventType.ANALYSIS,
        batch["status"],
        batch,
    )
    return JsonResponse(batch, status=status_code, json_dumps_params={"ensure_ascii": False})


@login_required
def local_invoice_simulation(request):
    if not settings.DEBUG:
        return JsonResponse(
            {
                "error": (
                    "La simulación técnica local solo está disponible con "
                    "DEBUG activo; no es un envío a Hacienda."
                )
            },
            status=404,
        )
    if request.method != "POST":
        return JsonResponse(
            {"error": "Usa POST multipart/form-data con el campo 'archivo'."},
            status=405,
        )

    upload = request.FILES.get("archivo")
    if not upload:
        return JsonResponse(
            {"error": "Selecciona un archivo con el campo 'archivo'."},
            status=400,
        )
    if upload.size > MAX_UPLOAD_BYTES:
        return JsonResponse(
            {
                "error": (
                    f"El archivo supera el límite de "
                    f"{MAX_UPLOAD_BYTES // (1024 * 1024)} MB."
                )
            },
            status=400,
        )

    try:
        batch = build_invoice_batch(upload)
    except InvoiceAIUnavailable as exc:
        _record_failed_invoice_analysis(request)
        return JsonResponse({"error": str(exc)}, status=503)
    except (InvoiceMappingError, FileValidationError) as exc:
        _record_failed_invoice_analysis(request)
        return JsonResponse({"error": str(exc)}, status=422)
    except (csv.Error, UnicodeError, OSError, ValueError, zipfile.BadZipFile) as exc:
        _record_failed_invoice_analysis(request)
        return JsonResponse(
            {"error": f"No se pudo leer el archivo: {exc}"},
            status=400,
        )

    simulation = simulate_local_invoice_submission(batch)
    batch_id = _invoice_audit_batch_id(request, batch)
    _record_invoice_event(
        request,
        InvoiceAuditEvent.EventType.ANALYSIS,
        batch["status"],
        batch,
        batch_id,
    )
    _record_invoice_event(
        request,
        InvoiceAuditEvent.EventType.LOCAL_SIMULATION,
        simulation["status"],
        batch,
        batch_id,
    )
    status_code = 200 if simulation["status"] == "simulation_passed" else 422
    return JsonResponse(
        {
            "invoice_batch": batch,
            "simulation": simulation,
        },
        status=status_code,
        json_dumps_params={"ensure_ascii": False},
    )


@login_required
def invoice_workspace(request):
    context = {
        "max_upload_mb": MAX_UPLOAD_BYTES // (1024 * 1024),
        "batch": None,
        "batch_json": "",
        "invoice_type_mismatch": False,
        "model_ready": False,
    }
    if request.method != "POST":
        batch = _load_invoice_batch(request)
        if batch is not None:
            context["batch"] = batch
            context["batch_json"] = json.dumps(
                batch,
                ensure_ascii=False,
                indent=2,
            )
            context.update(_invoice_month_dashboard(batch))
            context["model_ready"] = batch["status"] == "ready"
        return render(request, "validador/invoice_workspace.html", context)

    upload = request.FILES.get("archivo")
    if not upload:
        context["error"] = "Selecciona un archivo CSV, TSV o XLSX."
        return render(
            request,
            "validador/invoice_workspace.html",
            context,
            status=400,
        )
    previous_batch_id = request.session.pop("invoice_workspace_batch_id", None)
    request.session.pop("invoice_workspace_batch", None)
    request.session.pop("invoice_workspace_audit_batch_id", None)
    if previous_batch_id:
        cache.delete(f"invoice-workspace-batch:{previous_batch_id}")
    if upload.size > MAX_UPLOAD_BYTES:
        context["error"] = (
            f"El archivo supera el límite de "
            f"{MAX_UPLOAD_BYTES // (1024 * 1024)} MB."
        )
        return render(
            request,
            "validador/invoice_workspace.html",
            context,
            status=400,
        )

    try:
        batch = build_invoice_batch(upload)
    except InvoiceAIUnavailable as exc:
        _record_failed_invoice_analysis(request)
        context["error"] = str(exc)
        return render(
            request,
            "validador/invoice_workspace.html",
            context,
            status=503,
        )
    except InvoiceTableTypeError as exc:
        _record_failed_invoice_analysis(request)
        context["error"] = str(exc)
        context["invoice_type_mismatch"] = True
        return render(
            request,
            "validador/invoice_workspace.html",
            context,
            status=422,
        )
    except (InvoiceMappingError, FileValidationError) as exc:
        _record_failed_invoice_analysis(request)
        context["error"] = str(exc)
        return render(
            request,
            "validador/invoice_workspace.html",
            context,
            status=422,
        )
    except (csv.Error, UnicodeError, OSError, ValueError, zipfile.BadZipFile) as exc:
        _record_failed_invoice_analysis(request)
        context["error"] = f"No se pudo leer el archivo: {exc}"
        return render(
            request,
            "validador/invoice_workspace.html",
            context,
            status=400,
        )

    context["batch"] = batch
    batch_id = str(_invoice_audit_batch_id(request, batch))
    context["batch_json"] = json.dumps(batch, ensure_ascii=False, indent=2)
    context.update(_invoice_month_dashboard(batch))
    _record_invoice_event(
        request,
        InvoiceAuditEvent.EventType.ANALYSIS,
        batch["status"],
        batch,
    )
    request.session["invoice_workspace_batch_id"] = batch_id
    request.session["invoice_workspace_batch"] = {
        "batch": batch,
        "expires_at": time.time() + INVOICE_BATCH_TTL_SECONDS,
    }
    context["model_ready"] = batch["status"] == "ready"
    cache.set(
        f"invoice-workspace-batch:{batch_id}",
        batch,
        timeout=INVOICE_BATCH_TTL_SECONDS,
    )
    return render(request, "validador/invoice_workspace.html", context)


def _invoice_month_dashboard(batch):
    month_names = (
        "",
        "enero",
        "febrero",
        "marzo",
        "abril",
        "mayo",
        "junio",
        "julio",
        "agosto",
        "septiembre",
        "octubre",
        "noviembre",
        "diciembre",
    )
    months = sorted(
        {
            invoice["issue_date"][:7]
            for invoice in batch["invoices"]
            if invoice["issue_date"]
        },
        reverse=True,
    )
    month_options = [
        {
            "value": month,
            "label": f"{month_names[int(month[5:7])].title()} {month[:4]}",
        }
        for month in months
    ]
    currency_options = [COSTA_RICAN_CURRENCY] if any(
        invoice["status"] == "validated"
        and invoice["currency"] == COSTA_RICAN_CURRENCY
        for invoice in batch["invoices"]
    ) else []
    chart_invoices = [
        {
            "month": invoice["issue_date"][:7] if invoice["issue_date"] else "",
            "invoice_number": invoice["invoice_number"],
            "supplier": invoice["supplier"],
            "currency": invoice["currency"],
            "total": invoice["total"],
            "status": invoice["status"],
        }
        for invoice in batch["invoices"]
    ]
    return {
        "invoice_month_options": month_options,
        "selected_invoice_month": months[0] if months else "",
        "invoice_currency_options": currency_options,
        "selected_invoice_currency": currency_options[0]
        if currency_options
        else "",
        "invoice_chart_data": chart_invoices,
    }


@login_required
def download_sample_invoices(request):
    sample_path = settings.BASE_DIR / "ejemplo_facturas_octubre.csv"
    try:
        content = sample_path.read_bytes()
    except OSError as exc:
        return HttpResponse(
            "No se pudo leer el archivo de ejemplo. Comprueba que exista en el proyecto.",
            status=500,
            content_type="text/plain; charset=utf-8",
        )
    response = HttpResponse(content, content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = (
        'attachment; filename="ejemplo_facturas_octubre.csv"'
    )
    return response


@login_required
def issued_invoice_draft(request):
    context = {
        "draft": None,
        "draft_json": "",
        "error": "",
        "form_values": {},
        "line_values": [],
        "today": timezone.localdate().isoformat(),
    }
    draft_id = request.session.get("issued_invoice_draft_id")
    if request.method == "POST":
        if request.POST.get("action") != "prepare":
            return HttpResponse("Acción no válida.", status=400)
        line_lists = [
            request.POST.getlist("line_description"),
            request.POST.getlist("line_quantity"),
            request.POST.getlist("line_unit_price"),
            request.POST.getlist("line_tax_rate"),
        ]
        if len({len(values) for values in line_lists}) != 1:
            context["error"] = (
                "No se recibieron los campos completos de cada línea de detalle."
            )
        else:
            context["line_values"] = [
                dict(
                    zip(
                        ("description", "quantity", "unit_price", "tax_rate"),
                        values,
                        strict=True,
                    )
                )
                for values in zip(*line_lists, strict=True)
            ]
            context["form_values"] = request.POST
            try:
                draft = build_issued_invoice_draft(
                    request.POST,
                    context["line_values"],
                )
            except IssuedInvoiceDraftError as exc:
                context["error"] = str(exc)
            else:
                draft_id = draft["draft_id"]
                request.session["issued_invoice_draft_id"] = draft_id
                cache.set(
                    f"issued-invoice-draft:{request.user.pk}:{draft_id}",
                    draft,
                    timeout=INVOICE_BATCH_TTL_SECONDS,
                )
                InvoiceAuditEvent.objects.create(
                    user=request.user,
                    batch_id=uuid.UUID(draft_id),
                    event_type=InvoiceAuditEvent.EventType.ISSUED_DRAFT,
                    result="draft_prepared",
                    invoice_count=1,
                )
                context["draft"] = draft
                context["draft_json"] = json.dumps(
                    draft,
                    ensure_ascii=False,
                    indent=2,
                )
                context["line_values"] = []
    elif request.method == "GET":
        if draft_id:
            draft = cache.get(
                f"issued-invoice-draft:{request.user.pk}:{draft_id}"
            )
            if draft:
                context["draft"] = draft
                context["draft_json"] = json.dumps(
                    draft,
                    ensure_ascii=False,
                    indent=2,
                )
    else:
        return HttpResponse("Usa GET o POST.", status=405)

    return render(
        request,
        "validador/issued_invoice_draft.html",
        context,
    )


@login_required
def download_issued_invoice_draft(request):
    draft_id = request.session.get("issued_invoice_draft_id")
    draft = (
        cache.get(f"issued-invoice-draft:{request.user.pk}:{draft_id}")
        if draft_id
        else None
    )
    if draft is None:
        return HttpResponseNotFound(
            "El borrador expiró o no existe. Vuelve a prepararlo localmente."
        )
    InvoiceAuditEvent.objects.create(
        user=request.user,
        batch_id=uuid.UUID(draft_id),
        event_type=InvoiceAuditEvent.EventType.ISSUED_DRAFT_DOWNLOADED,
        result="draft_downloaded",
        invoice_count=1,
    )
    response = HttpResponse(
        json.dumps(draft, ensure_ascii=False, indent=2),
        content_type="application/json; charset=utf-8",
    )
    response["Content-Disposition"] = (
        'attachment; filename="borrador-factura-electronica-interno.json"'
    )
    return response


@login_required
def invoice_model_workspace(request):
    batch = _load_invoice_batch(request)
    if batch is None:
        messages.info(
            request,
            "Analiza una factura antes de preparar el modelo y consultar su trazabilidad.",
        )
        return redirect("invoice_workspace")
    if batch["status"] != "ready":
        messages.warning(
            request,
            "El lote contiene facturas que requieren revisión. Corrígelas antes de preparar el modelo.",
        )
        return redirect("invoice_workspace")

    audit_batch_id = _invoice_audit_batch_id(request, batch)
    _record_invoice_event(
        request,
        InvoiceAuditEvent.EventType.MODEL_PREPARED,
        "ready",
        batch,
        audit_batch_id,
        once_per_batch=True,
    )

    invoice_counts = {
        "validated": sum(
            invoice["status"] == "validated" for invoice in batch["invoices"]
        ),
        "needs_review": sum(
            invoice["status"] == "needs_review" for invoice in batch["invoices"]
        ),
    }
    status_maximum = max(invoice_counts.values(), default=0)
    status_chart = [
        {
            "label": "Válidas",
            "value": invoice_counts["validated"],
            "width": (
                f'{invoice_counts["validated"] / status_maximum * 100:.1f}'
                if status_maximum
                else "0"
            ),
            "color": "success",
        },
        {
            "label": "Requieren revisión",
            "value": invoice_counts["needs_review"],
            "width": (
                f'{invoice_counts["needs_review"] / status_maximum * 100:.1f}'
                if status_maximum
                else "0"
            ),
            "color": "warning",
        },
    ]

    validated_total_crc = Decimal("0")
    for invoice in batch["invoices"]:
        if (
            invoice["status"] != "validated"
            or invoice["currency"] != COSTA_RICAN_CURRENCY
        ):
            continue
        validated_total_crc += Decimal(invoice["total"])
    formatted_total_crc = f"₡{validated_total_crc:,.2f}"
    formatted_total_crc = formatted_total_crc.replace(",", " ").replace(".", ",")
    currency_chart = (
        [
            {
                "currency": COSTA_RICAN_CURRENCY,
                "amount": formatted_total_crc,
                "width": "100.0",
            }
        ]
        if validated_total_crc
        else []
    )

    latest_simulation = InvoiceAuditEvent.objects.filter(
        user=request.user,
        batch_id=audit_batch_id,
        event_type=InvoiceAuditEvent.EventType.LOCAL_SIMULATION,
    ).first()
    context = {
        "batch": batch,
        "model_ready": True,
        "batch_json": json.dumps(batch, ensure_ascii=False, indent=2),
        "status_chart": status_chart,
        "currency_chart": currency_chart,
        "ai_insight": "",
        "ai_error": "",
        "mock_enabled": settings.DEBUG,
        "mock_result": None,
        "mock_json": "",
        "document_profiles": INVOICE_DOCUMENT_PROFILES,
        "latest_simulation": latest_simulation,
        "trace_steps": [
            {
                "title": "Archivo recibido",
                "detail": batch["source_file"],
                "state": "complete",
            },
            {
                "title": "Tabla y columnas identificadas",
                "detail": (
                    f'{batch["source_sheet"]}, fila de encabezados '
                    f'{batch["header_row"]}; mapeo: {batch["mapping_source"]}.'
                ),
                "state": "complete",
            },
            {
                "title": "Validación de facturas",
                "detail": (
                    f'{invoice_counts["validated"]} válidas; '
                    f'{invoice_counts["needs_review"]} requieren revisión.'
                ),
                "state": (
                    "complete"
                    if batch["status"] == "ready"
                    else "warning"
                ),
            },
            {
                "title": "Modelo interno preparado",
                "detail": (
                    f'JSON schema {batch["schema_version"]}; estado: '
                    f'{batch["status"]}.'
                ),
                "state": "complete" if batch["status"] == "ready" else "warning",
            },
            {
                "title": "Simulación local del flujo",
                "detail": (
                    (
                        f"Último resultado: {latest_simulation.result_label}. "
                        "Solo controles internos; no se transmitió nada."
                    )
                    if latest_simulation
                    else (
                        "Todavía no ejecutada. La simulación no contacta a "
                        "TRIBU-CR ni transmite documentos."
                    )
                ),
                "state": (
                    "complete"
                    if latest_simulation
                    and latest_simulation.result == "simulation_passed"
                    else "warning"
                    if latest_simulation
                    else "pending"
                ),
            },
            {
                "title": "Envío real a Hacienda",
                "detail": (
                    "No disponible: falta implementar y validar el contrato oficial, "
                    "los esquemas, la autenticación y los perfiles fiscales."
                ),
                "state": "pending",
            },
        ],
    }
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "analyze":
            aggregate_summary = {
                "invoice_count": batch["summary"]["invoice_count"],
                "validated_count": invoice_counts["validated"],
                "needs_review_count": invoice_counts["needs_review"],
                "validated_total_crc": str(validated_total_crc),
            }
            try:
                context["ai_insight"] = generate_local_text(
                    system_prompt=(
                        "Eres un analista de trazabilidad de facturas. Resume "
                        "brevemente los agregados recibidos, señala cualquier "
                        "revisión pendiente. Los importes están expresados en "
                        "colones costarricenses (CRC). No afirmes que hubo "
                        "envío, aceptación fiscal ni cumplimiento tributario. "
                        "Recibes solo conteos y un importe agregado; no solicites "
                        "datos personales."
                    ),
                    user_prompt=json.dumps(aggregate_summary, ensure_ascii=False),
                    max_tokens=180,
                )
                _record_invoice_event(
                    request,
                    InvoiceAuditEvent.EventType.OLLAMA_SUMMARY,
                    "generated",
                    batch,
                    audit_batch_id,
                )
            except AIProviderError as exc:
                context["ai_error"] = str(exc)
                _record_invoice_event(
                    request,
                    InvoiceAuditEvent.EventType.OLLAMA_SUMMARY,
                    "unavailable",
                    batch,
                    audit_batch_id,
                )
        elif action == "simulate":
            if not settings.DEBUG:
                return HttpResponseNotFound(
                    "La simulación local solo está disponible con DEBUG activo."
                )
            if batch["status"] != "ready":
                context["mock_error"] = (
                    "El lote requiere correcciones antes de la simulación local."
                )
            else:
                context["mock_result"] = simulate_local_invoice_submission(batch)
                context["trace_steps"][4].update(
                    {
                        "detail": (
                            f"Resultado: {context['mock_result']['message']}"
                        ),
                        "state": (
                            "complete"
                            if context["mock_result"]["status"] == "simulation_passed"
                            else "warning"
                        ),
                    }
                )
                _record_invoice_event(
                    request,
                    InvoiceAuditEvent.EventType.LOCAL_SIMULATION,
                    context["mock_result"]["status"],
                    batch,
                    audit_batch_id,
                )
                context["mock_json"] = json.dumps(
                    context["mock_result"],
                    ensure_ascii=False,
                    indent=2,
                )
        else:
            return HttpResponse("Acción no válida.", status=400)
    elif request.method != "GET":
        return HttpResponse("Usa GET o POST.", status=405)

    context["audit_events"] = InvoiceAuditEvent.objects.filter(
        user=request.user,
        batch_id=audit_batch_id,
    ).order_by("created_at")
    return render(request, "validador/invoice_model_workspace.html", context)


@login_required
def download_invoice_model(request):
    batch = _load_invoice_batch(request)
    if batch is None:
        return HttpResponseNotFound(
            "El lote expiró o no existe. Vuelve a analizar el archivo."
        )
    if batch["status"] != "ready":
        return HttpResponse(
            "El lote requiere revisión; corrige las facturas antes de descargar el modelo.",
            status=409,
            content_type="text/plain; charset=utf-8",
        )
    _record_invoice_event(
        request,
        InvoiceAuditEvent.EventType.MODEL_DOWNLOADED,
        "downloaded",
        batch,
    )
    response = HttpResponse(
        json.dumps(batch, ensure_ascii=False, indent=2),
        content_type="application/json; charset=utf-8",
    )
    response["Content-Disposition"] = 'attachment; filename="modelo-facturas.json"'
    return response


@login_required
def invoice_audit(request):
    if request.method == "POST":
        if request.POST.get("action") != "clear":
            return HttpResponse("Acción no válida.", status=400)
        deleted_count, _ = InvoiceAuditEvent.objects.filter(
            user=request.user
        ).delete()
        messages.success(
            request,
            f"Bitácora borrada. Se eliminaron {deleted_count} registros; "
            "el lote de trabajo activo no se modificó.",
        )
        return redirect("invoice_audit")
    if request.method != "GET":
        return HttpResponse("Usa GET o POST.", status=405)

    events = InvoiceAuditEvent.objects.filter(user=request.user)
    page = Paginator(events, 25).get_page(request.GET.get("page"))
    return render(
        request,
        "validador/invoice_audit.html",
        {"audit_page": page, "has_audit_events": events.exists()},
    )


@login_required
def index(request):
    preference, _ = UserDesignPreference.objects.get_or_create(user=request.user)
    context = {
        "ai_enabled": ai_is_configured(),
        "ai_provider_label": ai_provider_label(),
        "max_upload_mb": MAX_UPLOAD_BYTES // (1024 * 1024),
        "max_rows": MAX_ROWS,
        "rows": None,
        "sharepoint_enabled": is_sharepoint_configured(),
        "design_theme": preference.theme,
    }
    if request.method != "POST":
        return render(request, "validador/index.html", context)

    upload = request.FILES.get("archivo")
    if not upload:
        context["error"] = "Selecciona un archivo CSV o XLSX."
        return render(request, "validador/index.html", context, status=400)
    if upload.size > MAX_UPLOAD_BYTES:
        context["error"] = f"El archivo supera el límite de {MAX_UPLOAD_BYTES // (1024 * 1024)} MB."
        return render(request, "validador/index.html", context, status=400)

    try:
        table = analyze_table(upload)
        rows = table["rows"]
        issue_counts = {}
        valid_rows = None
        invalid_rows = None
        report_headers = table["headers"]
        if table["is_expense_table"]:
            upload.seek(0)
            expense_rows, issue_counts = validate_upload(upload)
            valid_rows = sum(row["estado"] == "Correcto" for row in expense_rows)
            invalid_rows = len(expense_rows) - valid_rows
            report_headers = [
                *table["headers"],
                "Fila",
                "Estado de validacion",
                "Errores de validacion",
            ]
            for index, expense_row in enumerate(expense_rows):
                rows[index].update(
                    {
                        "Fila": expense_row["fila"],
                        "Estado de validacion": expense_row["estado"],
                        "Errores de validacion": expense_row["errores"],
                    }
                )
    except FileValidationError as exc:
        context["error"] = str(exc)
        return render(request, "validador/index.html", context, status=400)
    except (csv.Error, UnicodeError, OSError, ValueError, zipfile.BadZipFile) as exc:
        context["error"] = f"No se pudo leer el archivo: {exc}"
        return render(request, "validador/index.html", context, status=400)

    report_id = uuid.uuid4()
    cache.set(
        f"report:{report_id}",
        build_dynamic_csv_report(report_headers, rows),
        timeout=REPORT_TTL_SECONDS,
    )

    context.update(
        {
            "rows": rows[:200],
            "table_preview_rows": [
                [row.get(header, "") for header in report_headers]
                for row in rows[:200]
            ],
            "total_rows": len(rows),
            "table_headers": report_headers,
            "chart": table["chart"],
            "matrix_chart": table["matrix_chart"],
            "source_sheet": table["source_sheet"],
            "chart_notice": table["chart_notice"],
            "recommendation_source": table["recommendation_source"],
            "columns": [
                {
                    **column,
                    "display_name": table["headers"][column["index"]],
                    "type_label": {
                        "category": "Categoría",
                        "date": "Fecha",
                        "number": "Número",
                        "text": "Texto",
                    }[column["type"]],
                }
                for column in table["columns"]
            ],
            "expense_validation": table["is_expense_table"],
            "valid_rows": valid_rows,
            "invalid_rows": invalid_rows,
            "issue_counts": issue_counts,
            "report_url": reverse("download_report", args=[report_id]),
        }
    )

    if table["is_expense_table"] and rows and valid_rows == len(rows):
        try:
            context["sharepoint_upload"] = upload_to_sharepoint(
                upload, upload.name
            )
        except SharePointNotConfigured as exc:
            context["sharepoint_error"] = str(exc)
        except SharePointError as exc:
            context["sharepoint_error"] = (
                f"El archivo pasó la validación, pero no se pudo subir a SharePoint: {exc}"
            )
    elif table["is_expense_table"] and rows:
        context["sharepoint_blocked"] = True
    elif table["is_expense_table"]:
        context["sharepoint_error"] = (
            "El archivo no contiene filas de datos y no se subió a SharePoint."
        )
    else:
        context["sharepoint_not_applicable"] = True

    if request.POST.get("explicar_con_ia") == "on" and table["is_expense_table"]:
        if not ai_is_configured():
            context["ai_error"] = (
                f"Configura {ai_provider_label()} para activar la explicación con IA."
            )
        else:
            try:
                context["ai_explanation"] = explain_validation(
                    issue_counts, len(rows), valid_rows
                )
            except AIProviderError as exc:
                context["ai_error"] = (
                    f"No se pudo obtener la explicación de {ai_provider_label()}: {exc}"
                )
            except ValueError as exc:
                context["ai_error"] = f"No se pudo generar la explicación: {exc}"

    return render(request, "validador/index.html", context)


@login_required
def download_report(request, report_id):
    report = cache.get(f"report:{report_id}")
    if report is None:
        return HttpResponseNotFound(
            "El reporte expiró. Vuelve a cargar el archivo para generarlo otra vez."
        )
    response = HttpResponse(report, content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = 'attachment; filename="reporte_validacion.csv"'
    return response


@login_required
def mobile_capture(request):
    return render(request, "validador/mobile_capture.html")


@login_required
def issued_invoice_list(request):
    return render(request, "validador/issued_invoice_list.html")
