"""End-to-end offline workflow test: audit -> AI remediation -> safe apply -> export -> revalidate."""
from hashlib import sha256

import pytest

from src.clarification import (
    ImprovementRequestPayload,
    ImprovementRewriteOutput,
    apply_changes,
    approve_proposal,
    draft_remediation_proposals,
    export_improved_markdown,
    revalidate_improved_document,
)
from src.contracts import (
    INDIVIDUAL,
    WHOLE_SET,
    Answer,
    CriterionStatus,
    ExecutionStatus,
    FindingType,
    PipelineStage,
    ProviderSettings,
    QualityCriterion,
    QuestionPriority,
    Readiness,
    RequirementCategory,
    SemanticStatus,
    Severity,
    StageResult,
    StakeholderRole,
    ValidationReport,
)
from src.ingest import parse
from src.pipeline import execute_pipeline
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
    QuestionCandidate,
    QuestionGenerationOutput,
    QuestionOptionDraft,
    SemanticReview,
    VerificationOutput,
)

ORDERED_INDIVIDUAL = sorted(INDIVIDUAL, key=lambda c: c.value)
ORDERED_WHOLE = sorted(WHOLE_SET, key=lambda c: c.value)

SYNTHETIC_INITIAL_SPEC = """# Сервис корпоративных выплат

## 1. Бизнес-правила

FR-001: Операции учитываются в месяце даты создания платежа по часовому поясу UTC+5.

FR-002: Заявка в терминальном статусе PAID не подлежит повторному проведению.

FR-003: Статус выплаты обновляется быстро после ответа банка.

## 2. Таблица API

| Код | Метод | Описание |
| --- | --- | --- |
| API-001 | POST /api/v1/payouts | Создание выплаты, возвращает HTTP 201 |
| API-002 | GET /api/v1/payouts/{id} | Получение статуса выплаты, возвращает HTTP 200 |

## 3. Критерии приёмки

AC-001: Платёж, созданный 31 марта 23:50 UTC+5 и проведённый банком 1 апреля, учитывается в апреле.
"""


def _all_indiv(req_id: str, overrides=None) -> list[IndividualCriterionCandidate]:
    overrides = overrides or {}
    return [
        IndividualCriterionCandidate(
            requirement_id=req_id,
            criterion=crit,
            status=overrides[crit][0] if crit in overrides else CriterionStatus.PASS,
            rationale=overrides[crit][1] if crit in overrides else "Требование соответствует критерию.",
            local_finding_keys=overrides[crit][2] if crit in overrides else [],
        )
        for crit in ORDERED_INDIVIDUAL
    ]


def _all_whole(overrides=None) -> list[HolisticCriterionCandidate]:
    overrides = overrides or {}
    return [
        HolisticCriterionCandidate(
            criterion=crit,
            status=overrides[crit][0] if crit in overrides else CriterionStatus.PASS,
            rationale=overrides[crit][1] if crit in overrides else "Набор требований согласован.",
            local_finding_keys=overrides[crit][2] if crit in overrides else [],
        )
        for crit in ORDERED_WHOLE
    ]


def test_full_end_to_end_audit_improve_export_and_revalidate_workflow():
    doc_v1 = parse("payouts_v1.md", SYNTHETIC_INITIAL_SPEC.encode("utf-8"))
    original_v1_json = doc_v1.model_dump_json()

    # Keep track of provider payloads to confirm initial pipeline never invokes AI rewrite DTOs.
    initial_stages: list[tuple[PipelineStage, type]] = []

    def _pipeline_generate(stage: PipelineStage, payload, output_type, *, settings=None, **kwargs):
        initial_stages.append((stage, type(payload)))
        assert not isinstance(payload, ImprovementRequestPayload)

        if stage == PipelineStage.EXTRACT:
            items: list[ExtractedItem] = []
            ctx: list[ContextBlockDisposition] = []
            for blk in payload.blocks:
                if not blk.is_target:
                    continue
                if blk.text.startswith("#"):
                    ctx.append(ContextBlockDisposition(block_id=blk.block_id, reason="Заголовок раздела."))
                elif "| API-001 |" in blk.text:
                    for line in blk.text.splitlines():
                        if "| API-001 |" in line:
                            items.append(ExtractedItem(block_id=blk.block_id, original_id="API-001", category=RequirementCategory.INTERFACE, original_quote=line, summary="Создание выплаты POST /api/v1/payouts"))
                        elif "| API-002 |" in line:
                            items.append(ExtractedItem(block_id=blk.block_id, original_id="API-002", category=RequirementCategory.INTERFACE, original_quote=line, summary="Статус выплаты GET /api/v1/payouts/{id}"))
                else:
                    orig_id = blk.text.split(":", 1)[0].strip() if ":" in blk.text else None
                    cat = RequirementCategory.ACCEPTANCE if (orig_id or "").startswith("AC-") else RequirementCategory.FUNCTIONAL
                    items.append(
                        ExtractedItem(
                            block_id=blk.block_id,
                            original_id=orig_id,
                            category=cat,
                            original_quote=blk.text,
                            summary=blk.text,
                        )
                    )
            return StageResult[ExtractionOutput](
                stage=stage,
                output=ExtractionOutput(items=items, context_blocks=ctx),
                attempts=1,
                input_tokens=150,
                output_tokens=90,
            )

        if stage == PipelineStage.INDIVIDUAL:
            evals: list[IndividualCriterionCandidate] = []
            findings: list[IndividualFindingCandidate] = []
            for req in payload.requirements:
                if "быстро" in req.quote:
                    evals.extend(
                        _all_indiv(
                            req.req_id,
                            overrides={
                                QualityCriterion.VERIFIABLE: (
                                    CriterionStatus.FAIL,
                                    "Слово «быстро» не содержит измеримого SLA.",
                                    ["K_SLA"],
                                )
                            },
                        )
                    )
                    findings.append(
                        IndividualFindingCandidate(
                            local_key="K_SLA",
                            requirement_id=req.req_id,
                            kind=FindingType.UNVERIFIABLE,
                            severity=Severity.MAJOR,
                            claim="Формулировка «быстро» в FR-003 не проверяема тестами.",
                            recommendation="Задать измеримый срок обновления статуса.",
                            criteria=[QualityCriterion.VERIFIABLE],
                            evidence=[IndividualEvidenceQuote(block_id=req.block_id, quote=req.quote)],
                        )
                    )
                else:
                    evals.extend(_all_indiv(req.req_id))
            return StageResult[IndividualAuditOutput](
                stage=stage,
                output=IndividualAuditOutput(evaluations=evals, findings=findings),
                attempts=1,
                input_tokens=160,
                output_tokens=110,
            )

        if stage == PipelineStage.HOLISTIC:
            assert any("semantic_focus:timing_calendar" in s for s in payload.structural_signals)
            fr1 = next(r for r in payload.requirements if r.original_id == "FR-001")
            ac1 = next(r for r in payload.requirements if r.original_id == "AC-001")
            # Detect contradiction ONLY when AC-001 still places March 31 payment into April!
            if "в апреле" in ac1.quote:
                return StageResult[HolisticAuditOutput](
                    stage=stage,
                    output=HolisticAuditOutput(
                        evaluations=_all_whole(
                            overrides={
                                QualityCriterion.SET_CONSISTENT: (
                                    CriterionStatus.FAIL,
                                    "FR-001 и AC-001 относят операцию на границе месяца к разным периодам.",
                                    ["C_MONTH"],
                                ),
                                QualityCriterion.SRS_CONSISTENT: (
                                    CriterionStatus.FAIL,
                                    "Несогласованность даты события и даты банковской проводки.",
                                    ["C_MONTH"],
                                ),
                            }
                        ),
                        findings=[
                            HolisticFindingCandidate(
                                local_key="C_MONTH",
                                kind=FindingType.CONTRADICTION,
                                severity=Severity.CRITICAL,
                                claim="FR-001 относит операцию к месяцу создания (март), а AC-001 — к месяцу проводки (апрель).",
                                scenario="Платёж создан 31 марта 23:50 UTC+5 и проведён 1 апреля",
                                recommendation="Согласовать отнесение операций на границе месяца в FR-001 и AC-001.",
                                requirement_ids=[fr1.req_id, ac1.req_id],
                                criteria=[QualityCriterion.SET_CONSISTENT, QualityCriterion.SRS_CONSISTENT],
                                evidence=[
                                    HolisticEvidenceQuote(block_id=fr1.block_id, quote=fr1.quote),
                                    HolisticEvidenceQuote(block_id=ac1.block_id, quote=ac1.quote),
                                ],
                            )
                        ],
                    ),
                    attempts=1,
                    input_tokens=180,
                    output_tokens=100,
                )
            return StageResult[HolisticAuditOutput](
                stage=stage,
                output=HolisticAuditOutput(evaluations=_all_whole()),
                attempts=1,
                input_tokens=170,
                output_tokens=80,
            )

        if stage == PipelineStage.VERIFY:
            return StageResult[VerificationOutput](
                stage=stage,
                output=VerificationOutput(
                    reviews=[
                        SemanticReview(
                            candidate_id=c.candidate_id,
                            decision="supported",
                            reason="Подтверждено в полном контексте спецификации без исключений.",
                        )
                        for c in payload.candidates
                    ]
                ),
                attempts=1,
                input_tokens=120,
                output_tokens=60,
            )

        if stage == PipelineStage.CLARIFY:
            qs: list[QuestionCandidate] = []
            for f in payload.findings:
                if f.kind == FindingType.CONTRADICTION.value:
                    qs.append(
                        QuestionCandidate(
                            finding_ids=[f.finding_id],
                            requirement_ids=f.requirement_ids,
                            evidence_block_ids=[e.block_id for e in f.evidence],
                            uncertainty="По какой дате относить платёж к расчётному месяцу?",
                            stakeholder=StakeholderRole.PRODUCT,
                            priority=QuestionPriority.BLOCKER,
                            business_impact="Ошибки закрытия периода и расхождения отчётности.",
                            question="Должен ли платёж 31 марта 23:50 UTC+5 с проводкой 1 апреля относиться к марту (по дате создания) или к апрелю?",
                            options=[
                                QuestionOptionDraft(
                                    option_id="A",
                                    label="По дате создания платежа в UTC+5 (относится к марту)",
                                    consequence="Обновить AC-001, чтобы платёж учитывался в марте.",
                                ),
                                QuestionOptionDraft(
                                    option_id="B",
                                    label="По дате банковской проводки (относится к апрелю)",
                                    consequence="Обновить FR-001 на учёт по дате проводки.",
                                ),
                            ],
                        )
                    )
                else:
                    qs.append(
                        QuestionCandidate(
                            finding_ids=[f.finding_id],
                            requirement_ids=f.requirement_ids,
                            evidence_block_ids=[e.block_id for e in f.evidence],
                            uncertainty="Не задан измеримый SLA обновления статуса выплаты.",
                            stakeholder=StakeholderRole.ARCHITECT,
                            priority=QuestionPriority.HIGH,
                            business_impact="Невозможно настроить мониторинг и приёмочные тесты SLA.",
                            question="За какое максимальное время должен обновляться статус выплаты в FR-003?",
                            options=[
                                QuestionOptionDraft(
                                    option_id="A",
                                    label="Не позднее 5 секунд (p99) после получения ответа банка",
                                    consequence="Зафиксировать 5-секундный SLA в FR-003.",
                                ),
                                QuestionOptionDraft(
                                    option_id="B",
                                    label="Не позднее 60 секунд после ответа банка",
                                    consequence="Разрешить пакетный опрос статусов.",
                                ),
                            ],
                        )
                    )
            return StageResult[QuestionGenerationOutput](
                stage=stage,
                output=QuestionGenerationOutput(questions=qs),
                attempts=1,
                input_tokens=130,
                output_tokens=90,
            )

        raise AssertionError(f"Unexpected stage {stage}")

    # 1. Initial validation via production pipeline -> NO_GO with verified contradiction & SLA issue.
    settings = ProviderSettings(api_key="test-key", model="gemini-test")
    report_v1 = execute_pipeline(doc_v1, settings, generate_fn=_pipeline_generate)
    assert report_v1.coverage.execution == ExecutionStatus.COMPLETE
    assert report_v1.verdict.readiness == Readiness.NO_GO
    assert report_v1.statistics.critical == 1
    assert report_v1.statistics.major == 1
    assert len(report_v1.questions) == 2

    # Verify every confirmed contradiction has >= 2 distinct grounded references.
    contradictions = [f for f in report_v1.findings if f.kind == FindingType.CONTRADICTION]
    assert len(contradictions) == 1
    assert contradictions[0].semantic_status == SemanticStatus.SUPPORTED
    assert len([r for r in contradictions[0].evidence if r.grounded]) >= 2

    # 2. Stakeholder answers + AI-assisted remediation drafting (separate from run_pipeline!).
    q_month = next(q for q in report_v1.questions if q.priority == QuestionPriority.BLOCKER)
    q_sla = next(q for q in report_v1.questions if q.priority == QuestionPriority.HIGH)
    ac1_ref = next(r for r in q_month.evidence if "AC-001:" in r.quote)

    answers = [
        Answer(question_id=q_month.question_id, version=doc_v1.version.sha256, option_id="A"),
        Answer(question_id=q_sla.question_id, version=doc_v1.version.sha256, option_id="A"),
    ]

    def _mock_rewrite(stage: PipelineStage, payload: ImprovementRequestPayload, output_type, **kwargs):
        assert stage == PipelineStage.CLARIFY
        if "AC-001:" in payload.target_quote or "FR-001:" in payload.target_quote:
            # Rewrite AC-001 to match stakeholder option A (March / creation date).
            return StageResult[ImprovementRewriteOutput](
                stage=stage,
                output=ImprovementRewriteOutput(
                    status="ready",
                    improved_text="AC-001: Платёж, созданный 31 марта 23:50 UTC+5 и проведённый банком 1 апреля, учитывается в марте.",
                    rationale="Сценарий приёмки AC-001 согласован с правилом FR-001 по решению Product Owner.",
                    preserved_invariants=["AC-001", "UTC+5"],
                ),
                attempts=1,
                input_tokens=90,
                output_tokens=50,
            )
        return StageResult[ImprovementRewriteOutput](
            stage=stage,
            output=ImprovementRewriteOutput(
                status="ready",
                improved_text="FR-003: Статус выплаты обновляется не позднее 5 секунд (p99) после получения ответа банка.",
                rationale="Заменено неизмеримое «быстро» на утверждённый SLA 5 секунд (p99).",
                preserved_invariants=["FR-003"],
            ),
            attempts=1,
            input_tokens=85,
            output_tokens=45,
        )

    # Draft proposals: target AC-001 explicitly for q_month and auto-resolve FR-003 for q_sla.
    from src.clarification import draft_remediation_proposal

    p_month = draft_remediation_proposal(
        report_v1, answer=answers[0], target_ref=ac1_ref, generate_fn=_mock_rewrite
    )
    p_sla = draft_remediation_proposals(report_v1, answers=[answers[1]], generate_fn=_mock_rewrite)[0]

    # Approve both proposals and apply safely in one batch.
    bundle = apply_changes(doc_v1, [approve_proposal(p_month), approve_proposal(p_sla)])

    # Original document remains completely unchanged.
    assert doc_v1.model_dump_json() == original_v1_json
    assert bundle.document.version.parent_sha256 == doc_v1.version.sha256
    assert bundle.document.version.sha256 != doc_v1.version.sha256

    # Export complete improved Markdown preserving unaffected table and rules.
    improved_md = export_improved_markdown(bundle)
    assert sha256(improved_md.encode("utf-8")).hexdigest() == bundle.document.version.sha256
    assert "| API-001 | POST /api/v1/payouts | Создание выплаты, возвращает HTTP 201 |" in improved_md
    assert "FR-002: Заявка в терминальном статусе PAID не подлежит повторному проведению." in improved_md
    assert "учитывается в марте." in improved_md
    assert "не позднее 5 секунд (p99)" in improved_md
    assert bundle.change_log.applied_count == 2

    # Stale revision check: re-applying v1 proposals to v2 bundle must fail deterministically.
    with pytest.raises(ValueError, match="версии"):
        apply_changes(bundle.document, [approve_proposal(p_month)])

    # 3. Revalidate improved document through production pipeline -> transitions NO_GO -> GO!
    report_v2 = revalidate_improved_document(
        bundle,
        settings,
        runner=lambda doc, cfg, on_progress=None: execute_pipeline(
            doc, cfg, on_progress=on_progress, generate_fn=_pipeline_generate
        ),
    )
    assert ValidationReport.model_validate_json(report_v2.model_dump_json()) == report_v2
    assert report_v2.document.version.parent_sha256 == doc_v1.version.sha256
    assert report_v2.coverage.execution == ExecutionStatus.COMPLETE
    assert report_v2.verdict.readiness == Readiness.GO
    assert report_v2.statistics.violations == 0
    assert report_v2.statistics.critical == 0
    assert report_v2.statistics.major == 0
