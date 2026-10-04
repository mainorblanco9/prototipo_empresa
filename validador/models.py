import uuid

from django.conf import settings
from django.db import models


class UserDesignPreference(models.Model):
    class Theme(models.TextChoices):
        MINIMAL = "minimal", "Minimalista"
        ABSTRACT = "abstract", "Abstracto"
        CORPORATE = "corporate", "Corporativo"

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="design_preference",
    )
    theme = models.CharField(
        max_length=20,
        choices=Theme.choices,
        default=Theme.MINIMAL,
    )
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.user} — {self.get_theme_display()}"


class InvoiceAuditEvent(models.Model):
    class EventType(models.TextChoices):
        ANALYSIS = "analysis", "Archivo analizado"
        MODEL_PREPARED = "model_prepared", "Modelo preparado"
        MODEL_DOWNLOADED = "model_downloaded", "Modelo descargado"
        OLLAMA_SUMMARY = "ollama_summary", "Resumen local generado"
        LOCAL_SIMULATION = "local_simulation", "Simulación local"
        ISSUED_DRAFT = "issued_draft", "Borrador emitido local"
        ISSUED_DRAFT_DOWNLOADED = "issued_draft_downloaded", "Borrador descargado"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="invoice_audit_events",
    )
    batch_id = models.UUIDField(db_index=True)
    event_type = models.CharField(max_length=32, choices=EventType.choices)
    result = models.CharField(max_length=32)
    invoice_count = models.PositiveIntegerField(default=0)
    validated_count = models.PositiveIntegerField(default=0)
    needs_review_count = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = [
            models.Index(fields=("user", "-created_at"), name="invoice_audit_user_date"),
        ]

    def __str__(self):
        return f"{self.get_event_type_display()} · {self.batch_id}"

    @property
    def result_label(self):
        labels = {
            "ready": "Lote listo · CRC",
            "needs_review": "Requiere revisión",
            "failed": "No se pudo analizar",
            "downloaded": "Descarga completada",
            "generated": "Resumen generado localmente",
            "unavailable": "Ollama no disponible",
            "simulation_passed": "Superada · simulación local",
            "simulation_blocked": "Bloqueada · simulación local",
            "blocked": "Bloqueada · simulación local",
            "accepted_mock": "Registro legado · antigua etiqueta mock (no fue un envío)",
            "draft_prepared": "Borrador interno preparado · no fiscal",
            "draft_downloaded": "Descarga del borrador interno",
        }
        return labels.get(self.result, self.result)


class IssuedInvoice(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "Borrador"
        READY = "ready", "Listo para envío"
        CANCELLED = "cancelled", "Anulado localmente"

        QUEUED = "queued", "Recibido por Hacienda"
        PROCESSING = "processing", "Procesando en Hacienda"
        ACCEPTED = "accepted", "Aceptado por Hacienda"
        REJECTED = "rejected", "Rechazado por Hacienda"

    # QUEUED..REJECTED reflejan ind-estado de la API de recepción (recibido,
    # procesando, aceptado, rechazado); solo los fija el flujo de envío.
    HACIENDA_STATES = {
        Status.QUEUED,
        Status.PROCESSING,
        Status.ACCEPTED,
        Status.REJECTED,
    }
    ALLOWED_TRANSITIONS = {
        Status.DRAFT: {Status.READY, Status.CANCELLED},
        Status.READY: {Status.DRAFT, Status.CANCELLED, Status.QUEUED},
        Status.CANCELLED: set(),
        Status.QUEUED: {Status.PROCESSING, Status.ACCEPTED, Status.REJECTED},
        Status.PROCESSING: {Status.ACCEPTED, Status.REJECTED},
        Status.ACCEPTED: set(),
        Status.REJECTED: {Status.DRAFT},
    }

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="issued_invoices",
    )
    # Identificador generado por el cliente móvil para que reintentos o
    # sincronizaciones sin conexión no creen duplicados.
    client_id = models.UUIDField()
    document_type = models.CharField(max_length=32, default="factura_electronica")
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.DRAFT,
    )
    currency = models.CharField(max_length=3, default="CRC")
    issue_date = models.DateField()
    receiver_name = models.CharField(max_length=250)
    subtotal = models.DecimalField(max_digits=17, decimal_places=2)
    tax_total = models.DecimalField(max_digits=17, decimal_places=2)
    total = models.DecimalField(max_digits=17, decimal_places=2)
    document = models.JSONField()
    consecutive = models.PositiveIntegerField(null=True, blank=True)
    clave = models.CharField(max_length=50, blank=True, db_index=True)
    hacienda_message = models.TextField(blank=True)
    ai_analysis = models.TextField(blank=True)
    version = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True, db_index=True)

    class Meta:
        ordering = ("-updated_at",)
        constraints = [
            models.UniqueConstraint(
                fields=("owner", "client_id"),
                name="issued_invoice_unique_client_id",
            ),
        ]

    def __str__(self):
        return f"Factura electrónica {self.id} · {self.get_status_display()}"

    def can_transition_to(self, new_status):
        return new_status in self.ALLOWED_TRANSITIONS[self.Status(self.status)]


class IssuedInvoiceStatusEvent(models.Model):
    invoice = models.ForeignKey(
        IssuedInvoice,
        on_delete=models.CASCADE,
        related_name="status_events",
    )
    from_status = models.CharField(max_length=16, blank=True)
    to_status = models.CharField(max_length=16)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        on_delete=models.SET_NULL,
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("created_at", "id")


class ApiKey(models.Model):
    """Credencial para que un proveedor envíe facturas desde su sistema."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="api_keys"
    )
    name = models.CharField(max_length=80)
    prefix = models.CharField(max_length=16, db_index=True)
    key_hash = models.CharField(max_length=64, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    @staticmethod
    def hash_key(raw):
        import hashlib

        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    @classmethod
    def issue(cls, owner, name):
        import secrets

        raw = "gb_" + secrets.token_urlsafe(32)
        key = cls.objects.create(
            owner=owner, name=name[:80], prefix=raw[:10], key_hash=cls.hash_key(raw)
        )
        return key, raw

    @property
    def is_active(self):
        return self.revoked_at is None


class PeriodClosure(models.Model):
    """Bitácora de cierres de periodo (mes): intentos bloqueados, cierres y reaperturas."""

    class Result(models.TextChoices):
        CLOSED = "closed", "Periodo cerrado"
        BLOCKED = "blocked", "Cierre rechazado por validación"
        REOPENED = "reopened", "Periodo reabierto"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="period_closures"
    )
    period = models.CharField(max_length=7, db_index=True)
    result = models.CharField(max_length=16, choices=Result.choices)
    invoice_count = models.PositiveIntegerField(default=0)
    accepted_count = models.PositiveIntegerField(default=0)
    rejected_count = models.PositiveIntegerField(default=0)
    total = models.DecimalField(max_digits=17, decimal_places=2, default=0)
    tax_total = models.DecimalField(max_digits=17, decimal_places=2, default=0)
    issues = models.JSONField(default=list, blank=True)
    note = models.CharField(max_length=250, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = [models.Index(fields=("owner", "period", "-created_at"), name="closure_owner_period")]
