import json
import re
from collections import Counter
from datetime import date, datetime
from decimal import Decimal

from django.conf import settings

from .ai_client import AIProviderError, generate_text
from .validation import (
    MAX_ROWS,
    FileValidationError,
    _normalize_header,
    _parse_date,
    _parse_decimal,
    _read_rows,
)

MAX_COLUMNS = 100
MAX_CHART_CATEGORIES = 12
CHART_TYPES = {"bar", "line", "pie"}
AGGREGATIONS = {"sum", "average", "count"}
IDENTIFIER_HEADER = re.compile(
    r"(^|_)(id|codigo|code|folio|factura|invoice|telefono|phone|postal|zip|"
    r"cuenta|account|numero|number|clave)(_|$)",
    re.IGNORECASE,
)


def _display_value(value):
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.isoformat(sep=" ", timespec="minutes")
    if isinstance(value, date):
        return value.isoformat()
    return str(value).strip()


def _column_metadata(headers, rows):
    metadata = []
    for index, header in enumerate(headers):
        values = [
            row.get(header)
            for row in rows
            if row.get(header) not in (None, "")
        ]
        sample = values[:100]
        date_count = sum(_parse_date(value) is not None for value in sample)
        number_count = sum(_parse_decimal(value) is not None for value in sample)
        if sample and date_count / len(sample) >= 0.8:
            column_type = "date"
        elif (
            sample
            and number_count / len(sample) >= 0.8
            and not IDENTIFIER_HEADER.search(header)
        ):
            column_type = "number"
        else:
            distinct_count = len({_display_value(value) for value in sample})
            column_type = "category" if distinct_count <= 30 else "text"
        metadata.append(
            {
                "index": index,
                "name": header,
                "type": column_type,
                "non_empty": len(values),
                "distinct_sample": len({_display_value(value) for value in sample}),
            }
        )
    return metadata


def _parse_recommendation(response, columns):
    match = re.search(r"\{.*\}", response, flags=re.DOTALL)
    if not match:
        raise ValueError("La recomendación no contiene un objeto JSON válido.")
    try:
        recommendation = json.loads(match.group())
    except json.JSONDecodeError as exc:
        raise ValueError("La recomendación no contiene un objeto JSON válido.") from exc

    if not isinstance(recommendation, dict):
        raise ValueError("El formato de recomendación de gráfico no es válido.")
    x_index = recommendation.get("x_index")
    y_index = recommendation.get("y_index")
    aggregation = recommendation.get("aggregation")
    if (
        isinstance(x_index, bool)
        or not isinstance(x_index, int)
        or x_index < -1
        or x_index >= len(columns)
    ):
        raise ValueError("La IA eligió una columna X que no existe.")
    if (
        isinstance(y_index, bool)
        or not isinstance(y_index, int)
        or y_index < -1
        or y_index >= len(columns)
    ):
        raise ValueError("La IA eligió una columna Y que no existe.")
    if aggregation not in AGGREGATIONS:
        raise ValueError("La IA eligió una agregación no admitida.")
    if y_index >= 0 and columns[y_index]["type"] != "number":
        raise ValueError("La IA eligió como medida una columna que no es numérica.")
    if y_index == -1 and aggregation != "count":
        raise ValueError("Para contar registros la agregación debe ser count.")
    if x_index == -1 and y_index == -1:
        raise ValueError("El gráfico requiere por lo menos una columna.")
    return {
        "x_index": x_index,
        "y_index": y_index,
        "aggregation": aggregation,
    }


def _fallback_recommendation(columns):
    numeric_indices = [
        column["index"] for column in columns if column["type"] == "number"
    ]
    category_indices = [
        column["index"]
        for column in columns
        if column["type"] in ("category", "date")
    ]
    if not category_indices:
        category_indices = [
            column["index"] for column in columns if column["type"] == "text"
        ]
    x_index = category_indices[0] if category_indices else -1
    if numeric_indices:
        return {
            "x_index": x_index,
            "y_index": numeric_indices[0],
            "aggregation": "sum",
        }
    if x_index >= 0:
        return {"x_index": x_index, "y_index": -1, "aggregation": "count"}
    return {"x_index": -1, "y_index": -1, "aggregation": "count"}


def _recommend_chart(columns):
    schema = [
        {
            "index": column["index"],
            "name": column["name"],
            "type": column["type"],
            "non_empty": column["non_empty"],
            "distinct_sample": column["distinct_sample"],
        }
        for column in columns
    ]
    response = generate_text(
        system_prompt=(
            "Eres un analista de datos. Selecciona columnas para un gráfico "
            "horizontal. Recibirás solo nombres de columnas y tipos inferidos, "
            "no valores de celdas. Responde únicamente JSON con x_index, y_index "
            "y aggregation. Usa índices enteros desde cero; usa -1 en x_index "
            "para agrupar por fila y -1 en y_index para contar registros. "
            "aggregation debe ser sum, average o count. Elige una dimensión "
            "categoría/fecha y una medida numérica cuando sea posible. "
            "No inventes índices ni añadas otros campos."
        ),
        user_prompt=json.dumps({"columns": schema}, ensure_ascii=False),
        max_tokens=100,
    )
    return _parse_recommendation(response, columns)


def _make_chart(headers, rows, columns, recommendation):
    if not headers or not rows:
        return None

    x_index = recommendation["x_index"]
    y_index = recommendation["y_index"]
    aggregation = recommendation["aggregation"]
    x_header = headers[x_index] if x_index >= 0 else "Fila"
    y_header = headers[y_index] if y_index >= 0 else "Cantidad de registros"
    grouped = {}
    for row_number, row in enumerate(rows, start=1):
        x_value = _display_value(row.get(x_header)) if x_index >= 0 else str(row_number)
        if not x_value:
            x_value = "(Sin dato)"
        if aggregation == "count" or y_index < 0:
            measure = 1
        else:
            parsed_measure = _parse_decimal(row.get(y_header))
            if parsed_measure is None:
                continue
            measure = float(parsed_measure)
        grouped.setdefault(x_value, []).append(measure)

    points = []
    for label, values in grouped.items():
        if aggregation == "average":
            amount = sum(values) / len(values)
        elif aggregation == "count":
            amount = len(values)
        else:
            amount = sum(values)
        points.append({"label": label, "value": round(amount, 2)})
    points.sort(key=lambda point: point["value"], reverse=True)
    if len(points) > MAX_CHART_CATEGORIES:
        remainder = points[MAX_CHART_CATEGORIES - 1 :]
        points = points[: MAX_CHART_CATEGORIES - 1]
        points.append(
            {
                "label": "Otros",
                "value": round(sum(point["value"] for point in remainder), 2),
            }
        )
    if not points:
        return None

    maximum = max(abs(point["value"]) for point in points) or 1
    for point in points:
        point["width"] = round(abs(point["value"]) / maximum * 100, 1)
    measure_label = {
        "average": f"Promedio de {y_header}",
        "count": "Cantidad de registros",
        "sum": f"Suma de {y_header}",
    }[aggregation]
    return {
        "title": f"{measure_label} por {x_header}",
        "x_label": x_header,
        "y_label": measure_label,
        "points": points,
        "recommendation": recommendation,
    }


def analyze_table(upload):
    suffix = upload.name.rsplit(".", 1)[-1].lower() if "." in upload.name else ""
    if suffix not in ("csv", "tsv", "xlsx"):
        raise FileValidationError("Formato no admitido. Sube un archivo .csv, .tsv o .xlsx.")
    upload.seek(0)
    headers, source_rows = _read_rows(upload)
    if len(headers) > MAX_COLUMNS:
        raise FileValidationError(
            f"El archivo tiene más de {MAX_COLUMNS} columnas; divídelo antes de analizarlo."
        )
    if len(source_rows) > MAX_ROWS:
        raise FileValidationError(
            f"El archivo tiene más de {MAX_ROWS:,} filas; divídelo antes de procesarlo."
        )
    display_headers = [header.replace("_", " ").strip().title() for header in headers]
    rows = [
        {
            display_header: _display_value(source_row.get(header))
            for header, display_header in zip(headers, display_headers)
        }
        for source_row in source_rows
    ]
    columns = _column_metadata(headers, source_rows)
    recommendation_source = "Ollama"
    try:
        recommendation = _recommend_chart(columns)
        chart_notice = ""
    except (AIProviderError, ValueError) as exc:
        recommendation = _fallback_recommendation(columns)
        recommendation_source = "detección automática"
        chart_notice = (
            f"Ollama no pudo recomendar columnas para el gráfico ({exc}). "
            "Se usó una selección automática basada en los tipos de columna."
        )
    chart = _make_chart(headers, source_rows, columns, recommendation)
    return {
        "headers": display_headers,
        "rows": rows,
        "columns": columns,
        "chart": chart,
        "chart_notice": chart_notice,
        "recommendation_source": recommendation_source,
        "is_expense_table": set(headers).issuperset(
            {
                "fecha",
                "proveedor",
                "numero_factura",
                "moneda",
                "subtotal",
                "impuesto",
                "total",
            }
        ),
    }


def build_dynamic_csv_report(headers, rows):
    import csv
    import io

    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=headers, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        safe_row = {
            header: (
                "'" + str(value)
                if isinstance(value, str)
                and value[:1] in ("=", "+", "-", "@", "\t", "\r")
                else value
            )
            for header, value in row.items()
        }
        writer.writerow(safe_row)
    return "\ufeff" + output.getvalue()
