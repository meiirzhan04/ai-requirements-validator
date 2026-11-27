"""Release-integration regression tests: threading safety, AI improvement UI, exports, and security."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from hashlib import sha256
import logging
from pathlib import Path
import threading
import time

import pymupdf
import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

from src import ingest, pipeline
from src.clarification import (
    ImprovementRewriteOutput,
    apply_changes,
    draft_remediation_proposal,
)
from src.contracts import (
    INDIVIDUAL,
    WHOLE_SET,
    Answer,
    CriterionStatus,
    EvidenceRef,
    ExecutionStatus,
    FindingType,
    GroundingStatus,
    PipelineStage,
    ProgressEvent,
    ProposedChange,
    ProviderSettings,
    QualityCriterion,
    Readiness,
    RequirementCategory,
    SemanticStatus,
    Severity,
    StageResult,
    ValidationReport,
)
from src.pipeline import execute_pipeline
from src.provider.dto import (
    ContextBlockDisposition,
    ExtractedItem,
    ExtractionOutput,
    HolisticAuditOutput,
    HolisticCriterionCandidate,
    IndividualAuditOutput,
    IndividualCriterionCandidate,
)
from src.report import load_json
from ui import clarification, dashboard

APP = Path(__file__).resolve().parents[2] / "ui" / "app.py"
ORDERED_INDIVIDUAL = sorted(INDIVIDUAL, key=lambda c: c.value)
ORDERED_WHOLE = sorted(WHOLE_SET, key=lambda c: c.value)


def _live_report(report: ValidationReport, raw_doc: bytes | None = None) -> ValidationReport:
    payload = report.model_dump_json()
    if raw_doc is not None:
        payload = payload.replace(report.metadata.document_version, sha256(raw_doc).hexdigest())
    payload = payload.replace('"origin":"synthetic"', '"origin":"live"').replace(
        '"request_attempts":0', '"request_attempts":2'
    )
    return load_json(payload)


def _all_indiv(req_id: str) -> list[IndividualCriterionCandidate]:
    return [
        IndividualCriterionCandidate(
            requirement_id=req_id,
            criterion=c,
            status=CriterionStatus.PASS,
            rationale="Требование проверено.",
            local_finding_keys=[],
        )
        for c in ORDERED_INDIVIDUAL
    ]


def _all_whole() -> list[HolisticCriterionCandidate]:
    return [
        HolisticCriterionCandidate(
            criterion=c,
            status=CriterionStatus.PASS,
            rationale="Набор согласован.",
            local_finding_keys=[],
        )
        for c in ORDERED_WHOLE
    ]


def test_parallel_pipeline_emits_progress_strictly_on_caller_thread_and_survives_callback_error():
    doc = ingest.parse("concurrent.md", "# Раздел\n\nFR-1: Система хранит журнал событий 30 дней.\n".encode("utf-8"))
    heading_blk, rule_blk = doc.version.blocks
    caller_thread_id = threading.get_ident()
    observed_threads: list[tuple[PipelineStage, int]] = []

    def scripted_generate(stage, payload, output_type, *, settings=None, **kwargs):
        if stage == PipelineStage.EXTRACT:
            return StageResult[ExtractionOutput](
                stage=stage,
                output=ExtractionOutput(
                    items=[
                        ExtractedItem(
                            block_id=rule_blk.block_id,
                            original_id="FR-1",
                            category=RequirementCategory.DATA,
                            original_quote=rule_blk.text,
                            summary="Хранение журнала событий 30 дней.",
                        )
                    ],
                    context_blocks=[ContextBlockDisposition(block_id=heading_blk.block_id, reason="Заголовок.")],
                ),
                attempts=1,
            )
        if stage == PipelineStage.INDIVIDUAL:
            time.sleep(0.03)
            return StageResult[IndividualAuditOutput](
                stage=stage,
                output=IndividualAuditOutput(evaluations=_all_indiv("R001")),
                attempts=1,
            )
        if stage == PipelineStage.HOLISTIC:
            time.sleep(0.03)
            return StageResult[HolisticAuditOutput](
                stage=stage,
                output=HolisticAuditOutput(evaluations=_all_whole()),
                attempts=1,
            )
        raise AssertionError(f"Unexpected stage {stage}")

    def strict_progress(event: ProgressEvent) -> None:
        observed_threads.append((event.stage, threading.get_ident()))
        assert threading.get_ident() == caller_thread_id, (
            f"ProgressEvent for {event.stage} emitted from worker thread instead of caller thread!"
        )

    # 1. Parallel execution (max_concurrency=2) delivers every stage event on caller_thread_id.
    report = execute_pipeline(
        doc,
        ProviderSettings(api_key="test-key"),
        on_progress=strict_progress,
        generate_fn=scripted_generate,
        max_concurrency=2,
    )
    assert report.coverage.execution == ExecutionStatus.COMPLETE
    assert report.verdict.readiness == Readiness.GO
    stages_seen = {stage for stage, _ in observed_threads}
    assert {PipelineStage.INDIVIDUAL, PipelineStage.HOLISTIC, PipelineStage.REPORT} <= stages_seen
    assert all(tid == caller_thread_id for _, tid in observed_threads)

    # 2. A broken progress callback must never crash a healthy pipeline run.
    def broken_progress(_event: ProgressEvent) -> None:
        raise RuntimeError("missing ScriptRunContext simulated failure")

    report2 = execute_pipeline(
        doc,
        ProviderSettings(api_key="test-key"),
        on_progress=broken_progress,
        generate_fn=scripted_generate,
        max_concurrency=2,
    )
    assert report2.coverage.execution == ExecutionStatus.COMPLETE
    assert report2.verdict.readiness == Readiness.GO


def test_streamlit_concurrent_worker_progress_never_triggers_missing_script_run_context(report, monkeypatch, caplog):
    raw_doc = b"# Spec\n\nFR-1: Limit is 100 KZT.\n"
    live = _live_report(report, raw_doc)
    monkeypatch.setattr(dashboard, "load_settings", lambda: ProviderSettings(api_key="test-secret-key"))
    monkeypatch.setattr(ingest, "parse", lambda name, data: live.document)

    def threaded_pipeline(document, settings, on_progress=None):
        # Reproduce the exact production ThreadPoolExecutor fan-out immediately after extraction.
        if on_progress:
            on_progress(ProgressEvent(stage="ingest", state="completed", message="Проверено блоков: 2"))
            on_progress(ProgressEvent(stage="extract", state="completed", message="Извлечено требований: 2"))

        def worker(stage: str, msg: str) -> None:
            time.sleep(0.02)
            if on_progress:
                on_progress(ProgressEvent(stage=stage, state="started", message=msg))
                on_progress(ProgressEvent(stage=stage, state="completed", message=msg + " ok"))

        with ThreadPoolExecutor(max_workers=2) as pool:
            f1 = pool.submit(worker, "individual_audit", "Индивидуальный аудит")
            f2 = pool.submit(worker, "set_audit", "Целостный аудит")
            f1.result()
            f2.result()
        if on_progress:
            on_progress(ProgressEvent(stage="report", state="completed", message="Отчёт готов"))
        return live

    monkeypatch.setattr(pipeline, "run_pipeline", threaded_pipeline)

    app = AppTest.from_file(str(APP), default_timeout=15).run()
    app.file_uploader[0].set_value(("../../nested/spec.md", raw_doc, "text/markdown")).run()
    app.checkbox[0].check().run()
    with caplog.at_level(logging.WARNING):
        caplog.clear()
        app.button(key="launch").click().run()

    assert not app.exception
    assert len(app.metric) == 4
    assert app.status[0].state == "complete"
    assert "missing ScriptRunContext" not in caplog.text
    assert "ThreadPoolExecutor" not in caplog.text
    rendered_text = "\n".join(t.value for t in app.text)
    assert "Аудит требований" in rendered_text
    assert "Аудит документа" in rendered_text
    assert "Формирование отчёта" in rendered_text
    assert "../../" not in rendered_text


def test_ai_improvement_approve_reject_export_and_revalidation_ui_workflow(report, monkeypatch):
    live = _live_report(report)
    ai_calls = []

    def fake_draft(source, *, answer=None, finding_id=None, target_ref=None, settings=None, use_ai=True, allow_fallback=True):
        ai_calls.append((answer.question_id if answer else None, finding_id, use_ai))
        return draft_remediation_proposal(
            source,
            answer=answer,
            finding_id=finding_id,
            target_ref=target_ref,
            generate_fn=lambda stage, payload, output_type, **kw: StageResult[ImprovementRewriteOutput](
                stage=stage,
                output=ImprovementRewriteOutput(
                    status="ready",
                    improved_text="При отмене заявки на экскурсию посетитель не получает письмо и видит статус отмены.",
                    rationale="Согласовано с выбранным решением стейкхолдера без выдумывания новых правил.",
                    preserved_invariants=["отмена заявки"],
                ),
                attempts=1,
                input_tokens=80,
                output_tokens=40,
            ),
            use_ai=True,
            allow_fallback=allow_fallback,
        )

    exports: dict[str, str] = {}
    orig_download = st.download_button

    def capture_download(label, data, file_name, *args, **kwargs):
        exports[file_name] = data
        return orig_download(label, data, file_name, *args, **kwargs)

    monkeypatch.setattr(st, "download_button", capture_download)
    monkeypatch.setattr(dashboard, "load_settings", lambda: ProviderSettings(api_key="test-key"))
    monkeypatch.setattr(clarification, "draft_remediation_proposal", fake_draft)
    monkeypatch.setattr(pipeline, "run_pipeline", lambda *a, **k: pytest.fail("No live revalidation before consent"))

    app = AppTest.from_file(str(APP), default_timeout=15).run()
    app.radio[0].set_value("Сохранённый отчёт").run()
    app.file_uploader[0].set_value(("live.json", live.model_dump_json().encode(), "application/json")).run()
    assert not app.exception
    # Before applying edits, only original canonical report exports exist.
    assert set(exports) == {"report.md", "report.json"}

    # Enable explicit AI drafting consent and generate an AI formulation.
    ai_consent = next(cb for cb in app.checkbox if "AI-формулировки" in cb.label)
    ai_consent.check().run()
    ai_btn = next(b for b in app.button if b.label == "Сгенерировать AI-формулировку")
    ai_btn.click().run()
    assert not app.exception
    assert len(ai_calls) == 1

    edit_area = next(ta for ta in app.text_area if ta.label == "После изменения")
    assert "видит статус отмены" in edit_area.value

    # Approve proposal into multi-edit change package, then apply package to working document.
    approve_btn = next(b for b in app.button if b.label == "Утвердить в пакет правок")
    approve_btn.click().run()
    assert not app.exception
    apply_batch_btn = next(b for b in app.button if b.label.startswith("Применить утверждённые правки"))
    apply_batch_btn.click().run()
    assert not app.exception

    # Complete improved Markdown and Russian change log exports are now available with matching SHA-256.
    assert "requirements_improved.md" in exports
    assert "changes.md" in exports
    assert "видит статус отмены" in exports["requirements_improved.md"]
    assert "Журнал применённых изменений" in exports["changes.md"]


def test_cyrillic_requirement_id_preserved_and_pdf_derived_label_present():
    # 1. Cyrillic requirement IDs (e.g. Ф-1, НФ-2) must be preserved by rewrite invariant checker.
    spec = "# Требования\n\n* **Ф-1.** Порог задаётся быстро.\n"
    doc = ingest.parse("cyrillic.md", spec.encode("utf-8"))
    b_req = next(b for b in doc.version.blocks if "Ф-1" in b.text)
    ref = EvidenceRef(
        version=doc.version.sha256,
        block_id=b_req.block_id,
        start=0,
        end=len(b_req.text),
        quote=b_req.text,
        grounding=GroundingStatus.EXACT,
    )
    from src.clarification.improvement import _check_rewrite_invariants

    assert _check_rewrite_invariants("* **Ф-1.** Порог задаётся быстро.", "* Порог задаётся за 1 секунду.") is not None
    assert _check_rewrite_invariants("* **Ф-1.** Порог задаётся быстро.", "* **Ф-1.** Порог задаётся за 1 секунду.") is None

    # 2. PDF input improved working document must explicitly label derived Markdown in change log.
    with pymupdf.open() as pdf:
        pdf.new_page().insert_text((35, 35), "FR-1: Transfer executes quickly.")
        pdf_bytes = pdf.tobytes()
    pdf_doc = ingest.parse("spec.pdf", pdf_bytes)
    blk = pdf_doc.version.blocks[0]
    pdf_ref = EvidenceRef(
        version=pdf_doc.version.sha256,
        block_id=blk.block_id,
        start=0,
        end=len(blk.text),
        quote=blk.text,
        grounding=GroundingStatus.EXACT,
    )
    bundle = apply_changes(
        pdf_doc,
        [ProposedChange(question_id="Q001", source=pdf_ref, replacement="FR-1: Transfer executes within 2 seconds.")],
    )
    assert any("производный рабочий документ markdown" in d.lower() for d in bundle.change_log.diagnostics)


def test_compare_reports_never_marks_resolved_on_partial_revalidation(report):
    live = _live_report(report)
    partial_data = deepcopy(live.model_dump(mode="json"))
    partial_data.pop("statistics", None)
    partial_data["findings"] = []
    partial_data["questions"] = []
    partial_data["coverage"]["execution"] = ExecutionStatus.PARTIAL.value
    partial_data["coverage"]["incomplete_reasons"] = ["Не завершён этап целостного аудита."]
    partial_data["evaluations"] = [
        ev if ev["status"] != "fail" else {**ev, "status": "unknown", "finding_ids": []}
        for ev in partial_data["evaluations"]
    ]
    partial_data["verdict"] = {
        "readiness": Readiness.UNDETERMINED.value,
        "rationale": "Частичный прогон.",
        "next_actions": ["Повторить проверку."],
    }
    partial_report = ValidationReport.model_validate(partial_data)

    cmp_partial = clarification.compare_reports(live, partial_report, applied_changes=1)
    assert cmp_partial["complete"] is False
    assert cmp_partial["resolved"] == []
    assert len(cmp_partial["unverified_previous"]) == 1
