"""Especificación OpenAPI 3.0 de la API pública de GuanaBill (publicada en /api/v1/openapi.json)."""

from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET

STATUSES = ["draft", "ready", "queued", "processing", "accepted", "rejected", "cancelled"]
TAX_RATES = ["13", "4", "2", "1", "0", "exempt"]


def _ref(name):
    return {"$ref": f"#/components/schemas/{name}"}


def _json(schema, description="OK"):
    return {"description": description, "content": {"application/json": {"schema": schema}}}


def _err(description):
    return _json(_ref("Error"), description)


def _id_param():
    return {"name": "id", "in": "path", "required": True, "schema": {"type": "string", "format": "uuid"}}


def _op(summary, description, tag, responses, body=None, path_id=False, query=None, operation_id=None):
    op = {"summary": summary, "description": description, "tags": [tag], "responses": responses}
    if operation_id:
        op["operationId"] = operation_id
    params = ([_id_param()] if path_id else []) + (query or [])
    if params:
        op["parameters"] = params
    if body:
        op["requestBody"] = {"required": True, "content": body}
    return op


def _json_body(schema_name, example=None):
    content = {"schema": _ref(schema_name)}
    if example:
        content["example"] = example
    return {"application/json": content}


INVOICE_EXAMPLE = {
    "client_id": "erp-000123",
    "issuer_name": "Comercial Guanacaste S.A.",
    "issuer_id": "3101123456",
    "receiver_name": "Cliente de Prueba S.A.",
    "receiver_id": "3101654321",
    "issue_date": "2026-01-15",
    "lines": [{"description": "Servicio de consultoría", "quantity": "1", "unit_price": "113000", "tax_rate": "13"}],
}


def build_spec(server_url="http://127.0.0.1:8001"):
    inv = "Facturas"
    flow = "Envío a Hacienda"
    ai = "Inteligencia artificial"
    imp = "Importación"
    paths = {
        "/api/v1/": {
            "get": _op(
                "Estado de la API",
                "Devuelve versión, moneda y si la integración oficial está activa.",
                "General",
                {"200": _json({"type": "object", "properties": {"version": {"type": "string"}, "currency": {"type": "string"}}})},
                operation_id="apiRoot",
            )
        },
        "/api/v1/auth/token/": {
            "post": _op(
                "Obtener token de sesión",
                "Alternativa a las API keys para apps móviles. Use `Authorization: Token <token>`.",
                "General",
                {"200": _json({"type": "object", "properties": {"token": {"type": "string"}}}), "400": _err("Credenciales inválidas")},
                body=_json_body("Credentials"),
                operation_id="obtainToken",
            )
        },
        "/api/v1/invoices/": {
            "get": _op(
                "Listar facturas",
                "Facturas del usuario, con filtros opcionales.",
                inv,
                {"200": _json(_ref("InvoicePage")), "401": _err("Sin autenticación")},
                query=[
                    {"name": "status", "in": "query", "schema": {"type": "string", "enum": STATUSES}},
                    {"name": "month", "in": "query", "description": "AAAA-MM", "schema": {"type": "string", "example": "2026-01"}},
                    {"name": "updated_since", "in": "query", "description": "ISO 8601; solo facturas modificadas después", "schema": {"type": "string", "format": "date-time"}},
                    {"name": "page", "in": "query", "schema": {"type": "integer", "minimum": 1}},
                ],
                operation_id="listInvoices",
            ),
            "post": _op(
                "Crear una factura",
                "Crea un borrador. Reenviar el mismo `client_id` es idempotente y devuelve la factura existente. "
                "Los precios incluyen IVA.",
                inv,
                {
                    "201": _json(_ref("InvoiceDetail"), "Creada"),
                    "200": _json(_ref("InvoiceDetail"), "Ya existía (idempotente)"),
                    "400": _err("Datos inválidos"),
                    "409": _err("El periodo está cerrado"),
                },
                body=_json_body("InvoiceInput", INVOICE_EXAMPLE),
                operation_id="createInvoice",
            ),
        },
        "/api/v1/invoices/summary/": {
            "get": _op(
                "Resumen por estado",
                "Conteos y totales por estado (excluye anuladas).",
                inv,
                {"200": _json(_ref("Summary"))},
                query=[{"name": "month", "in": "query", "schema": {"type": "string", "example": "2026-01"}}],
                operation_id="invoiceSummary",
            )
        },
        "/api/v1/invoices/{id}/": {
            "get": _op("Consultar una factura", "Estado, clave, documento y respuesta de Hacienda.", inv,
                       {"200": _json(_ref("InvoiceDetail")), "404": _err("No existe")}, path_id=True, operation_id="getInvoice"),
            "put": _op("Actualizar un borrador", "Requiere `version` para control de concurrencia.", inv,
                       {"200": _json(_ref("InvoiceDetail")), "409": _err("Versión desactualizada o no editable")},
                       body=_json_body("InvoiceInput"), path_id=True, operation_id="updateInvoice"),
        },
        "/api/v1/invoices/{id}/transition/": {
            "post": _op("Cambiar estado manual", "Permite pasar a `draft`, `ready` o `cancelled`.", inv,
                        {"200": _json(_ref("InvoiceDetail")), "409": _err("Transición no permitida")},
                        body=_json_body("Transition", {"to": "ready", "version": 1}), path_id=True, operation_id="transitionInvoice"),
        },
        "/api/v1/invoices/{id}/history/": {
            "get": _op("Historial de estados", "Bitácora de cambios de la factura.", inv,
                       {"200": _json({"type": "array", "items": _ref("StatusEvent")})}, path_id=True, operation_id="invoiceHistory"),
        },
        "/api/v1/invoices/{id}/validate/": {
            "post": _op("Validar localmente", "Valida emisor, receptor, fechas y montos sin enviar nada a Hacienda.", flow,
                        {"200": _json(_ref("ValidationResult"), "`valid` indica si pasó; `issues` lista las observaciones")},
                        path_id=True, operation_id="validateInvoice"),
        },
        "/api/v1/invoices/{id}/queue/": {
            "post": _op("Encolar para envío", "Valida, asigna consecutivo y clave y deja la factura en `queued`. Requiere estado `ready`.", flow,
                        {"200": _json(_ref("InvoiceDetail")), "409": _err("No está lista o cambió de versión"), "422": _json(_ref("ValidationResult"), "No pasó la validación local")},
                        body=_json_body("Queue", {"version": 2}), path_id=True, operation_id="queueInvoice"),
        },
        "/api/v1/invoices/{id}/reception-payload/": {
            "get": _op("Payload de recepción", "Clave, consecutivo y XML codificado que se envía a Hacienda (sin firma XAdES).", flow,
                       {"200": _json({"type": "object"})}, path_id=True, operation_id="receptionPayload"),
        },
        "/api/v1/invoices/{id}/submit/": {
            "post": _op("Enviar a Hacienda", "Pasa por el gateway Node.js hacia el simulador local y actualiza el estado.", flow,
                        {"200": _json(_ref("SubmitResult")), "409": _err("Estado no enviable"), "422": _json(_ref("ValidationResult"), "No pasó la validación local"),
                         "502": _err("El gateway respondió con error"), "503": _err("Gateway no disponible")}, path_id=True, operation_id="submitInvoice"),
        },
        "/api/v1/invoices/{id}/hacienda-result/": {
            "post": _op("Registrar respuesta de Hacienda", "Uso del gateway: `procesando`, `aceptado` o `rechazado`.", flow,
                        {"200": _json(_ref("InvoiceDetail"))}, body=_json_body("HaciendaResult", {"state": "aceptado", "message": ""}),
                        path_id=True, operation_id="haciendaResult"),
        },
        "/api/v1/invoices/{id}/ai-analysis/": {
            "post": _op("Análisis por IA de la respuesta", "Explica con Ollama por qué fue aceptada o rechazada.", ai,
                        {"200": _json({"type": "object", "properties": {"analysis": {"type": "string"}, "ai_used": {"type": "boolean"}}}), "409": _err("Aún no hay respuesta de Hacienda")},
                        path_id=True, operation_id="aiAnalysis"),
        },
        "/api/v1/ai/review-draft/": {
            "post": _op("Revisar un borrador", "Costo exacto y consejos de IA sin guardar la factura.", ai,
                        {"200": _json({"type": "object"})}, body=_json_body("InvoiceInput"), operation_id="reviewDraft"),
        },
        "/api/v1/invoices/import/": {
            "post": _op(
                "Importar Excel/CSV",
                "Una fila por línea, agrupadas por número de factura. Con `confirm=true` crea las facturas válidas; "
                "sin él solo valida (vista previa). Máximo 2 MB y 2000 filas.",
                imp,
                {"200": _json(_ref("ImportResult")), "400": _err("Archivo inválido")},
                body={"multipart/form-data": {"schema": {
                    "type": "object", "required": ["file"],
                    "properties": {"file": {"type": "string", "format": "binary"}, "confirm": {"type": "boolean"}},
                }}},
                operation_id="importInvoices",
            )
        },
    }
    schemas = {
        "Error": {"type": "object", "properties": {"detail": {"type": "string"}}},
        "Credentials": {"type": "object", "required": ["username", "password"],
                        "properties": {"username": {"type": "string"}, "password": {"type": "string", "format": "password"}}},
        "Line": {"type": "object", "required": ["description", "quantity", "unit_price", "tax_rate"], "properties": {
            "description": {"type": "string", "maxLength": 250},
            "quantity": {"type": "string", "example": "1"},
            "unit_price": {"type": "string", "description": "Precio unitario con IVA incluido", "example": "113000"},
            "tax_rate": {"type": "string", "enum": TAX_RATES},
        }},
        "InvoiceInput": {"type": "object", "required": ["issuer_name", "issuer_id", "receiver_name", "receiver_id", "issue_date", "lines"], "properties": {
            "client_id": {"type": "string", "description": "Identificador del ERP; hace la creación idempotente"},
            "issuer_name": {"type": "string"},
            "issuer_id": {"type": "string", "description": "Cédula física (9), jurídica (10) o DIMEX/NITE (11–12 dígitos)"},
            "receiver_name": {"type": "string"},
            "receiver_id": {"type": "string"},
            "issue_date": {"type": "string", "format": "date"},
            "lines": {"type": "array", "minItems": 1, "items": _ref("Line")},
            "version": {"type": "integer", "minimum": 1},
        }},
        "InvoiceSummary": {"type": "object", "properties": {
            "id": {"type": "string", "format": "uuid"}, "client_id": {"type": "string"},
            "document_type": {"type": "string", "example": "factura_electronica"},
            "status": {"type": "string", "enum": STATUSES}, "status_label": {"type": "string"},
            "currency": {"type": "string", "example": "CRC"}, "issue_date": {"type": "string", "format": "date"},
            "receiver_name": {"type": "string"}, "subtotal": {"type": "string"}, "tax_total": {"type": "string"},
            "total": {"type": "string"}, "clave": {"type": "string", "description": "Clave numérica de 50 dígitos"},
            "hacienda_message": {"type": "string"}, "ai_analysis": {"type": "string"},
            "version": {"type": "integer"}, "created_at": {"type": "string", "format": "date-time"},
            "updated_at": {"type": "string", "format": "date-time"},
        }},
        "InvoiceDetail": {"allOf": [_ref("InvoiceSummary"), {"type": "object", "properties": {"document": {"type": "object"}}}]},
        "Transition": {"type": "object", "required": ["to", "version"], "properties": {
            "to": {"type": "string", "enum": ["draft", "ready", "cancelled"]}, "version": {"type": "integer"}}},
        "Queue": {"type": "object", "required": ["version"], "properties": {"version": {"type": "integer"}}},
        "HaciendaResult": {"type": "object", "required": ["state"], "properties": {
            "state": {"type": "string", "enum": ["procesando", "aceptado", "rechazado"]}, "message": {"type": "string"}}},
        "StatusEvent": {"type": "object", "properties": {
            "from_status": {"type": "string"}, "to_status": {"type": "string"}, "created_at": {"type": "string", "format": "date-time"}}},
        "InvoicePage": {"type": "object", "properties": {
            "count": {"type": "integer"}, "next": {"type": "string", "nullable": True}, "previous": {"type": "string", "nullable": True},
            "results": {"type": "array", "items": _ref("InvoiceSummary")}}},
        "Summary": {"type": "object", "properties": {
            "currency": {"type": "string"}, "month": {"type": "string", "nullable": True},
            "by_status": {"type": "object", "additionalProperties": {"type": "object", "properties": {"count": {"type": "integer"}, "total": {"type": "string"}}}},
            "totals": {"type": "object", "properties": {"count": {"type": "integer"}, "subtotal": {"type": "string"}, "tax": {"type": "string"}, "total": {"type": "string"}}}}},
        "SubmitResult": {"type": "object", "properties": {"gateway": {"type": "object"}, "invoice": _ref("InvoiceDetail")}},
        "ValidationResult": {"type": "object", "properties": {
            "valid": {"type": "boolean"},
            "detail": {"type": "string"},
            "issues": {"type": "array", "items": {"type": "object", "properties": {"field": {"type": "string"}, "message": {"type": "string"}}}},
        }},
        "ImportResult": {"type": "object", "properties": {
            "confirmed": {"type": "boolean"}, "total": {"type": "integer"}, "valid": {"type": "integer"},
            "with_errors": {"type": "integer"}, "created": {"type": "integer"},
            "results": {"type": "array", "items": {"type": "object", "properties": {
                "factura": {"type": "string"}, "lines": {"type": "integer"}, "total": {"type": "string"},
                "receiver": {"type": "string"}, "status": {"type": "string", "enum": ["valid", "error", "created", "exists"]},
                "errors": {"type": "array", "items": {"type": "string"}}}}},
        }},
    }
    return {
        "openapi": "3.0.3",
        "info": {
            "title": "GuanaBill API",
            "version": "1.0.0",
            "description": (
                "API de facturación electrónica para Costa Rica. En el entorno local las facturas se validan y se "
                "envían al **simulador de Hacienda (ATV)**; la firma XAdES y el CABYS real quedan para producción.\n\n"
                "Flujo: crear → `validate` → `transition` a `ready` → `submit` → consultar estado y análisis de IA."
            ),
        },
        "servers": [{"url": server_url, "description": "Entorno local"}],
        "tags": [{"name": n} for n in ("General", inv, flow, imp, ai)],
        "security": [{"ApiKey": []}],
        "paths": paths,
        "components": {
            "securitySchemes": {
                "ApiKey": {"type": "http", "scheme": "bearer", "bearerFormat": "gb_…", "description": "API key creada en Integraciones: `Authorization: Bearer gb_...`"},
                "Token": {"type": "apiKey", "in": "header", "name": "Authorization", "description": "`Token <token>` de /auth/token/"},
            },
            "schemas": schemas,
        },
    }


@require_GET
def openapi_json(request):
    spec = build_spec(f"{request.scheme}://{request.get_host()}")
    return JsonResponse(spec, json_dumps_params={"ensure_ascii": False})


@login_required
@require_GET
def api_docs(request):
    return render(request, "validador/api_docs.html", {"spec_url": "/api/v1/openapi.json"})
