from hashlib import sha256
from pathlib import Path

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

from src import ingest, pipeline
from src.contracts import ProgressEvent, ProviderSettings
from src.report import load_json, render_json, render_markdown
from ui import dashboard


APP = Path(__file__).resolve().parents[2] / "ui" / "app.py"


def upload(app, filename, data):
    app.file_uploader[0].set_value((filename, data, "application/octet-stream")).run()
    assert not app.exception


def test_user_launch_filters_download_and_revision(report, monkeypatch):
    data = b"# Public test"
    sample = report.model_dump_json().replace(report.metadata.document_version, sha256(data).hexdigest())
    live = load_json(sample.replace('"origin":"synthetic"', '"origin":"live"')
                    .replace('"request_attempts":0', '"request_attempts":1'))
    monkeypatch.setattr(dashboard, "load_settings", lambda: ProviderSettings(api_key="test"))
    monkeypatch.setattr(ingest, "parse", lambda name, content: live.document)
    calls = []

    def run(document, settings, on_progress):
        calls.append((document, settings.model))
        on_progress(ProgressEvent(stage="extract", state="started", message="test"))
        return live

    exports = {}
    download = st.download_button

    def capture(label, data, file_name, *args, **kwargs):
        exports[file_name] = data
        return download(label, data, file_name, *args, **kwargs)

    monkeypatch.setattr(st, "download_button", capture)
    monkeypatch.setattr(pipeline, "run_pipeline", run)
    app = AppTest.from_file(str(APP), default_timeout=15).run()
    upload(app, "public.md", data)
    assert app.button(key="launch").disabled and not calls
    app.checkbox[0].check().run()
    app.button(key="launch").click().run()
    assert not app.exception and len(calls) == 1
    assert any("Извлечение требований" in text.value for text in app.text)
    assert exports == {"report.md": render_markdown(live), "report.json": render_json(live)}
    app.selectbox[1].select("Незначительная").run()
    app.get("download_button")[1].click().run()
    assert app.button(key="launch").disabled and len(calls) == 1
    upload(app, "new.md", b"another revision")
    assert not app.metric and not app.checkbox[0].value and len(calls) == 1


def test_saved_reports_never_generate_and_invalid_json_stays_visible(report, monkeypatch):
    monkeypatch.setattr(pipeline, "run_pipeline", lambda *a, **k: pytest.fail("Unexpected call"))
    app = AppTest.from_file(str(APP), default_timeout=15).run()
    app.radio[0].set_value("Сохранённый отчёт").run()
    upload(app, "old.json", b"{}")
    assert app.error and not app.metric
    upload(app, "example.json", report.model_dump_json().encode())
    assert any("Синтетический пример" in caption.value for caption in app.caption)
    assert len(app.metric) == 4 and len(app.get("download_button")) == 2
    app.run()
    assert not app.exception


def test_safe_failure_and_no_auto_retry(report, monkeypatch):
    calls = []
    monkeypatch.setattr(dashboard, "load_settings", lambda: ProviderSettings(api_key="test"))
    monkeypatch.setattr(ingest, "parse", lambda name, data: report.document)

    def fail(*args, **kwargs):
        calls.append(1)
        raise RuntimeError("private document text")

    monkeypatch.setattr(pipeline, "run_pipeline", fail)
    app = AppTest.from_file(str(APP), default_timeout=15).run()
    upload(app, "public.md", b"# Public")
    app.checkbox[0].check().run()
    app.button(key="launch").click().run()
    assert app.status[0].state == "error" and not app.metric and not app.exception
    assert "private document text" not in app.status[0].label
    app.run()
    assert len(calls) == 1 and app.error
