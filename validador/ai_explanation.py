from .ai_client import generate_text


def explain_validation(issue_counts, total_rows, valid_rows):
    if not issue_counts:
        issue_summary = "No se detectaron errores."
    else:
        issue_summary = "\n".join(
            f"- {category}: {count} filas"
            for category, count in sorted(issue_counts.items())
        )

    prompt = (
        "Explica en español, de forma breve y práctica, los resultados de "
        "validación de un archivo de gastos. Sugiere qué revisar primero. "
        "No inventes reglas fiscales ni afirmes que los datos cumplen leyes.\n\n"
        f"Filas: {total_rows}\n"
        f"Filas válidas: {valid_rows}\n"
        f"Errores por categoría:\n{issue_summary}"
    )
    return generate_text(
        system_prompt=(
            "Explica en español, de forma breve y práctica, los resultados de "
            "validación de un archivo de gastos. Sugiere qué revisar primero. "
            "No inventes reglas fiscales ni afirmes que los datos cumplen leyes."
        ),
        user_prompt=prompt,
    )
