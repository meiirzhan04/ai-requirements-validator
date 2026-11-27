"""Comprehensive UI/UX, design-system, and workflow tests for Issue #36."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
import re

import pytest
from streamlit.testing.v1 import AppTest

from src import ingest, pipeline
from src.contracts import (
    ExecutionStatus,
    ProgressEvent,
    ProviderSettings,
    Readiness,
    ValidationReport,
)
from src.report import load_json
from ui import clarification, dashboard, theme

ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "ui" / "app.py"
UI_FILES = [
    ROOT / "ui" / "app.py",
    ROOT / "ui" / "theme.py",
    ROOT / "ui" / "state.py",
    ROOT / "ui" / "components.py",
    ROOT / "ui" / "dashboard.py",
    ROOT / "ui" / "clarification.py",
]
EMOJI_RE = re.compile(r"[\U0001F300-\U0001FAFF\u2600-\u27BF]")
CDN_RE = re.compile(r"https?://|@import\s+url", re.IGNORECASE)


def _live_copy(report: ValidationReport, raw_doc: bytes | None = None) -> ValidationReport:
    payload = report.model_dump_json()
    if raw_doc is not None:
        payload = payload.replace(report.metadata.document_version, sha256(raw_doc).hexdigest())
    payload = payload.replace('"origin":"synthetic"', '"origin":"live"').replace(
        '"request_attempts":0', '"request_attempts":2'
    )
    return load_json(payload)


def test_design_system_has_no_emojis_no_external_cdns_and_preserves_public_exports():
    for path in UI_FILES:
        text = path.read_text(encoding="utf-8")
        assert not EMOJI_RE.search(text), f"Decorative emoji found in {path.name}"
    assert not CDN_RE.search(theme.CSS), "Theme CSS must not reference external CDNs or remote fonts"
    assert ":focus-visible" in theme.CSS, "Theme CSS must enforce visible keyboard focus"
    assert "@media (max-width: 768px)" in theme.CSS
    assert "@media (max-width: 480px)" in theme.CSS

    for attr in (
        "load_settings",
        "inject_theme",
        "render_header",
        "open_saved",
        "analyze",
        "show_report",
        "STAGE",
        "STATE",
        "_rules_version",
    ):
        assert hasattr(dashboard, attr), f"Missing required dashboard export: {attr}"
    for attr in ("render_clarification", "recheck_summary", "recheck_diff"):
        assert hasattr(clarification, attr), f"Missing required clarification export: {attr}"


def test_upload_boundary_missing_key_and_real_progress_with_retry_notice(report, monkeypatch):
    raw_doc = b"# Spec\n\nR1. Test rule.\n"
    live = _live_copy(report, raw_doc)
    monkeypatch.setattr(ingest, "parse", lambda name, data: live.document)

    # 1. Missing API key shows explicit diagnostic and blocks execution without calling pipeline.
    monkeypatch.setattr(dashboard, "load_settings", lambda: ProviderSettings(api_key=None))
    monkeypatch.setattr(pipeline, "run_pipeline", lambda *a, **k: pytest.fail("Must not run without key"))
    app = AppTest.from_file(str(APP), default_timeout=15).run()
    assert not app.exception
    assert any("10 МиБ" in m.value for m in app.markdown)
    app.file_uploader[0].set_value(("spec.md", raw_doc, "text/markdown")).run()
    assert app.button(key="launch").disabled
    assert any("GEMINI_API_KEY" in w.value for w in app.warning)
    app.checkbox[0].check().run()
    app.button(key="launch").click().run()
    assert any("Ключ Gemini отсутствует" in err.value for err in app.error)

    # 2. Valid key streams actual ProgressEvent stages including provider retry/wait notice.
    monkeypatch.setattr(dashboard, "load_settings", lambda: ProviderSettings(api_key="live-test-key"))

    def fake_run(document, settings, on_progress=None):
        if on_progress:
            on_progress(ProgressEvent(stage="ingest", state="completed", message="Проверено блоков: 2"))
            on_progress(ProgressEvent(stage="extract", state="started", message="Повтор после HTTP 503 (ожидание 1.0 с)"))
            on_progress(ProgressEvent(stage="extract", state="completed", message="Извлечено требований: 2"))
            ready = Barrier(2)

            def audit(stage):
                ready.wait(timeout=5)
                on_progress(ProgressEvent(stage=stage, state="completed", message=stage))

            with ThreadPoolExecutor(max_workers=2) as pool:
                list(pool.map(audit, ["individual_audit", "set_audit"]))
            on_progress(ProgressEvent(stage="report", state="completed", message="Отчёт готов"))
        return live

    monkeypatch.setattr(pipeline, "run_pipeline", fake_run)
    app2 = AppTest.from_file(str(APP), default_timeout=15).run()
    app2.file_uploader[0].set_value(("spec.md", raw_doc, "text/markdown")).run()
    app2.checkbox[0].check().run()
    app2.button(key="launch").click().run()
    assert not app2.exception

    all_text = "\n".join(t.value for t in app2.text)
    assert "individual_audit" in all_text and "set_audit" in all_text
    all_md = "\n".join(m.value for m in app2.markdown)
    assert "individual_audit" in all_text and "set_audit" in all_text
    assert "Чтение документа" in all_text
    assert "Извлечение требований" in all_text
    assert "Фактическое время:" in all_md
    assert "Ожидание / повтор провайдера:" in all_md
    assert "Текущий запуск Gemini" in "\n".join(c.value for c in app2.caption)


def test_executive_verdict_separates_readiness_from_execution_for_partial_and_failed(report, monkeypatch):
    monkeypatch.setattr(pipeline, "run_pipeline", lambda *a, **k: pytest.fail("Saved mode must not call pipeline"))

    # Partial run with confirmed critical blocker: readiness=NO_GO, execution=PARTIAL.
    partial_data = deepcopy(_live_copy(report).model_dump(mode="json"))
    partial_data.pop("statistics", None)
    partial_data["coverage"]["execution"] = ExecutionStatus.PARTIAL.value
    partial_data["coverage"]["incomplete_reasons"] = ["Пакет indiv-2 не завершён из-за HTTP 503."]
    partial_report = ValidationReport.model_validate(partial_data)

    app = AppTest.from_file(str(APP), default_timeout=15).run()
    app.radio[0].set_value("Сохранённый отчёт").run()
    app.file_uploader[0].set_value(("partial.json", partial_report.model_dump_json().encode(), "application/json")).run()
    assert not app.exception
    md_blob = "\n".join(m.value for m in app.markdown)
    assert "Вердикт готовности документа" in md_blob
    assert "Полнота выполнения аудита" in md_blob
    assert "Не готово" in md_blob
    assert "Частичный анализ" in md_blob
    assert any("Сохранённый результат запуска" in c.value for c in app.caption)

    # Failed run: readiness=UNDETERMINED, execution=FAILED.
    failed_data = deepcopy(_live_copy(report).model_dump(mode="json"))
    failed_data.pop("statistics", None)
    failed_data["requirements"] = []
    failed_data["evaluations"] = []
    failed_data["findings"] = []
    failed_data["questions"] = []
    failed_data["coverage"]["execution"] = ExecutionStatus.FAILED.value
    failed_data["coverage"]["extraction_blocks"]["completed"] = []
    failed_data["coverage"]["context_blocks"] = {}
    failed_data["coverage"]["individual_requirements"] = {"planned": [], "completed": []}
    failed_data["coverage"]["audit_batches"]["completed"] = []
    failed_data["coverage"]["cross_section_groups"]["completed"] = []
    failed_data["coverage"]["incomplete_reasons"] = ["Превышен дневной лимит провайдера."]
    failed_data["verdict"] = {
        "readiness": Readiness.UNDETERMINED.value,
        "rationale": "Анализ не завершён, решение о готовности невозможно.",
        "next_actions": ["Повторить запуск после восстановления квоты."],
    }
    failed_report = ValidationReport.model_validate(failed_data)

    app.file_uploader[0].set_value(("failed.json", failed_report.model_dump_json().encode(), "application/json")).run()
    assert not app.exception
    md_failed = "\n".join(m.value for m in app.markdown)
    assert "Недостаточно данных" in md_failed
    assert "Сбой анализ" in md_failed


def test_findings_search_filters_and_no_unimplemented_export_buttons(report, monkeypatch):
    monkeypatch.setattr(pipeline, "run_pipeline", lambda *a, **k: pytest.fail("Filtering must not call pipeline"))
    app = AppTest.from_file(str(APP), default_timeout=15).run()
    app.radio[0].set_value("Сохранённый отчёт").run()
    app.file_uploader[0].set_value(("sample.json", report.model_dump_json().encode(), "application/json")).run()
    assert not app.exception

    # Only the two real canonical export formats (Markdown and JSON) are exposed.
    downloads = app.get("download_button")
    assert len(downloads) == 2

    # Search filtering matches by finding ID and filters out non-matching terms cleanly.
    search_box = next(ti for ti in app.text_input if "Поиск" in ti.label)
    search_box.set_value("несуществующий-термин").run()
    assert not app.exception
    assert any("По выбранному фильтру находок нет" in info.value for info in app.info)

    search_box = next(ti for ti in app.text_input if "Поиск" in ti.label)
    search_box.set_value("F001").run()
    assert not app.exception
    assert any("F001" in exp.label for exp in app.expander)


def test_interrupted_render_resumes_without_duplicate_work(monkeypatch):
    from types import SimpleNamespace
    from ui import progress

    monkeypatch.setattr(progress, "st", SimpleNamespace(session_state={}))
    calls = []
    event = ProgressEvent(stage="extract", state="completed", message="extracted")

    def run(document, settings, on_progress):
        calls.append(document)
        on_progress(event)
        return document

    class Tracker:
        def start_at(self, started):
            self.started = started

        def handle(self, received, timestamp):
            assert received == event and timestamp >= self.started

    class Interrupted(Tracker):
        def handle(self, received, timestamp):
            raise InterruptedError

    monkeypatch.setattr(pipeline, "run_pipeline", run)
    with pytest.raises(InterruptedError):
        progress.run_with_progress("revision-1", "old", None, Interrupted())
    assert progress.run_with_progress("revision-2", "new", None, Tracker()) == "new"
    assert progress.run_with_progress("revision-1", "old", None, Tracker()) == "old"
    assert calls == ["old", "new"]


def test_worker_diagnostics_exclude_private_exception(monkeypatch, caplog):
    from unittest.mock import Mock
    from ui import progress

    monkeypatch.setattr(progress, "st", Mock(session_state={}))
    monkeypatch.setattr(pipeline, "run_pipeline", Mock(side_effect=ValueError("private-document")))
    with pytest.raises(ValueError):
        progress.run_with_progress("revision", None, None, Mock())
    assert "Analysis worker failed (ValueError)" in caplog.text
    assert "private-document" not in caplog.text
