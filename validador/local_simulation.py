from datetime import datetime, timezone
from uuid import uuid4

from .validation import COSTA_RICAN_CURRENCY


def simulate_local_invoice_submission(invoice_batch):
    invoices = invoice_batch["invoices"]
    is_ready = (
        invoice_batch["status"] == "ready"
        and bool(invoices)
        and all(
            invoice["currency"] == COSTA_RICAN_CURRENCY
            and invoice["status"] == "validated"
            for invoice in invoices
        )
    )
    simulation_id = f"LOCAL-SIM-{uuid4()}"
    simulated_at = datetime.now(timezone.utc).isoformat()

    return {
        "provider": "Simulador local de flujo TRIBU-CR",
        "mode": "simulation_only",
        "real_hacienda_contacted": False,
        "real_submission": False,
        "simulation_id": simulation_id,
        "simulated_at": simulated_at,
        "document_type": invoice_batch.get(
            "document_type",
            "factura_electronica_internal_demo",
        ),
        "status": "simulation_passed" if is_ready else "simulation_blocked",
        "message": (
            "Los documentos superaron los controles internos de demostración. "
            "No se generó un comprobante fiscal y no se transmitió nada a Hacienda."
            if is_ready
            else "Simulación bloqueada: corrige los datos marcados para revisión. "
            "Hacienda no recibió ningún documento."
        ),
        "simulated_ready_count": len(invoices) if is_ready else 0,
        "blocked_count": 0 if is_ready else len(invoices),
        "invoices": [
            {
                "invoice_number": invoice["invoice_number"],
                "status": "simulated_ready" if is_ready else "blocked",
                "errors": [] if is_ready else invoice["errors"],
            }
            for invoice in invoices
        ],
    }
