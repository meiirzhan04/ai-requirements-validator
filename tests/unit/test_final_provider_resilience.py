from concurrent.futures import ThreadPoolExecutor
from email.utils import formatdate
from types import SimpleNamespace

import pytest
from google.genai import errors

from src.contracts import PipelineStage
from src.provider import FailureReason, ProviderError, QuotaBudget, extract_retry_delay


def test_http_date_and_shared_recovery(monkeypatch):
    monkeypatch.setattr("src.provider.runtime.time.time", lambda: 1000)
    headers = {"Retry-After": formatdate(1100, usegmt=True)}
    exc = errors.APIError(503, {}, response=SimpleNamespace(headers=headers))
    now = [0.0]

    def advance(seconds):
        now[0] += seconds

    budget = QuotaBudget(clock=lambda: now[0], sleep=advance, rand=lambda: 1, max_delay_seconds=3)
    assert extract_retry_delay(exc) == 100
    assert budget.wait_before_retry(attempt=1, server_delay=100) == 3
    budget.reserve_attempt(PipelineStage.EXTRACT, call_attempts=1)
    budget.reserve_attempt(PipelineStage.EXTRACT, call_attempts=0)
    assert budget.rate_limit_wait_seconds == 1
    assert budget.retry_attempts == 1


def test_concurrent_estimates_respect_total_limit():
    budget = QuotaBudget(max_total_tokens=10)
    budget.reserve_attempt(PipelineStage.EXTRACT, call_attempts=0, estimated_tokens=8)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(budget.reserve_attempt, PipelineStage.VERIFY, call_attempts=0, estimated_tokens=3)
        with pytest.raises(ProviderError) as err:
            future.result()
    assert err.value.reason == FailureReason.BUDGET_EXHAUSTED
    budget.record_unknown_usage()
    budget.reserve_attempt(PipelineStage.VERIFY, call_attempts=0, estimated_tokens=10)


def test_submillisecond_budget():
    budget = QuotaBudget(max_elapsed_seconds=0.0009, clock=lambda: 0)
    with pytest.raises(ProviderError):
        budget.effective_timeout_ms(1000)
