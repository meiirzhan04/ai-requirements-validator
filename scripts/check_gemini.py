"""Run from the repository root with python scripts/check_gemini.py."""
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.contracts import PipelineStage
from src.provider import ProviderError, generate, load_settings
from src.provider.dto import ProbeRequest, ProbeResponse


def main() -> int:
    settings = load_settings()
    if not settings.api_key:
        print("SKIPPED — API key required")
        return 0
    try:
        probe = generate(PipelineStage.SMOKE, ProbeRequest(), ProbeResponse, settings=settings)
        if probe.output.value != 7:
            raise ProviderError("Incorrect structured value")
    except ProviderError as exc:
        print(f"FAIL — {exc}")
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
