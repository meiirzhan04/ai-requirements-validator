from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from google.genai import errors, types

from scripts import check_gemini
from src.contracts import PipelineStage, ProviderSettings
from src.provider import (
    FailureAction,
    FailureReason,
    ProviderError,
    QuotaBudget,
    generate,
)
from src.provider.dto import ProbeRequest, ProbeResponse


class FakeClock:
    def __init__(self, start: float = 100.0) -> None:
        self.current = start
        self.sleeps: list[float] = []

    def now(self) -> float:
        return self.current

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.current += seconds


class ReadTimeout(Exception):
    """Simulates an SDK/HTTP transport timeout that does not subclass TimeoutError."""


def _make_response(
    *,
    finish: str = "STOP",
    body: str = '{"value":7}',
    in_tokens: int | None = 12,
    out_tokens: int | None = 4,
    with_usage: bool = True,
) -> SimpleNamespace:
    usage = (
        SimpleNamespace(prompt_token_count=in_tokens, candidates_token_count=out_tokens)
        if with_usage
        else None
    )
    return SimpleNamespace(
        candidates=[SimpleNamespace(finish_reason=types.FinishReason(finish))],
        text=body,
        usage_metadata=usage,
    )


@pytest.fixture
def send(monkeypatch):
    client = MagicMock()
    monkeypatch.setattr("src.provider.genai.Client", client)
    return client.return_value.__enter__.return_value.models.generate_content


def _settings() -> ProviderSettings:
    return ProviderSettings(api_key="unit-secret-token", model="gemini-3.5-flash-lite")


def test_429_retries_with_jitter_and_tracks_unified_usage(send):
    clock = FakeClock()
    budget = QuotaBudget(
        base_delay_seconds=2.0,
        jitter_ratio=0.25,
        clock=clock.now,
        sleep=clock.sleep,
        rand=lambda: 0.5,
    )
    send.side_effect = [
        errors.APIError(429, {"error": {"message": "per-minute rate limit secret"}}),
        _make_response(in_tokens=20, out_tokens=5),
    ]

    result = generate(
        PipelineStage.EXTRACT,
        ProbeRequest(),
        ProbeResponse,
        settings=_settings(),
        budget=budget,
    )

    assert result.output.value == 7
    assert result.attempts == 2
    assert result.input_tokens == 20
    assert result.output_tokens == 5
    assert send.call_count == 2
    assert clock.sleeps == [pytest.approx(2.25)]
    assert budget.attempts == 2
    assert budget.attempts_by_stage[PipelineStage.EXTRACT] == 2
    assert budget.input_tokens == 20
    assert budget.output_tokens == 5
    assert budget.total_tokens == 25
    assert budget.known_usage_attempts == 1
    assert budget.unknown_usage_attempts == 1


def test_server_directed_delay_and_503_recovery(send):
    clock = FakeClock()
    budget = QuotaBudget(
        base_delay_seconds=1.0,
        jitter_ratio=0.2,
        clock=clock.now,
        sleep=clock.sleep,
        rand=lambda: 0.5,
    )
    rate_err = errors.APIError(
        429,
        {
            "error": {
                "status": "RESOURCE_EXHAUSTED",
                "details": [
                    {
                        "@type": "type.googleapis.com/google.rpc.RetryInfo",
                        "retryDelay": "4s",
                    }
                ],
            }
        },
        response=SimpleNamespace(headers={"Retry-After": "3"}),
    )
    send.side_effect = [
        rate_err,
        errors.APIError(503, {"error": {"message": "backend overloaded"}}),
        _make_response(in_tokens=10, out_tokens=3),
    ]

    result = generate(
        PipelineStage.INDIVIDUAL,
        ProbeRequest(),
        ProbeResponse,
        settings=_settings(),
        budget=budget,
    )

    assert result.attempts == 3
    assert clock.sleeps == [pytest.approx(4.4), pytest.approx(2.2)]
    assert budget.attempts == 3
    assert budget.unknown_usage_attempts == 2
    assert budget.known_usage_attempts == 1


def test_budget_exhaustion_on_attempts_elapsed_time_and_tokens(send):
    # 1. Total attempt cap stops before sleeping for an unreachable retry.
    clock_attempts = FakeClock()
    attempt_budget = QuotaBudget(
        max_attempts_per_call=3,
        max_total_attempts=2,
        clock=clock_attempts.now,
        sleep=clock_attempts.sleep,
        rand=lambda: 0.0,
    )
    send.side_effect = [
        errors.APIError(429, {"error": {"message": "transient"}}),
        errors.APIError(503, {"error": {"message": "transient"}}),
    ]
    with pytest.raises(ProviderError) as exc_info:
        generate(
            PipelineStage.EXTRACT,
            ProbeRequest(),
            ProbeResponse,
            settings=_settings(),
            budget=attempt_budget,
        )
    assert exc_info.value.reason == FailureReason.BUDGET_EXHAUSTED
    assert exc_info.value.action == FailureAction.STOP
    assert exc_info.value.attempts == 2
    assert clock_attempts.sleeps == [pytest.approx(1.0)]
    assert send.call_count == 2

    # 2. Server retry delay exceeding remaining elapsed budget aborts immediately.
    send.reset_mock()
    clock_time = FakeClock()
    time_budget = QuotaBudget(
        max_elapsed_seconds=3.0,
        clock=clock_time.now,
        sleep=clock_time.sleep,
        rand=lambda: 0.0,
    )
    send.side_effect = [
        errors.APIError(
            429,
            {"error": {"message": "slow down"}},
            response=SimpleNamespace(headers={"Retry-After": "5"}),
        )
    ]
    with pytest.raises(ProviderError) as time_err:
        generate(
            PipelineStage.EXTRACT,
            ProbeRequest(),
            ProbeResponse,
            settings=_settings(),
            budget=time_budget,
        )
    assert time_err.value.reason == FailureReason.BUDGET_EXHAUSTED
    assert time_err.value.should_stop
    assert clock_time.sleeps == []
    assert send.call_count == 1

    # 3. Shared token budget stops subsequent stage calls once exhausted.
    send.reset_mock()
    clock_tokens = FakeClock()
    token_budget = QuotaBudget(
        max_total_tokens=25,
        clock=clock_tokens.now,
        sleep=clock_tokens.sleep,
    )
    send.side_effect = [_make_response(in_tokens=20, out_tokens=5)]
    first = generate(
        PipelineStage.EXTRACT,
        ProbeRequest(),
        ProbeResponse,
        settings=_settings(),
        budget=token_budget,
    )
    assert first.output.value == 7
    assert token_budget.total_tokens == 25

    with pytest.raises(ProviderError) as tok_err:
        generate(
            PipelineStage.HOLISTIC,
            ProbeRequest(),
            ProbeResponse,
            settings=_settings(),
            budget=token_budget,
        )
    assert tok_err.value.reason == FailureReason.BUDGET_EXHAUSTED
    assert tok_err.value.attempts == 0
    assert send.call_count == 1


def test_rate_window_pacing_and_budget_cutoff(send):
    clock = FakeClock()
    budget = QuotaBudget(
        rpm_limit=1,
        max_elapsed_seconds=70.0,
        clock=clock.now,
        sleep=clock.sleep,
    )
    send.side_effect = [_make_response(), _make_response()]

    generate(PipelineStage.EXTRACT, ProbeRequest(), ProbeResponse, settings=_settings(), budget=budget)
    generate(PipelineStage.VERIFY, ProbeRequest(), ProbeResponse, settings=_settings(), budget=budget)

    assert clock.sleeps == [pytest.approx(60.0)]
    assert budget.attempts == 2

    with pytest.raises(ProviderError) as err:
        generate(PipelineStage.CLARIFY, ProbeRequest(), ProbeResponse, settings=_settings(), budget=budget)
    assert err.value.reason == FailureReason.BUDGET_EXHAUSTED


def test_403_and_daily_quota_stop_without_retries(send):
    clock = FakeClock()
    budget = QuotaBudget(clock=clock.now, sleep=clock.sleep)
    send.side_effect = errors.APIError(403, {"error": {"message": "private-project-id"}})

    with pytest.raises(ProviderError) as forbidden:
        generate(
            PipelineStage.EXTRACT,
            ProbeRequest(),
            ProbeResponse,
            settings=_settings(),
            budget=budget,
        )
    assert forbidden.value.reason == FailureReason.AUTH_ERROR
    assert forbidden.value.action == FailureAction.STOP
    assert forbidden.value.status_code == 403
    assert forbidden.value.attempts == 1
    assert "private-project-id" not in str(forbidden.value)
    assert send.call_count == 1
    assert clock.sleeps == []

    # Daily quota 429 must not retry even when retry budget remains.
    send.reset_mock()
    send.side_effect = errors.APIError(
        429,
        {
            "error": {
                "status": "RESOURCE_EXHAUSTED",
                "message": "Quota exceeded for metric",
                "details": [
                    {
                        "@type": "type.googleapis.com/google.rpc.QuotaFailure",
                        "violations": [
                            {"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}
                        ],
                    }
                ],
            }
        },
    )
    with pytest.raises(ProviderError) as daily:
        generate(
            PipelineStage.EXTRACT,
            ProbeRequest(),
            ProbeResponse,
            settings=_settings(),
            budget=budget,
        )
    assert daily.value.reason == FailureReason.DAILY_QUOTA_EXHAUSTED
    assert daily.value.should_stop
    assert daily.value.attempts == 1
    assert send.call_count == 1
    assert clock.sleeps == []
    assert budget.attempts == 2
    assert budget.unknown_usage_attempts == 2


def test_timeout_retries_then_fails_with_typed_reason_and_unknown_usage(send):
    clock = FakeClock()
    budget = QuotaBudget(
        max_attempts_per_call=3,
        base_delay_seconds=1.0,
        jitter_ratio=0.1,
        clock=clock.now,
        sleep=clock.sleep,
        rand=lambda: 1.0,
    )
    send.side_effect = [
        TimeoutError("internal-endpoint-secret"),
        ReadTimeout("proxy-header-secret"),
        TimeoutError("socket-detail-secret"),
    ]

    with pytest.raises(ProviderError) as err:
        generate(
            PipelineStage.EXTRACT,
            ProbeRequest(),
            ProbeResponse,
            settings=_settings(),
            budget=budget,
        )

    assert err.value.reason == FailureReason.TIMEOUT
    assert err.value.action == FailureAction.STOP
    assert err.value.attempts == 3
    assert "secret" not in str(err.value)
    assert send.call_count == 3
    assert clock.sleeps == [pytest.approx(1.1), pytest.approx(2.2)]
    assert budget.attempts == 3
    assert budget.unknown_usage_attempts == 3
    assert budget.reported_input_tokens is None
    assert budget.reported_output_tokens is None


def test_truncation_max_tokens_accounts_usage_and_requests_batch_split(send):
    clock = FakeClock()
    budget = QuotaBudget(clock=clock.now, sleep=clock.sleep)
    send.side_effect = [
        _make_response(finish="MAX_TOKENS", body='{"value":', in_tokens=150, out_tokens=512),
        _make_response(finish="MAX_TOKENS", body="", with_usage=False),
    ]

    with pytest.raises(ProviderError) as first_err:
        generate(
            PipelineStage.EXTRACT,
            ProbeRequest(),
            ProbeResponse,
            settings=_settings(),
            budget=budget,
        )
    assert first_err.value.reason == FailureReason.TRUNCATED
    assert first_err.value.action == FailureAction.SPLIT
    assert first_err.value.should_split
    assert not first_err.value.should_stop
    assert first_err.value.attempts == 1
    assert clock.sleeps == []
    assert send.call_count == 1
    assert budget.attempts == 1
    assert budget.input_tokens == 150
    assert budget.output_tokens == 512
    assert budget.unknown_usage_attempts == 0

    with pytest.raises(ProviderError) as second_err:
        generate(
            PipelineStage.EXTRACT,
            ProbeRequest(),
            ProbeResponse,
            settings=_settings(),
            budget=budget,
        )
    assert second_err.value.reason == FailureReason.TRUNCATED
    assert second_err.value.should_split
    assert budget.attempts == 2
    assert budget.unknown_usage_attempts == 1


def test_smoke_probe_stays_single_shot_and_redacts_secret_in_output(monkeypatch, send, capsys):
    clock = FakeClock()
    budget = QuotaBudget(max_attempts_per_call=3, clock=clock.now, sleep=clock.sleep)
    send.side_effect = errors.APIError(429, {"error": {"message": "rate-limited"}})

    with pytest.raises(ProviderError) as smoke_err:
        generate(
            PipelineStage.SMOKE,
            ProbeRequest(),
            ProbeResponse,
            settings=_settings(),
            budget=budget,
        )
    assert smoke_err.value.reason == FailureReason.RATE_LIMITED
    assert smoke_err.value.attempts == 1
    assert send.call_count == 1
    assert clock.sleeps == []

    send.reset_mock()
    send.side_effect = None
    send.return_value = _make_response(in_tokens=8, out_tokens=2)
    monkeypatch.setattr(check_gemini, "load_settings", _settings)
    assert check_gemini.main() == 0
    out = capsys.readouterr().out
    assert out.startswith("PASS")
    assert "model=gemini-3.5-flash-lite" in out
    assert "value=7" in out
    assert "input_tokens=8" in out
    assert "output_tokens=2" in out
    assert "unit-secret-token" not in out

    with pytest.raises(ValueError, match="between 1 and 3"):
        QuotaBudget(max_attempts_per_call=4)
