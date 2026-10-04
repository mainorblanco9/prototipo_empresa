import json
import secrets
import urllib.error
import urllib.request
from decimal import Decimal

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.db.models import Count, F, Max, Sum
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from rest_framework import serializers, status as http_status
from rest_framework.authtoken.models import Token
from rest_framework.authtoken.views import ObtainAuthToken
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.viewsets import GenericViewSet

from .issued_invoice import (
    IssuedInvoiceDraftError,
    MAX_INVOICE_LINES,
    TAX_RATES,
    build_issued_invoice_draft,
)
from . import ai_assist, hacienda, periods
from .models import IssuedInvoice, IssuedInvoiceStatusEvent


class TokenView(ObtainAuthToken):
    """POST obtiene (o reutiliza) el token; DELETE lo revoca."""

    throttle_scope = "auth"

    @staticmethod
    def _demo_user(data):
        # Mismo acceso mock que la web: solo con DEBUG y sin privilegios.
        if not settings.DEMO_LOGIN_ENABLED:
            return None
        username = str(data.get("username", ""))
        password = str(data.get("password", ""))
        if not (
            secrets.compare_digest(username, settings.DEMO_USERNAME)
            and secrets.compare_digest(password, settings.DEMO_PASSWORD)
        ):
            return None
        user, _ = get_user_model().objects.get_or_create(
            username=settings.DEMO_USERNAME,
            defaults={"is_staff": False, "is_superuser": False},
        )
        if not user.is_active or user.is_staff or user.is_superuser:
            return None
        if not user.password or not user.has_usable_password():
            user.set_unusable_password()
            user.save(update_fields=["password"])
        return user

    def post(self, request, *args, **kwargs):
        user = self._demo_user(request.data)
        if user is None:
            serializer = self.serializer_class(
                data=request.data,
                context={"request": request},
            )
            serializer.is_valid(raise_exception=True)
            user = serializer.validated_data["user"]
        token, _ = Token.objects.get_or_create(user=user)
        return Response({"token": token.key})

    def delete(self, request, *args, **kwargs):
        if request.auth is None or not hasattr(request.auth, "delete"):
            return Response(status=http_status.HTTP_401_UNAUTHORIZED)
        request.auth.delete()
        return Response(status=http_status.HTTP_204_NO_CONTENT)


class LineSerializer(serializers.Serializer):
    description = serializers.CharField(max_length=250)
    quantity = serializers.CharField(max_length=32)
    unit_price = serializers.CharField(max_length=32)
    tax_rate = serializers.ChoiceField(choices=sorted(TAX_RATES))


class InvoiceWriteSerializer(serializers.Serializer):
    client_id = serializers.UUIDField(required=False)
    issuer_name = serializers.CharField(max_length=250)
    issuer_id = serializers.CharField(max_length=100)
    receiver_name = serializers.CharField(max_length=250)
    receiver_id = serializers.CharField(max_length=100)
    issue_date = serializers.DateField()
    lines = LineSerializer(many=True, allow_empty=False, max_length=MAX_INVOICE_LINES)
    version = serializers.IntegerField(required=False, min_value=1)


MANUAL_STATUSES = [
    IssuedInvoice.Status.DRAFT,
    IssuedInvoice.Status.READY,
    IssuedInvoice.Status.CANCELLED,
]


class TransitionSerializer(serializers.Serializer):
    to = serializers.ChoiceField(choices=MANUAL_STATUSES)
    version = serializers.IntegerField(min_value=1)


class QueueSerializer(serializers.Serializer):
    version = serializers.IntegerField(min_value=1)


HACIENDA_STATE_MAP = {
    "procesando": IssuedInvoice.Status.PROCESSING,
    "aceptado": IssuedInvoice.Status.ACCEPTED,
    "rechazado": IssuedInvoice.Status.REJECTED,
}


class HaciendaResultSerializer(serializers.Serializer):
    state = serializers.ChoiceField(choices=sorted(HACIENDA_STATE_MAP))
    message = serializers.CharField(required=False, allow_blank=True, max_length=4000)


class InvoiceListSerializer(serializers.ModelSerializer):
    status_label = serializers.CharField(source="get_status_display", read_only=True)

    class Meta:
        model = IssuedInvoice
        fields = (
            "id",
            "client_id",
            "document_type",
            "status",
            "status_label",
            "currency",
            "issue_date",
            "receiver_name",
            "subtotal",
            "tax_total",
            "total",
            "clave",
            "hacienda_message",
            "ai_analysis",
            "version",
            "created_at",
            "updated_at",
        )


class InvoiceDetailSerializer(InvoiceListSerializer):
    class Meta(InvoiceListSerializer.Meta):
        fields = InvoiceListSerializer.Meta.fields + ("document",)


class StatusEventSerializer(serializers.ModelSerializer):
    class Meta:
        model = IssuedInvoiceStatusEvent
        fields = ("from_status", "to_status", "created_at")


def _money(value):
    return str((value or Decimal("0")).quantize(Decimal("0.01")))


def _bad_request(message):
    return Response({"detail": message}, status=http_status.HTTP_400_BAD_REQUEST)


def _conflict(message):
    return Response({"detail": message}, status=http_status.HTTP_409_CONFLICT)


def _build_document(data):
    lines = [dict(line) for line in data["lines"]]
    form = {key: str(data[key]) for key in (
        "issuer_name", "issuer_id", "receiver_name", "receiver_id",
    )}
    form["issue_date"] = data["issue_date"].isoformat()
    document = build_issued_invoice_draft(form, lines)
    document.pop("draft_id", None)
    return document


def _document_fields(document):
    return {
        "issue_date": document["issue_date"],
        "receiver_name": document["receiver"]["name"],
        "subtotal": Decimal(document["totals"]["subtotal"]),
        "tax_total": Decimal(document["totals"]["tax"]),
        "total": Decimal(document["totals"]["total"]),
        "document": document,
    }


class IssuedInvoiceViewSet(GenericViewSet):
    """API de borradores de factura emitida (solo local, no fiscal)."""

    lookup_field = "pk"
    lookup_value_regex = "[0-9a-fA-F-]{36}"
    serializer_class = InvoiceDetailSerializer

    def get_queryset(self):
        return IssuedInvoice.objects.filter(owner=self.request.user)

    def _filtered_list(self):
        queryset = self.get_queryset()
        params = self.request.query_params
        if params.get("status") in IssuedInvoice.Status.values:
            queryset = queryset.filter(status=params["status"])
        month = params.get("month")
        if month:
            try:
                year, month_number = (int(part) for part in month.split("-"))
                queryset = queryset.filter(
                    issue_date__year=year,
                    issue_date__month=month_number,
                )
            except ValueError:
                raise serializers.ValidationError({"month": "Usa el formato AAAA-MM."})
        since = params.get("updated_since")
        if since:
            parsed = parse_datetime(since)
            if parsed is None:
                raise serializers.ValidationError(
                    {"updated_since": "Usa una fecha y hora ISO 8601."}
                )
            if timezone.is_naive(parsed):
                parsed = timezone.make_aware(parsed)
            queryset = queryset.filter(updated_at__gt=parsed)
        return queryset.order_by("updated_at", "id")

    def list(self, request):
        page = self.paginate_queryset(self._filtered_list())
        return self.get_paginated_response(
            InvoiceListSerializer(page, many=True).data
        )

    def retrieve(self, request, pk=None):
        invoice = self.get_object()
        return Response(InvoiceDetailSerializer(invoice).data)

    def create(self, request):
        serializer = InvoiceWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        if "client_id" not in data:
            return _bad_request("Falta client_id (UUID generado por el cliente).")
        existing = self.get_queryset().filter(client_id=data["client_id"]).first()
        if existing:
            return Response(InvoiceDetailSerializer(existing).data)
        if periods.is_closed(request.user, periods.period_of(data["issue_date"])):
            return _conflict("El periodo de esa fecha está cerrado. Reabre el periodo en Cierres para modificarlo.")
        try:
            document = _build_document(data)
        except IssuedInvoiceDraftError as exc:
            return _bad_request(str(exc))
        try:
            with transaction.atomic():
                invoice = IssuedInvoice.objects.create(
                    owner=request.user,
                    client_id=data["client_id"],
                    **_document_fields(document),
                )
                IssuedInvoiceStatusEvent.objects.create(
                    invoice=invoice,
                    from_status="",
                    to_status=invoice.status,
                    actor=request.user,
                )
        except IntegrityError:
            invoice = self.get_queryset().get(client_id=data["client_id"])
            return Response(InvoiceDetailSerializer(invoice).data)
        return Response(
            InvoiceDetailSerializer(invoice).data,
            status=http_status.HTTP_201_CREATED,
        )

    def update(self, request, pk=None):
        invoice = self.get_object()
        serializer = InvoiceWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        if "version" not in data:
            return _bad_request("Falta version para detectar cambios simultáneos.")
        if invoice.status != IssuedInvoice.Status.DRAFT:
            return _conflict("Solo se pueden editar facturas en borrador.")
        if periods.is_closed(request.user, periods.period_of(data["issue_date"])):
            return _conflict("El periodo de esa fecha está cerrado. Reabre el periodo en Cierres para modificarlo.")
        try:
            document = _build_document(data)
        except IssuedInvoiceDraftError as exc:
            return _bad_request(str(exc))
        updated = IssuedInvoice.objects.filter(
            pk=invoice.pk,
            owner=request.user,
            version=data["version"],
            status=IssuedInvoice.Status.DRAFT,
        ).update(
            version=data["version"] + 1,
            updated_at=timezone.now(),
            **_document_fields(document),
        )
        if not updated:
            return _conflict(
                "La factura cambió en otro dispositivo. Descarga la versión actual."
            )
        invoice.refresh_from_db()
        return Response(InvoiceDetailSerializer(invoice).data)

    @staticmethod
    def _apply_transition(request, invoice, to_status, version):
        if not invoice.can_transition_to(to_status):
            return _conflict(
                f"No se permite pasar de {invoice.get_status_display()} a "
                f"{IssuedInvoice.Status(to_status).label}."
            )
        with transaction.atomic():
            updated = IssuedInvoice.objects.filter(
                pk=invoice.pk,
                owner=request.user,
                version=version,
                status=invoice.status,
            ).update(
                status=to_status,
                version=version + 1,
                updated_at=timezone.now(),
            )
            if not updated:
                return _conflict(
                    "La factura cambió en otro dispositivo. Descarga la versión actual."
                )
            IssuedInvoiceStatusEvent.objects.create(
                invoice=invoice,
                from_status=invoice.status,
                to_status=to_status,
                actor=request.user,
            )
        invoice.refresh_from_db()
        return Response(InvoiceDetailSerializer(invoice).data)

    def transition(self, request, pk=None):
        invoice = self.get_object()
        serializer = TransitionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return self._apply_transition(
            request,
            invoice,
            serializer.validated_data["to"],
            serializer.validated_data["version"],
        )

    def _record(self, request, invoice, from_status, to_status):
        IssuedInvoiceStatusEvent.objects.create(
            invoice=invoice,
            from_status=from_status,
            to_status=to_status,
            actor=request.user,
        )

    @staticmethod
    def _issues_response(issues):
        detail = "No se envió: " + " ".join(item["message"] for item in issues)
        return Response(
            {"detail": detail, "issues": issues},
            status=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
        )

    def validate(self, request, pk=None):
        """Valida localmente la factura sin enviarla a Hacienda."""
        invoice = self.get_object()
        issues = hacienda.validate_invoice(invoice)
        return Response({"valid": not issues, "issues": issues})

    def queue(self, request, pk=None):
        """Asigna consecutivo y clave y deja la factura lista para el gateway."""
        invoice = self.get_object()
        serializer = QueueSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        version = serializer.validated_data["version"]
        if invoice.status != IssuedInvoice.Status.READY:
            return _conflict("Solo se puede enviar una factura en estado Listo para envío.")
        issues = hacienda.validate_invoice(invoice)
        if issues:
            return self._issues_response(issues)
        with transaction.atomic():
            last = (
                IssuedInvoice.objects.filter(owner=request.user)
                .aggregate(last=Max("consecutive"))["last"]
                or 0
            )
            try:
                consecutive = hacienda.build_consecutive(last + 1)
                clave = hacienda.build_clave(
                    invoice.issue_date,
                    invoice.document["issuer"]["identification"],
                    consecutive,
                )
            except hacienda.HaciendaDocumentError as exc:
                return _bad_request(str(exc))
            updated = IssuedInvoice.objects.filter(
                pk=invoice.pk,
                owner=request.user,
                version=version,
                status=IssuedInvoice.Status.READY,
            ).update(
                status=IssuedInvoice.Status.QUEUED,
                consecutive=last + 1,
                clave=clave,
                version=version + 1,
                updated_at=timezone.now(),
            )
            if not updated:
                return _conflict(
                    "La factura cambió en otro dispositivo. Descarga la versión actual."
                )
            self._record(request, invoice, invoice.status, IssuedInvoice.Status.QUEUED)
        invoice.refresh_from_db()
        return Response(InvoiceDetailSerializer(invoice).data)

    def reception_payload(self, request, pk=None):
        invoice = self.get_object()
        if not invoice.clave:
            return _conflict("La factura aún no tiene clave; ponla en cola primero.")
        try:
            return Response(hacienda.build_reception_payload(invoice))
        except hacienda.HaciendaDocumentError as exc:
            return _bad_request(str(exc))

    def hacienda_result(self, request, pk=None):
        """El gateway informa el ind-estado devuelto por Hacienda."""
        invoice = self.get_object()
        serializer = HaciendaResultSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        state = serializer.validated_data["state"]
        message = serializer.validated_data.get("message", "")
        target = HACIENDA_STATE_MAP[state]
        if invoice.status == target:
            return Response(InvoiceDetailSerializer(invoice).data)
        if not invoice.can_transition_to(target):
            return _conflict(
                f"No se permite pasar de {invoice.get_status_display()} a "
                f"{IssuedInvoice.Status(target).label}."
            )
        with transaction.atomic():
            updated = IssuedInvoice.objects.filter(
                pk=invoice.pk,
                owner=request.user,
                status=invoice.status,
            ).update(
                status=target,
                hacienda_message=message[:2000],
                version=F("version") + 1,
                updated_at=timezone.now(),
            )
            if not updated:
                return _conflict("La factura cambió mientras se procesaba el envío.")
            self._record(request, invoice, invoice.status, target)
        invoice.refresh_from_db()
        return Response(InvoiceDetailSerializer(invoice).data)

    def submit(self, request, pk=None):
        """Pide al gateway Node.js enviar la factura (flujo de escritorio)."""
        invoice = self.get_object()
        if invoice.status not in (
            IssuedInvoice.Status.READY,
            IssuedInvoice.Status.QUEUED,
            IssuedInvoice.Status.PROCESSING,
        ):
            return _conflict("Solo se envían facturas Listas para envío.")
        issues = hacienda.validate_invoice(invoice)
        if issues:
            return self._issues_response(issues)
        token, _ = Token.objects.get_or_create(user=request.user)
        gateway_request = urllib.request.Request(
            f"{settings.GATEWAY_URL}/invoices/{invoice.pk}/submit",
            method="POST",
            headers={"Authorization": f"Token {token.key}"},
        )
        try:
            with urllib.request.urlopen(gateway_request, timeout=30) as response:
                body = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            return Response(
                {"detail": f"El gateway respondió {exc.code}: {detail}"},
                status=http_status.HTTP_502_BAD_GATEWAY,
            )
        except (urllib.error.URLError, TimeoutError, ValueError):
            return Response(
                {"detail": "No se pudo contactar al gateway Node.js. ¿Está en ejecución?"},
                status=http_status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        invoice.refresh_from_db()
        return Response(
            {"gateway": body, "invoice": InvoiceDetailSerializer(invoice).data}
        )

    def ai_analysis(self, request, pk=None):
        """Análisis por IA (Ollama local) de la respuesta de Hacienda."""
        invoice = self.get_object()
        if invoice.status not in (
            IssuedInvoice.Status.ACCEPTED,
            IssuedInvoice.Status.REJECTED,
            IssuedInvoice.Status.PROCESSING,
            IssuedInvoice.Status.QUEUED,
        ):
            return _conflict("Aún no hay respuesta de Hacienda que analizar.")
        text, used_ai = ai_assist.analyze_hacienda_result(invoice)
        IssuedInvoice.objects.filter(pk=invoice.pk, owner=request.user).update(
            ai_analysis=text
        )
        return Response({"analysis": text, "ai_used": used_ai})

    def history(self, request, pk=None):
        invoice = self.get_object()
        return Response(
            StatusEventSerializer(invoice.status_events.all(), many=True).data
        )

    def summary(self, request):
        queryset = self.get_queryset().exclude(status=IssuedInvoice.Status.CANCELLED)
        month = request.query_params.get("month")
        if month:
            try:
                year, month_number = (int(part) for part in month.split("-"))
                queryset = queryset.filter(
                    issue_date__year=year,
                    issue_date__month=month_number,
                )
            except ValueError:
                return _bad_request("Usa el formato AAAA-MM en month.")
        by_status = {
            row["status"]: {
                "count": row["count"],
                "total": _money(row["total"]),
            }
            for row in queryset.values("status").annotate(
                count=Count("id"),
                total=Sum("total"),
            )
        }
        totals = queryset.aggregate(
            count=Count("id"),
            subtotal=Sum("subtotal"),
            tax=Sum("tax_total"),
            total=Sum("total"),
        )
        return Response(
            {
                "currency": "CRC",
                "month": month or None,
                "by_status": by_status,
                "totals": {
                    "count": totals["count"],
                    "subtotal": _money(totals["subtotal"]),
                    "tax": _money(totals["tax"]),
                    "total": _money(totals["total"]),
                },
            }
        )


class DraftReviewSerializer(serializers.Serializer):
    issuer_name = serializers.CharField(max_length=250, default="Emisor")
    issuer_id = serializers.CharField(max_length=100, default="0")
    receiver_name = serializers.CharField(max_length=250, default="Receptor")
    receiver_id = serializers.CharField(max_length=100, default="0")
    issue_date = serializers.DateField()
    lines = LineSerializer(many=True, allow_empty=False, max_length=MAX_INVOICE_LINES)
    use_ai = serializers.BooleanField(default=True)


class DraftReviewView(APIView):
    """Costo exacto y revisión por IA de un borrador sin guardarlo."""

    throttle_scope = "ai"

    def post(self, request):
        serializer = DraftReviewSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            document = _build_document(serializer.validated_data)
        except IssuedInvoiceDraftError as exc:
            return _bad_request(str(exc))
        if not serializer.validated_data["use_ai"]:
            return Response(
                {"cost": ai_assist.estimate_cost(document), "ai_available": True, "advice": "", "error": ""}
            )
        return Response(ai_assist.review_draft(document))


class ApiRootView(APIView):
    def get(self, request):
        return Response(
            {
                "version": "v1",
                "currency": "CRC",
                "official_tribucr_integration": False,
                "note": (
                    "API de facturación GuanaBill. En el sandbox local las facturas se envían al simulador de Hacienda."
                ),
            }
        )
