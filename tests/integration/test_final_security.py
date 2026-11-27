import logging
from unittest.mock import MagicMock

import pytest
from google.genai import errors

from scripts import check_gemini
from src.contracts import PipelineStage, ProviderSettings
from src.provider import FailureReason, ProviderError, generate, load_settings
from src.provider.dto import ProbeRequest, ProbeResponse


def test_private_error_is_detached_and_smoke_output_is_safe(monkeypatch, capsys, caplog):
    client = MagicMock()
    monkeypatch.setattr("src.provider.genai.Client", client)
    settings = ProviderSettings(api_key="test-placeholder")
    monkeypatch.setattr(check_gemini, "load_settings", lambda: settings)
    error = {"message": "private-document ?key=test-placeholder", "details": [{"reason": "API_KEY_INVALID"}]}
    send = client.return_value.__enter__.return_value.models.generate_content
    def fail(**kwargs):
        logging.getLogger("httpx2").warning(error["message"])
        raise errors.APIError(400, {"error": error})

    send.side_effect = fail
    with pytest.raises(ProviderError) as exc:
        generate(PipelineStage.EXTRACT, ProbeRequest(), ProbeResponse, settings=settings)
    assert exc.value.reason == FailureReason.AUTH_ERROR
    assert exc.value.__context__ is None
    assert send.call_count == 1
    assert check_gemini.main() == 1
    visible = str(exc.value) + capsys.readouterr().out + caplog.text
    assert "private-document" not in visible
    assert "test-placeholder" not in visible


def test_invalid_model_hides_credential(monkeypatch, tmp_path):
    monkeypatch.setenv("GEMINI_API_KEY", "test-placeholder")
    monkeypatch.setenv("GEMINI_MODEL", " ")
    with pytest.raises(ValueError, match="Empty provider model") as exc:
        load_settings(tmp_path / "missing")
    assert "test-placeholder" not in str(exc.value)


def test_invalid_document_has_safe_diagnostic():
    from src.ingest import parse

    with pytest.raises(ValueError) as exc:
        parse("broken.pdf", b"private-document")
    assert "private-document" not in str(exc.value)
