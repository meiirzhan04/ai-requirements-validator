"""Integration tests for the shared production validation pipeline."""
import pytest

from src.contracts import (
    INDIVIDUAL,
    WHOLE_SET,
    CriterionStatus,
    DocumentFormat,
    DocumentVersion,
    ExecutionStatus,
    FindingType,
    ParsedDocument,
    PipelineStage,
    ProgressEvent,
    ProviderSettings,
    QualityCriterion,
    QuestionPriority,
    Readiness,
    RequirementCategory,
    SemanticStatus,
    Severity,
    SourceBlock,
    SourceLocation,
    StageResult,
    StakeholderRole,
    ValidationReport,
)
from src.ingest import parse
from src.pipeline import execute_pipeline, run_pipeline
from src.provider import FailureAction, FailureReason, ProviderError
from src.provider.dto import (
    ContextBlockDisposition,
    ExtractedItem,
    ExtractionBatchPayload,
    ExtractionOutput,
    HolisticAuditOutput,
    HolisticCriterionCandidate,
    HolisticEvidenceQuote,
    HolisticFindingCandidate,
    HolisticGroupPayload,
    IndividualAuditOutput,
    IndividualAuditPayload,
    IndividualCriterionCandidate,
    IndividualEvidenceQuote,
    IndividualFindingCandidate,
    QuestionCandidate,
    QuestionGenerationOutput,
    QuestionGenerationPayload,
    QuestionOptionDraft,
    SemanticReview,
    VerificationOutput,
    VerificationPayload,
)
from src.report import render_markdown

ORDERED_INDIVIDUAL = sorted(INDIVIDUAL, key=lambda c: c.value)
ORDERED_WHOLE = sorted(WHOLE_SET, key=lambda c: c.value)


def _all_indiv(req_id: str, overrides=None) -> list[IndividualCriterionCandidate]:
    overrides = overrides or {}
    out = []
    for crit in ORDERED_INDIVIDUAL:
        if crit in overrides:
            status, rationale, keys = overrides[crit]
        elif crit in (QualityCriterion.NECESSARY, QualityCriterion.FEASIBLE, QualityCriterion.CORRECT):
            status, rationale, keys = CriterionStatus.UNKNOWN, "Внешние данные не приложены к спецификации.", []
        elif crit == QualityCriterion.CONFORMING:
            status, rationale, keys = CriterionStatus.NOT_APPLICABLE, "Шаблон оформления не задан.", []
        else:
            status, rationale, keys = CriterionStatus.PASS, "Требование соответствует критерию.", []
        out.append(
            IndividualCriterionCandidate(
                requirement_id=req_id,
                criterion=crit,
                status=status,
                rationale=rationale,
                local_finding_keys=keys,
            )
        )
    return out


def _all_whole(overrides=None) -> list[HolisticCriterionCandidate]:
    overrides = overrides or {}
    out = []
    for crit in ORDERED_WHOLE:
        if crit in overrides:
            status, rationale, keys = overrides[crit]
        else:
            status, rationale, keys = CriterionStatus.PASS, "Набор требований согласован по критерию.", []
        out.append(
            HolisticCriterionCandidate(
                criterion=crit,
                status=status,
                rationale=rationale,
                local_finding_keys=keys,
            )
        )
    return out


def _sample_markdown_document() -> ParsedDocument:
    md = (
        "# Платёжный модуль переводов\n\n"
        "Документ описывает базовый процесс клиентских переводов.\n\n"
        "## API переводов\n\n"
        "REQ-1: POST /api/v1/transfers должен быстро обрабатывать запрос и возвращать HTTP 201.\n\n"
        "## Приёмка переводов\n\n"
        "REQ-2: При успешном создании перевода POST /api/v1/transfers возвращает HTTP 200.\n"
    )
    return parse("synthetic-payments.md", md.encode("utf-8"))


def test_complete_pipeline_run_with_verification_clarification_and_markdown_render():
    doc = _sample_markdown_document()
    events: list[ProgressEvent] = []
    calls_by_stage: list[PipelineStage] = []

    req1_block = next(b for b in doc.version.blocks if "REQ-1" in b.text)
    req2_block = next(b for b in doc.version.blocks if "REQ-2" in b.text)
    context_ids = [b.block_id for b in doc.version.blocks if b.block_id not in (req1_block.block_id, req2_block.block_id)]

    def scripted_generate(stage: PipelineStage, payload, output_type, *, settings=None, **kwargs):
        calls_by_stage.append(stage)
        if stage == PipelineStage.EXTRACT:
            assert isinstance(payload, ExtractionBatchPayload)
            return StageResult[ExtractionOutput](
                stage=stage,
                output=ExtractionOutput(
                    items=[
                        ExtractedItem(
                            block_id=req1_block.block_id,
                            original_id="REQ-1",
                            category=RequirementCategory.INTERFACE,
                            original_quote=req1_block.text,
                            summary="POST /api/v1/transfers быстро обрабатывает запрос и возвращает HTTP 201.",
                        ),
                        ExtractedItem(
                            block_id=req2_block.block_id,
                            original_id="REQ-2",
                            category=RequirementCategory.ACCEPTANCE,
                            original_quote=req2_block.text,
                            summary="При успешном создании перевода POST /api/v1/transfers возвращает HTTP 200.",
                        ),
                    ],
                    context_blocks=[
                        ContextBlockDisposition(block_id=bid, reason="Заголовок или вводное описание раздела.")
                        for bid in context_ids
                        if bid in payload.target_block_ids
                    ],
                ),
                attempts=1,
                input_tokens=120,
                output_tokens=60,
            )

        if stage == PipelineStage.INDIVIDUAL:
            assert isinstance(payload, IndividualAuditPayload)
            r1_evals = _all_indiv(
                "R001",
                overrides={
                    QualityCriterion.UNAMBIGUOUS: (
                        CriterionStatus.FAIL,
                        "Слово «быстро» не задаёт измеримый порог отклика.",
                        ["K1"],
                    ),
                    QualityCriterion.VERIFIABLE: (
                        CriterionStatus.FAIL,
                        "Без числового SLA невозможно написать автоматический тест производительности.",
                        ["K1"],
                    ),
                },
            )
            r2_evals = _all_indiv("R002")
            return StageResult[IndividualAuditOutput](
                stage=stage,
                output=IndividualAuditOutput(
                    evaluations=[*r1_evals, *r2_evals],
                    findings=[
                        IndividualFindingCandidate(
                            local_key="K1",
                            requirement_id="R001",
                            kind=FindingType.AMBIGUITY,
                            severity=Severity.MAJOR,
                            claim="Формулировка «быстро обрабатывать запрос» не содержит измеримого SLA.",
                            recommendation="Задать целевой порог отклика, например p95 <= 500 мс.",
                            criteria=[QualityCriterion.UNAMBIGUOUS, QualityCriterion.VERIFIABLE],
                            evidence=[
                                IndividualEvidenceQuote(
                                    block_id=req1_block.block_id,
                                    quote="быстро обрабатывать запрос",
                                )
                            ],
                        )
                    ],
                ),
                attempts=1,
                input_tokens=150,
                output_tokens=90,
            )

        if stage == PipelineStage.HOLISTIC:
            assert isinstance(payload, HolisticGroupPayload)
            return StageResult[HolisticAuditOutput](
                stage=stage,
                output=HolisticAuditOutput(
                    evaluations=_all_whole(
                        overrides={
                            QualityCriterion.SET_CONSISTENT: (
                                CriterionStatus.FAIL,
                                "Разделы API и приёмки задают разные HTTP-коды успеха.",
                                ["C1"],
                            ),
                            QualityCriterion.SRS_CONSISTENT: (
                                CriterionStatus.FAIL,
                                "Конфликт кода ответа между API (201) и приёмкой (200).",
                                ["C1"],
                            ),
                        }
                    ),
                    findings=[
                        HolisticFindingCandidate(
                            local_key="C1",
                            kind=FindingType.CONTRADICTION,
                            severity=Severity.CRITICAL,
                            claim="Для POST /api/v1/transfers указаны несовместимые коды HTTP 201 и HTTP 200.",
                            scenario="Успешное создание клиентского перевода через POST /api/v1/transfers",
                            recommendation="Согласовать единый HTTP-код успешного создания перевода.",
                            requirement_ids=["R001", "R002"],
                            criteria=[QualityCriterion.SET_CONSISTENT, QualityCriterion.SRS_CONSISTENT],
                            evidence=[
                                HolisticEvidenceQuote(block_id=req1_block.block_id, quote="возвращать HTTP 201"),
                                HolisticEvidenceQuote(block_id=req2_block.block_id, quote="возвращает HTTP 200"),
                            ],
                        )
                    ],
                ),
                attempts=1,
                input_tokens=140,
                output_tokens=80,
            )

        if stage == PipelineStage.VERIFY:
            assert isinstance(payload, VerificationPayload)
            reviews = [
                SemanticReview(
                    candidate_id=c.candidate_id,
                    decision="supported",
                    reason="Дефект подтверждён в полном контексте без исключений.",
                )
                for c in payload.candidates
            ]
            return StageResult[VerificationOutput](
                stage=stage,
                output=VerificationOutput(reviews=reviews),
                attempts=1,
                input_tokens=110,
                output_tokens=50,
            )

        if stage == PipelineStage.CLARIFY:
            assert isinstance(payload, QuestionGenerationPayload)
            crit = next(f for f in payload.findings if f.severity == Severity.CRITICAL.value)
            return StageResult[QuestionGenerationOutput](
                stage=stage,
                output=QuestionGenerationOutput(
                    questions=[
                        QuestionCandidate(
                            finding_ids=[crit.finding_id],
                            requirement_ids=crit.requirement_ids,
                            evidence_block_ids=[e.block_id for e in crit.evidence],
                            uncertainty="Не выбран единый HTTP-статус для успешного создания перевода.",
                            stakeholder=StakeholderRole.ARCHITECT,
                            priority=QuestionPriority.BLOCKER,
                            business_impact="Интеграционные клиенты и автотесты приёмки не сойдутся по контракту API.",
                            question="Какой HTTP-код должен возвращать POST /api/v1/transfers при создании перевода?",
                            options=[
                                QuestionOptionDraft(
                                    option_id="A",
                                    label="HTTP 201 Created",
                                    consequence="Сохранить REQ-1 и обновить сценарий приёмки REQ-2 на HTTP 201.",
                                ),
                                QuestionOptionDraft(
                                    option_id="B",
                                    label="HTTP 200 OK",
                                    consequence="Обновить раздел API (REQ-1) на HTTP 200 и зафиксировать это в OpenAPI.",
                                ),
                            ],
                        )
                    ]
                ),
                attempts=1,
                input_tokens=100,
                output_tokens=70,
            )

        raise AssertionError(f"Unexpected stage {stage}")

    report = execute_pipeline(
        doc,
        ProviderSettings(api_key="test-key", model="gemini-test"),
        on_progress=events.append,
        generate_fn=scripted_generate,
    )

    # Canonical contract round-trip validation.
    dumped = report.model_dump_json()
    reloaded = ValidationReport.model_validate_json(dumped)
    assert reloaded == report

    assert report.metadata.origin == "live"
    assert report.metadata.request_attempts == 5
    assert report.metadata.input_tokens == 620
    assert report.metadata.output_tokens == 350
    assert report.coverage.execution == ExecutionStatus.COMPLETE
    assert report.verdict.readiness == Readiness.NO_GO
    assert report.statistics.requirements == 2
    assert report.statistics.violations == 2
    assert report.statistics.critical == 1
    assert report.statistics.major == 1
    assert report.statistics.questions == 1
    assert calls_by_stage == [
        PipelineStage.EXTRACT,
        PipelineStage.INDIVIDUAL,
        PipelineStage.HOLISTIC,
        PipelineStage.VERIFY,
        PipelineStage.CLARIFY,
    ]

    # Verify genuine start/completed transitions across all 7 pipeline stages.
    expected_stages = [
        PipelineStage.INGEST,
        PipelineStage.EXTRACT,
        PipelineStage.INDIVIDUAL,
        PipelineStage.HOLISTIC,
        PipelineStage.VERIFY,
        PipelineStage.CLARIFY,
        PipelineStage.REPORT,
    ]
    for st in expected_stages:
        assert any(e.stage == st and e.state == "started" for e in events)
        assert any(e.stage == st and e.state == "completed" for e in events)

    # Rendering uses zero extra provider calls.
    markdown = render_markdown(report)
    assert "POST /api/v1/transfers" in markdown
    assert len(calls_by_stage) == 5


def test_clean_complete_document_yields_go_without_unnecessary_verify_or_clarify_calls():
    doc = parse("clean.md", "# Лимиты\n\nСуточный лимит перевода составляет 500 000 KZT.\n".encode("utf-8"))
    heading_block, rule_block = doc.version.blocks
    calls: list[PipelineStage] = []

    def scripted_generate(stage: PipelineStage, payload, output_type, *, settings=None, **kwargs):
        calls.append(stage)
        if stage == PipelineStage.EXTRACT:
            return StageResult[ExtractionOutput](
                stage=stage,
                output=ExtractionOutput(
                    items=[
                        ExtractedItem(
                            block_id=rule_block.block_id,
                            category=RequirementCategory.FUNCTIONAL,
                            original_quote=rule_block.text,
                            summary="Суточный лимит перевода составляет 500 000 KZT.",
                        )
                    ],
                    context_blocks=[
                        ContextBlockDisposition(block_id=heading_block.block_id, reason="Заголовок раздела.")
                    ],
                ),
                attempts=1,
            )
        if stage == PipelineStage.INDIVIDUAL:
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
        raise AssertionError(f"Unexpected call for clean doc: {stage}")

    report = execute_pipeline(
        doc,
        ProviderSettings(api_key="test-key"),
        generate_fn=scripted_generate,
    )

    assert calls == [PipelineStage.EXTRACT, PipelineStage.INDIVIDUAL, PipelineStage.HOLISTIC]
    assert report.coverage.execution == ExecutionStatus.COMPLETE
    assert report.verdict.readiness == Readiness.GO
    assert report.statistics.violations == 0
    assert report.metadata.request_attempts == 3


def test_mid_run_holistic_failure_produces_partial_undetermined_and_never_go():
    doc = parse("partial.md", "# Правила\n\nКомиссия за перевод составляет 100 KZT.\n".encode("utf-8"))
    heading_block, rule_block = doc.version.blocks
    events: list[ProgressEvent] = []

    def scripted_generate(stage: PipelineStage, payload, output_type, *, settings=None, **kwargs):
        if stage == PipelineStage.EXTRACT:
            return StageResult[ExtractionOutput](
                stage=stage,
                output=ExtractionOutput(
                    items=[
                        ExtractedItem(
                            block_id=rule_block.block_id,
                            original_quote=rule_block.text,
                            summary="Комиссия за перевод 100 KZT.",
                        )
                    ],
                    context_blocks=[
                        ContextBlockDisposition(block_id=heading_block.block_id, reason="Заголовок.")
                    ],
                ),
                attempts=1,
            )
        if stage == PipelineStage.INDIVIDUAL:
            return StageResult[IndividualAuditOutput](
                stage=stage,
                output=IndividualAuditOutput(evaluations=_all_indiv("R001")),
                attempts=1,
            )
        if stage == PipelineStage.HOLISTIC:
            raise ProviderError(
                "Service unavailable (HTTP 503)",
                reason=FailureReason.SERVER_ERROR,
                action=FailureAction.STOP,
                attempts=3,
            )
        raise AssertionError(f"Unexpected stage {stage}")

    report = execute_pipeline(
        doc,
        ProviderSettings(api_key="test-key"),
        on_progress=events.append,
        generate_fn=scripted_generate,
    )

    assert report.coverage.execution == ExecutionStatus.PARTIAL
    assert report.verdict.readiness == Readiness.UNDETERMINED
    assert report.metadata.request_attempts == 5  # 1 extract + 1 individual + 3 failed holistic attempts
    assert any(e.stage == PipelineStage.HOLISTIC and e.state == "failed" for e in events)
    assert any("503" in reason for reason in report.coverage.incomplete_reasons)
    assert ValidationReport.model_validate_json(report.model_dump_json()) == report


def test_mid_run_failure_with_confirmed_critical_blocker_yields_no_go():
    doc = parse("blocker.md", "Таймаут 5 секунд и одновременно таймаут 60 секунд.\n".encode("utf-8"))
    blk = doc.version.blocks[0]

    def scripted_generate(stage: PipelineStage, payload, output_type, *, settings=None, **kwargs):
        if stage == PipelineStage.EXTRACT:
            return StageResult[ExtractionOutput](
                stage=stage,
                output=ExtractionOutput(
                    items=[
                        ExtractedItem(
                            block_id=blk.block_id,
                            original_quote=blk.text,
                            summary="Противоречивый таймаут 5 и 60 секунд.",
                        )
                    ]
                ),
                attempts=1,
            )
        if stage == PipelineStage.INDIVIDUAL:
            return StageResult[IndividualAuditOutput](
                stage=stage,
                output=IndividualAuditOutput(
                    evaluations=_all_indiv(
                        "R001",
                        overrides={
                            QualityCriterion.UNAMBIGUOUS: (
                                CriterionStatus.FAIL,
                                "Два противоречащих значения таймаута в одном требовании.",
                                ["K1"],
                            )
                        },
                    ),
                    findings=[
                        IndividualFindingCandidate(
                            local_key="K1",
                            requirement_id="R001",
                            kind=FindingType.AMBIGUITY,
                            severity=Severity.CRITICAL,
                            claim="Заданы взаимоисключающие таймауты 5 и 60 секунд.",
                            recommendation="Оставить одно значение таймаута.",
                            criteria=[QualityCriterion.UNAMBIGUOUS],
                            evidence=[IndividualEvidenceQuote(block_id=blk.block_id, quote=blk.text)],
                        )
                    ],
                ),
                attempts=1,
            )
        if stage == PipelineStage.HOLISTIC:
            raise ProviderError(
                "Daily quota exhausted (HTTP 429)",
                reason=FailureReason.DAILY_QUOTA_EXHAUSTED,
                action=FailureAction.STOP,
                attempts=1,
            )
        if stage == PipelineStage.VERIFY:
            return StageResult[VerificationOutput](
                stage=stage,
                output=VerificationOutput(
                    reviews=[
                        SemanticReview(
                            candidate_id=payload.candidates[0].candidate_id,
                            decision="supported",
                            reason="Подтверждено в исходной формулировке.",
                        )
                    ]
                ),
                attempts=1,
            )
        if stage == PipelineStage.CLARIFY:
            return StageResult[QuestionGenerationOutput](
                stage=stage,
                output=QuestionGenerationOutput(),
                attempts=1,
            )
        raise AssertionError(f"Unexpected stage {stage}")

    report = execute_pipeline(
        doc,
        ProviderSettings(api_key="test-key"),
        generate_fn=scripted_generate,
    )

    assert report.coverage.execution == ExecutionStatus.PARTIAL
    assert report.verdict.readiness == Readiness.NO_GO
    assert report.statistics.critical == 1


def test_partially_readable_pdf_with_unread_page_never_produces_go():
    sha = "e" * 64
    text = "Максимальный размер перевода 1 000 000 KZT."
    doc = ParsedDocument(
        version=DocumentVersion(
            sha256=sha,
            parser_version="ingest-1",
            name="mixed.pdf",
            format=DocumentFormat.PDF,
            page_count=2,
            blocks=[
                SourceBlock(
                    block_id="B00001",
                    text=text,
                    location=SourceLocation(section="Лимиты", page=1),
                )
            ],
        ),
        unread_pages=[2],
        diagnostics=["Страница 2: графическая страница без извлекаемого текста."],
    )

    def scripted_generate(stage: PipelineStage, payload, output_type, *, settings=None, **kwargs):
        if stage == PipelineStage.EXTRACT:
            return StageResult[ExtractionOutput](
                stage=stage,
                output=ExtractionOutput(
                    items=[ExtractedItem(block_id="B00001", original_quote=text, summary=text)]
                ),
                attempts=1,
            )
        if stage == PipelineStage.INDIVIDUAL:
            return StageResult[IndividualAuditOutput](
                stage=stage,
                output=IndividualAuditOutput(evaluations=_all_indiv("R001")),
                attempts=1,
            )
        if stage == PipelineStage.HOLISTIC:
            assert payload.has_uninspected_scope is True
            return StageResult[HolisticAuditOutput](
                stage=stage,
                output=HolisticAuditOutput(evaluations=_all_whole()),
                attempts=1,
            )
        raise AssertionError(f"Unexpected stage {stage}")

    report = execute_pipeline(
        doc,
        ProviderSettings(api_key="test-key"),
        generate_fn=scripted_generate,
    )

    assert report.coverage.execution == ExecutionStatus.PARTIAL
    assert report.verdict.readiness == Readiness.UNDETERMINED
    assert any("Страница 2" in r for r in report.coverage.incomplete_reasons)


def test_total_extraction_failure_missing_credentials_and_unreadable_pdf():
    doc = parse("fail.md", "Обязательное правило авторизации.\n".encode("utf-8"))

    # 1. Missing API key raises typed ProviderError before making requests.
    events: list[ProgressEvent] = []
    with pytest.raises(ProviderError) as err:
        run_pipeline(doc, ProviderSettings(api_key=None), on_progress=events.append)
    assert err.value.reason == FailureReason.AUTH_ERROR
    assert any(e.stage == PipelineStage.EXTRACT and e.state == "failed" for e in events)

    # 2. Provider failure on extraction yields canonical FAILED / UNDETERMINED report.
    def failing_generate(stage, payload, output_type, *, settings=None, **kwargs):
        raise ProviderError("Transport timeout", reason=FailureReason.TIMEOUT, attempts=3)

    failed_report = execute_pipeline(
        doc,
        ProviderSettings(api_key="test-key"),
        generate_fn=failing_generate,
    )
    assert failed_report.coverage.execution == ExecutionStatus.FAILED
    assert failed_report.verdict.readiness == Readiness.UNDETERMINED
    assert failed_report.metadata.request_attempts == 3
    assert failed_report.requirements == []

    # 3. Completely unreadable PDF (0 readable blocks, 1 unread page) returns FAILED report with 0 attempts.
    unreadable_pdf = ParsedDocument(
        version=DocumentVersion(
            sha256="f" * 64,
            parser_version="ingest-1",
            name="scan.pdf",
            format=DocumentFormat.PDF,
            page_count=1,
            blocks=[],
        ),
        unread_pages=[1],
        diagnostics=["Страница 1: графическая страница без извлекаемого текста."],
    )
    unreadable_report = execute_pipeline(
        unreadable_pdf,
        ProviderSettings(api_key=None),
        generate_fn=lambda *a, **k: pytest.fail("should not contact provider on unreadable pdf"),
    )
    assert unreadable_report.coverage.execution == ExecutionStatus.FAILED
    assert unreadable_report.verdict.readiness == Readiness.UNDETERMINED
    assert unreadable_report.metadata.request_attempts == 0


def test_truncation_malformed_and_invalid_evidence_handled_conservatively():
    doc = parse(
        "resilience.md",
        "Правило 1: Платёж подтверждается OTP.\n\nПравило 2: Возврат выполняется за 24 часа.\n".encode("utf-8"),
    )
    b1, b2 = doc.version.blocks
    extract_calls = 0

    def scripted_generate(stage: PipelineStage, payload, output_type, *, settings=None, **kwargs):
        nonlocal extract_calls
        if stage == PipelineStage.EXTRACT:
            extract_calls += 1
            if extract_calls == 1:
                raise ProviderError(
                    "Truncated structured response (MAX_TOKENS)",
                    reason=FailureReason.TRUNCATED,
                    action=FailureAction.SPLIT,
                    attempts=1,
                )
            if extract_calls == 2 and payload.repair_hint is None:
                raise ProviderError(
                    "Malformed structured response",
                    reason=FailureReason.MALFORMED_RESPONSE,
                    action=FailureAction.REPAIR,
                    attempts=1,
                )
            if b1.block_id in payload.target_block_ids:
                return StageResult[ExtractionOutput](
                    stage=stage,
                    output=ExtractionOutput(
                        items=[ExtractedItem(block_id=b1.block_id, original_quote=b1.text, summary=b1.text)]
                    ),
                    attempts=1,
                )
            # Return fabricated quote for b2 -> grounding fails, b2 stays incomplete!
            return StageResult[ExtractionOutput](
                stage=stage,
                output=ExtractionOutput(
                    items=[ExtractedItem(block_id=b2.block_id, original_quote="выдуманная цитата", summary="Возврат.")]
                ),
                attempts=1,
            )
        if stage == PipelineStage.INDIVIDUAL:
            return StageResult[IndividualAuditOutput](
                stage=stage,
                output=IndividualAuditOutput(evaluations=_all_indiv("R001")),
                attempts=1,
            )
        if stage == PipelineStage.HOLISTIC:
            assert payload.has_uninspected_scope is True
            return StageResult[HolisticAuditOutput](
                stage=stage,
                output=HolisticAuditOutput(evaluations=_all_whole()),
                attempts=1,
            )
        raise AssertionError(f"Unexpected stage {stage}")

    report = execute_pipeline(
        doc,
        ProviderSettings(api_key="test-key"),
        generate_fn=scripted_generate,
    )

    assert extract_calls == 4  # 1 split + 1 malformed + 1 repair (b1) + 1 invalid quote (b2)
    assert report.coverage.execution == ExecutionStatus.PARTIAL
    assert report.verdict.readiness == Readiness.UNDETERMINED
    assert report.coverage.extraction_blocks.completed == [b1.block_id]
    assert len(report.requirements) == 1
    assert report.metadata.request_attempts == 6
