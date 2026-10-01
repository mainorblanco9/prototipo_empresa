import json
from unittest.mock import MagicMock, patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from .ai_client import AIProviderError, generate_text
from .models import UserDesignPreference
from .sharepoint import is_sharepoint_configured, upload_to_sharepoint
from .validation import FileValidationError, build_csv_report, validate_upload


@override_settings(
    AI_PROVIDER="ollama",
    OLLAMA_BASE_URL="http://localhost:11434",
    OLLAMA_MODEL="qwen2.5:3b",
    OLLAMA_TIMEOUT_SECONDS=90,
)
class LocalAIClientTests(TestCase):
    @patch("validador.ai_client.requests.post")
    def test_sends_prompts_to_local_ollama(self, post):
        response = MagicMock()
        response.json.return_value = {"message": {"content": "abstract"}}
        post.return_value = response

        result = generate_text("system prompt", "user prompt", max_tokens=10)

        self.assertEqual(result, "abstract")
        post.assert_called_once_with(
            "http://localhost:11434/api/chat",
            json={
                "model": "qwen2.5:3b",
                "messages": [
                    {"role": "system", "content": "system prompt"},
                    {"role": "user", "content": "user prompt"},
                ],
                "stream": False,
                "options": {"num_predict": 10},
            },
            timeout=90,
        )

    @patch("validador.ai_client.requests.post")
    def test_reports_when_local_ollama_is_unavailable(self, post):
        import requests

        post.side_effect = requests.ConnectionError("connection refused")

        with self.assertRaisesMessage(AIProviderError, "No se pudo conectar con Ollama"):
            generate_text("system prompt", "user prompt")

    @override_settings(AI_PROVIDER="openai", OPENAI_API_KEY="test-key")
    @patch("validador.ai_client.OpenAI")
    def test_openai_remains_available_as_an_alternative(self, openai):
        openai.return_value.responses.create.return_value.output_text = "minimal"

        result = generate_text("system prompt", "user prompt", max_tokens=10)

        self.assertEqual(result, "minimal")
        openai.assert_called_once_with(api_key="test-key")
        openai.return_value.responses.create.assert_called_once_with(
            model="gpt-4.1-mini",
            instructions="system prompt",
            input="user prompt",
            max_output_tokens=10,
        )


@override_settings(DEBUG=True, DEMO_LOGIN_ENABLED=True)
class ExpenseValidationTests(TestCase):
    def upload(self, content, name="gastos.csv"):
        return SimpleUploadedFile(name, content.encode("utf-8"))

    def setUp(self):
        response = self.client.post(
            reverse("login"),
            {"username": "demo", "password": "Demo123!"},
        )
        self.assertEqual(response.status_code, 302)

    def test_validates_a_correct_expense_row(self):
        rows, issues = validate_upload(
            self.upload(
                "fecha,proveedor,numero_factura,moneda,subtotal,impuesto,total\n"
                "2026-09-01,Proveedor A,F-001,MXN,1000,160,1160\n"
            )
        )

        self.assertEqual(issues, {})
        self.assertEqual(rows[0]["estado"], "Correcto")

    def test_reports_duplicate_invoice_and_invalid_total(self):
        rows, issues = validate_upload(
            self.upload(
                "fecha,proveedor,numero_factura,moneda,subtotal,impuesto,total\n"
                "2026-09-01,Proveedor A,F-001,MXN,1000,160,1160\n"
                "2026-09-02,Proveedor A,F-001,MXN,1000,160,1200\n"
            )
        )

        self.assertEqual(rows[1]["estado"], "Revisar")
        self.assertIn("Factura duplicada", rows[1]["errores"])
        self.assertEqual(issues["Total no coincide con subtotal + impuesto"], 1)

    def test_rejects_missing_columns(self):
        with self.assertRaisesMessage(FileValidationError, "Faltan columnas requeridas"):
            validate_upload(self.upload("fecha,proveedor\n2026-09-01,Proveedor A\n"))

    def test_report_escapes_spreadsheet_formulas_in_text_fields(self):
        rows, _ = validate_upload(
            self.upload(
                "fecha,proveedor,numero_factura,moneda,subtotal,impuesto,total\n"
                '2026-09-01,=HYPERLINK("https://example.invalid"),F-001,MXN,1000,160,1160\n'
            )
        )

        report = build_csv_report(rows)
        self.assertIn("'=HYPERLINK", report)

    def test_upload_page_shows_report_and_download(self):
        response = self.client.post(
            reverse("index"),
            {
                "archivo": self.upload(
                    "fecha,proveedor,numero_factura,moneda,subtotal,impuesto,total\n"
                    "2026-09-01,Proveedor A,F-001,MXN,1000,160,1160\n"
                )
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Resumen de validación")
        self.assertContains(response, "Correcto")
        self.assertContains(response, "Descargar CSV")
        report_url = response.context["report_url"]

        download = self.client.get(report_url)
        self.assertEqual(download.status_code, 200)
        self.assertIn("Correcto", download.content.decode("utf-8-sig"))

    @override_settings(
        SHAREPOINT_TENANT_ID="tenant",
        SHAREPOINT_CLIENT_ID="client",
        SHAREPOINT_CLIENT_SECRET="secret",
        SHAREPOINT_DRIVE_ID="drive",
    )
    @patch("validador.views.upload_to_sharepoint")
    def test_uploads_original_to_sharepoint_when_all_rows_are_valid(
        self, upload_to_sharepoint
    ):
        upload_to_sharepoint.return_value = {
            "name": "gastos.csv",
            "web_url": "https://sharepoint.example/file",
            "item_id": "item-id",
        }
        uploaded_file = self.upload(
            "fecha,proveedor,numero_factura,moneda,subtotal,impuesto,total\n"
            "2026-09-01,Proveedor A,F-001,MXN,1000,160,1160\n"
        )

        response = self.client.post(reverse("index"), {"archivo": uploaded_file})

        self.assertEqual(response.status_code, 200)
        upload_to_sharepoint.assert_called_once()
        self.assertEqual(upload_to_sharepoint.call_args.args[0].name, "gastos.csv")
        self.assertEqual(upload_to_sharepoint.call_args.args[1], "gastos.csv")
        self.assertContains(response, "Archivo guardado en SharePoint")

    @override_settings(
        SHAREPOINT_TENANT_ID="tenant",
        SHAREPOINT_CLIENT_ID="client",
        SHAREPOINT_CLIENT_SECRET="secret",
        SHAREPOINT_DRIVE_ID="drive",
    )
    @patch("validador.views.upload_to_sharepoint")
    def test_does_not_upload_file_with_validation_errors(self, upload_to_sharepoint):
        response = self.client.post(
            reverse("index"),
            {
                "archivo": self.upload(
                    "fecha,proveedor,numero_factura,moneda,subtotal,impuesto,total\n"
                    "fecha-invalida,Proveedor A,F-001,MXN,1000,160,1160\n"
                )
            },
        )

        self.assertEqual(response.status_code, 200)
        upload_to_sharepoint.assert_not_called()
        self.assertContains(response, "El archivo no se subió")

    @patch("validador.views.upload_to_sharepoint")
    def test_does_not_upload_empty_file(self, upload_to_sharepoint):
        response = self.client.post(
            reverse("index"),
            {
                "archivo": self.upload(
                    "fecha,proveedor,numero_factura,moneda,subtotal,impuesto,total\n"
                )
            },
        )

        self.assertEqual(response.status_code, 200)
        upload_to_sharepoint.assert_not_called()
        self.assertContains(response, "no contiene filas de datos")


@override_settings(
    SHAREPOINT_TENANT_ID="tenant",
    SHAREPOINT_CLIENT_ID="client",
    SHAREPOINT_CLIENT_SECRET="secret",
    SHAREPOINT_DRIVE_ID="drive-id",
    SHAREPOINT_FOLDER="Gastos/Validos",
)
class SharePointUploadTests(TestCase):
    @patch("validador.sharepoint.urlopen")
    @patch("validador.sharepoint.msal.ConfidentialClientApplication")
    def test_uploads_to_configured_drive_folder_without_overwriting(
        self, msal_application, urlopen
    ):
        confidential_client = MagicMock()
        confidential_client.acquire_token_for_client.return_value = {
            "access_token": "test-token"
        }
        msal_application.return_value = confidential_client

        graph_response = MagicMock()
        graph_response.__enter__.return_value.read.return_value = json.dumps(
            {
                "id": "item-id",
                "name": "gastos.csv",
                "webUrl": "https://sharepoint.example/gastos.csv",
            }
        ).encode()
        urlopen.return_value = graph_response

        result = upload_to_sharepoint(
            SimpleUploadedFile("gastos.csv", b"validated file"),
            "gastos.csv",
        )

        request = urlopen.call_args.args[0]
        self.assertIn("/drives/drive-id/root:/Gastos/Validos/gastos.csv:/content", request.full_url)
        self.assertIn("conflictBehavior=rename", request.full_url)
        self.assertEqual(request.data, b"validated file")
        self.assertEqual(request.get_header("Authorization"), "Bearer test-token")
        self.assertEqual(result["item_id"], "item-id")


    @override_settings(
        SHAREPOINT_TENANT_ID="tenant",
        SHAREPOINT_CLIENT_ID="client",
        SHAREPOINT_CLIENT_SECRET="secret",
        SHAREPOINT_DRIVE_ID="",
        SHAREPOINT_SITE_ID="site-id",
        SHAREPOINT_DOCUMENT_LIBRARY="Documentos",
    )
    @patch("validador.sharepoint.urlopen")
    @patch("validador.sharepoint.msal.ConfidentialClientApplication")
    def test_resolves_configured_document_library_from_site(
        self, msal_application, urlopen
    ):
        confidential_client = MagicMock()
        confidential_client.acquire_token_for_client.return_value = {
            "access_token": "test-token"
        }
        msal_application.return_value = confidential_client

        drives_response = MagicMock()
        drives_response.__enter__.return_value.read.return_value = json.dumps(
            {"value": [{"id": "documents-drive", "name": "Documentos"}]}
        ).encode()
        upload_response = MagicMock()
        upload_response.__enter__.return_value.read.return_value = json.dumps(
            {"id": "item-id", "name": "gastos.csv"}
        ).encode()
        urlopen.side_effect = [drives_response, upload_response]

        upload_to_sharepoint(
            SimpleUploadedFile("gastos.csv", b"validated file"),
            "gastos.csv",
        )

        self.assertIn(
            "/sites/site-id/drives",
            urlopen.call_args_list[0].args[0].full_url,
        )
        self.assertIn(
            "/drives/documents-drive/root:/Gastos/Validos/gastos.csv:/content",
            urlopen.call_args_list[1].args[0].full_url,
        )

    @override_settings(
        SHAREPOINT_TENANT_ID="tenant",
        SHAREPOINT_CLIENT_ID="client",
        SHAREPOINT_CLIENT_SECRET="secret",
        SHAREPOINT_DRIVE_ID="",
        SHAREPOINT_SITE_ID="site-id",
        SHAREPOINT_DOCUMENT_LIBRARY="",
    )
    def test_site_without_drive_or_library_name_is_not_configured(self):
        self.assertFalse(is_sharepoint_configured())


@override_settings(DEBUG=True, DEMO_LOGIN_ENABLED=True)
class MockLoginTests(TestCase):
    def test_protected_pages_redirect_to_login(self):
        response = self.client.get(reverse("index"))
        self.assertRedirects(response, f"{reverse('login')}?next=/")

    def test_login_page_shows_demo_username(self):
        response = self.client.get(reverse("login"))
        self.assertContains(response, "demo")
        self.assertContains(response, "No usar en producción")

    def test_valid_mock_credentials_create_session(self):
        response = self.client.post(
            reverse("login"),
            {"username": "demo", "password": "Demo123!"},
        )
        self.assertRedirects(response, reverse("design_preferences"))
        self.assertTrue(self.client.session.get("_auth_user_id"))

    def test_invalid_credentials_are_rejected(self):
        response = self.client.post(
            reverse("login"),
            {"username": "demo", "password": "wrong"},
        )
        self.assertEqual(response.status_code, 401)
        self.assertContains(response, "Usuario o contraseña incorrectos", status_code=401)

    @override_settings(DEBUG=False)
    def test_mock_login_is_disabled_when_debug_is_off(self):
        response = self.client.get(reverse("login"))
        self.assertEqual(response.status_code, 404)

    def test_logout_requires_post_and_clears_session(self):
        self.client.post(
            reverse("login"),
            {"username": "demo", "password": "Demo123!"},
        )
        response = self.client.post(reverse("logout"))
        self.assertRedirects(response, reverse("login"))
        self.assertNotIn("_auth_user_id", self.client.session)


@override_settings(DEBUG=True, DEMO_LOGIN_ENABLED=True)
class DesignPreferenceTests(TestCase):
    def setUp(self):
        self.client.post(
            reverse("login"),
            {"username": "demo", "password": "Demo123!"},
        )

    def test_post_login_design_picker_displays_curated_themes(self):
        response = self.client.get(reverse("design_preferences"))
        self.assertContains(response, "Minimalista")
        self.assertContains(response, "Abstracto")
        self.assertContains(response, "Corporativo")

    def test_saving_theme_persists_and_redirects_to_app(self):
        response = self.client.post(
            reverse("design_preferences"),
            {"action": "save", "theme": "abstract"},
        )

        self.assertRedirects(
            response,
            reverse("index"),
            fetch_redirect_response=False,
        )
        preference = UserDesignPreference.objects.get(user__username="demo")
        self.assertEqual(preference.theme, "abstract")
        app_response = self.client.get(reverse("index"))
        self.assertContains(app_response, 'data-theme="abstract"')

    def test_invalid_theme_is_not_saved(self):
        response = self.client.post(
            reverse("design_preferences"),
            {"action": "save", "theme": "custom-css"},
        )

        self.assertEqual(response.status_code, 400)
        self.assertFalse(
            UserDesignPreference.objects.filter(user__username="demo", theme="custom-css").exists()
        )

    @override_settings(AI_PROVIDER="ollama")
    @patch("validador.views.recommend_design", return_value="corporate")
    def test_ai_recommendation_saves_theme_and_redirects_to_applied_design(
        self, recommend_design
    ):
        response = self.client.post(
            reverse("design_preferences"),
            {
                "action": "recommend",
                "design_description": "Formal y sobrio para finanzas",
            },
        )

        recommend_design.assert_called_once_with("Formal y sobrio para finanzas")
        self.assertRedirects(
            response,
            reverse("index"),
            fetch_redirect_response=False,
        )
        preference = UserDesignPreference.objects.get(user__username="demo")
        self.assertEqual(preference.theme, "corporate")

        app_response = self.client.get(reverse("index"))
        self.assertContains(app_response, 'data-theme="corporate"')

    @override_settings(OPENAI_API_KEY="test-key")
    @patch("validador.views.recommend_design")
    def test_ai_recommendation_requires_description_and_does_not_call_model(
        self, recommend_design
    ):
        response = self.client.post(
            reverse("design_preferences"),
            {"action": "recommend", "design_description": ""},
        )

        self.assertContains(response, "Describe brevemente", status_code=200)
        recommend_design.assert_not_called()
