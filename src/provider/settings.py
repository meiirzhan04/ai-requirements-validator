import os
from pathlib import Path

from dotenv import dotenv_values

from src.contracts import ProviderSettings


def load_settings(env_file: Path | None = None) -> ProviderSettings:
    path = env_file if env_file is not None else Path(__file__).resolve().parents[2] / ".env"
    values = dotenv_values(path, interpolate=False) if path.is_file() else {}
    api_key = (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
               or values.get("GEMINI_API_KEY") or values.get("GOOGLE_API_KEY"))
    model = os.environ.get("GEMINI_MODEL") or values.get("GEMINI_MODEL")
    return ProviderSettings(api_key=api_key, **({"model": model} if model else {}))
