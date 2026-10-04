import uuid
from datetime import date

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand

from validador.api import _build_document, _document_fields
from validador.models import ApiKey, IssuedInvoice, IssuedInvoiceStatusEvent

SAMPLES = [
    ("seed-001", "Servicio de consultoria", "113000", "13"),
    ("seed-002", "Canasta basica", "50000", "1"),
    ("seed-003", "Curso de capacitacion", "80000", "exempt"),
]


class Command(BaseCommand):
    help = "Crea un proveedor de prueba, una API key y facturas de ejemplo (solo sandbox local)."

    def add_arguments(self, parser):
        parser.add_argument("--user", default="proveedor_prueba", help="Usuario que recibe los datos")

    def handle(self, *args, **options):
        if getattr(settings, "HACIENDA_MODE", "local") != "local":
            self.stderr.write("Solo disponible con HACIENDA_MODE=local.")
            return
        User = get_user_model()
        user, created = User.objects.get_or_create(username=options["user"])
        if created:
            user.set_password("Proveedor123!")
            user.save()
        ApiKey.objects.filter(owner=user, name="Sandbox seed").delete()
        _, raw = ApiKey.issue(user, "Sandbox seed")
        for client_id, description, price, rate in SAMPLES:
            document = _build_document(
                {
                    "issuer_name": "Comercial Guanacaste S.A.",
                    "issuer_id": "3101123456",
                    "receiver_name": "Cliente de Prueba S.A.",
                    "receiver_id": "3101654321",
                    "issue_date": date.today(),
                    "lines": [
                        {"description": description, "quantity": "1", "unit_price": price, "tax_rate": rate}
                    ],
                }
            )
            invoice, was_created = IssuedInvoice.objects.get_or_create(
                owner=user,
                client_id=uuid.uuid5(uuid.NAMESPACE_URL, client_id),
                defaults=_document_fields(document),
            )
            if was_created:
                IssuedInvoiceStatusEvent.objects.create(
                    invoice=invoice, from_status="", to_status=invoice.status, actor=user
                )
        self.stdout.write(self.style.SUCCESS("Datos de prueba listos."))
        self.stdout.write(f"Usuario: {user.username}")
        self.stdout.write(f"API key (sandbox): {raw}")
