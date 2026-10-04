import json
import re
from datetime import date, datetime

from .ai_client import AIProviderError, generate_text
from .validation import (
    MAX_ROWS,
    FileValidationError,
    _parse_date,
    _parse_decimal,
    _normalize_header,
    _read_rows,
    _read_xlsx_tables,
)

MAX_COLUMNS = 100
MAX_CHART_CATEGORIES = 12
AGGREGATIONS = {"sum", "average", "count"}
IDENTIFIER_HEADER = re.compile(
    r"(^|_)(id|codigo|code|folio|factura|invoice|telefono|phone|postal|zip|"
    r"cuenta|account|numero|number|clave)(_|$)",
    re.IGNORECASE,
)
MONTH_HEADERS = (
    "enero",
    "febrero",
    "marzo",
    "abril",
    "mayo",
    "junio",
    "julio",
    "agosto",
    "septiembre",
    "octubre",
    "noviembre",
    "diciembre",
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
    if x_index == -1:
        dimensions = [
            column["index"]
            for column in columns
            if column["type"] in ("category", "date", "text")
        ]
        if dimensions:
            x_index = dimensions[0]
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


def _detect_monthly_matrix(headers, source_rows):
    month_indices = [
        index for index, header in enumerate(headers) if header in MONTH_HEADERS
    ]
    label_indices = [
        index
        for index, header in enumerate(headers)
        if header in ("concepto", "indicador", "categoria", "rubro", "concept")
    ]
    if len(month_indices) < 3 or not label_indices:
        return None

    label_header = headers[label_indices[0]]
    valid_rows = []
    skip_year_summary = False
    for row in source_rows:
        label = str(row.get(label_header) or "").strip()
        normalized_label = _normalize_header(label)
        if "resumen_ano" in normalized_label or "resumen_anual" in normalized_label:
            skip_year_summary = True
        if (
            not skip_year_summary
            and label
            and any(
                _parse_decimal(row.get(headers[index])) is not None
                for index in month_indices
            )
        ):
            valid_rows.append(row)
    if not valid_rows:
        return None

    return {
        "label_index": label_indices[0],
        "month_indices": month_indices,
        "rows": valid_rows,
    }


def _detect_month_rows(headers, source_rows, columns):
    period_headers = {"mes", "month", "periodo", "period", "fecha", "date"}
    numeric_indices = [
        column["index"] for column in columns if column["type"] == "number"
    ]
    for period_index, header in enumerate(headers):
        if header not in period_headers:
            continue
        period_header = headers[period_index]
        valid_rows = []
        for row in source_rows:
            value = row.get(period_header)
            normalized = _normalize_header(value)
            if normalized in MONTH_HEADERS or _parse_date(value):
                valid_rows.append(row)
        if len(valid_rows) < 3 or not numeric_indices:
            continue
        measured_indices = [
            index
            for index in numeric_indices
            if any(
                _parse_decimal(row.get(headers[index])) is not None
                for row in valid_rows
            )
        ]
        if measured_indices:
            return {
                "period_index": period_index,
                "measure_indices": measured_indices,
                "rows": valid_rows,
            }
    return None


def _period_chart_data(headers, display_headers, period, selected_measure_index=None):
    period_header = headers[period["period_index"]]
    periods = [_display_value(row.get(period_header)) for row in period["rows"]]
    series = [
        {
            "index": index,
            "label": display_headers[index],
            "values": [
                float(_parse_decimal(row.get(headers[index])) or 0)
                for row in period["rows"]
            ],
            "priority": (
                0
                if "flujo neto" in headers[index].replace("_", " ")
                else 1
                if "ingresos totales" in headers[index].replace("_", " ")
                else 2
            ),
        }
        for index in period["measure_indices"]
    ]
    series.sort(key=lambda item: item["priority"])
    selected = next(
        (
            item
            for item in series
            if item["index"] == selected_measure_index
        ),
        series[0],
    )
    chart = _make_monthly_chart(periods, selected["label"], selected["values"])
    selected_index = series.index(selected)
    return {
        "months": periods,
        "series": [
            {
                "label": item["label"],
                "values": item["values"],
            }
            for item in series
        ],
        "selected": selected["label"],
        "selected_index": selected_index,
        "chart": chart,
    }


def _recommend_matrix_layout(headers, columns, matrix):
    schema = [
        {"index": column["index"], "name": column["name"], "type": column["type"]}
        for column in columns
    ]
    response = generate_text(
        system_prompt=(
            "Interpreta la estructura de una tabla financiera de meses en columnas. "
            "Recibirás solo encabezados y tipos, nunca valores de celdas. Devuelve "
            "JSON con label_index (la columna de conceptos) y period_indices (todos "
            "los índices de meses en su orden original). Usa únicamente índices "
            "existentes. No incluyas la columna de total anual como periodo."
        ),
        user_prompt=json.dumps({"columns": schema}, ensure_ascii=False),
        max_tokens=120,
        json_mode=True,
    )
    match = re.search(r"\{.*\}", response, flags=re.DOTALL)
    if not match:
        raise ValueError("La recomendación no contiene un objeto JSON válido.")
    recommendation = json.loads(match.group())
    if not isinstance(recommendation, dict):
        raise ValueError("La recomendación no contiene un objeto JSON válido.")
    label_index = recommendation.get("label_index")
    period_indices = recommendation.get("period_indices")
    if (
        isinstance(label_index, bool)
        or not isinstance(label_index, int)
        or label_index != matrix["label_index"]
    ):
        raise ValueError("La IA eligió una columna de conceptos no válida.")
    if (
        not isinstance(period_indices, list)
        or any(
            isinstance(index, bool)
            or not isinstance(index, int)
            or index not in matrix["month_indices"]
            for index in period_indices
        )
        or set(period_indices) != set(matrix["month_indices"])
    ):
        raise ValueError("La IA no reconoció todos los meses de la tabla.")
    return {
        "label_index": label_index,
        "month_indices": sorted(period_indices),
    }


def _matrix_chart_data(headers, display_headers, matrix):
    label_header = headers[matrix["label_index"]]
    month_indices = matrix["month_indices"]
    series = []
    for row in matrix["rows"]:
        label = _display_value(row.get(label_header))
        values = [
            float(_parse_decimal(row.get(headers[index])) or 0)
            for index in month_indices
        ]
        normalized_label = label.casefold()
        series.append(
            {
                "label": label,
                "values": values,
                "priority": (
                    0
                    if "flujo neto del periodo" in normalized_label
                    or "flujo neto del período" in normalized_label
                    else 1
                    if "saldo final acumulado" in normalized_label
                    else 2
                ),
            }
        )
    series.sort(key=lambda item: item["priority"])
    selected = series[0]
    chart = _make_monthly_chart(
        [display_headers[index] for index in month_indices],
        selected["label"],
        selected["values"],
    )
    return {
        "months": [display_headers[index] for index in month_indices],
        "series": [
            {"label": item["label"], "values": item["values"]} for item in series
        ],
        "selected": selected["label"],
        "selected_index": 0,
        "chart": chart,
    }


def _make_monthly_chart(months, label, values):
    maximum = max((abs(value) for value in values), default=0) or 1
    return {
        "title": f"{label} por mes",
        "x_label": "Mes",
        "y_label": label,
        "points": [
            {
                "label": month,
                "value": round(value, 2),
                "width": round(abs(value) / maximum * 50, 1),
            }
            for month, value in zip(months, values)
        ],
    }


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
        json_mode=True,
    )
    return _parse_recommendation(response, columns)


def _make_chart(headers, display_headers, rows, columns, recommendation):
    if not headers or not rows:
        return None

    x_index = recommendation["x_index"]
    y_index = recommendation["y_index"]
    aggregation = recommendation["aggregation"]
    x_header = headers[x_index] if x_index >= 0 else "Fila"
    y_header = headers[y_index] if y_index >= 0 else "Cantidad de registros"
    x_label = display_headers[x_index] if x_index >= 0 else x_header
    y_label = (
        display_headers[y_index] if y_index >= 0 else "Cantidad de registros"
    )
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
        "average": f"Promedio de {y_label}",
        "count": "Cantidad de registros",
        "sum": f"Suma de {y_label}",
    }[aggregation]
    return {
        "title": f"{measure_label} por {x_label}",
        "x_label": x_label,
        "y_label": measure_label,
        "points": points,
        "recommendation": recommendation,
    }


def analyze_table(upload):
    suffix = upload.name.rsplit(".", 1)[-1].lower() if "." in upload.name else ""
    if suffix not in ("csv", "tsv", "xlsx"):
        raise FileValidationError("Formato no admitido. Sube un archivo .csv, .tsv o .xlsx.")
    upload.seek(0)
    source_sheet = ""
    workbook_tables = []
    if suffix == "xlsx":
        workbook_tables = _read_xlsx_tables(upload)
        if not workbook_tables:
            raise FileValidationError("El Excel está vacío o no tiene encabezados.")
        selected_table = max(
            workbook_tables,
            key=lambda table: (
                table["score"],
                table["month_count"],
                table["data_rows"],
                table["sheet_name"] == table["active_sheet"],
            ),
        )
        headers = selected_table["headers"]
        source_rows = selected_table["rows"]
        source_sheet = selected_table["sheet_name"]
    else:
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
    is_expense_table = set(headers).issuperset(
        {
            "fecha",
            "proveedor",
            "numero_factura",
            "moneda",
            "subtotal",
            "impuesto",
            "total",
        }
    )
    matrix = None if is_expense_table else _detect_monthly_matrix(headers, source_rows)
    period = (
        None
        if is_expense_table or matrix
        else _detect_month_rows(headers, source_rows, columns)
    )
    matrix_chart = None
    if matrix:
        recommendation_source = "Ollama"
        try:
            layout = _recommend_matrix_layout(headers, columns, matrix)
            matrix["label_index"] = layout["label_index"]
            matrix["month_indices"] = layout["month_indices"]
            chart_notice = ""
        except (AIProviderError, ValueError, json.JSONDecodeError) as exc:
            recommendation_source = "detección automática"
            chart_notice = (
                f"Ollama no pudo interpretar la estructura mensual ({exc}). "
                "Se usó la detección local de encabezados."
            )
        matrix_chart = _matrix_chart_data(headers, display_headers, matrix)
        matrix_chart["sheet_name"] = source_sheet
        matrix_chart["selected_sheet"] = source_sheet
        matrix_chart["sheets"] = [dict(matrix_chart)]
        for table in workbook_tables:
            if table["sheet_name"] == source_sheet:
                continue
            sheet_matrix = _detect_monthly_matrix(table["headers"], table["rows"])
            if not sheet_matrix:
                continue
            sheet_headers = table["headers"]
            sheet_display_headers = [
                header.replace("_", " ").strip().title() for header in sheet_headers
            ]
            sheet_chart = _matrix_chart_data(
                sheet_headers, sheet_display_headers, sheet_matrix
            )
            sheet_chart["sheet_name"] = table["sheet_name"]
            matrix_chart["sheets"].append(sheet_chart)
        chart = matrix_chart["chart"]
        source_rows = matrix["rows"]
        rows = [
            {
                display_header: _display_value(source_row.get(header))
                for header, display_header in zip(headers, display_headers)
            }
            for source_row in source_rows
        ]
    elif period:
        recommendation_source = "Ollama"
        try:
            recommendation = _recommend_chart(columns)
            if recommendation["y_index"] not in period["measure_indices"]:
                raise ValueError("La IA no recomendó una medida disponible.")
            chart_notice = ""
            recommended_measure_index = recommendation["y_index"]
        except (AIProviderError, ValueError) as exc:
            recommendation_source = "detección automática"
            recommended_measure_index = None
            chart_notice = (
                f"Ollama no pudo recomendar una medida para el gráfico ({exc}). "
                "Se usó la detección financiera local."
            )
        matrix_chart = _period_chart_data(
            headers,
            display_headers,
            period,
            recommended_measure_index,
        )
        matrix_chart["sheet_name"] = source_sheet
        matrix_chart["selected_sheet"] = source_sheet
        matrix_chart["sheets"] = [dict(matrix_chart)]
        for table in workbook_tables:
            if table["sheet_name"] == source_sheet:
                continue
            sheet_headers = table["headers"]
            sheet_display_headers = [
                header.replace("_", " ").strip().title() for header in sheet_headers
            ]
            sheet_columns = _column_metadata(sheet_headers, table["rows"])
            sheet_period = _detect_month_rows(
                sheet_headers, table["rows"], sheet_columns
            )
            if not sheet_period:
                continue
            sheet_chart = _period_chart_data(
                sheet_headers, sheet_display_headers, sheet_period
            )
            sheet_chart["sheet_name"] = table["sheet_name"]
            matrix_chart["sheets"].append(sheet_chart)
        chart = matrix_chart["chart"]
        source_rows = period["rows"]
        rows = [
            {
                display_header: _display_value(source_row.get(header))
                for header, display_header in zip(headers, display_headers)
            }
            for source_row in source_rows
        ]
    else:
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
        chart = _make_chart(
            headers,
            display_headers,
            source_rows,
            columns,
            recommendation,
        )
    return {
        "headers": display_headers,
        "rows": rows,
        "columns": columns,
        "chart": chart,
        "matrix_chart": matrix_chart,
        "source_sheet": source_sheet,
        "chart_notice": chart_notice,
        "recommendation_source": recommendation_source,
        "is_expense_table": is_expense_table,
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
