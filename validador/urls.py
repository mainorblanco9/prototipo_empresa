from django.urls import path

from . import api, app_views, invoice_import, pwa, views

urlpatterns = [
    path("api/v1/", api.ApiRootView.as_view(), name="api_root"),
    path("api/v1/auth/token/", api.TokenView.as_view(), name="api_token"),
    path(
        "api/v1/invoices/",
        api.IssuedInvoiceViewSet.as_view({"get": "list", "post": "create"}),
        name="api_invoice_list",
    ),
    path(
        "api/v1/invoices/summary/",
        api.IssuedInvoiceViewSet.as_view({"get": "summary"}),
        name="api_invoice_summary",
    ),
    path(
        "api/v1/invoices/<uuid:pk>/",
        api.IssuedInvoiceViewSet.as_view({"get": "retrieve", "put": "update"}),
        name="api_invoice_detail",
    ),
    path(
        "api/v1/invoices/<uuid:pk>/transition/",
        api.IssuedInvoiceViewSet.as_view({"post": "transition"}),
        name="api_invoice_transition",
    ),
    path(
        "api/v1/invoices/<uuid:pk>/history/",
        api.IssuedInvoiceViewSet.as_view({"get": "history"}),
        name="api_invoice_history",
    ),
    path(
        "api/v1/invoices/<uuid:pk>/queue/",
        api.IssuedInvoiceViewSet.as_view({"post": "queue"}),
        name="api_invoice_queue",
    ),
    path(
        "api/v1/invoices/<uuid:pk>/reception-payload/",
        api.IssuedInvoiceViewSet.as_view({"get": "reception_payload"}),
        name="api_invoice_reception_payload",
    ),
    path(
        "api/v1/invoices/<uuid:pk>/hacienda-result/",
        api.IssuedInvoiceViewSet.as_view({"post": "hacienda_result"}),
        name="api_invoice_hacienda_result",
    ),
    path(
        "api/v1/invoices/<uuid:pk>/submit/",
        api.IssuedInvoiceViewSet.as_view({"post": "submit"}),
        name="api_invoice_submit",
    ),
    path(
        "api/v1/invoices/<uuid:pk>/ai-analysis/",
        api.IssuedInvoiceViewSet.as_view({"post": "ai_analysis"}),
        name="api_invoice_ai_analysis",
    ),
    path("api/v1/ai/review-draft/", api.DraftReviewView.as_view(), name="api_ai_review_draft"),
    path("facturas/nueva/", views.invoice_wizard, name="invoice_wizard"),
    path("integraciones/", views.integrations, name="integrations"),
    path("api/v1/invoices/import/", invoice_import.InvoiceImportView.as_view(), name="api_invoice_import"),
    path("panel/", app_views.dashboard, name="dashboard"),
    path("cierres/", app_views.closures, name="closures"),
    path("bitacora/", app_views.activity_log, name="activity_log"),
    path("facturas/importar/", app_views.import_invoices, name="import_invoices"),
    path("facturas/importar/plantilla.csv", app_views.import_template, name="import_template"),
    path("manifest.webmanifest", pwa.manifest, name="pwa_manifest"),
    path("sw.js", pwa.service_worker, name="pwa_service_worker"),
    path("icon.svg", pwa.icon, name="pwa_icon"),
    path("offline/", pwa.offline, name="pwa_offline"),
    path("login/", views.login_view, name="login"),
    path("logout/", views.logout_view, name="logout"),
    path("diseno/", views.design_preferences, name="design_preferences"),
    path("facturas/", views.invoice_workspace, name="invoice_workspace"),
    path(
        "facturas/ejemplo/descargar/",
        views.download_sample_invoices,
        name="download_sample_invoices",
    ),
    path(
        "facturas/emitidas/factura-electronica/",
        views.issued_invoice_draft,
        name="issued_invoice_draft",
    ),
    path(
        "facturas/emitidas/factura-electronica/descargar/",
        views.download_issued_invoice_draft,
        name="download_issued_invoice_draft",
    ),
    path(
        "facturas/modelo/",
        views.invoice_model_workspace,
        name="invoice_model_workspace",
    ),
    path(
        "facturas/modelo/descargar/",
        views.download_invoice_model,
        name="download_invoice_model",
    ),
    path("facturas/movil/", views.mobile_capture, name="mobile_capture"),
    path("facturas/emitidas/", views.issued_invoice_list, name="issued_invoice_list"),
    path("facturas/bitacora/", views.invoice_audit, name="invoice_audit"),
    path("api/invoices/parse/", views.parse_invoice_documents, name="parse_invoice_documents"),
    path(
        "api/invoices/local-simulation/",
        views.local_invoice_simulation,
        name="local_invoice_simulation",
    ),
    path(
        "api/invoices/mock-submit/",
        views.local_invoice_simulation,
        name="mock_hacienda_submission",
    ),
    path("", views.index, name="index"),
    path("reportes/<uuid:report_id>.csv", views.download_report, name="download_report"),
]
