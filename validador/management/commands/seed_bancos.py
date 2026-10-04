import uuid
from datetime import date, timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.urls import reverse
from rest_framework.test import APIClient

BANKS = [
    ("Banco Nacional de Costa Rica", "4000001021"),
    ("Banco de Costa Rica", "4000000019"),
    ("BAC Credomatic", "3101012345"),
    ("Banco Popular y de Desarrollo Comunal", "4000042152"),
    ("Scotiabank de Costa Rica", "3101022154"),
    ("Davivienda Costa Rica", "3101086122"),
]
CLIENTS = [
    ("Comercial Guanacaste S.A.", "3101123456"),
    ("Minor Andres Blanco Chaves", "114560789"),
    ("Servicios Pacifico Ltda.", "3102654321"),
    ("Agro Nicoya S.A.", "3101777888"),
    ("Maria Fernanda Rojas Vargas", "205670123"),
]
CONCEPTS = [
    ("Comision por transferencia SINPE", "1500", "13"),
    ("Mantenimiento de cuenta empresarial", "8500", "13"),
    ("Servicio de custodia de valores", "45000", "13"),
    ("Intereses por prestamo (exento)", "120000", "exempt"),
    ("Poliza de seguro de credito", "32000", "13"),
    ("Cargo por emision de tarjeta", "12500", "13"),
    ("Asesoria financiera", "95000", "13"),
    ("Servicio de cobro y recaudacion", "27500", "13"),
    ("Alquiler de caja de seguridad", "18000", "13"),
    ("Comision de apertura de credito", "210000", "13"),
]


class Command(BaseCommand):
    help = "Genera facturas de bancos (varios periodos y estados) para probar todo el flujo local."

    def add_arguments(self, parser):
        parser.add_argument("--user", default="demo")
        parser.add_argument("--count", type=int, default=24)
        parser.add_argument("--send", action="store_true", help="Envia las facturas 'listas' al gateway/simulador")

    def handle(self, *args, **options):
        if getattr(settings, "HACIENDA_MODE", "local") != "local":
            self.stderr.write("Solo disponible con HACIENDA_MODE=local.")
            return
        user, _ = get_user_model().objects.get_or_create(username=options["user"])
        client = APIClient(SERVER_NAME="127.0.0.1")
        client.force_authenticate(user)
        today = date.today()
        created = ready_ids = 0
        to_send = []
        for index in range(options["count"]):
            bank, bank_id = BANKS[index % len(BANKS)]
            customer, customer_id = CLIENTS[(index * 3) % len(CLIENTS)]
            first_day = today.replace(day=1)
            month_start = (first_day - timedelta(days=31 * (index % 3))).replace(day=1)
            issue_date = min(month_start + timedelta(days=(index * 5) % 26), today)
            lines = []
            for offset in range(1 + index % 3):
                text, price, rate = CONCEPTS[(index + offset * 2) % len(CONCEPTS)]
                lines.append({"description": text, "quantity": str(1 + offset), "unit_price": price, "tax_rate": rate})
            if index % 11 == 7:
                lines[0]["description"] += " RECHAZAR"
            body = {
                "client_id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"banco-{user.username}-{index}")),
                "issuer_name": bank,
                "issuer_id": "1" if index % 13 == 12 else bank_id,
                "receiver_name": customer,
                "receiver_id": customer_id,
                "issue_date": issue_date.isoformat(),
                "lines": lines,
            }
            response = client.post(reverse("api_invoice_list"), body, format="json")
            if response.status_code not in (200, 201):
                self.stderr.write(f"Factura {index}: {response.status_code} {response.content[:120]!r}")
                continue
            invoice = response.json()
            created += 1
            if invoice["status"] == "draft" and index % 4 != 3:
                moved = client.post(
                    reverse("api_invoice_transition", args=[invoice["id"]]),
                    {"to": "ready", "version": invoice["version"]},
                    format="json",
                )
                if moved.status_code == 200:
                    ready_ids += 1
                    to_send.append(invoice["id"])
        sent = failed = 0
        if options["send"]:
            for invoice_id in to_send:
                result = client.post(reverse("api_invoice_submit", args=[invoice_id]))
                if result.status_code == 200:
                    sent += 1
                else:
                    failed += 1
        self.stdout.write(self.style.SUCCESS(
            f"Facturas de bancos para '{user.username}': {created} procesadas, {ready_ids} listas"
            + (f", {sent} enviadas, {failed} con error (esperado: emisor invalido)." if options["send"] else ".")
        ))

