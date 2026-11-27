"""Frontend performance, caching, state isolation, and zero-call guarantee tests for Issue #36."""
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
import time

import pytest
from streamlit.testing.v1 import AppTest

from src import ingest, pipeline
from src.contracts import (
    FindingType,
    ProgressEvent,
    ProviderSettings,
    QualityCriterion,
    SemanticStatus,
    Severity,
    ValidationReport,
)
from src.report import load_json
from ui import dashboard, state

ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "ui" / "app.py"


def test_rules_version_caches_file_reads_across_calls(monkeypatch):
    # Reset module cache to observe initial hash followed by zero file reads.
    monkeypatch.setattr(state, "_RULES_CACHE", None)
    real_read_bytes = Path.read_bytes
    reads = []

    def counting_read_bytes(self):
        reads.append(self)
        return real_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", counting_read_bytes)
    first = dashboard._rules_version()
    initial_reads = len(reads)
    assert initial_reads > 0

    second = dashboard._rules_version()
    third = dashboard._rules_version()
    assert first == second == third
    assert len(reads) == initial_reads, "Repeated _rules_version() calls must not reread unchanged files"


def test_saved_report_parsing_and_exports_cached_across_reruns_with_zero_api_calls(report, monkeypatch):
    monkeypatch.setattr(pipeline, "run_pipeline", lambda *a, **k: pytest.fail("No Gemini call allowed"))

    parse_calls = 0
    md_calls = 0
    json_calls = 0
    orig_load = state.load_json
    orig_md = state.render_markdown
    orig_json = state.render_json

    def count_load(data):
        nonlocal parse_calls
        parse_calls += 1
        return orig_load(data)

    def count_md(rep):
        nonlocal md_calls
        md_calls += 1
        return orig_md(rep)

    def count_json(rep):
        nonlocal json_calls
        json_calls += 1
        return orig_json(rep)

    monkeypatch.setattr(state, "load_json", count_load)
    monkeypatch.setattr(state, "render_markdown", count_md)
    monkeypatch.setattr(state, "render_json", count_json)

    raw = report.model_dump_json().encode("utf-8")
    app = AppTest.from_file(str(APP), default_timeout=15).run()
    app.radio[0].set_value("Сохранённый отчёт").run()
    app.file_uploader[0].set_value(("saved.json", raw, "application/json")).run()
    assert not app.exception
    assert parse_calls == 1
    assert md_calls == 1
    assert json_calls == 1

    # Multiple interactive reruns (filtering, search, download click) must reuse session caches.
    app.selectbox[1].select("Критическая").run()
    app.selectbox[1].select("Все").run()
    search_box = next(ti for ti in app.text_input if "Поиск" in ti.label)
    search_box.set_value("F001").run()
    app.get("download_button")[0].click().run()
    app.get("download_button")[1].click().run()
    assert not app.exception

    assert parse_calls == 1, f"Expected 1 JSON parse across reruns, got {parse_calls}"
    assert md_calls == 1, f"Expected 1 Markdown render across reruns, got {md_calls}"
    assert json_calls == 1, f"Expected 1 JSON render across reruns, got {json_calls}"


def test_session_isolation_prevents_cross_session_leakage_and_resets_on_file_switch(report, monkeypatch):
    doc_a = b"# Document A\n\nR1. Rule A.\n"
    doc_b = b"# Document B\n\nR1. Rule B.\n"
    live_a = load_json(
        report.model_dump_json()
        .replace(report.metadata.document_version, sha256(doc_a).hexdigest())
        .replace('"origin":"synthetic"', '"origin":"live"')
        .replace('"request_attempts":0', '"request_attempts":1')
    )

    calls = []
    monkeypatch.setattr(dashboard, "load_settings", lambda: ProviderSettings(api_key="test-key"))
    monkeypatch.setattr(ingest, "parse", lambda name, data: live_a.document)

    def fake_run(document, settings, on_progress=None):
        calls.append(document.version.sha256)
        if on_progress:
            on_progress(ProgressEvent(stage="extract", state="completed", message="ok"))
        return live_a

    monkeypatch.setattr(pipeline, "run_pipeline", fake_run)

    # Session 1 runs analysis on doc_a.
    session1 = AppTest.from_file(str(APP), default_timeout=15).run()
    session1.file_uploader[0].set_value(("doc_a.md", doc_a, "text/markdown")).run()
    session1.checkbox[0].check().run()
    session1.button(key="launch").click().run()
    assert len(calls) == 1 and len(session1.metric) == 4
    # On subsequent rerun, duplicate submission button stays disabled and never triggers a 2nd run.
    session1.run()
    assert session1.button(key="launch").disabled
    assert len(calls) == 1

    # Switching to doc_b resets stale report and consent with zero extra API calls.
    session1.file_uploader[0].set_value(("doc_b.md", doc_b, "text/markdown")).run()
    assert not session1.metric
    assert not session1.checkbox[0].value
    assert len(calls) == 1

    # Independent Session 2 uploading the same doc_a must NOT see Session 1's cached run.
    session2 = AppTest.from_file(str(APP), default_timeout=15).run()
    session2.file_uploader[0].set_value(("doc_a.md", doc_a, "text/markdown")).run()
    assert not session2.metric, "Session 2 must never inherit reports from Session 1"
    assert not session2.checkbox[0].value
    assert len(calls) == 1


def test_large_findings_list_paginates_and_reruns_quickly(report, monkeypatch):
    monkeypatch.setattr(pipeline, "run_pipeline", lambda *a, **k: pytest.fail("No Gemini call allowed"))
    data = deepcopy(report.model_dump(mode="json"))
    data.pop("statistics", None)
    base_finding = data["findings"][0]
    first_ev = base_finding["evidence"][0]
    # Add 24 extra non-confirmed (refuted) findings to keep canonical evaluation integrity valid.
    for idx in range(2, 26):
        data["findings"].append(
            {
                "finding_id": f"F{idx:03d}",
                "kind": FindingType.AMBIGUITY.value,
                "severity": Severity.MINOR.value,
                "claim": f"Кандидат замечания №{idx} для проверки пагинации списка находок.",
                "recommendation": "Уточнить формулировку.",
                "requirement_ids": ["R001"],
                "criteria": [QualityCriterion.UNAMBIGUOUS.value],
                "evidence": [first_ev],
                "gap": None,
                "semantic_status": SemanticStatus.REFUTED.value,
                "verification_reason": "Опровергнуто проверкой контекста.",
            }
        )
    large_report = ValidationReport.model_validate(data)
    raw = large_report.model_dump_json().encode("utf-8")

    app = AppTest.from_file(str(APP), default_timeout=15).run()
    app.radio[0].set_value("Сохранённый отчёт").run()
    app.file_uploader[0].set_value(("large.json", raw, "application/json")).run()
    assert not app.exception

    # Pagination control appears for >20 findings and caps rendered finding expanders to page size (20).
    page_inputs = [ni for ni in app.number_input if "Страница находок" in ni.label]
    assert len(page_inputs) == 1
    finding_expanders = [e for e in app.expander if e.label.startswith("F")]
    assert len(finding_expanders) == 20

    # Measure rerun time when advancing to page 2 (must stay fast with session caching).
    t0 = time.monotonic()
    page_inputs[0].set_value(2).run()
    elapsed = time.monotonic() - t0
    assert not app.exception
    finding_expanders_p2 = [e for e in app.expander if e.label.startswith("F")]
    assert len(finding_expanders_p2) == 5
    assert elapsed < 5.0, f"Paginated rerun unexpectedly slow: {elapsed:.2f}s"
