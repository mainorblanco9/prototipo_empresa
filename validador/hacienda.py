"""Clave numérica y XML de factura electrónica (esquema v4.4 de Hacienda CR).

El XML que se genera NO está firmado (XAdES-EPES con certificado .p12 del
contribuyente) ni usa códigos CABYS reales; sirve para el flujo local y de
pruebas contra el simulador de recepción.
"""
import base64
import re
import secrets
import xml.etree.ElementTree as ET
from decimal import Decimal

from django.utils import timezone

NAMESPACE = (
    "https://cdn.comprobanteselectronicos.go.cr/xml-schemas/v4.4/facturaElectronica"
)
DOCUMENT_TYPE_CODE = "01"  # Factura electrónica
BRANCH = "001"
TERMINAL = "00001"
NORMAL_SITUATION = "1"
PLACEHOLDER_CABYS = "0000000000000"

# Código de tarifa de IVA (nota 8 de los anexos del XML v4.4).
RATE_CODES = {"13": "08", "4": "04", "2": "02", "1": "01", "0": "10"}


class HaciendaDocumentError(ValueError):
    pass


def digits_only(value):
    return re.sub(r"\D", "", str(value or ""))


def identification(value):
    number = digits_only(value)
    if not number or len(number) > 12:
        raise HaciendaDocumentError(
            "La identificación debe tener entre 1 y 12 dígitos para generar la clave."
        )
    return {"tipo": "01" if len(number) == 9 else "02", "numero": number}


def build_consecutive(sequence):
    # sucursal(3) + terminal(5) + tipo de comprobante(2) + secuencia(10) = 20
    return f"{BRANCH}{TERMINAL}{DOCUMENT_TYPE_CODE}{int(sequence):010d}"


def build_clave(issue_date, issuer_number, consecutive, security_code=None):
    """506 + ddmmyy + cédula(12) + consecutivo(20) + situación(1) + código(8)."""
    code = security_code or f"{secrets.randbelow(10**8):08d}"
    clave = (
        "506"
        + issue_date.strftime("%d%m%y")
        + digits_only(issuer_number).zfill(12)
        + consecutive
        + NORMAL_SITUATION
        + code
    )
    if len(clave) != 50 or not clave.isdigit():
        raise HaciendaDocumentError("No se pudo construir una clave de 50 dígitos.")
    return clave


def build_xml(invoice):
    document = invoice.document
    ET.register_namespace("", NAMESPACE)
    root = ET.Element(f"{{{NAMESPACE}}}FacturaElectronica")

    def q(tag):
        return f"{{{NAMESPACE}}}{tag}"

    def sub(parent, tag, value=None):
        node = ET.SubElement(parent, q(tag))
        if value is not None:
            node.text = str(value)
        return node

    def party(tag, data):
        ident = identification(data["identification"])
        node = sub(root, tag)
        sub(node, "Nombre", data["name"])
        id_node = sub(node, "Identificacion")
        sub(id_node, "Tipo", ident["tipo"])
        sub(id_node, "Numero", ident["numero"])

    emitted = timezone.localtime(invoice.created_at).replace(microsecond=0)
    sub(root, "Clave", invoice.clave)
    sub(root, "NumeroConsecutivo", invoice.clave[21:41])
    sub(root, "FechaEmision", emitted.isoformat())
    party("Emisor", document["issuer"])
    party("Receptor", document["receiver"])
    sub(root, "CondicionVenta", "01")
    detail = sub(root, "DetalleServicio")
    for line in document["lines"]:
        node = sub(detail, "LineaDetalle")
        sub(node, "NumeroLinea", line["line_number"])
        sub(node, "CodigoCABYS", PLACEHOLDER_CABYS)
        sub(node, "Cantidad", line["quantity"])
        sub(node, "UnidadMedida", "Unid")
        sub(node, "Detalle", line["description"])
        quantity = Decimal(line["quantity"])
        subtotal = Decimal(line["subtotal"])
        sub(node, "PrecioUnitario", f"{(subtotal / quantity):.5f}")
        sub(node, "MontoTotal", line["subtotal"])
        sub(node, "SubTotal", line["subtotal"])
        sub(node, "BaseImponible", line["subtotal"])
        tax = sub(node, "Impuesto")
        sub(tax, "Codigo", "01")
        sub(tax, "CodigoTarifaIVA", RATE_CODES.get(line["tax_rate"], "10"))
        sub(tax, "Tarifa", line["tax_rate"] if line["tax_rate"] != "exempt" else "0")
        sub(tax, "Monto", line["tax_amount"])
        sub(node, "ImpuestoNeto", line["tax_amount"])
        sub(node, "MontoTotalLinea", line["total"])
    summary = sub(root, "ResumenFactura")
    currency = sub(summary, "CodigoTipoMoneda")
    sub(currency, "CodigoMoneda", "CRC")
    sub(currency, "TipoCambio", "1")
    totals = document["totals"]
    sub(summary, "TotalVenta", totals["subtotal"])
    sub(summary, "TotalVentaNeta", totals["subtotal"])
    sub(summary, "TotalImpuesto", totals["tax"])
    sub(summary, "TotalComprobante", totals["total"])
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def build_reception_payload(invoice):
    """Cuerpo de POST /recepcion según la API de recepción de Hacienda."""
    document = invoice.document
    emitted = timezone.localtime(invoice.created_at).replace(microsecond=0)
    issuer = identification(document["issuer"]["identification"])
    receiver = identification(document["receiver"]["identification"])
    return {
        "clave": invoice.clave,
        "fecha": emitted.isoformat(),
        "emisor": {
            "tipoIdentificacion": issuer["tipo"],
            "numeroIdentificacion": issuer["numero"],
        },
        "receptor": {
            "tipoIdentificacion": receiver["tipo"],
            "numeroIdentificacion": receiver["numero"],
        },
        "comprobanteXml": base64.b64encode(build_xml(invoice)).decode("ascii"),
    }
