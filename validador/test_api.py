import uuid

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework.test import APIClient

from .models import IssuedInvoice


def payload(**overrides):
    data = {
        "client_id": str(uuid.uuid4()),
        "issuer_name": "Empresa Emisora",
        "issuer_id": "3-101-000000",
        "receiver_name": "Cliente Receptor",
        "receiver_id": "1-0000-0000",
        "issue_date": "2026-10-03",
        "lines": [
            {
                "description": "Servicio",
                "quantity": "2",
                "unit_price": "1130",
                "tax_rate": "13",
            }
        ],
    }
    data.update(overrides)
    return data


class IssuedInvoiceApiTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.user = user_model.objects.create_user("api-user", password="Clave-segura-1")
        self.other = user_model.objects.create_user("otro", password="Clave-segura-2")
        self.client = APIClient()
        token = self.client.post(
            reverse("api_token"),
            {"username": "api-user", "password": "Clave-segura-1"},
            format="json",
        ).json()["token"]
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {token}")

    def create(self, **overrides):
        return self.client.post(
            reverse("api_invoice_list"),
            payload(**overrides),
            format="json",
        )

    def test_requires_authentication_and_rejects_bad_credentials(self):
        anonymous = APIClient()
        self.assertEqual(anonymous.get(reverse("api_invoice_list")).status_code, 401)
        bad = anonymous.post(
            reverse("api_token"),
            {"username": "api-user", "password": "incorrecta"},
            format="json",
        )
        self.assertEqual(bad.status_code, 400)

    def test_demo_credentials_only_work_when_demo_login_is_enabled(self):
        credentials = {"username": "demo", "password": "Demo123!"}
        with override_settings(DEMO_LOGIN_ENABLED=True):
            ok = APIClient().post(reverse("api_token"), credentials, format="json")
            self.assertEqual(ok.status_code, 200)
            wrong = APIClient().post(
                reverse("api_token"),
                {"username": "demo", "password": "mala"},
                format="json",
            )
            self.assertEqual(wrong.status_code, 400)
        with override_settings(DEMO_LOGIN_ENABLED=False):
            off = APIClient().post(reverse("api_token"), credentials, format="json")
            self.assertEqual(off.status_code, 400)

    def test_token_can_be_revoked(self):
        self.assertEqual(self.client.delete(reverse("api_token")).status_code, 204)
        self.assertEqual(self.client.get(reverse("api_invoice_list")).status_code, 401)

    def test_create_extracts_included_vat_and_is_idempotent_by_client_id(self):
        body = payload()
        first = self.client.post(reverse("api_invoice_list"), body, format="json")
        self.assertEqual(first.status_code, 201)
        data = first.json()
        self.assertEqual(data["status"], "draft")
        self.assertEqual(data["currency"], "CRC")
        self.assertEqual(data["subtotal"], "2000.00")
        self.assertEqual(data["tax_total"], "260.00")
        self.assertEqual(data["total"], "2260.00")
        self.assertEqual(data["version"], 1)
        self.assertTrue(data["document"]["not_official_format"])
        self.assertFalse(data["document"]["transmission"]["real_hacienda_contacted"])

        repeated = self.client.post(reverse("api_invoice_list"), body, format="json")
        self.assertEqual(repeated.status_code, 200)
        self.assertEqual(repeated.json()["id"], data["id"])
        self.assertEqual(IssuedInvoice.objects.count(), 1)

    def test_validation_errors_return_400_without_saving(self):
        self.assertEqual(self.create(client_id=None).status_code, 400)
        self.assertEqual(self.create(lines=[]).status_code, 400)
        bad_rate = self.create(
            lines=[
                {
                    "description": "x",
                    "quantity": "1",
                    "unit_price": "100",
                    "tax_rate": "99",
                }
            ]
        )
        self.assertEqual(bad_rate.status_code, 400)
        bad_quantity = self.create(
            lines=[
                {
                    "description": "x",
                    "quantity": "0",
                    "unit_price": "100",
                    "tax_rate": "13",
                }
            ]
        )
        self.assertEqual(bad_quantity.status_code, 400)
        self.assertEqual(IssuedInvoice.objects.count(), 0)

    def test_invoices_are_private_to_their_owner(self):
        invoice_id = self.create().json()["id"]
        other_client = APIClient()
        other_client.force_authenticate(self.other)

        self.assertEqual(
            other_client.get(reverse("api_invoice_list")).json()["count"], 0
        )
        detail = reverse("api_invoice_detail", args=[invoice_id])
        self.assertEqual(other_client.get(detail).status_code, 404)
        self.assertEqual(
            other_client.post(
                reverse("api_invoice_transition", args=[invoice_id]),
                {"to": "ready", "version": 1},
                format="json",
            ).status_code,
            404,
        )

    def test_update_uses_version_to_detect_conflicts_and_only_edits_drafts(self):
        created = self.create().json()
        detail = reverse("api_invoice_detail", args=[created["id"]])
        edit = payload(
            receiver_name="Cliente Nuevo",
            lines=[
                {
                    "description": "Otro",
                    "quantity": "1",
                    "unit_price": "1000",
                    "tax_rate": "exempt",
                }
            ],
        )
        edit.pop("client_id")

        missing_version = self.client.put(detail, edit, format="json")
        self.assertEqual(missing_version.status_code, 400)

        edited = self.client.put(detail, {**edit, "version": 1}, format="json")
        self.assertEqual(edited.status_code, 200)
        self.assertEqual(edited.json()["version"], 2)
        self.assertEqual(edited.json()["receiver_name"], "Cliente Nuevo")
        self.assertEqual(edited.json()["tax_total"], "0.00")
        self.assertEqual(edited.json()["total"], "1000.00")

        stale = self.client.put(detail, {**edit, "version": 1}, format="json")
        self.assertEqual(stale.status_code, 409)

        self.client.post(
            reverse("api_invoice_transition", args=[created["id"]]),
            {"to": "ready", "version": 2},
            format="json",
        )
        locked = self.client.put(detail, {**edit, "version": 3}, format="json")
        self.assertEqual(locked.status_code, 409)

    def test_status_transitions_are_validated_and_recorded(self):
        created = self.create().json()
        url = reverse("api_invoice_transition", args=[created["id"]])

        ready = self.client.post(url, {"to": "ready", "version": 1}, format="json")
        self.assertEqual(ready.status_code, 200)
        self.assertEqual(ready.json()["status"], "ready")
        self.assertEqual(ready.json()["version"], 2)

        stale = self.client.post(url, {"to": "cancelled", "version": 1}, format="json")
        self.assertEqual(stale.status_code, 409)

        cancelled = self.client.post(
            url, {"to": "cancelled", "version": 2}, format="json"
        )
        self.assertEqual(cancelled.status_code, 200)
        reopened = self.client.post(url, {"to": "draft", "version": 3}, format="json")
        self.assertEqual(reopened.status_code, 409)
        invalid = self.client.post(url, {"to": "accepted", "version": 3}, format="json")
        self.assertEqual(invalid.status_code, 400)

        history = self.client.get(
            reverse("api_invoice_history", args=[created["id"]])
        ).json()
        self.assertEqual(
            [(event["from_status"], event["to_status"]) for event in history],
            [("", "draft"), ("draft", "ready"), ("ready", "cancelled")],
        )

    def test_list_filters_support_incremental_sync_and_summary(self):
        first = self.create(issue_date="2026-10-03").json()
        second = self.create(issue_date="2026-11-04").json()
        self.client.post(
            reverse("api_invoice_transition", args=[second["id"]]),
            {"to": "ready", "version": 1},
            format="json",
        )

        october = self.client.get(
            reverse("api_invoice_list"), {"month": "2026-10"}
        ).json()
        self.assertEqual([item["id"] for item in october["results"]], [first["id"]])
        self.assertNotIn("document", october["results"][0])

        ready = self.client.get(reverse("api_invoice_list"), {"status": "ready"}).json()
        self.assertEqual([item["id"] for item in ready["results"]], [second["id"]])

        since = self.client.get(
            reverse("api_invoice_list"),
            {"updated_since": first["updated_at"]},
        ).json()
        self.assertEqual([item["id"] for item in since["results"]], [second["id"]])

        self.assertEqual(
            self.client.get(reverse("api_invoice_list"), {"month": "x"}).status_code,
            400,
        )

        summary = self.client.get(reverse("api_invoice_summary")).json()
        self.assertEqual(summary["totals"]["count"], 2)
        self.assertEqual(summary["totals"]["total"], "4520.00")
        self.assertEqual(summary["by_status"]["ready"]["count"], 1)
        october_summary = self.client.get(
            reverse("api_invoice_summary"), {"month": "2026-10"}
        ).json()
        self.assertEqual(october_summary["totals"]["total"], "2260.00")
