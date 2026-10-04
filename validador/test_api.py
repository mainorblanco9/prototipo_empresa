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


class HaciendaFlowTests(TestCase):
    def setUp(self):
        user = get_user_model().objects.create_user("hacienda-user", password="Clave-segura-1")
        self.client = APIClient()
        self.client.force_authenticate(user)
        response = self.client.post(reverse("api_invoice_list"), payload(), format="json")
        self.invoice = response.json()

    def transition(self, to):
        invoice = self.client.get(reverse("api_invoice_detail", args=[self.invoice["id"]])).json()
        return self.client.post(
            reverse("api_invoice_transition", args=[invoice["id"]]),
            {"to": to, "version": invoice["version"]},
            format="json",
        )

    def queue(self):
        invoice = self.client.get(reverse("api_invoice_detail", args=[self.invoice["id"]])).json()
        return self.client.post(
            reverse("api_invoice_queue", args=[invoice["id"]]),
            {"version": invoice["version"]},
            format="json",
        )

    def test_hacienda_states_cannot_be_set_manually(self):
        self.transition("ready")
        self.assertEqual(self.transition("accepted").status_code, 400)

    def test_draft_cannot_be_queued(self):
        self.assertEqual(self.queue().status_code, 409)

    def test_queue_generates_50_digit_clave_and_payload(self):
        self.transition("ready")
        queued = self.queue()
        self.assertEqual(queued.status_code, 200)
        data = queued.json()
        self.assertEqual(data["status"], "queued")
        self.assertRegex(data["clave"], r"^506\d{47}$")
        self.assertEqual(data["clave"][3:9], "031026")
        self.assertEqual(data["clave"][9:21], "003101000000")
        payload_response = self.client.get(
            reverse("api_invoice_reception_payload", args=[data["id"]])
        ).json()
        self.assertEqual(payload_response["clave"], data["clave"])
        import base64

        xml = base64.b64decode(payload_response["comprobanteXml"]).decode()
        self.assertIn("<Clave>" + data["clave"] + "</Clave>", xml)
        self.assertIn("FacturaElectronica", xml)

    def test_consecutive_increments(self):
        self.transition("ready")
        first = self.queue().json()
        second_draft = self.client.post(
            reverse("api_invoice_list"), payload(), format="json"
        ).json()
        self.client.post(
            reverse("api_invoice_transition", args=[second_draft["id"]]),
            {"to": "ready", "version": second_draft["version"]},
            format="json",
        )
        refreshed = self.client.get(
            reverse("api_invoice_detail", args=[second_draft["id"]])
        ).json()
        second = self.client.post(
            reverse("api_invoice_queue", args=[second_draft["id"]]),
            {"version": refreshed["version"]},
            format="json",
        ).json()
        self.assertNotEqual(first["clave"], second["clave"])
        self.assertEqual(first["clave"][21:41][-10:], "0000000001")
        self.assertEqual(second["clave"][21:41][-10:], "0000000002")

    def test_hacienda_result_updates_status(self):
        self.transition("ready")
        data = self.queue().json()
        url = reverse("api_invoice_hacienda_result", args=[data["id"]])
        processing = self.client.post(url, {"state": "procesando"}, format="json")
        self.assertEqual(processing.json()["status"], "processing")
        accepted = self.client.post(
            url, {"state": "aceptado", "message": "OK"}, format="json"
        )
        self.assertEqual(accepted.json()["status"], "accepted")
        self.assertEqual(accepted.json()["hacienda_message"], "OK")
        again = self.client.post(url, {"state": "rechazado"}, format="json")
        self.assertEqual(again.status_code, 409)

    def test_rejected_can_return_to_draft(self):
        self.transition("ready")
        data = self.queue().json()
        self.client.post(
            reverse("api_invoice_hacienda_result", args=[data["id"]]),
            {"state": "rechazado", "message": "Error"},
            format="json",
        )
        self.assertEqual(self.transition("draft").status_code, 200)

    def test_pages_render_for_logged_user(self):
        from django.test import Client

        web = Client()
        web.force_login(get_user_model().objects.get(username="hacienda-user"))
        self.assertEqual(web.get(reverse("mobile_capture")).status_code, 200)
        self.assertEqual(web.get(reverse("issued_invoice_list")).status_code, 200)
        self.assertIn("/facturas/movil/", web.get(reverse("pwa_service_worker")).content.decode())


class AiAssistTests(HaciendaFlowTests):
    def test_review_draft_returns_exact_cost_and_ai_advice(self):
        from unittest import mock

        with mock.patch("validador.ai_assist.generate_local_text", return_value="Sugerencia"):
            response = self.client.post(
                reverse("api_ai_review_draft"),
                {k: v for k, v in payload().items() if k != "client_id"},
                format="json",
            )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["cost"]["total"], "2260.00")
        self.assertEqual(data["advice"], "Sugerencia")

    def test_review_draft_degrades_without_ollama(self):
        from unittest import mock

        from .ai_client import AIProviderError

        with mock.patch("validador.ai_assist.generate_local_text", side_effect=AIProviderError("sin ollama")):
            data = self.client.post(
                reverse("api_ai_review_draft"),
                {k: v for k, v in payload().items() if k != "client_id"},
                format="json",
            ).json()
        self.assertFalse(data["ai_available"])
        self.assertEqual(data["cost"]["total"], "2260.00")

    def test_hacienda_analysis_saved_and_fallback(self):
        from unittest import mock

        from .ai_client import AIProviderError

        self.transition("ready")
        data = self.queue().json()
        url = reverse("api_invoice_ai_analysis", args=[data["id"]])
        self.client.post(
            reverse("api_invoice_hacienda_result", args=[data["id"]]),
            {"state": "rechazado", "message": "Error en receptor"},
            format="json",
        )
        with mock.patch("validador.ai_assist.generate_local_text", side_effect=AIProviderError("x")):
            result = self.client.post(url).json()
        self.assertFalse(result["ai_used"])
        self.assertIn("rechazó", result["analysis"])
        with mock.patch("validador.ai_assist.generate_local_text", return_value="Causa: receptor"):
            self.assertTrue(self.client.post(url).json()["ai_used"])
        detail = self.client.get(reverse("api_invoice_detail", args=[data["id"]])).json()
        self.assertEqual(detail["ai_analysis"], "Causa: receptor")


class ApiKeyTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("prov", password="x-Pass-123")

    def _payload(self):
        return {
            "client_id": str(uuid.uuid4()),
            "issuer_name": "Emisor", "issuer_id": "3101123456",
            "receiver_name": "Receptor", "receiver_id": "3101654321",
            "issue_date": "2026-01-15",
            "lines": [{"description": "Servicio", "quantity": 1, "unit_price": "113", "tax_rate": "13"}],
        }

    def test_api_key_creates_invoice_with_bearer_and_header(self):
        from .models import ApiKey

        key, raw = ApiKey.issue(self.user, "ERP")
        self.assertNotIn(raw, key.key_hash)
        client = APIClient()
        res = client.post("/api/v1/invoices/", self._payload(), format="json", HTTP_AUTHORIZATION=f"Bearer {raw}")
        self.assertEqual(res.status_code, 201, res.content)
        res = client.get("/api/v1/invoices/", HTTP_X_API_KEY=raw)
        self.assertEqual(res.status_code, 200)
        key.refresh_from_db()
        self.assertIsNotNone(key.last_used_at)

    def test_invalid_or_revoked_key_is_rejected(self):
        from django.utils import timezone

        from .models import ApiKey

        client = APIClient()
        self.assertEqual(client.get("/api/v1/invoices/", HTTP_X_API_KEY="gb_nope").status_code, 401)
        key, raw = ApiKey.issue(self.user, "ERP")
        key.revoked_at = timezone.now()
        key.save()
        self.assertEqual(client.get("/api/v1/invoices/", HTTP_X_API_KEY=raw).status_code, 401)

    def test_integrations_page_issues_key_once_and_wizard_renders(self):
        self.client.force_login(self.user)
        res = self.client.post(reverse("integrations"), {"name": "Mi ERP"})
        self.assertContains(res, "gb_")
        self.assertEqual(self.user.api_keys.count(), 1)
        self.assertNotContains(self.client.get(reverse("integrations")), "no se vuelve a mostrar")
        self.assertContains(self.client.get(reverse("invoice_wizard")), "GuanaBill")

    def test_review_draft_without_ai_returns_cost_only(self):
        self.client.force_login(self.user)
        res = self.client.post("/api/v1/ai/review-draft/", {**self._payload(), "use_ai": False}, content_type="application/json")
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.json()["cost"]["total"], "113.00")

    def test_seed_command(self):
        from django.core.management import call_command

        call_command("seed_guanabill", stdout=__import__("io").StringIO())
        self.assertEqual(IssuedInvoice.objects.filter(owner__username="proveedor_prueba").count(), 3)


class PeriodAndActivityTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("cierre", password="x-Pass-123")
        self.client.force_login(self.user)

    def _invoice(self, status, day="2026-01-15", n=1):
        from .models import IssuedInvoiceStatusEvent

        doc = {"issue_date": day, "receiver": {"name": "Cliente"}, "totals": {"subtotal": "100", "tax": "13", "total": "113"}}
        inv = IssuedInvoice.objects.create(
            owner=self.user, client_id=uuid.uuid4(), status=status, issue_date=day,
            receiver_name="Cliente", subtotal=100, tax_total=13, total=113, document=doc, consecutive=n,
            clave=f"{n:050d}",
        )
        IssuedInvoiceStatusEvent.objects.create(invoice=inv, from_status="", to_status=status, actor=self.user)
        return inv

    def test_close_blocked_by_draft_and_rejected_then_closed(self):
        from .models import PeriodClosure

        self._invoice("draft", n=1)
        rej = self._invoice("rejected", n=2)
        res = self.client.post(reverse("closures"), {"period": "2026-01", "action": "close"})
        self.assertContains(res, "No se pudo cerrar")
        self.assertEqual(PeriodClosure.objects.get().result, "blocked")
        IssuedInvoice.objects.filter(status="draft").update(status="accepted")
        rej.status = "accepted"
        rej.save()
        res = self.client.post(reverse("closures"), {"period": "2026-01", "action": "close"})
        self.assertContains(res, "cerrado")
        self.assertEqual(PeriodClosure.objects.first().result, "closed")

    def test_closed_period_rejects_new_invoices_until_reopened(self):
        self._invoice("accepted")
        self.client.post(reverse("closures"), {"period": "2026-01", "action": "close"})
        payload = {
            "client_id": str(uuid.uuid4()), "issuer_name": "E", "issuer_id": "1", "receiver_name": "R",
            "receiver_id": "2", "issue_date": "2026-01-20",
            "lines": [{"description": "x", "quantity": 1, "unit_price": "113", "tax_rate": "13"}],
        }
        self.assertEqual(self.client.post("/api/v1/invoices/", payload, content_type="application/json").status_code, 409)
        self.client.post(reverse("closures"), {"period": "2026-01", "action": "reopen", "note": "ajuste"})
        payload["client_id"] = str(uuid.uuid4())
        self.assertEqual(self.client.post("/api/v1/invoices/", payload, content_type="application/json").status_code, 201)

    def test_activity_log_filters_and_csv(self):
        self._invoice("accepted", n=1)
        self._invoice("rejected", n=2)
        self.client.post(reverse("closures"), {"period": "2026-01", "action": "close"})
        res = self.client.get(reverse("activity_log"), {"resultado": "rechazados"})
        self.assertContains(res, "Rechazada por Hacienda")
        self.assertNotContains(res, "Aceptada por Hacienda")
        res = self.client.get(reverse("activity_log"), {"tipo": "cierres", "formato": "csv"})
        self.assertIn("Cierre rechazado", res.content.decode("utf-8-sig"))

    def test_dashboard_and_pages_render_without_demo_wording(self):
        for name in ("dashboard", "issued_invoice_list", "invoice_wizard", "import_invoices", "closures", "integrations", "activity_log"):
            body = self.client.get(reverse(name)).content.decode()
            self.assertNotIn("Demo gen", body)
            self.assertNotIn("emostraci", body)
            self.assertNotIn("rototipo", body)

    def test_excel_import_validates_then_creates_idempotently(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        csv_text = (
            "factura,emisor,cedula_emisor,receptor,cedula_receptor,fecha,descripcion,cantidad,precio_con_iva,iva\n"
            "F1,Emisor,3101,Cliente,3102,2026-01-10,Servicio,1,113,13\n"
            "F1,Emisor,3101,Cliente,3102,2026-01-10,Otro,2,50,exempt\n"
            "F2,Emisor,3101,Cliente,3102,fecha-mala,Servicio,1,113,13\n"
        )
        def upload(confirm):
            f = SimpleUploadedFile("lote.csv", csv_text.encode(), content_type="text/csv")
            return self.client.post("/api/v1/invoices/import/", {"file": f, **({"confirm": "1"} if confirm else {})}).json()
        preview = upload(False)
        self.assertEqual((preview["valid"], preview["with_errors"], preview["created"]), (1, 1, 0))
        self.assertEqual(IssuedInvoice.objects.count(), 0)
        self.assertEqual(upload(True)["created"], 1)
        self.assertEqual(upload(True)["created"], 0)
        self.assertEqual(IssuedInvoice.objects.count(), 1)
        self.assertEqual(IssuedInvoice.objects.get().total, 213)
        self.assertContains(self.client.get(reverse("import_template")), "factura,emisor")


class LocalValidationAndThemeTests(HaciendaFlowTests):
    def test_validate_reports_bad_issuer_without_sending(self):
        bad = self.client.post(
            reverse("api_invoice_list"), payload(issuer_id="1", client_id=str(uuid.uuid4())), format="json"
        ).json()
        result = self.client.post(reverse("api_invoice_validate", args=[bad["id"]])).json()
        self.assertFalse(result["valid"])
        self.assertIn("emisor", result["issues"][0]["message"])
        good = self.client.post(reverse("api_invoice_validate", args=[self.invoice["id"]])).json()
        self.assertTrue(good["valid"])

    def test_queue_blocks_invalid_issuer_with_422(self):
        bad = self.client.post(
            reverse("api_invoice_list"), payload(issuer_id="1", client_id=str(uuid.uuid4())), format="json"
        ).json()
        self.client.post(
            reverse("api_invoice_transition", args=[bad["id"]]), {"to": "ready", "version": bad["version"]}, format="json"
        )
        current = self.client.get(reverse("api_invoice_detail", args=[bad["id"]])).json()
        response = self.client.post(
            reverse("api_invoice_queue", args=[bad["id"]]), {"version": current["version"]}, format="json"
        )
        self.assertEqual(response.status_code, 422)

    def test_theme_save_and_recommend(self):
        user = get_user_model().objects.get(username="hacienda-user")
        web = self.client_class()
        web.force_login(user)
        url = reverse("api_design_theme")
        ok = web.post(url, {"action": "save", "theme": "ocean"}, content_type="application/json")
        self.assertEqual(ok.json()["theme"], "ocean")
        self.assertEqual(web.post(url, {"action": "save", "theme": "x"}, content_type="application/json").status_code, 400)
        with override_settings(AI_PROVIDER="none"):
            rec = web.post(url, {"action": "recommend", "description": "algo oscuro de noche"}, content_type="application/json").json()
        self.assertFalse(rec["ai_used"])
        self.assertEqual(rec["suggestions"][0]["theme"], "midnight")
        self.assertContains(web.get(reverse("dashboard")), 'data-theme="ocean"')

class SeedBancosTests(TestCase):
    def test_seed_bancos_creates_varied_invoices(self):
        from django.core.management import call_command
        call_command("seed_bancos", "--user", "tester", "--count", "14", stdout=__import__("io").StringIO())
        invoices = IssuedInvoice.objects.filter(owner__username="tester")
        self.assertEqual(invoices.count(), 14)
        self.assertTrue(invoices.filter(status="ready").exists())
        self.assertGreater(len({i.issue_date.strftime("%Y-%m") for i in invoices}), 1)

class HaciendaFlowPageTests(TestCase):
    def test_flow_page_renders_stages(self):
        user = get_user_model().objects.create_user("flow", password="x-Pass-12345")
        self.client.force_login(user)
        response = self.client.get(reverse("hacienda_flow"))
        self.assertContains(response, "Firma XAdES-EPES")
        self.assertContains(response, "/recepcion/v1/recepcion")
