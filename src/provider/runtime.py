"""Retry policy, unified quota/token accounting, and typed failure classification."""
from collections import Counter, deque
from collections.abc import Callable
from enum import StrEnum
import math
import random
import re
import threading
import time
from typing import Any

from google.genai import errors

from src.contracts import PipelineStage

_DAILY_QUOTA_PATTERN = re.compile(
    r"(?:per[_ ]?day|daily[_ ]?(?:quota|limit)|requestsperday|tokensperday|\brpd\b)",
    re.IGNORECASE,
)
_DURATION_PATTERN = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(ms|s)?\s*$", re.IGNORECASE)


class FailureReason(StrEnum):
    AUTH_ERROR = "auth_error"
    INVALID_REQUEST = "invalid_request"
    MODEL_UNAVAILABLE = "model_unavailable"
    DAILY_QUOTA_EXHAUSTED = "daily_quota_exhausted"
    RATE_LIMITED = "rate_limited"
    SERVER_ERROR = "server_error"
    TIMEOUT = "timeout"
    TRANSPORT_ERROR = "transport_error"
    TRUNCATED = "truncated"
    BLOCKED_RESPONSE = "blocked_response"
    MALFORMED_RESPONSE = "malformed_response"
    BUDGET_EXHAUSTED = "budget_exhausted"
    API_ERROR = "api_error"


class FailureAction(StrEnum):
    SPLIT = "split"
    REPAIR = "repair"
    STOP = "stop"


class ProviderError(RuntimeError):
    """Safe diagnostic, suitable for CLI output. Never include raw request data."""

    def __init__(
        self,
        message: str,
        *,
        reason: FailureReason = FailureReason.API_ERROR,
        action: FailureAction = FailureAction.STOP,
        status_code: int | None = None,
        attempts: int = 0,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.action = action
        self.status_code = status_code
        self.attempts = attempts
        self.retryable = retryable

    @property
    def should_split(self) -> bool:
        return self.action == FailureAction.SPLIT

    @property
    def should_stop(self) -> bool:
        return self.action == FailureAction.STOP


class QuotaBudget:
    """Unified request, token, rate-window, and elapsed-time budget across stages."""

    def __init__(
        self,
        *,
        max_attempts_per_call: int = 3,
        max_total_attempts: int | None = None,
        rpd_limit: int | None = None,
        rpm_limit: int | None = None,
        tpm_limit: int | None = None,
        max_total_tokens: int | None = None,
        max_elapsed_seconds: float | None = None,
        base_delay_seconds: float = 1.0,
        max_delay_seconds: float = 30.0,
        jitter_ratio: float = 0.25,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        rand: Callable[[], float] = random.random,
    ) -> None:
        if not 1 <= max_attempts_per_call <= 3:
            raise ValueError("max_attempts_per_call must be between 1 and 3")
        if base_delay_seconds < 0 or max_delay_seconds < 0 or jitter_ratio < 0:
            raise ValueError("Delay and jitter settings must be non-negative")
        self.max_attempts_per_call = max_attempts_per_call
        self.max_total_attempts = max_total_attempts
        self.rpd_limit = rpd_limit
        self.rpm_limit = rpm_limit
        self.tpm_limit = tpm_limit
        self.max_total_tokens = max_total_tokens
        self.max_elapsed_seconds = max_elapsed_seconds
        self.base_delay_seconds = base_delay_seconds
        self.max_delay_seconds = max_delay_seconds
        self.jitter_ratio = jitter_ratio
        self._clock = clock
        self._sleep = sleep
        self._rand = rand
        self._started_at = clock()
        self._lock = threading.RLock()

        self.attempts: int = 0
        self.input_tokens: int = 0
        self.output_tokens: int = 0
        self.known_usage_attempts: int = 0
        self.unknown_usage_attempts: int = 0
        self.rate_limit_wait_seconds: float = 0.0
        self.retry_wait_seconds: float = 0.0
        self.attempts_by_stage: Counter[PipelineStage] = Counter()
        self._request_window: deque[float] = deque()
        self._token_window: deque[tuple[float, int]] = deque()

    @property
    def total_tokens(self) -> int:
        with self._lock:
            return self.input_tokens + self.output_tokens

    @property
    def reported_input_tokens(self) -> int | None:
        with self._lock:
            return self.input_tokens if self.known_usage_attempts > 0 else None

    @property
    def reported_output_tokens(self) -> int | None:
        with self._lock:
            return self.output_tokens if self.known_usage_attempts > 0 else None

    @property
    def elapsed_seconds(self) -> float:
        return max(0.0, self._clock() - self._started_at)

    def remaining_seconds(self) -> float | None:
        if self.max_elapsed_seconds is None:
            return None
        return self.max_elapsed_seconds - self.elapsed_seconds

    def effective_timeout_ms(self, default_timeout_ms: int, *, call_attempts: int = 0) -> int:
        remaining = self.remaining_seconds()
        if remaining is None:
            return default_timeout_ms
        if remaining <= 0:
            raise ProviderError(
                "Elapsed time budget exhausted",
                reason=FailureReason.BUDGET_EXHAUSTED,
                action=FailureAction.STOP,
                attempts=call_attempts,
            )
        return max(1, min(default_timeout_ms, int(remaining * 1000)))

    def _prune_windows(self, now: float) -> None:
        cutoff = now - 60.0
        while self._request_window and self._request_window[0] <= cutoff:
            self._request_window.popleft()
        while self._token_window and self._token_window[0][0] <= cutoff:
            self._token_window.popleft()

    def _sleep_within_budget(
        self,
        delay: float,
        *,
        call_attempts: int,
        message: str,
        category: str = "rate",
    ) -> None:
        if delay <= 0:
            return
        remaining = self.remaining_seconds()
        if remaining is not None and delay >= remaining:
            raise ProviderError(
                message,
                reason=FailureReason.BUDGET_EXHAUSTED,
                action=FailureAction.STOP,
                attempts=call_attempts,
            )
        self._sleep(delay)
        with self._lock:
            if category == "retry":
                self.retry_wait_seconds += delay
            else:
                self.rate_limit_wait_seconds += delay

    def _check_hard_limits_locked(self, *, call_attempts: int, estimated_tokens: int) -> None:
        if self.rpd_limit is not None and self.attempts >= self.rpd_limit:
            raise ProviderError(
                "Daily request quota exhausted",
                reason=FailureReason.DAILY_QUOTA_EXHAUSTED,
                action=FailureAction.STOP,
                attempts=call_attempts,
            )
        if self.max_total_attempts is not None and self.attempts >= self.max_total_attempts:
            raise ProviderError(
                "Request attempt budget exhausted",
                reason=FailureReason.BUDGET_EXHAUSTED,
                action=FailureAction.STOP,
                attempts=call_attempts,
            )
        if self.max_total_tokens is not None:
            total = self.input_tokens + self.output_tokens
            projected = total + max(0, estimated_tokens)
            if total >= self.max_total_tokens or (
                estimated_tokens > 0 and projected > self.max_total_tokens
            ):
                raise ProviderError(
                    "Token budget exhausted",
                    reason=FailureReason.BUDGET_EXHAUSTED,
                    action=FailureAction.STOP,
                    attempts=call_attempts,
                )
        remaining = self.remaining_seconds()
        if remaining is not None and remaining <= 0:
            raise ProviderError(
                "Elapsed time budget exhausted",
                reason=FailureReason.BUDGET_EXHAUSTED,
                action=FailureAction.STOP,
                attempts=call_attempts,
            )

    def reserve_attempt(
        self,
        stage: PipelineStage,
        *,
        call_attempts: int,
        estimated_tokens: int = 0,
    ) -> None:
        needed_tokens = max(0, estimated_tokens)
        if self.tpm_limit is not None and needed_tokens > self.tpm_limit:
            raise ProviderError(
                "Estimated request exceeds TPM budget",
                reason=FailureReason.BUDGET_EXHAUSTED,
                action=FailureAction.SPLIT,
                attempts=call_attempts,
            )

        while True:
            with self._lock:
                self._check_hard_limits_locked(
                    call_attempts=call_attempts,
                    estimated_tokens=estimated_tokens,
                )
                now = self._clock()
                self._prune_windows(now)
                wait = 0.0
                if self.rpm_limit is not None and len(self._request_window) >= self.rpm_limit:
                    wait = max(wait, (self._request_window[0] + 60.0) - now)
                if self.tpm_limit is not None:
                    window_tokens = sum(tokens for _, tokens in self._token_window)
                    over_limit = (
                        window_tokens >= self.tpm_limit
                        if needed_tokens == 0
                        else (window_tokens + needed_tokens) > self.tpm_limit
                    )
                    if over_limit and self._token_window:
                        wait = max(wait, (self._token_window[0][0] + 60.0) - now)
                if wait <= 0:
                    self.attempts += 1
                    self.attempts_by_stage[stage] += 1
                    self._request_window.append(now)
                    return

            # Sleep outside the lock so concurrent worker threads can proceed or finish cleanly.
            self._sleep_within_budget(
                wait,
                call_attempts=call_attempts,
                message="Rate limit window wait exceeds elapsed budget",
                category="rate",
            )

    def record_usage(self, usage_metadata: Any) -> tuple[int | None, int | None]:
        in_tokens = getattr(usage_metadata, "prompt_token_count", None) if usage_metadata is not None else None
        cand_tokens = getattr(usage_metadata, "candidates_token_count", None) if usage_metadata is not None else None
        thought_tokens = getattr(usage_metadata, "thoughts_token_count", None) if usage_metadata is not None else None
        out_tokens = (cand_tokens + (thought_tokens or 0)) if cand_tokens is not None else None
        with self._lock:
            if in_tokens is not None:
                self.input_tokens += in_tokens
            if out_tokens is not None:
                self.output_tokens += out_tokens
            if in_tokens is None or out_tokens is None:
                self.unknown_usage_attempts += 1
            else:
                self.known_usage_attempts += 1
            added = (in_tokens or 0) + (out_tokens or 0)
            if added > 0:
                now = self._clock()
                self._prune_windows(now)
                self._token_window.append((now, added))
        return in_tokens, out_tokens

    def record_unknown_usage(self) -> None:
        with self._lock:
            self.unknown_usage_attempts += 1

    def wait_before_retry(self, *, attempt: int, server_delay: float | None = None) -> float:
        with self._lock:
            if self.rpd_limit is not None and self.attempts >= self.rpd_limit:
                raise ProviderError(
                    "Daily request quota exhausted",
                    reason=FailureReason.DAILY_QUOTA_EXHAUSTED,
                    action=FailureAction.STOP,
                    attempts=attempt,
                )
            if self.max_total_attempts is not None and self.attempts >= self.max_total_attempts:
                raise ProviderError(
                    "Request attempt budget exhausted",
                    reason=FailureReason.BUDGET_EXHAUSTED,
                    action=FailureAction.STOP,
                    attempts=attempt,
                )
            if self.max_total_tokens is not None and (self.input_tokens + self.output_tokens) >= self.max_total_tokens:
                raise ProviderError(
                    "Token budget exhausted",
                    reason=FailureReason.BUDGET_EXHAUSTED,
                    action=FailureAction.STOP,
                    attempts=attempt,
                )
            backoff = min(self.max_delay_seconds, self.base_delay_seconds * (2 ** (attempt - 1)))
            base = max(backoff, server_delay) if server_delay is not None and server_delay > 0 else backoff
            sample = min(1.0, max(0.0, float(self._rand())))
            delay = base + (base * self.jitter_ratio * sample)

        self._sleep_within_budget(
            delay,
            call_attempts=attempt,
            message="Elapsed time budget exhausted before retry",
            category="retry",
        )
        return delay


def _parse_duration_seconds(raw: Any) -> float | None:
    if isinstance(raw, (int, float)):
        val = float(raw)
        return val if math.isfinite(val) and val > 0 else None
    if not isinstance(raw, str):
        return None
    match = _DURATION_PATTERN.match(raw)
    if not match:
        return None
    amount = float(match.group(1))
    unit = (match.group(2) or "s").lower()
    val = amount / 1000.0 if unit == "ms" else amount
    return val if math.isfinite(val) and val > 0 else None


def extract_retry_delay(exc: errors.APIError) -> float | None:
    candidates: list[float] = []
    headers = getattr(getattr(exc, "response", None), "headers", None)
    if headers is not None and hasattr(headers, "items"):
        for key, value in headers.items():
            if str(key).lower() == "retry-after":
                parsed = _parse_duration_seconds(value)
                if parsed is not None:
                    candidates.append(parsed)

    details = getattr(exc, "details", None)
    if isinstance(details, dict):
        error_obj = details.get("error") if isinstance(details.get("error"), dict) else details
        detail_items = error_obj.get("details") if isinstance(error_obj, dict) else None
        if isinstance(detail_items, list):
            for item in detail_items:
                if isinstance(item, dict) and "retryDelay" in item:
                    parsed = _parse_duration_seconds(item.get("retryDelay"))
                    if parsed is not None:
                        candidates.append(parsed)

    return max(candidates) if candidates else None


def _is_daily_quota_error(exc: errors.APIError) -> bool:
    if exc.code != 429:
        return False
    texts: list[str] = []
    if isinstance(exc.message, str):
        texts.append(exc.message)
    details = getattr(exc, "details", None)
    if isinstance(details, dict):
        error_obj = details.get("error") if isinstance(details.get("error"), dict) else details
        if isinstance(error_obj, dict):
            msg = error_obj.get("message")
            if isinstance(msg, str):
                texts.append(msg)
            detail_items = error_obj.get("details")
            if isinstance(detail_items, list):
                for item in detail_items:
                    if not isinstance(item, dict):
                        continue
                    for key in ("quotaId", "quotaMetric", "description", "reason"):
                        val = item.get(key)
                        if isinstance(val, str):
                            texts.append(val)
                    violations = item.get("violations")
                    if isinstance(violations, list):
                        for violation in violations:
                            if isinstance(violation, dict):
                                for key in ("quotaId", "quotaMetric", "description"):
                                    val = violation.get(key)
                                    if isinstance(val, str):
                                        texts.append(val)
    return any(_DAILY_QUOTA_PATTERN.search(text) for text in texts)


def classify_api_error(exc: errors.APIError, *, attempts: int) -> ProviderError:
    code = exc.code
    if code == 400:
        return ProviderError(
            "Invalid request, schema or API key (HTTP 400)",
            reason=FailureReason.INVALID_REQUEST,
            action=FailureAction.STOP,
            status_code=400,
            attempts=attempts,
        )
    if code == 401:
        return ProviderError(
            "Authentication failed (HTTP 401)",
            reason=FailureReason.AUTH_ERROR,
            action=FailureAction.STOP,
            status_code=401,
            attempts=attempts,
        )
    if code == 403:
        return ProviderError(
            "Access denied (HTTP 403)",
            reason=FailureReason.AUTH_ERROR,
            action=FailureAction.STOP,
            status_code=403,
            attempts=attempts,
        )
    if code == 404:
        return ProviderError(
            "Model unavailable (HTTP 404)",
            reason=FailureReason.MODEL_UNAVAILABLE,
            action=FailureAction.STOP,
            status_code=404,
            attempts=attempts,
        )
    if code == 429:
        if _is_daily_quota_error(exc):
            return ProviderError(
                "Daily quota exhausted (HTTP 429)",
                reason=FailureReason.DAILY_QUOTA_EXHAUSTED,
                action=FailureAction.STOP,
                status_code=429,
                attempts=attempts,
                retryable=False,
            )
        return ProviderError(
            "Rate limit exceeded (HTTP 429)",
            reason=FailureReason.RATE_LIMITED,
            action=FailureAction.STOP,
            status_code=429,
            attempts=attempts,
            retryable=True,
        )
    if code in (500, 502, 503, 504) or (isinstance(code, int) and 500 <= code < 600):
        return ProviderError(
            f"Service unavailable (HTTP {code})",
            reason=FailureReason.SERVER_ERROR,
            action=FailureAction.STOP,
            status_code=code,
            attempts=attempts,
            retryable=True,
        )
    return ProviderError(
        f"Gemini API error (HTTP {code})",
        reason=FailureReason.API_ERROR,
        action=FailureAction.STOP,
        status_code=code if isinstance(code, int) else None,
        attempts=attempts,
    )


def _is_timeout_exception(exc: BaseException) -> bool:
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, TimeoutError) or "timeout" in type(current).__name__.lower():
            return True
        current = current.__cause__ or current.__context__
    return False


def classify_transport_error(exc: Exception, *, attempts: int) -> ProviderError:
    if _is_timeout_exception(exc):
        return ProviderError(
            f"Transport timeout ({type(exc).__name__})",
            reason=FailureReason.TIMEOUT,
            action=FailureAction.STOP,
            attempts=attempts,
            retryable=True,
        )
    return ProviderError(
        f"Transport or SDK failure ({type(exc).__name__})",
        reason=FailureReason.TRANSPORT_ERROR,
        action=FailureAction.STOP,
        attempts=attempts,
        retryable=False,
    )
