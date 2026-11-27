"""Unit tests for the AI Document Improvement Engine (Issue #34 Priority 1)."""
from datetime import datetime, timezone
from hashlib import sha256
import json

import pymupdf
import pytest

from src.clarification import (
    ImprovedDocumentBundle,
    ImprovementRequestPayload,
    ImprovementRewriteOutput,
    RemediationProposal,
    apply_changes,
    approve_proposal,
    draft_remediation_proposal,
    draft_remediation_proposals,
    export_improved_markdown,
    reject_proposal,
    render_change_log_markdown,
    revalidate_improved_document,
)
from src.contracts import (
    Answer,
    Coverage,
    CriterionEvaluation,
    CriterionStatus,
    EvidenceRef,
    ExecutionStatus,
    Finding,
    FindingType,
    GapEvidence,
    GroundingStatus,
    PipelineStage,
    ProposedChange,
    ProviderSettings,
    QualityCriterion,
    Question,
    QuestionOption,
    QuestionPriority,
    Readiness,
    Requirement,
    RequirementCategory,
    RunMetadata,
    SemanticStatus,
    Severity,
    StageResult,
    StakeholderRole,
    ValidationReport,
    Verdict,
    WorkSet,
)
from src.contracts.enums import INDIVIDUAL, WHOLE_SET
from src.ingest import parse
from src.provider import FailureAction, FailureReason, ProviderError

SYNTHETIC_SPEC = """# Платёжный модуль переводов

## 1. Функциональные правила

FR-001: Лимит перевода составляет 500 000 KZT в сутки.

FR-002: Перевод выполняется быстро при достаточном балансе.

## 2. Таблица тарифов и статусов

| Код | Операция | Срок зачисления | Примечание |
| --- | --- | --- | --- |
| FR-003 | Перевод по номеру | 1 день | Комиссия 0% |
| FR-004 | Возврат платежа | 3 дня | Без исключений |

## 3. Критерии приёмки

AC-001: Лимит перевода проверяется на уровне 300 000 KZT в календарные сутки.
"""


def _build_synthetic_report() -> ValidationReport:
    doc = parse("synthetic_transfers.md", SYNTHETIC_SPEC.encode("utf-8"))
    ver = doc.version
    blocks = {b.block_id: b for b in ver.blocks}

    b_fr1 = next(b for b in ver.blocks if "FR-001:" in b.text)
    b_fr2 = next(b for b in ver.blocks if "FR-002:" in b.text)
    b_tbl = next(b for b in ver.blocks if "| FR-003 |" in b.text)
    b_ac1 = next(b for b in ver.blocks if "AC-001:" in b.text)

    def _ref(block, needle: str) -> EvidenceRef:
        start = block.text.index(needle)
        return EvidenceRef(
            version=ver.sha256,
            block_id=block.block_id,
            start=start,
            end=start + len(needle),
            quote=needle,
            grounding=GroundingStatus.EXACT,
        )

    ref_fr1 = _ref(b_fr1, "FR-001: Лимит перевода составляет 500 000 KZT в сутки.")
    ref_fr2 = _ref(b_fr2, "FR-002: Перевод выполняется быстро при достаточном балансе.")
    ref_tbl_row1 = _ref(b_tbl, "| FR-003 | Перевод по номеру | 1 день | Комиссия 0% |")
    ref_tbl_row2 = _ref(b_tbl, "| FR-004 | Возврат платежа | 3 дня | Без исключений |")
    ref_ac1 = _ref(b_ac1, "AC-001: Лимит перевода проверяется на уровне 300 000 KZT в календарные сутки.")

    requirements = [
        Requirement(req_id="R001", original_id="FR-001", category=RequirementCategory.FUNCTIONAL, summary="Лимит 500 000 KZT", source=ref_fr1),
        Requirement(req_id="R002", original_id="FR-002", category=RequirementCategory.QUALITY, summary="Скорость перевода", source=ref_fr2),
        Requirement(req_id="R003", original_id="FR-003", category=RequirementCategory.FUNCTIONAL, summary="Перевод по номеру", source=ref_tbl_row1),
        Requirement(req_id="R004", original_id="FR-004", category=RequirementCategory.FUNCTIONAL, summary="Возврат платежа", source=ref_tbl_row2),
        Requirement(req_id="R005", original_id="AC-001", category=RequirementCategory.ACCEPTANCE, summary="Проверка лимита 300 000 KZT", source=ref_ac1),
    ]

    f_contra = Finding(
        finding_id="F001",
        kind=FindingType.CONTRADICTION,
        severity=Severity.CRITICAL,
        claim="Суточный лимит перевода в FR-001 (500 000 KZT) противоречит AC-001 (300 000 KZT).",
        recommendation="Согласовать единый лимит перевода.",
        requirement_ids=["R001", "R005"],
        criteria=[QualityCriterion.SET_CONSISTENT],
        evidence=[ref_fr1, ref_ac1],
        semantic_status=SemanticStatus.SUPPORTED,
        verification_reason="Оба требования задают несовместимые суточные лимиты.",
    )
    f_ambig = Finding(
        finding_id="F002",
        kind=FindingType.UNVERIFIABLE,
        severity=Severity.MAJOR,
        claim="Слово «быстро» в FR-002 не содержит измеримого SLA.",
        recommendation="Задать измеримое время обработки.",
        requirement_ids=["R002"],
        criteria=[QualityCriterion.VERIFIABLE],
        evidence=[ref_fr2],
        semantic_status=SemanticStatus.SUPPORTED,
        verification_reason="Отсутствует числовой порог задержки.",
    )
    f_gap = Finding(
        finding_id="F003",
        kind=FindingType.MISSING_INFORMATION,
        severity=Severity.MAJOR,
        claim="В таблице не указано, рабочие или календарные дни применяются к FR-003.",
        recommendation="Уточнить тип дней для срока зачисления.",
        requirement_ids=["R003"],
        criteria=[QualityCriterion.COMPLETE],
        evidence=[ref_tbl_row1],
        gap=GapEvidence(
            expected_information="Тип дней (календарные или банковские рабочие дни) для FR-003",
            context=ref_tbl_row1,
            inspected_blocks=[b.block_id for b in ver.blocks],
        ),
        semantic_status=SemanticStatus.SUPPORTED,
        verification_reason="В спецификации отсутствует определение типа дней.",
    )

    evaluations: list[CriterionEvaluation] = []
    for req in requirements:
        for crit in sorted(INDIVIDUAL):
            fids = [f.finding_id for f in (f_ambig, f_gap) if req.req_id in f.requirement_ids and crit in f.criteria]
            evaluations.append(
                CriterionEvaluation(
                    requirement_id=req.req_id,
                    criterion=crit,
                    status=CriterionStatus.FAIL if fids else CriterionStatus.PASS,
                    rationale="Выявлен дефект." if fids else "Нарушений не обнаружено.",
                    finding_ids=fids,
                )
            )
    for crit in sorted(WHOLE_SET):
        fids = [f_contra.finding_id] if crit in f_contra.criteria else []
        evaluations.append(
            CriterionEvaluation(
                requirement_id=None,
                criterion=crit,
                status=CriterionStatus.FAIL if fids else CriterionStatus.PASS,
                rationale="Обнаружено противоречие." if fids else "Согласовано.",
                finding_ids=fids,
            )
        )

    cited_blocks = {r.source.block_id for r in requirements}
    context_blocks = {
        b.block_id: "Заголовок раздела." for b in ver.blocks if b.block_id not in cited_blocks
    }
    coverage = Coverage(
        execution=ExecutionStatus.COMPLETE,
        extraction_blocks=WorkSet(planned=[b.block_id for b in ver.blocks], completed=[b.block_id for b in ver.blocks]),
        context_blocks=context_blocks,
        individual_requirements=WorkSet(planned=[r.req_id for r in requirements], completed=[r.req_id for r in requirements]),
        audit_batches=WorkSet(planned=["batch-1"], completed=["batch-1"]),
        cross_section_groups=WorkSet(planned=["whole-spec"], completed=["whole-spec"]),
        incomplete_reasons=[],
        uncertainties=[],
    )

    q1 = Question(
        question_id="Q001",
        finding_ids=["F001"],
        requirement_ids=["R001", "R005"],
        evidence=[ref_fr1, ref_ac1],
        uncertainty="Какой суточный лимит перевода является целевым?",
        stakeholder=StakeholderRole.PRODUCT,
        priority=QuestionPriority.BLOCKER,
        business_impact="Разные лимиты приведут к неверной блокировке переводов.",
        question="Какой суточный лимит перевода нужно зафиксировать в FR-001 и AC-001?",
        options=[
            QuestionOption(option_id="A", label="Зафиксировать лимит 500 000 KZT в календарные сутки.", consequence="Потребуется обновить AC-001."),
            QuestionOption(option_id="B", label="Зафиксировать лимит 300 000 KZT в календарные сутки.", consequence="Потребуется снизить лимит в FR-001."),
        ],
    )
    q2 = Question(
        question_id="Q002",
        finding_ids=["F002"],
        requirement_ids=["R002"],
        evidence=[ref_fr2],
        uncertainty="Каков измеримый SLA для перевода?",
        stakeholder=StakeholderRole.ARCHITECT,
        priority=QuestionPriority.HIGH,
        business_impact="Невозможно провести нагрузочное тестирование.",
        question="Какой измеримый порог времени выполнения перевода зафиксировать в FR-002?",
        options=[
            QuestionOption(option_id="A", label="До 3 секунд для 99% запросов.", consequence="Стандартный онлайн SLA."),
            QuestionOption(option_id="B", label="До 10 секунд для 95% запросов.", consequence="Упрощённый SLA."),
        ],
    )
    q3 = Question(
        question_id="Q003",
        finding_ids=["F003"],
        requirement_ids=["R003"],
        evidence=[ref_tbl_row1],
        uncertainty="Какие дни используются для срока зачисления FR-003?",
        stakeholder=StakeholderRole.ANALYST,
        priority=QuestionPriority.HIGH,
        business_impact="Неоднозначность расчёта сроков в выходные дни.",
        question="Какие дни применять для срока зачисления в строке таблицы FR-003?",
        options=[
            QuestionOption(option_id="A", label="1 рабочий банковский день", consequence="Срок переносится на следующий рабочий день."),
            QuestionOption(option_id="B", label="1 календарный день", consequence="Зачисление работает 24/7."),
        ],
    )

    return ValidationReport(
        metadata=RunMetadata(
            run_id="run-synthetic-001",
            started_at=datetime.now(timezone.utc),
            origin="synthetic",
            document_version=ver.sha256,
            model="synthetic-fixture",
            agent_revision="test-rev",
            prompt_version="test-v1",
            duration_seconds=0.0,
            request_attempts=0,
        ),
        document=doc,
        requirements=requirements,
        evaluations=evaluations,
        findings=[f_contra, f_ambig, f_gap],
        coverage=coverage,
        questions=[q1, q2, q3],
        verdict=Verdict(
            readiness=Readiness.NO_GO,
            rationale="Подтверждено критическое противоречие F001.",
            next_actions=["Устранить противоречие между FR-001 и AC-001."],
        ),
    )


def test_never_invents_unapproved_business_rules_for_gap_or_contradiction():
    report = _build_synthetic_report()
    called = False

    def _forbid_call(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("Provider must not be invoked to invent unapproved rules")

    gap_prop = draft_remediation_proposal(report, finding_id="F003", generate_fn=_forbid_call)
    contra_prop = draft_remediation_proposal(report, finding_id="F001", generate_fn=_forbid_call)

    assert not called
    assert gap_prop.status == "needs_clarification"
    assert gap_prop.proposed_text is None
    assert gap_prop.clarification_needed and "Q003" in gap_prop.clarification_needed
    assert contra_prop.status == "needs_clarification"
    assert contra_prop.proposed_text is None
    assert contra_prop.clarification_needed and "Q001" in contra_prop.clarification_needed


def test_ai_assisted_rewrite_uses_clarify_stage_preserves_ids_and_repairs_broken_table():
    report = _build_synthetic_report()
    ver = report.document.version.sha256
    answer = Answer(question_id="Q003", version=ver, option_id="A")
    calls: list[ImprovementRequestPayload] = []

    def _mock_generate(stage, payload, output_type, **kwargs):
        assert stage == PipelineStage.CLARIFY
        assert isinstance(payload, ImprovementRequestPayload)
        assert output_type is ImprovementRewriteOutput
        calls.append(payload)
        if len(calls) == 1:
            # First response breaks table pipe count and drops FR-003 -> triggers repair!
            out = ImprovementRewriteOutput(
                status="ready",
                improved_text="Перевод по номеру: 1 рабочий банковский день",
                rationale="Первичная попытка без таблицы.",
            )
        else:
            assert payload.repair_hint is not None
            out = ImprovementRewriteOutput(
                status="ready",
                improved_text="| FR-003 | Перевод по номеру | 1 рабочий банковский день | Комиссия 0% |",
                rationale="Уточнён тип дней по решению стейкхолдера с сохранением таблицы и FR-003.",
                preserved_invariants=["FR-003", "Комиссия 0%", "Структура Markdown-таблицы"],
            )
        return StageResult[ImprovementRewriteOutput](
            stage=stage,
            output=out,
            attempts=1,
            input_tokens=120,
            output_tokens=60,
        )

    proposal = draft_remediation_proposal(report, answer=answer, generate_fn=_mock_generate)
    assert len(calls) == 2
    assert proposal.status == "draft"
    assert proposal.origin == "ai_assisted"
    assert proposal.question_id == "Q003"
    assert proposal.finding_ids == ["F003"]
    assert proposal.proposed_text == "| FR-003 | Перевод по номеру | 1 рабочий банковский день | Комиссия 0% |"
    assert proposal.attempts == 2
    assert proposal.input_tokens == 240
    assert proposal.output_tokens == 120


def test_ai_rewrite_returns_needs_clarification_when_model_refuses_to_invent():
    report = _build_synthetic_report()

    def _mock_needs_clarification(stage, payload, output_type, **kwargs):
        return StageResult[ImprovementRewriteOutput](
            stage=stage,
            output=ImprovementRewriteOutput(
                status="needs_clarification",
                improved_text=None,
                rationale="Без утверждённого SLA нельзя заменить слово «быстро».",
                clarification_needed="Укажите числовой SLA (например, p99 в секундах) для FR-002.",
            ),
            attempts=1,
            input_tokens=100,
            output_tokens=40,
        )

    proposal = draft_remediation_proposal(report, finding_id="F002", generate_fn=_mock_needs_clarification)
    assert proposal.status == "needs_clarification"
    assert proposal.origin == "ai_assisted"
    assert proposal.proposed_text is None
    assert "SLA" in (proposal.clarification_needed or "")


def test_provider_failure_fallback_and_strict_error_modes():
    report = _build_synthetic_report()
    ver = report.document.version.sha256
    answer = Answer(
        question_id="Q002",
        version=ver,
        custom_text="FR-002: Перевод выполняется за время не более 3 секунд (p99) при достаточном балансе.",
    )

    def _failing_generate(*args, **kwargs):
        raise ProviderError(
            "Transient timeout",
            reason=FailureReason.TIMEOUT,
            action=FailureAction.STOP,
            attempts=2,
        )

    fallback = draft_remediation_proposal(
        report, answer=answer, generate_fn=_failing_generate, allow_fallback=True
    )
    assert fallback.status == "draft"
    assert fallback.origin == "deterministic_fallback"
    assert fallback.proposed_text == answer.custom_text
    assert "fallback" in fallback.rationale.lower()
    assert fallback.attempts == 2

    with pytest.raises(ProviderError):
        draft_remediation_proposal(
            report, answer=answer, generate_fn=_failing_generate, allow_fallback=False
        )


def test_apply_multiple_non_overlapping_changes_exports_complete_markdown_and_changelog():
    report = _build_synthetic_report()
    original_snapshot = report.document.model_dump_json()
    ver = report.document.version.sha256

    ans1 = Answer(
        question_id="Q001",
        version=ver,
        custom_text="FR-001: Лимит перевода составляет 300 000 KZT в календарные сутки.",
    )
    ans2 = Answer(
        question_id="Q002",
        version=ver,
        custom_text="FR-002: Перевод выполняется за время не более 3 секунд (p99) при достаточном балансе.",
    )
    ans3 = Answer(
        question_id="Q003",
        version=ver,
        custom_text="| FR-003 | Перевод по номеру | 1 рабочий банковский день | Комиссия 0% |",
    )

    drafts = draft_remediation_proposals(report, answers=[ans1, ans2, ans3], use_ai=False)
    assert len(drafts) == 3
    assert all(p.status == "draft" and p.origin == "stakeholder_decision" for p in drafts)

    # Unapproved drafts must be rejected by apply_changes.
    with pytest.raises(ValueError, match="утверждённые"):
        apply_changes(report.document, drafts)

    approved = [
        approve_proposal(drafts[0]),
        approve_proposal(
            drafts[1],
            edited_text="FR-002: Перевод выполняется не дольше 2 секунд (p99) при достаточном балансе.",
        ),
        approve_proposal(drafts[2]),
    ]

    # Also test two non-overlapping edits inside the SAME table block (row 1 + row 2).
    b_tbl = next(b for b in report.document.version.blocks if "| FR-004 |" in b.text)
    row2_quote = "| FR-004 | Возврат платежа | 3 дня | Без исключений |"
    row2_start = b_tbl.text.index(row2_quote)
    row2_ref = EvidenceRef(
        version=ver,
        block_id=b_tbl.block_id,
        start=row2_start,
        end=row2_start + len(row2_quote),
        quote=row2_quote,
        grounding=GroundingStatus.EXACT,
    )
    row2_change = ProposedChange(
        question_id="Q003",
        source=row2_ref,
        replacement="| FR-004 | Возврат платежа | 3 рабочих дня | Без исключений |",
    )

    bundle = apply_changes(report.document, [*approved, row2_change])
    assert isinstance(bundle, ImprovedDocumentBundle)

    # Original ParsedDocument is strictly immutable.
    assert report.document.model_dump_json() == original_snapshot

    # Parent/child SHA-256 lineage and exact Markdown export parity.
    assert bundle.document.version.parent_sha256 == ver
    assert bundle.document.version.sha256 == sha256(bundle.markdown.encode("utf-8")).hexdigest()
    assert bundle.change_log.parent_sha256 == ver
    assert bundle.change_log.improved_sha256 == bundle.document.version.sha256
    assert bundle.change_log.applied_count == 4

    # Preserves unaffected sections, headings, IDs, table headers, and AC-001.
    clean_md = export_improved_markdown(bundle)
    assert "# Платёжный модуль переводов" in clean_md
    assert "| Код | Операция | Срок зачисления | Примечание |" in clean_md
    assert "FR-001: Лимит перевода составляет 300 000 KZT в календарные сутки." in clean_md
    assert "FR-002: Перевод выполняется не дольше 2 секунд (p99) при достаточном балансе." in clean_md
    assert "| FR-003 | Перевод по номеру | 1 рабочий банковский день | Комиссия 0% |" in clean_md
    assert "| FR-004 | Возврат платежа | 3 рабочих дня | Без исключений |" in clean_md
    assert "AC-001: Лимит перевода проверяется на уровне 300 000 KZT в календарные сутки." in clean_md

    # Structured change log JSON + Markdown rendering.
    log_json = json.loads(bundle.change_log.model_dump_json())
    assert [e["change_id"] for e in log_json["entries"]] == ["CHG001", "CHG002", "CHG003", "CHG004"]
    appendix_md = export_improved_markdown(bundle, include_change_log_appendix=True)
    assert "## Журнал применённых изменений" in appendix_md
    assert "CHG001" in render_change_log_markdown(bundle.change_log)


def test_stale_overlapping_rejected_and_mismatched_edits_raise_value_error():
    report = _build_synthetic_report()
    ver = report.document.version.sha256
    b_fr1 = next(b for b in report.document.version.blocks if "FR-001:" in b.text)

    # Overlapping spans in the same block must raise ValueError.
    ref_a = EvidenceRef(
        version=ver, block_id=b_fr1.block_id, start=0, end=23, quote=b_fr1.text[0:23], grounding=GroundingStatus.EXACT
    )
    ref_b = EvidenceRef(
        version=ver, block_id=b_fr1.block_id, start=10, end=35, quote=b_fr1.text[10:35], grounding=GroundingStatus.EXACT
    )
    c_a = ProposedChange(question_id="Q001", source=ref_a, replacement="FR-001: Суточный лимит")
    c_b = ProposedChange(question_id="Q001", source=ref_b, replacement="перевода равен 300 000")
    with pytest.raises(ValueError, match="пересекаются"):
        apply_changes(report.document, [c_a, c_b])

    # Exact duplicate span must also raise ValueError.
    with pytest.raises(ValueError, match="пересекаются"):
        apply_changes(report.document, [c_a, c_a])

    # Stale revision after first edit must raise ValueError.
    bundle = apply_changes(report.document, [c_a])
    with pytest.raises(ValueError, match="версии"):
        apply_changes(bundle.document, [c_a])

    # Rejected proposal cannot be approved or applied.
    ans = Answer(question_id="Q001", version=ver, option_id="A")
    draft = draft_remediation_proposal(report, answer=ans, use_ai=False)
    rejected = reject_proposal(draft, reason="Не согласовано бизнесом.")
    assert rejected.status == "rejected"
    with pytest.raises(ValueError):
        approve_proposal(rejected)
    with pytest.raises(ValueError, match="утверждённые"):
        apply_changes(report.document, [rejected])

    # Empty changes list raises ValueError.
    with pytest.raises(ValueError):
        apply_changes(report.document, [])


def test_pdf_improvement_preserves_unread_diagnostics_and_revalidates():
    with pymupdf.open() as pdf:
        pdf.new_page().insert_text((35, 35), "FR-010: Transfer limit is 500000 KZT.")
        pdf.new_page().draw_rect((15, 15, 50, 50), fill=(0.3, 0.3, 0.3))
        data = pdf.tobytes()
    doc = parse("transfers.pdf", data)
    block = doc.version.blocks[0]
    ref = EvidenceRef(
        version=doc.version.sha256,
        block_id=block.block_id,
        start=block.text.index("500000"),
        end=block.text.index("500000") + 6,
        quote="500000",
        grounding=GroundingStatus.EXACT,
    )
    bundle = apply_changes(doc, [ProposedChange(question_id="Q001", source=ref, replacement="300000")])
    assert bundle.document.version.format == "markdown"
    assert bundle.document.version.parent_sha256 == doc.version.sha256
    assert "300000" in bundle.markdown
    assert any("2" in diag for diag in bundle.change_log.diagnostics)

    seen_docs: list[str] = []

    def _fake_runner(document, settings, on_progress=None):
        seen_docs.append(document.version.sha256)
        return "revalidated-sentinel"

    result = revalidate_improved_document(
        bundle,
        ProviderSettings(model="gemini-2.5-flash"),
        runner=_fake_runner,
    )
    assert result == "revalidated-sentinel"
    assert seen_docs == [bundle.document.version.sha256]
