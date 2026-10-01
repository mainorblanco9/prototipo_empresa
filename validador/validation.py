import csv
import io
import re
import unicodedata
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from openpyxl import load_workbook

MAX_ROWS = 20_000
REQUIRED_FIELDS = (
    "fecha",
    "proveedor",
    "numero_factura",
    "moneda",
    "subtotal",
    "impuesto",
    "total",
)
HEADER_ALIASES = {
    "date": "fecha",
    "fecha": "fecha",
    "provider": "proveedor",
    "vendor": "proveedor",
    "proveedor": "proveedor",
    "invoice_number": "numero_factura",
    "invoice_no": "numero_factura",
    "numero_factura": "numero_factura",
    "factura": "numero_factura",
    "currency": "moneda",
    "moneda": "moneda",
    "subtotal": "subtotal",
    "tax": "impuesto",
    "impuesto": "impuesto",
    "total": "total",
}
OUTPUT_FIELDS = ("estado", "errores")


class FileValidationError(ValueError):
    pass


def _normalize_header(value):
    text = unicodedata.normalize("NFKD", str(value or "").strip().lower())
    text = "".join(char for char in text if not unicodedata.combining(char))
    text = re.sub(r"[\s-]+", "_", text)
    return HEADER_ALIASES.get(text, text)


def _read_rows(upload):
    suffix = upload.name.rsplit(".", 1)[-1].lower() if "." in upload.name else ""
    if suffix in ("csv", "tsv"):
        try:
            text = upload.read().decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise FileValidationError("El CSV debe estar codificado como UTF-8.") from exc
        sample = text[:8192]
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
        except csv.Error:
            dialect = csv.excel_tab if suffix == "tsv" else csv.excel
        reader = csv.reader(io.StringIO(text), dialect)
        first_row = next(reader, None)
        if not first_row:
            raise FileValidationError("El CSV está vacío o no tiene encabezados.")
        headers = [_normalize_header(header) for header in first_row]
        rows = [
            dict(zip(headers, row))
            for row in reader
            if any(value for value in row)
        ]
        return headers, rows

    if suffix == "xlsx":
        workbook = load_workbook(upload, read_only=True, data_only=True)
        try:
            worksheet = workbook.active
            values = worksheet.iter_rows(values_only=True)
            first_row = next(values, None)
            if not first_row:
                raise FileValidationError("El Excel está vacío o no tiene encabezados.")
            headers = [_normalize_header(value) for value in first_row]
            rows = [
                dict(zip(headers, row))
                for row in values
                if any(value is not None for value in row)
            ]
            return headers, rows
        finally:
            workbook.close()

    raise FileValidationError("Formato no admitido. Sube un archivo .csv, .tsv o .xlsx.")


def _parse_date(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    for date_format in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(text, date_format).date()
        except ValueError:
            pass
    return None


def _parse_decimal(value):
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float, Decimal)):
        try:
            return Decimal(str(value))
        except InvalidOperation:
            return None
    text = str(value).strip().replace(" ", "")
    if "," in text and "." not in text:
        text = text.replace(",", ".")
    try:
        amount = Decimal(text)
        return amount if amount.is_finite() else None
    except InvalidOperation:
        return None


def validate_upload(upload):
    headers, rows = _read_rows(upload)
    missing = [field for field in REQUIRED_FIELDS if field not in headers]
    if missing:
        raise FileValidationError(
            "Faltan columnas requeridas: " + ", ".join(missing)
        )
    if len(rows) > MAX_ROWS:
        raise FileValidationError(
            f"El archivo tiene más de {MAX_ROWS:,} filas; divídelo antes de procesarlo."
        )

    seen_invoices = set()
    reported_rows = []
    issue_counts = {}

    for row_number, original in enumerate(rows, start=2):
        row = {
            field: original.get(field)
            for field in REQUIRED_FIELDS
        }
        errors = []

        if not str(row["proveedor"] or "").strip():
            errors.append("Falta proveedor")
        invoice_number = str(row["numero_factura"] or "").strip()
        if not invoice_number:
            errors.append("Falta número de factura")
        else:
            invoice_key = (
                str(row["proveedor"] or "").strip().casefold(),
                invoice_number.casefold(),
            )
            if invoice_key in seen_invoices:
                errors.append("Factura duplicada para el proveedor")
            seen_invoices.add(invoice_key)

        if _parse_date(row["fecha"]) is None:
            errors.append("Fecha inválida")
        if not re.fullmatch(r"[A-Za-z]{3}", str(row["moneda"] or "").strip()):
            errors.append("Moneda inválida (usa código de 3 letras, por ejemplo MXN)")

        amounts = {}
        for field in ("subtotal", "impuesto", "total"):
            amount = _parse_decimal(row[field])
            if amount is None:
                errors.append(f"{field.capitalize()} inválido")
            elif amount < 0:
                errors.append(f"{field.capitalize()} no puede ser negativo")
            else:
                amounts[field] = amount
        if len(amounts) == 3 and abs(
            amounts["subtotal"] + amounts["impuesto"] - amounts["total"]
        ) > Decimal("0.01"):
            errors.append("Total no coincide con subtotal + impuesto")

        for error in errors:
            issue_counts[error] = issue_counts.get(error, 0) + 1
        reported_rows.append(
            {
                **row,
                "fila": row_number,
                "estado": "Revisar" if errors else "Correcto",
                "errores": "; ".join(errors),
            }
        )

    return reported_rows, issue_counts


def build_csv_report(rows):
    output = io.StringIO(newline="")
    fields = ["fila", *REQUIRED_FIELDS, *OUTPUT_FIELDS]
    writer = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        safe_row = {
            field: (
                "'" + value
                if isinstance(value, str) and value[:1] in ("=", "+", "-", "@", "\t", "\r")
                else value
            )
            for field, value in row.items()
        }
        writer.writerow(safe_row)
    return "\ufeff" + output.getvalue()
