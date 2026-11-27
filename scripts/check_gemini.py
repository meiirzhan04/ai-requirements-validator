"""Run from the repository root with python scripts/check_gemini.py."""
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.contracts import PipelineStage
from src.provider import FailureReason, ProviderError, generate, load_settings
from src.provider.dto import ProbeRequest, ProbeResponse


def main() -> int:
    try:
        settings = load_settings()
    except (ValueError, OSError):
        print("FAIL - invalid provider settings")
        return 1
    model = settings.model
    if settings.api_key and settings.api_key.get_secret_value() in model:
        model = "[redacted]"
    if not settings.api_key:
        print(f"SKIPPED — model={model} — API key required")
        return 0
    try:
        probe = generate(PipelineStage.SMOKE, ProbeRequest(), ProbeResponse, settings=settings)
        if probe.output.value != 7:
            raise ProviderError(
                "Incorrect structured value",
                reason=FailureReason.MALFORMED_RESPONSE,
                attempts=probe.attempts,
            )
    except ProviderError as exc:
        print(f"FAIL — model={model} — {exc.reason.value}")
        return 1
    print(
        f"PASS — model={model} value={probe.output.value} "
        f"attempts={probe.attempts} input_tokens={probe.input_tokens} output_tokens={probe.output_tokens}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
