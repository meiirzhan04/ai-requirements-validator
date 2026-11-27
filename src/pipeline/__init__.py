from collections.abc import Callable

from src.contracts import ParsedDocument, ProgressEvent, ProviderSettings, ValidationReport
from src.pipeline.orchestrator import execute_pipeline

__all__ = ["execute_pipeline", "run_pipeline"]


def run_pipeline(
    document: ParsedDocument,
    settings: ProviderSettings,
    on_progress: Callable[[ProgressEvent], None] | None = None,
) -> ValidationReport:
    """Audit an explicitly shareable revision. Notify only on actual stage transitions."""
    return execute_pipeline(document, settings, on_progress=on_progress)
