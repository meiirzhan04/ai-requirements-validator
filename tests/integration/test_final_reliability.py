from types import SimpleNamespace
from unittest.mock import MagicMock

from google.genai import errors, types

from src.contracts import PipelineStage, ProviderSettings
from src.provider import FailureReason, QuotaBudget, generate, provider_session
from src.provider.dto import ProbeRequest, ProbeResponse


def test_nested_sessions_recover_and_measure_only_sdk_time(monkeypatch):
    factory = MagicMock()
    monkeypatch.setattr("src.provider.genai.Client", factory)
    outer, inner = MagicMock(), MagicMock()
    factory.side_effect = [outer, inner]
    now = [0]

    def advance(seconds):
        now[0] += seconds

    budget = QuotaBudget(clock=lambda: now[0], sleep=advance, rand=lambda: 0)
    send = outer.__enter__.return_value.models.generate_content
    response = SimpleNamespace(candidates=[SimpleNamespace(finish_reason=types.FinishReason.STOP)],
                               usage_metadata=None, text='{"value":7}')

    def call(**kwargs):
        advance(2)
        if send.call_count == 1:
            raise errors.APIError(503, {})
        return response

    send.side_effect = call
    settings = ProviderSettings(api_key="outer-placeholder")
    with provider_session(settings):
        with provider_session(ProviderSettings(api_key="inner-placeholder")):
            generate(PipelineStage.EXTRACT, ProbeRequest(), ProbeResponse, settings=settings, budget=budget)
        inner.__exit__.assert_called_once()
        outer.__exit__.assert_not_called()
    outer.__exit__.assert_called_once()
    snapshot = budget.metrics_snapshot()
    assert snapshot["api_latency_seconds"] == 4
    assert snapshot["retry_wait_seconds"] == 1
    assert snapshot["retry_attempts"] == 1
    assert snapshot["failures_by_reason"] == {FailureReason.SERVER_ERROR: 1}

def test_session_exit_waits_for_active_worker(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    factory = MagicMock()
    monkeypatch.setattr("src.provider.genai.Client", factory)
    ctx = factory.return_value
    entered, release, exiting = Event(), Event(), Event()
    settings = ProviderSettings(api_key="test-placeholder")

    def call(**kwargs):
        entered.set()
        assert release.wait(5)
        return SimpleNamespace(candidates=[SimpleNamespace(finish_reason=types.FinishReason.STOP)],
                               usage_metadata=None, text='{"value":7}')

    ctx.__enter__.return_value.models.generate_content.side_effect = call
    with ThreadPoolExecutor(max_workers=2) as pool:
        def owner():
            with provider_session(settings):
                result = pool.submit(generate, PipelineStage.EXTRACT, ProbeRequest(), ProbeResponse,
                                     settings=settings)
                assert entered.wait(5)
                exiting.set()
            return result.result()

        finished = pool.submit(owner)
        try:
            assert exiting.wait(5)
            ctx.__exit__.assert_not_called()
        finally:
            release.set()
        assert finished.result(timeout=5).output.value == 7
    ctx.__exit__.assert_called_once()
