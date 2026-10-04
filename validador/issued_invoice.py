from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import uuid


CENT = Decimal("0.01")
MAX_INVOICE_LINES = 100
MAX_AMOUNT = Decimal("999999999999999.99")
MAX_QUANTITY = Decimal("999999999.999")
TAX_RATES = {
    "13": Decimal("13"),
    "4": Decimal("4"),
    "2": Decimal("2"),
    "1": Decimal("1"),
    "0": Decimal("0"),
    "exempt": Decimal("0"),
}


class IssuedInvoiceDraftError(ValueError):
    pass


def _amount(value, field_label):
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise IssuedInvoiceDraftError(
            f"Revisa el importe de {field_label}."
        ) from exc
    if not amount.is_finite() or amount < 0 or amount > MAX_AMOUNT:
        raise IssuedInvoiceDraftError(
            f"El importe de {field_label} debe ser no negativo y no exceder "
            f"{MAX_AMOUNT}."
        )
    return amount.quantize(CENT, rounding=ROUND_HALF_UP)


def build_issued_invoice_draft(form_data, uploaded_lines):
    issuer_name = str(form_data.get("issuer_name") or "").strip()
    issuer_id = str(form_data.get("issuer_id") or "").strip()
    receiver_name = str(form_data.get("receiver_name") or "").strip()
    receiver_id = str(form_data.get("receiver_id") or "").strip()
    issue_date = str(form_data.get("issue_date") or "").strip()
    errors = []
    for label, value in (
        ("el nombre del emisor", issuer_name),
        ("la identificación del emisor", issuer_id),
        ("el nombre del receptor", receiver_name),
        ("la identificación del receptor", receiver_id),
    ):
        if not value:
            errors.append(f"Falta {label}.")
    try:
        parsed_date = date.fromisoformat(issue_date)
    except ValueError:
        parsed_date = None
    if parsed_date is None:
        errors.append("La fecha de emisión no es válida.")
    if any(len(value) > 250 for value in (issuer_name, receiver_name)):
        errors.append("El nombre del emisor o receptor supera 250 caracteres.")
    if any(len(value) > 100 for value in (issuer_id, receiver_id)):
        errors.append("La identificación ingresada supera 100 caracteres.")
    if not uploaded_lines:
        errors.append("Agrega al menos una línea de detalle.")
    if len(uploaded_lines) > MAX_INVOICE_LINES:
        errors.append(f"El máximo permitido es {MAX_INVOICE_LINES} líneas.")
    if errors:
        raise IssuedInvoiceDraftError(" ".join(errors))

    lines = []
    subtotal_total = Decimal("0")
    tax_total = Decimal("0")
    for line_number, uploaded_line in enumerate(uploaded_lines, start=1):
        description = str(uploaded_line.get("description") or "").strip()
        if not description:
            raise IssuedInvoiceDraftError(
                f"Falta la descripción del detalle en la línea {line_number}."
            )
        if len(description) > 250:
            raise IssuedInvoiceDraftError(
                f"La descripción de la línea {line_number} supera 250 caracteres."
            )
        try:
            quantity = Decimal(str(uploaded_line.get("quantity", "")))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise IssuedInvoiceDraftError(
                f"Revisa la cantidad de la línea {line_number}."
            ) from exc
        if (
            not quantity.is_finite()
            or quantity <= 0
            or quantity > MAX_QUANTITY
        ):
            raise IssuedInvoiceDraftError(
                f"La cantidad de la línea {line_number} debe ser mayor que cero "
                f"y no exceder {MAX_QUANTITY}."
            )
        quantity = quantity.quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)
        if quantity <= 0:
            raise IssuedInvoiceDraftError(
                f"La cantidad de la línea {line_number} es demasiado pequeña."
            )
        unit_price = _amount(
            uploaded_line.get("unit_price", ""),
            f"precio unitario de la línea {line_number}",
        )
        if unit_price and quantity > MAX_AMOUNT / unit_price:
            raise IssuedInvoiceDraftError(
                f"El total de la línea {line_number} supera el máximo "
                f"permitido de {MAX_AMOUNT}."
            )
        line_total = (quantity * unit_price).quantize(
            CENT,
            rounding=ROUND_HALF_UP,
        )
        tax_rate = str(uploaded_line.get("tax_rate") or "")
        if tax_rate not in TAX_RATES:
            raise IssuedInvoiceDraftError(
                f"Selecciona una tarifa de IVA válida para la línea {line_number}."
            )
        rate = TAX_RATES[tax_rate]
        tax_amount = (
            (line_total * rate / (Decimal("100") + rate)).quantize(
                CENT,
                rounding=ROUND_HALF_UP,
            )
            if rate
            else Decimal("0").quantize(CENT)
        )
        line_subtotal = line_total - tax_amount
        if line_total > MAX_AMOUNT:
            raise IssuedInvoiceDraftError(
                f"El total de la línea {line_number} supera el máximo permitido "
                f"de {MAX_AMOUNT}."
            )
        if subtotal_total + line_subtotal > MAX_AMOUNT:
            raise IssuedInvoiceDraftError(
                f"El subtotal acumulado supera el máximo permitido de {MAX_AMOUNT}."
            )
        if tax_total + tax_amount > MAX_AMOUNT:
            raise IssuedInvoiceDraftError(
                f"El impuesto acumulado supera el máximo permitido de {MAX_AMOUNT}."
            )
        if subtotal_total + line_subtotal + tax_total + tax_amount > MAX_AMOUNT:
            raise IssuedInvoiceDraftError(
                f"El total acumulado supera el máximo permitido de {MAX_AMOUNT}."
            )
        subtotal_total += line_subtotal
        tax_total += tax_amount
        lines.append(
            {
                "line_number": line_number,
                "description": description,
                "quantity": format(quantity, "f"),
                "unit_price_including_tax": format(unit_price, "f"),
                "subtotal": format(line_subtotal, "f"),
                "tax_rate": tax_rate,
                "tax_treatment": (
                    "exempt" if tax_rate == "exempt" else "taxable"
                ),
                "tax_amount": format(tax_amount, "f"),
                "total": format(line_total, "f"),
            }
        )

    invoice_total = subtotal_total + tax_total
    return {
        "draft_id": str(uuid.uuid4()),
        "schema_version": "0.2.0",
        "document_type": "factura_electronica",
        "document_type_label": "Factura electrónica",
        "status": "internal_draft",
        "currency": "CRC",
        "issue_date": parsed_date.isoformat(),
        "issuer": {"name": issuer_name, "identification": issuer_id},
        "receiver": {"name": receiver_name, "identification": receiver_id},
        "lines": lines,
        "totals": {
            "subtotal": format(subtotal_total, "f"),
            "tax": format(tax_total, "f"),
            "total": format(invoice_total, "f"),
        },
        "validation_scope": (
            "internal_required_fields_arithmetic_and_inclusive_tax_calculation"
        ),
        "not_official_format": True,
        "transmission": {
            "status": "not_configured",
            "real_hacienda_contacted": False,
        },
    }
