"""Regression tests for post-extraction crash resilience, observability, and UI recovery."""
from hashlib import sha256
import logging
from pathlib import Path
import re
import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

from google.genai import types
import pytest
from streamlit.testing.v1 import AppTest

from src import ingest, pipeline
from src.contracts import (
    INDIVIDUAL,
    WHOLE_SET,
    CriterionStatus,
    ExecutionStatus,
    FindingType,
    PipelineStage,
    ProgressEvent,
    ProviderSettings,
    QualityCriterion,
    QuestionPriority,
    Readiness,
    RequirementCategory,
    Severity,
    StageResult,
    StakeholderRole,
    ValidationReport,
)
from src.pipeline import execute_pipeline
from src.pipeline.diagnostics import classify_error_category, new_run_id, safe_stack_frames
from src.pipeline.individual import audit_individual_requirements
from src.pipeline.orchestrator import _fallback_holistic
from src.provider import FailureAction, FailureReason, ProviderError, QuotaBudget, generate
from src.provider.dto import (
    ContextBlockDisposition,
    ExtractedItem,
    ExtractionOutput,
    HolisticAuditOutput,
    HolisticCriterionCandidate,
    HolisticEvidenceQuote,
    HolisticFindingCandidate,
    IndividualAuditOutput,
    IndividualCriterionCandidate,
    IndividualEvidenceQuote,
    IndividualFindingCandidate,
    ProbeRequest,
    ProbeResponse,
    QuestionCandidate,
    QuestionGenerationOutput,
    QuestionOptionDraft,
    SemanticReview,
    VerificationOutput,
)
from src.report import load_json, render_json, render_markdown
from ui import dashboard

APP = Path(__file__).resolve().parents[2] / "ui" / "app.py"
ORDERED_INDIVIDUAL = sorted(INDIVIDUAL, key=lambda c: c.value)
ORDERED_WHOLE = sorted(WHOLE_SET, key=lambda c: c.value)


def _all_indiv(req_id: str, overrides=None) -> list[IndividualCriterionCandidate]:
    overrides = overrides or {}
    items = []
    for crit in ORDERED_INDIVIDUAL:
        if crit in overrides:
            status, rationale, keys = overrides[crit]
        else:
            status, rationale, keys = CriterionStatus.PASS, "Требование проверено.", []
        items.append(
            IndividualCriterionCandidate(
                requirement_id=req_id,
                criterion=crit,
                status=status,
                rationale=rationale,
                local_finding_keys=keys,
            )
        )
    return items


def _all_whole(overrides=None) -> list[HolisticCriterionCandidate]:
    overrides = overrides or {}
    items = []
    for crit in ORDERED_WHOLE:
        if crit in overrides:
            status, rationale, keys = overrides[crit]
        else:
            status, rationale, keys = CriterionStatus.PASS, "Набор согласован.", []
        items.append(
            HolisticCriterionCandidate(
                criterion=crit,
                status=status,
                rationale=rationale,
                local_finding_keys=keys,
            )
        )
    return items


def _two_rule_doc():
    md = (
        "# Модуль уведомлений\n\n"
        "FR-1: Система отправляет push при достижении порога за календарный месяц.\n\n"
        "FR-2: Пользователь может менять порог в любой день месяца.\n"
    )
    return ingest.parse("spec.md", md.encode("utf-8"))


def _live_report(report: ValidationReport, raw_doc: bytes) -> ValidationReport:
    payload = report.model_dump_json().replace(report.metadata.document_version, sha256(raw_doc).hexdigest())
    payload = payload.replace('"origin":"synthetic"', '"origin":"live"').replace(
        '"request_attempts":0', '"request_attempts":2'
    )
    return load_json(payload)


def test_unexpected_exception_immediately_after_extraction_isolates_and_never_yields_go(monkeypatch, caplog):
    doc = _two_rule_doc()
    heading, b1, b2 = doc.version.blocks
    events: list[ProgressEvent] = []

    def extract_only(stage, payload, output_type, *, settings=None, **kwargs):
        if stage == PipelineStage.EXTRACT:
            return StageResult[ExtractionOutput](
                stage=stage,
                output=ExtractionOutput(
                    items=[
                        ExtractedItem(block_id=b1.block_id, original_quote=b1.text, summary="Отправка push по порогу."),
                        ExtractedItem(block_id=b2.block_id, original_quote=b2.text, summary="Изменение порога."),
                    ],
                    context_blocks=[ContextBlockDisposition(block_id=heading.block_id, reason="Заголовок.")],
                ),
                attempts=1,
            )
        raise AssertionError(f"Unexpected call {stage}")

    def boom_individual(*args, **kwargs):
        raise RuntimeError("SECRET_DOC_PAYLOAD_DO_NOT_LOG")

    def boom_holistic(*args, **kwargs):
        raise KeyError("SECRET_HOLISTIC_KEY")

    monkeypatch.setattr("src.pipeline.orchestrator.audit_individual_requirements", boom_individual)
    monkeypatch.setattr("src.pipeline.orchestrator.audit_holistic_set", boom_holistic)

    with caplog.at_level(logging.INFO):
        report = execute_pipeline(
            doc,
            ProviderSettings(api_key="test-secret-key"),
            on_progress=events.append,
            generate_fn=extract_only,
            run_id="run-postext00001",
            max_concurrency=2,
        )

    assert report.metadata.run_id == "run-postext00001"
    assert len(report.requirements) == 2
    assert report.coverage.extraction_blocks.complete
    assert report.coverage.execution == ExecutionStatus.FAILED
    assert report.verdict.readiness == Readiness.UNDETERMINED
    assert report.coverage.individual_requirements.completed == []
    assert report.coverage.cross_section_groups.completed == []
    assert any("run-postext00001" in r for r in report.coverage.incomplete_reasons)
    assert any(e.stage == PipelineStage.INDIVIDUAL and e.state == "failed" for e in events)
    assert any(e.stage == PipelineStage.HOLISTIC and e.state == "failed" for e in events)
    assert ValidationReport.model_validate_json(render_json(report)) == report
    assert "Отчёт о проверке требований" in render_markdown(report)
    assert "SECRET_DOC_PAYLOAD_DO_NOT_LOG" not in caplog.text
    assert "SECRET_HOLISTIC_KEY" not in caplog.text
    assert "test-secret-key" not in caplog.text
    assert "run=run-postext00001" in caplog.text
    assert "frames=" in caplog.text


def test_individual_audit_crash_preserves_holistic_results_as_partial(monkeypatch):
    doc = _two_rule_doc()
    heading, b1, b2 = doc.version.blocks

    def scripted_generate(stage, payload, output_type, *, settings=None, **kwargs):
        if stage == PipelineStage.EXTRACT:
            return StageResult[ExtractionOutput](
                stage=stage,
                output=ExtractionOutput(
                    items=[
                        ExtractedItem(block_id=b1.block_id, original_quote=b1.text, summary="Отправка push по порогу."),
                        ExtractedItem(block_id=b2.block_id, original_quote=b2.text, summary="Изменение порога."),
                    ],
                    context_blocks=[ContextBlockDisposition(block_id=heading.block_id, reason="Заголовок.")],
                ),
                attempts=1,
            )
        if stage == PipelineStage.HOLISTIC:
            return StageResult[HolisticAuditOutput](
                stage=stage,
                output=HolisticAuditOutput(evaluations=_all_whole()),
                attempts=1,
            )
        raise AssertionError(f"Unexpected call {stage}")

    monkeypatch.setattr(
        "src.pipeline.orchestrator.audit_individual_requirements",
        lambda *a, **k: (_ for _ in ()).throw(TypeError("unexpected individual crash")),
    )

    report = execute_pipeline(
        doc,
        ProviderSettings(api_key="test-key"),
        generate_fn=scripted_generate,
        max_concurrency=2,
    )

    assert report.coverage.execution == ExecutionStatus.PARTIAL
    assert report.verdict.readiness == Readiness.UNDETERMINED
    assert report.coverage.cross_section_groups.complete
    assert report.coverage.individual_requirements.completed == []
    assert any("индивидуального аудита" in r for r in report.coverage.incomplete_reasons)
    assert ValidationReport.model_validate_json(render_json(report)) == report


def test_holistic_audit_crash_preserves_individual_results_as_partial(monkeypatch):
    doc = _two_rule_doc()
    heading, b1, b2 = doc.version.blocks

    def scripted_generate(stage, payload, output_type, *, settings=None, **kwargs):
        if stage == PipelineStage.EXTRACT:
            return StageResult[ExtractionOutput](
                stage=stage,
                output=ExtractionOutput(
                    items=[
                        ExtractedItem(block_id=b1.block_id, original_quote=b1.text, summary="Отправка push по порогу."),
                        ExtractedItem(block_id=b2.block_id, original_quote=b2.text, summary="Изменение порога."),
                    ],
                    context_blocks=[ContextBlockDisposition(block_id=heading.block_id, reason="Заголовок.")],
                ),
                attempts=1,
            )
        if stage == PipelineStage.INDIVIDUAL:
            return StageResult[IndividualAuditOutput](
                stage=stage,
                output=IndividualAuditOutput(evaluations=[*_all_indiv("R001"), *_all_indiv("R002")]),
                attempts=1,
            )
        raise AssertionError(f"Unexpected call {stage}")

    monkeypatch.setattr(
        "src.pipeline.orchestrator.audit_holistic_set",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("unexpected holistic crash")),
    )

    report = execute_pipeline(
        doc,
        ProviderSettings(api_key="test-key"),
        generate_fn=scripted_generate,
        max_concurrency=2,
    )

    assert report.coverage.execution == ExecutionStatus.PARTIAL
    assert report.verdict.readiness == Readiness.UNDETERMINED
    assert report.coverage.individual_requirements.complete
    assert report.coverage.cross_section_groups.completed == []
    assert any("целостного аудита" in r for r in report.coverage.incomplete_reasons)
    assert ValidationReport.model_validate_json(render_json(report)) == report


def test_parallel_worker_exception_isolates_failed_batch_and_preserves_healthy_batches(caplog):
    doc = _two_rule_doc()
    heading, b1, b2 = doc.version.blocks

    def scripted_generate(stage, payload, output_type, *, settings=None, **kwargs):
        if stage == PipelineStage.EXTRACT:
            return StageResult[ExtractionOutput](
                stage=stage,
                output=ExtractionOutput(
                    items=[
                        ExtractedItem(block_id=b1.block_id, original_quote=b1.text, summary="Отправка push по порогу."),
                        ExtractedItem(block_id=b2.block_id, original_quote=b2.text, summary="Изменение порога."),
                    ],
                    context_blocks=[ContextBlockDisposition(block_id=heading.block_id, reason="Заголовок.")],
                ),
                attempts=1,
            )
        if stage == PipelineStage.INDIVIDUAL:
            if payload.target_req_ids == ["R002"]:
                raise RuntimeError("RAW_PRIVATE_TEXT_INSIDE_WORKER")
            return StageResult[IndividualAuditOutput](
                stage=stage,
                output=IndividualAuditOutput(evaluations=_all_indiv("R001")),
                attempts=1,
            )
        if stage == PipelineStage.HOLISTIC:
            return StageResult[HolisticAuditOutput](
                stage=stage,
                output=HolisticAuditOutput(evaluations=_all_whole()),
                attempts=1,
            )
        raise AssertionError(f"Unexpected stage {stage}")

    with caplog.at_level(logging.ERROR):
        report = execute_pipeline(
            doc,
            ProviderSettings(api_key="test-key"),
            generate_fn=scripted_generate,
            max_requirements_per_batch=1,
            max_concurrency=2,
            run_id="run-workererr01",
        )

    assert report.coverage.execution == ExecutionStatus.PARTIAL
    assert report.verdict.readiness == Readiness.UNDETERMINED
    assert report.coverage.individual_requirements.completed == ["R001"]
    assert report.coverage.individual_requirements.planned == ["R001", "R002"]
    assert any("RuntimeError" in r for r in report.coverage.incomplete_reasons)
    assert "RAW_PRIVATE_TEXT_INSIDE_WORKER" not in caplog.text
    assert "RAW_PRIVATE_TEXT_INSIDE_WORKER" not in render_markdown(report)
    assert ValidationReport.model_validate_json(render_json(report)) == report


def test_quota_budget_max_in_flight_bounds_concurrent_calls(monkeypatch):
    client_cls = MagicMock()
    monkeypatch.setattr("src.provider.genai.Client", client_cls)
    active = 0
    peak = 0
    lock = threading.Lock()

    def fake_call(**kwargs):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.05)
        with lock:
            active -= 1
        return SimpleNamespace(
            candidates=[SimpleNamespace(finish_reason=types.FinishReason.STOP)],
            usage_metadata=None,
            text='{"value":7}',
        )

    client_cls.return_value.__enter__.return_value.models.generate_content.side_effect = fake_call
    budget = QuotaBudget(max_in_flight=2)
    settings = ProviderSettings(api_key="test-key")

    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=5) as pool:
        futs = [
            pool.submit(generate, PipelineStage.INDIVIDUAL, ProbeRequest(), ProbeResponse, settings=settings, budget=budget)
            for _ in range(5)
        ]
        results = [f.result() for f in futs]

    assert all(r.output.value == 7 for r in results)
    assert peak <= 2
    assert budget.attempts == 5


def test_gemini_structured_response_property_value_error_maps_to_malformed_repair(monkeypatch):
    client_cls = MagicMock()
    monkeypatch.setattr("src.provider.genai.Client", client_cls)

    class BrokenTextResponse:
        candidates = [SimpleNamespace(finish_reason=types.FinishReason.STOP)]
        usage_metadata = None

        @property
        def text(self):
            raise ValueError("private raw response payload")

    client_cls.return_value.__enter__.return_value.models.generate_content.return_value = BrokenTextResponse()

    with pytest.raises(ProviderError) as exc_info:
        generate(
            PipelineStage.INDIVIDUAL,
            ProbeRequest(),
            ProbeResponse,
            settings=ProviderSettings(api_key="test-key"),
        )
    assert exc_info.value.reason == FailureReason.MALFORMED_RESPONSE
    assert exc_info.value.action == FailureAction.REPAIR
    assert "private raw response payload" not in str(exc_info.value)


def test_quota_exhaustion_mid_run_produces_honest_partial_report():
    doc = _two_rule_doc()
    heading, b1, b2 = doc.version.blocks

    def scripted_generate(stage, payload, output_type, *, settings=None, **kwargs):
        if stage == PipelineStage.EXTRACT:
            return StageResult[ExtractionOutput](
                stage=stage,
                output=ExtractionOutput(
                    items=[
                        ExtractedItem(block_id=b1.block_id, original_quote=b1.text, summary="Отправка push по порогу."),
                        ExtractedItem(block_id=b2.block_id, original_quote=b2.text, summary="Изменение порога."),
                    ],
                    context_blocks=[ContextBlockDisposition(block_id=heading.block_id, reason="Заголовок.")],
                ),
                attempts=1,
            )
        if stage == PipelineStage.INDIVIDUAL:
            return StageResult[IndividualAuditOutput](
                stage=stage,
                output=IndividualAuditOutput(evaluations=[*_all_indiv("R001"), *_all_indiv("R002")]),
                attempts=1,
            )
        if stage == PipelineStage.HOLISTIC:
            raise ProviderError(
                "Daily quota exhausted (HTTP 429)",
                reason=FailureReason.DAILY_QUOTA_EXHAUSTED,
                action=FailureAction.STOP,
                attempts=1,
            )
        raise AssertionError(f"Unexpected stage {stage}")

    report = execute_pipeline(
        doc,
        ProviderSettings(api_key="test-key"),
        generate_fn=scripted_generate,
        max_concurrency=1,
    )

    assert report.coverage.execution == ExecutionStatus.PARTIAL
    assert report.verdict.readiness == Readiness.UNDETERMINED
    assert report.coverage.individual_requirements.complete
    assert not report.coverage.cross_section_groups.complete
    assert any("Daily quota exhausted" in r for r in report.coverage.incomplete_reasons)


def test_safe_ui_error_renders_diagnostic_ref_id_and_preserves_previous_valid_report(report, monkeypatch, caplog):
    doc_ok = b"# Doc A\n\nFR-1: Valid rule.\n"
    doc_fail = b"# Doc B\n\nFR-2: Failing rule.\n"
    live_ok = _live_report(report, doc_ok)
    failing_doc = _two_rule_doc()
    monkeypatch.setattr(dashboard, "load_settings", lambda: ProviderSettings(api_key="super-secret-api-key"))
    monkeypatch.setattr(ingest, "parse", lambda name, data: live_ok.document if data == doc_ok else failing_doc)

    calls: list[tuple[str, str | None]] = []

    def scripted_run(document, settings, on_progress=None, *, run_id=None):
        calls.append((document.version.name, run_id))
        if on_progress:
            on_progress(ProgressEvent(stage="ingest", state="completed", message="ok"))
            on_progress(ProgressEvent(stage="extract", state="completed", message="55 требований"))
        if document.version.sha256 == live_ok.document.version.sha256:
            return live_ok
        raise RuntimeError("LEAKED_SECRET_DOCUMENT_TEXT super-secret-api-key")

    monkeypatch.setattr(pipeline, "run_pipeline", scripted_run)

    app = AppTest.from_file(str(APP), default_timeout=15).run()
    # 1. Run valid document A -> produces full report and metrics.
    app.file_uploader[0].set_value(("doc_a.md", doc_ok, "text/markdown")).run()
    app.checkbox[0].check().run()
    app.button(key="launch").click().run()
    assert not app.exception
    assert len(app.metric) == 4
    assert len(calls) == 1 and calls[0][1] and calls[0][1].startswith("run-")

    # 2. Upload document B and trigger internal failure -> safe Russian error with run ID, zero leaks.
    app.file_uploader[0].set_value(("doc_b.md", doc_fail, "text/markdown")).run()
    app.checkbox[0].check().run()
    with caplog.at_level(logging.ERROR):
        caplog.clear()
        app.button(key="launch").click().run()

    assert not app.exception
    assert app.status[0].state == "error"
    err_label = app.status[0].label
    assert "Внутренняя ошибка обработки документа" in err_label
    assert "Извлечение требований" in err_label
    assert re.search(r"код диагностики:\s*run-[a-f0-9]{12}", err_label)
    assert "LEAKED_SECRET_DOCUMENT_TEXT" not in err_label
    assert "super-secret-api-key" not in err_label
    assert "LEAKED_SECRET_DOCUMENT_TEXT" not in caplog.text
    assert "super-secret-api-key" not in caplog.text
    assert "category=internal_error" in caplog.text
    assert "error_type=RuntimeError" in caplog.text
    assert "frames=" in caplog.text
    assert any("сохранены" in c.value for c in app.caption)

    # 3. Switching back to document A restores preserved valid report with ZERO new API calls.
    app.file_uploader[0].set_value(("doc_a.md", doc_ok, "text/markdown")).run()
    assert not app.exception
    assert len(app.metric) == 4
    assert len(calls) == 2


@pytest.mark.parametrize("concurrency", [1, 2])
def test_successful_full_pipeline_execution_sequential_and_parallel(concurrency):
    doc = _two_rule_doc()
    heading, b1, b2 = doc.version.blocks
    events: list[ProgressEvent] = []

    def scripted_generate(stage, payload, output_type, *, settings=None, **kwargs):
        if stage == PipelineStage.EXTRACT:
            return StageResult[ExtractionOutput](
                stage=stage,
                output=ExtractionOutput(
                    items=[
                        ExtractedItem(block_id=b1.block_id, original_quote=b1.text, summary="Отправка push по порогу."),
                        ExtractedItem(block_id=b2.block_id, original_quote=b2.text, summary="Изменение порога."),
                    ],
                    context_blocks=[ContextBlockDisposition(block_id=heading.block_id, reason="Заголовок.")],
                ),
                attempts=1,
            )
        if stage == PipelineStage.INDIVIDUAL:
            return StageResult[IndividualAuditOutput](
                stage=stage,
                output=IndividualAuditOutput(
                    evaluations=[
                        *_all_indiv(
                            "R001",
                            overrides={
                                QualityCriterion.COMPLETE: (
                                    CriterionStatus.FAIL,
                                    "Не указана таймзона для календарного месяца.",
                                    ["K1"],
                                )
                            },
                        ),
                        *_all_indiv("R002"),
                    ],
                    findings=[
                        IndividualFindingCandidate(
                            local_key="K1",
                            requirement_id="R001",
                            kind=FindingType.MISSING_INFORMATION,
                            severity=Severity.MAJOR,
                            claim="Не задан часовой пояс расчёта календарного месяца.",
                            recommendation="Зафиксировать часовой пояс UTC+5 (Алматы).",
                            criteria=[QualityCriterion.COMPLETE],
                            evidence=[IndividualEvidenceQuote(block_id=b1.block_id, quote="календарный месяц")],
                            expected_information="Часовой пояс календарного месяца",
                        )
                    ],
                ),
                attempts=1,
            )
        if stage == PipelineStage.HOLISTIC:
            return StageResult[HolisticAuditOutput](
                stage=stage,
                output=HolisticAuditOutput(evaluations=_all_whole()),
                attempts=1,
            )
        if stage == PipelineStage.VERIFY:
            return StageResult[VerificationOutput](
                stage=stage,
                output=VerificationOutput(
                    reviews=[
                        SemanticReview(
                            candidate_id=c.candidate_id,
                            decision="supported",
                            reason="Подтверждено в тексте спецификации.",
                        )
                        for c in payload.candidates
                    ]
                ),
                attempts=1,
            )
        if stage == PipelineStage.CLARIFY:
            f0 = payload.findings[0]
            return StageResult[QuestionGenerationOutput](
                stage=stage,
                output=QuestionGenerationOutput(
                    questions=[
                        QuestionCandidate(
                            finding_ids=[f0.finding_id],
                            requirement_ids=f0.requirement_ids,
                            evidence_block_ids=[e.block_id for e in f0.evidence],
                            uncertainty="Не указан часовой пояс для границ календарного месяца.",
                            stakeholder=StakeholderRole.PRODUCT,
                            priority=QuestionPriority.HIGH,
                            business_impact="Возможны расхождения при покупке на границе месяца.",
                            question="По какому часовому поясу определять границы календарного месяца?",
                            options=[
                                QuestionOptionDraft(
                                    option_id="A",
                                    label="Время Алматы (UTC+5)",
                                    consequence="Учитываются покупки по локальному времени банка.",
                                ),
                                QuestionOptionDraft(
                                    option_id="B",
                                    label="UTC",
                                    consequence="Требуется конвертация пользовательского времени.",
                                ),
                            ],
                        )
                    ]
                ),
                attempts=1,
            )
        raise AssertionError(f"Unexpected stage {stage}")

    run_id = new_run_id()
    report = execute_pipeline(
        doc,
        ProviderSettings(api_key="test-key"),
        on_progress=events.append,
        generate_fn=scripted_generate,
        run_id=run_id,
        max_concurrency=concurrency,
    )

    assert report.metadata.run_id == run_id
    assert report.coverage.execution == ExecutionStatus.COMPLETE
    assert report.verdict.readiness == Readiness.CONDITIONAL
    assert report.statistics.requirements == 2
    assert report.statistics.violations == 1
    assert report.statistics.major == 1
    assert report.statistics.questions == 1
    assert ValidationReport.model_validate_json(render_json(report)) == report
    md = render_markdown(report)
    assert run_id in md
    assert "календарный месяц" in md
    seen_stages = {e.stage for e in events if e.state == "completed"}
    assert {
        PipelineStage.INGEST,
        PipelineStage.EXTRACT,
        PipelineStage.INDIVIDUAL,
        PipelineStage.HOLISTIC,
        PipelineStage.VERIFY,
        PipelineStage.CLARIFY,
        PipelineStage.REPORT,
    } <= seen_stages


def test_diagnostics_helpers_never_expose_secrets_or_locals():
    secret_local = "AIzaSyFAKE_SECRET_DO_NOT_EXPOSED"
    try:
        raise ValueError(f"bad input containing {secret_local}")
    except ValueError as exc:
        frames = safe_stack_frames(exc)
        category = classify_error_category(exc)
    assert category == "input_or_io_error"
    assert "test_post_extraction_resilience.py:" in frames
    assert secret_local not in frames
    assert "bad input" not in frames
