"""Secret-free structured diagnostics, run IDs, and sanitized stack-frame formatting."""
import logging
from pathlib import Path
import traceback
from uuid import uuid4

from pydantic import ValidationError

from src.provider import ProviderError

_REPO_ROOT = Path(__file__).resolve().parents[2]


def new_run_id() -> str:
    """Return a canonical, stable diagnostic reference ID matching contract Id constraints."""
    return f"run-{uuid4().hex[:12]}"


def classify_error_category(exc: BaseException) -> str:
    """Map an exception instance to a fixed, secret-free error category."""
    if isinstance(exc, ProviderError):
        return f"provider:{exc.reason.value}"
    if isinstance(exc, ValidationError):
        return "validation_error"
    if isinstance(exc, NotImplementedError):
        return "not_implemented"
    name_lower = type(exc).__name__.lower()
    if "nosessioncontext" in name_lower or "scriptruncontext" in name_lower:
        return "ui_thread_context_error"
    if isinstance(exc, TimeoutError) or "timeout" in name_lower:
        return "timeout_error"
    if isinstance(exc, (ValueError, OSError)):
        return "input_or_io_error"
    return "internal_error"


def safe_stack_frames(exc: BaseException, *, limit: int = 6) -> str:
    """Extract only file path, line number, and function name — never locals or messages."""
    tb = exc.__traceback__
    if tb is None:
        return "none"
    extracted = traceback.extract_tb(tb)
    if not extracted:
        return "none"
    frames: list[str] = []
    for frame in extracted[-max(1, limit) :]:
        raw_path = Path(frame.filename)
        try:
            display_path = raw_path.resolve().relative_to(_REPO_ROOT).as_posix()
        except (OSError, ValueError):
            display_path = raw_path.name
        frames.append(f"{display_path}:{frame.lineno} in {frame.name}")
    return " -> ".join(frames)


def log_safe_exception(
    target_logger: logging.Logger,
    event: str,
    exc: BaseException,
    *,
    run_id: str,
    stage: str | None = None,
    level: int = logging.ERROR,
) -> tuple[str, str]:
    """Log sanitized exception metadata without leaking secrets, prompts, or document content."""
    category = classify_error_category(exc)
    frames = safe_stack_frames(exc)
    target_logger.log(
        level,
        "run=%s event=%s stage=%s category=%s error_type=%s frames=%s",
        run_id,
        event,
        stage or "unknown",
        category,
        type(exc).__name__,
        frames,
    )
    return category, frames
