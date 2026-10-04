from decimal import Decimal
import secrets

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.db.models import Count, Sum
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


class TransitionSerializer(serializers.Serializer):
    to = serializers.ChoiceField(choices=IssuedInvoice.Status.values)
    version = serializers.IntegerField(min_value=1)


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


class ApiRootView(APIView):
    def get(self, request):
        return Response(
            {
                "version": "v1",
                "currency": "CRC",
                "official_tribucr_integration": False,
                "note": (
                    "Borradores locales no fiscales; no se transmiten a Hacienda."
                ),
            }
        )
