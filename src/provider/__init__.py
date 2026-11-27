"""Single Gemini transport entry point for pipeline and clarification stages."""
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import lru_cache
import logging
import threading
from typing import Any, TypeVar

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


_PRIVATE_REQUEST = ContextVar("private_gemini_request", default=False)


def _allow_transport_log(record: logging.LogRecord) -> bool:
    return not _PRIVATE_REQUEST.get()


# Transport diagnostics can include complete URLs, response headers and SDK payloads.
for _logger_name in ("httpx", "httpx2", "httpcore.connection", "httpcore.http11", "httpcore.http2",
                     "httpcore.proxy", "httpcore.socks", "google_genai._api_client", "google_genai.models"):
    logging.getLogger(_logger_name).addFilter(_allow_transport_log)


@dataclass(eq=False)
class _Session:
    secret: str = field(repr=False)
    client: Any = field(repr=False)
    borrowers: int = 0


_SESSION_LOCK = threading.Condition()
_ACTIVE_SESSIONS: list[_Session] = []

__all__ = [
    "FailureAction",
    "FailureReason",
    "ProviderError",
    "QuotaBudget",
    "generate",
    "load_settings",
    "provider_session",
]


@lru_cache(maxsize=32)
def _cached_json_schema(model_cls: type[BaseModel]) -> dict[str, Any]:
    return model_cls.model_json_schema()


@contextmanager
def provider_session(settings: ProviderSettings) -> Iterator[None]:
    """Reuse one thread-safe google-genai Client across a pipeline run for HTTP keep-alive pooling."""
    if not settings.api_key:
        yield
        return
    secret = settings.api_key.get_secret_value()
    http = types.HttpOptions(timeout=settings.timeout_ms, retry_options=types.HttpRetryOptions(attempts=1))
    with ExitStack() as stack:
        try:
            client = stack.enter_context(genai.Client(api_key=secret, http_options=http))
        except Exception:
            raise ProviderError("Provider session initialization failed") from None
        entry = _Session(secret, client)
        with _SESSION_LOCK:
            _ACTIVE_SESSIONS.append(entry)
        try:
            yield
        finally:
            with _SESSION_LOCK:
                _ACTIVE_SESSIONS.remove(entry)
                _SESSION_LOCK.wait_for(lambda: entry.borrowers == 0)
            try:
                stack.close()
            except Exception:
                raise ProviderError("Provider session close failed") from None


@contextmanager
def _borrow_client(secret: str, http: types.HttpOptions):
    with _SESSION_LOCK:
        entry = next((item for item in reversed(_ACTIVE_SESSIONS) if item.secret == secret), None)
        if entry:
            entry.borrowers += 1
    if entry is None:
        with genai.Client(api_key=secret, http_options=http) as client:
            yield client
        return
    try:
        yield entry.client
    finally:
        with _SESSION_LOCK:
            entry.borrowers -= 1
            _SESSION_LOCK.notify_all()


_THINKING_BUDGET_BY_STAGE: dict[PipelineStage, int] = {
    PipelineStage.HOLISTIC: 4096,
    PipelineStage.VERIFY: 2048,
}


def _supports_thinking_budget(model: str) -> bool:
    lowered = model.lower()
    return "gemini-3" in lowered or "gemini-2.5" in lowered


def _prepare_request(payload: BaseModel) -> tuple[str | None, str]:
    """Separate trusted system instructions from untrusted JSON payload when present."""
    raw_sys = getattr(payload, "system_instruction", None)
    if isinstance(raw_sys, str) and raw_sys.strip() and "system_instruction" in type(payload).model_fields:
        return raw_sys, payload.model_dump_json(exclude={"system_instruction"}, exclude_none=True)
    return None, payload.model_dump_json(exclude_none=True)


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
    """Perform a structured Gemini request with bounded retries, pooling, and quota accounting."""
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
    system_instruction, serialized_payload = _prepare_request(payload)
    schema = _cached_json_schema(output_type)
    secret = settings.api_key.get_secret_value()

    call_attempts = 0
    while True:
        active_budget.reserve_attempt(
            stage,
            call_attempts=call_attempts,
            estimated_tokens=estimated_tokens,
        )
        call_attempts += 1
        config_kwargs: dict[str, Any] = {
            "response_mime_type": "application/json",
            "response_json_schema": schema,
            "temperature": 0.0,
            "automatic_function_calling": types.AutomaticFunctionCallingConfig(disable=True),
        }
        thinking_budget = _THINKING_BUDGET_BY_STAGE.get(stage)
        if thinking_budget is not None and _supports_thinking_budget(settings.model):
            config_kwargs["thinking_config"] = types.ThinkingConfig(thinking_budget=thinking_budget)
        if system_instruction is not None:
            config_kwargs["system_instruction"] = system_instruction

        privacy_token = _PRIVATE_REQUEST.set(True)
        try:
            http = types.HttpOptions(
                timeout=active_budget.effective_timeout_ms(settings.timeout_ms, call_attempts=call_attempts),
                retry_options=types.HttpRetryOptions(attempts=1),
            )
            with _borrow_client(secret, http) as client:
                http.timeout = active_budget.effective_timeout_ms(settings.timeout_ms, call_attempts=call_attempts)
                config_kwargs["http_options"] = http
                config = types.GenerateContentConfig(**config_kwargs)
                with active_budget.measure_api_call():
                    response = client.models.generate_content(
                        model=settings.model, contents=serialized_payload, config=config,
                    )
        except errors.APIError as exc:
            err = classify_api_error(exc, attempts=call_attempts)
            server_delay = extract_retry_delay(exc) if err.retryable else None
        except ProviderError as exc:
            err = exc
            server_delay = None
        except Exception as exc:
            err = classify_transport_error(exc, attempts=call_attempts)
            server_delay = None
        else:
            break
        finally:
            _PRIVATE_REQUEST.reset(privacy_token)

        active_budget.record_failure(err.reason)
        active_budget.record_unknown_usage()
        if err.retryable and call_attempts < allowed_attempts:
            active_budget.wait_before_retry(attempt=call_attempts, server_delay=server_delay)
            continue
        raise err from None

    in_tokens, out_tokens = active_budget.record_usage(response.usage_metadata)
    candidate = next(iter(response.candidates or []), None)
    if not candidate or candidate.finish_reason != types.FinishReason.STOP:
        if candidate and candidate.finish_reason == types.FinishReason.MAX_TOKENS:
            active_budget.record_failure(FailureReason.TRUNCATED)
            raise ProviderError(
                "Truncated structured response (MAX_TOKENS)",
                reason=FailureReason.TRUNCATED,
                action=FailureAction.SPLIT,
                attempts=call_attempts,
            )
        active_budget.record_failure(FailureReason.BLOCKED_RESPONSE)
        raise ProviderError(
            "Blocked, empty or truncated response",
            reason=FailureReason.BLOCKED_RESPONSE,
            action=FailureAction.STOP,
            attempts=call_attempts,
        )
    try:
        output = output_type.model_validate_json(response.text or "", strict=True)
    except ValidationError:
        active_budget.record_failure(FailureReason.MALFORMED_RESPONSE)
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
