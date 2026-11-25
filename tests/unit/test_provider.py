from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from google.genai import errors, types

from scripts import check_gemini
from src.contracts import PipelineStage, ProviderSettings
from src.provider import ProviderError, generate, load_settings
from src.provider.dto import ProbeRequest, ProbeResponse


def test_settings_without_credentials(monkeypatch, tmp_path):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    assert load_settings(tmp_path / "absent").api_key is None


def test_smoke_without_key(monkeypatch, capsys):
    monkeypatch.setattr(check_gemini, "load_settings", ProviderSettings)
    monkeypatch.setattr(check_gemini, "generate", lambda *a, **k: pytest.fail("unexpected network request"))
    assert check_gemini.main() == 0
    assert "SKIPPED" in capsys.readouterr().out


def test_env_file_and_secret_redaction(monkeypatch, tmp_path):
    for key in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GEMINI_MODEL"):
        monkeypatch.delenv(key, raising=False)
    path = tmp_path / ".env"
    path.write_text("GEMINI_API_KEY=unit-test-placeholder\nGEMINI_MODEL=file-model\n")
    monkeypatch.setenv("GEMINI_MODEL", "env-model")
    settings = load_settings(path)
    assert settings.api_key.get_secret_value() == "unit-test-placeholder"
    assert settings.model == "env-model"
    assert "unit-test-placeholder" not in repr(settings) + settings.model_dump_json()


@pytest.fixture
def send(monkeypatch):
    client = MagicMock()
    monkeypatch.setattr("src.provider.genai.Client", client)
    return client.return_value.__enter__.return_value.models.generate_content


def invoke():
    return generate(PipelineStage.SMOKE, ProbeRequest(), ProbeResponse, settings=ProviderSettings(api_key="local-test"))


@pytest.mark.parametrize(("finish", "body", "valid"), [
    ("STOP", '{"value":7}', True), ("STOP", "invalid", False),
    ("STOP", '{"value":"7"}', False), ("MAX_TOKENS", '{"value":7}', False),
    ("SAFETY", "", False),
])
def test_provider_structured_output(send, finish, body, valid):
    send.return_value = SimpleNamespace(candidates=[SimpleNamespace(finish_reason=types.FinishReason(finish))],
                                        text=body, usage_metadata=None)
    if valid:
        assert invoke().output.value == 7
    else:
        with pytest.raises(ProviderError):
            invoke()
    assert send.call_count == 1


@pytest.mark.parametrize("code", [400, 401, 403, 404, 429, 503])
def test_provider_errors_are_safe(send, code):
    send.side_effect = errors.APIError(code, {"error": {"message": "hidden-test-detail"}})
    with pytest.raises(ProviderError) as err:
        invoke()
    assert str(code) in str(err.value) and "hidden-test-detail" not in str(err.value)


def test_network_failure_is_safe(send):
    send.side_effect = TimeoutError("hidden-test-detail")
    with pytest.raises(ProviderError, match="Transport") as err:
        invoke()
    assert "hidden-test-detail" not in str(err.value)


@pytest.mark.parametrize(("value", "code", "status"), [(7, 0, "PASS"), (8, 1, "FAIL")])
def test_smoke_requires_expected_value(monkeypatch, send, capsys, value, code, status):
    monkeypatch.setattr(check_gemini, "load_settings", lambda: ProviderSettings(api_key="local-test"))
    send.return_value = SimpleNamespace(
        candidates=[SimpleNamespace(finish_reason=types.FinishReason.STOP)],
        text=ProbeResponse(value=value).model_dump_json(), usage_metadata=None,
    )
    assert check_gemini.main() == code
    assert capsys.readouterr().out.startswith(status)
