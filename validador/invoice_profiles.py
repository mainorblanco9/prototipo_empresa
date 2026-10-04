INVOICE_DOCUMENT_PROFILES = (
    {
        "code": "factura_electronica",
        "name": "Factura electrónica",
        "status": "available",
        "status_label": "Borrador interno genérico",
        "detail": (
            "El prototipo analiza una fila de gastos por factura. No incluye "
            "emisor, receptor, líneas de detalle ni el XML fiscal oficial."
        ),
    },
    {
        "code": "tiquete_electronico",
        "name": "Tiquete electrónico",
        "status": "not_implemented",
        "status_label": "Próximamente",
        "detail": "Pendiente de campos, reglas y esquema oficial propios.",
    },
    {
        "code": "nota_credito",
        "name": "Nota de crédito electrónica",
        "status": "not_implemented",
        "status_label": "Próximamente",
        "detail": "Pendiente de referencia al comprobante original y reglas propias.",
    },
    {
        "code": "nota_debito",
        "name": "Nota de débito electrónica",
        "status": "not_implemented",
        "status_label": "Próximamente",
        "detail": "Pendiente de referencia al comprobante original y reglas propias.",
    },
    {
        "code": "factura_compra",
        "name": "Factura electrónica de compra",
        "status": "not_implemented",
        "status_label": "Próximamente",
        "detail": "Pendiente de datos tributarios y reglas propias.",
    },
    {
        "code": "factura_exportacion",
        "name": "Factura electrónica de exportación",
        "status": "not_implemented",
        "status_label": "Próximamente",
        "detail": "Pendiente de datos de exportación y reglas propias.",
    },
)
