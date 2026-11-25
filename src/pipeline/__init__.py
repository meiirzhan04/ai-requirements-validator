from collections.abc import Callable

from src.contracts import ParsedDocument, ProgressEvent, ProviderSettings, ValidationReport


def run_pipeline(
    document: ParsedDocument, settings: ProviderSettings,
    on_progress: Callable[[ProgressEvent], None] | None = None,
) -> ValidationReport:
    """Audit an explicitly shareable revision. Notify only on actual stage transitions."""
    raise NotImplementedError("AI pipeline pending (Meirzhan)")
