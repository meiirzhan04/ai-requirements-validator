"""Single Gemini transport entry point for pipeline and clarification stages."""
from typing import TypeVar

from google import genai
from google.genai import errors, types
from pydantic import BaseModel, ValidationError

from src.contracts import PipelineStage, ProviderSettings, StageResult
from .settings import load_settings

Output = TypeVar("Output", bound=BaseModel)


class ProviderError(RuntimeError):
    """Safe diagnostic, suitable for CLI output. Never include raw request data."""


def generate(
    stage: PipelineStage, payload: BaseModel, output_type: type[Output],
    *, settings: ProviderSettings | None = None,
) -> StageResult[Output]:
    """Perform one structured request. Caller owns consent, budgets and retries."""
    settings = settings or load_settings()
    if not settings.api_key:
        raise ProviderError("API key required")
    http = types.HttpOptions(timeout=settings.timeout_ms, retry_options=types.HttpRetryOptions(attempts=1))
    try:
        with genai.Client(api_key=settings.api_key.get_secret_value(), http_options=http) as client:
            response = client.models.generate_content(
                model=settings.model,
                contents=payload.model_dump_json(),
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_json_schema=output_type.model_json_schema(),
                ),
            )
        candidate = next(iter(response.candidates or []), None)
        if not candidate or candidate.finish_reason != types.FinishReason.STOP:
            raise ProviderError("Blocked, empty or truncated response")
        output = output_type.model_validate_json(response.text or "", strict=True)
    except errors.APIError as exc:
        reason = {400: "Invalid request, schema or API key", 401: "Authentication failed",
                  403: "Access denied", 404: "Model unavailable", 429: "Rate limit exceeded"}.get(
                      exc.code, "Gemini API error")
        raise ProviderError(f"{reason} (HTTP {exc.code})") from None
    except ValidationError:
        raise ProviderError("Malformed structured response") from None
    except ProviderError:
        raise
    except Exception as exc:
        # SDK/transport exceptions can embed headers and request content.
        raise ProviderError(f"Transport or SDK failure ({type(exc).__name__})") from None
    usage = response.usage_metadata
    return StageResult[output_type](
        stage=stage, output=output, attempts=1,
        input_tokens=usage.prompt_token_count if usage else None,
        output_tokens=usage.candidates_token_count if usage else None,
    )
