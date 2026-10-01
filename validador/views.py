import csv
import io
import secrets
import uuid
import zipfile

from django.conf import settings
from django.contrib.auth import get_user_model, login, logout
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.core.cache import cache
from django.http import HttpResponse, HttpResponseNotFound
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme

from .ai_explanation import explain_validation
from .ai_client import AIProviderError, ai_is_configured, ai_provider_label
from .design_advisor import recommend_design
from .models import UserDesignPreference
from .sharepoint import (
    SharePointError,
    SharePointNotConfigured,
    is_sharepoint_configured,
    upload_to_sharepoint,
)
from .table_analysis import analyze_table, build_dynamic_csv_report
from .validation import (
    MAX_ROWS,
    FileValidationError,
    validate_upload,
)

MAX_UPLOAD_BYTES = 10 * 1024 * 1024
REPORT_TTL_SECONDS = 600


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
            "chart_notice": table["chart_notice"],
            "recommendation_source": table["recommendation_source"],
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
