from django.urls import path

from . import api, pwa, views

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
