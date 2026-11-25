"""Single Gemini transport entry point for pipeline and clarification stages."""
from typing import TypeVar

from google import genai
from google.genai import errors, types
from pydantic import BaseModel, ValidationError

from src.contracts import PipelineStage, ProviderSettings, StageResult
from .runtime import (
    FailureAction,
    FailureReason,
    ProviderError,
    QuotaBudget,
    classify_api_error,
    classify_transport_error,
    extract_retry_delay,
)
from .settings import load_settings

Output = TypeVar("Output", bound=BaseModel)

__all__ = [
    "FailureAction",
    "FailureReason",
    "ProviderError",
    "QuotaBudget",
    "generate",
    "load_settings",
]


def generate(
    stage: PipelineStage,
    payload: BaseModel,
    output_type: type[Output],
    *,
    settings: ProviderSettings | None = None,
    budget: QuotaBudget | None = None,
    max_attempts: int | None = None,
    estimated_input_tokens: int = 0,
    estimated_output_tokens: int = 0,
) -> StageResult[Output]:
    """Perform a structured Gemini request with bounded retries and quota accounting."""
    settings = settings or load_settings()
    if not settings.api_key:
        raise ProviderError(
            "API key required",
            reason=FailureReason.AUTH_ERROR,
            action=FailureAction.STOP,
        )
    if max_attempts is not None and not 1 <= max_attempts <= 3:
        raise ValueError("max_attempts must be between 1 and 3")

    active_budget = budget or QuotaBudget(
        max_attempts_per_call=1 if stage == PipelineStage.SMOKE else 3,
    )
    allowed_attempts = (
        max_attempts
        if max_attempts is not None
        else (1 if stage == PipelineStage.SMOKE else active_budget.max_attempts_per_call)
    )
    estimated_tokens = max(0, estimated_input_tokens) + max(0, estimated_output_tokens)
    serialized_payload = payload.model_dump_json()
    schema = output_type.model_json_schema()

    call_attempts = 0
    while True:
        active_budget.reserve_attempt(
            stage,
            call_attempts=call_attempts,
            estimated_tokens=estimated_tokens,
        )
        timeout_ms = active_budget.effective_timeout_ms(
            settings.timeout_ms,
            call_attempts=call_attempts,
        )
        call_attempts += 1
        http = types.HttpOptions(
            timeout=timeout_ms,
            retry_options=types.HttpRetryOptions(attempts=1),
        )
        try:
            with genai.Client(api_key=settings.api_key.get_secret_value(), http_options=http) as client:
                response = client.models.generate_content(
                    model=settings.model,
                    contents=serialized_payload,
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        response_json_schema=schema,
                    ),
                )
        except errors.APIError as exc:
            active_budget.record_unknown_usage()
            err = classify_api_error(exc, attempts=call_attempts)
            if err.retryable and call_attempts < allowed_attempts:
                active_budget.wait_before_retry(
                    attempt=call_attempts,
                    server_delay=extract_retry_delay(exc),
                )
                continue
            raise err from None
        except ProviderError:
            raise
        except Exception as exc:
            # SDK/transport exceptions can embed headers and request content.
            active_budget.record_unknown_usage()
            err = classify_transport_error(exc, attempts=call_attempts)
            if err.retryable and call_attempts < allowed_attempts:
                active_budget.wait_before_retry(attempt=call_attempts)
                continue
            raise err from None

        in_tokens, out_tokens = active_budget.record_usage(response.usage_metadata)
        candidate = next(iter(response.candidates or []), None)
        if not candidate or candidate.finish_reason != types.FinishReason.STOP:
            if candidate and candidate.finish_reason == types.FinishReason.MAX_TOKENS:
                raise ProviderError(
                    "Truncated structured response (MAX_TOKENS)",
                    reason=FailureReason.TRUNCATED,
                    action=FailureAction.SPLIT,
                    attempts=call_attempts,
                )
            raise ProviderError(
                "Blocked, empty or truncated response",
                reason=FailureReason.BLOCKED_RESPONSE,
                action=FailureAction.STOP,
                attempts=call_attempts,
            )
        try:
            output = output_type.model_validate_json(response.text or "", strict=True)
        except ValidationError:
            raise ProviderError(
                "Malformed structured response",
                reason=FailureReason.MALFORMED_RESPONSE,
                action=FailureAction.REPAIR,
                attempts=call_attempts,
            ) from None

        return StageResult[output_type](
            stage=stage,
            output=output,
            attempts=call_attempts,
            input_tokens=in_tokens,
            output_tokens=out_tokens,
        )
