from src.contracts import ValidationReport


def render_markdown(report: ValidationReport) -> str:
    """Render the validated report in Russian. No network or recalculated business rules."""
    raise NotImplementedError("Russian Markdown rendering pending (Parasat)")
