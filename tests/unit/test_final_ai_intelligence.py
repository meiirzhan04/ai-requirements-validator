"""Unit tests for generalized semantic accuracy & verification enhancements (Issue #34 Priority 2)."""
from src.contracts import (
    WHOLE_SET,
    CriterionStatus,
    DocumentFormat,
    DocumentVersion,
    EvidenceRef,
    Finding,
    FindingType,
    GroundingStatus,
    ParsedDocument,
    PipelineStage,
    ProviderSettings,
    QualityCriterion,
    QuestionPriority,
    Requirement,
    RequirementCategory,
    SemanticStatus,
    Severity,
    SourceBlock,
    SourceLocation,
    StageResult,
    StakeholderRole,
)
from src.ingest import parse
from src.pipeline.holistic import (
    build_cross_section_groups,
    build_semantic_focus_signals,
    deduplicate_findings,
)
from src.pipeline.questions import generate_questions
from src.pipeline.verification import verify_findings
from src.prompts.holistic import HOLISTIC_LOGIC_SYSTEM_PROMPT, HOLISTIC_SYSTEM_PROMPT
from src.prompts.improvement import IMPROVEMENT_SYSTEM_PROMPT
from src.prompts.individual import INDIVIDUAL_SYSTEM_PROMPT
from src.prompts.questions import QUESTIONS_SYSTEM_PROMPT
from src.prompts.verification import VERIFICATION_SYSTEM_PROMPT
from src.provider.dto import (
    QuestionCandidate,
    QuestionGenerationOutput,
    QuestionOptionDraft,
    SemanticReview,
    VerificationOutput,
    VerificationPayload,
)

SHA = "a" * 64


def test_semantic_focus_signals_cover_timing_states_repeated_events_api_and_exceptions():
    spec = """# Сервис лимитов и статусов заявок

## 1. Календарные границы и таймзоны

FR-101: Учёт операций ведётся по дате события в часовом поясе UTC+5 в рамках календарного месяца.

## 2. Жизненный цикл и переходы состояний

FR-102: Заявка переходит по маршруту DRAFT -> PENDING -> CLOSED; статус CLOSED является терминальным.

## 3. Повторные события и сброс

FR-103: Уведомление отправляется не более одного раза за расчётный период, кроме случая ручного сброса.
FR-104: При изменении порога уведомление отправляется повторно независимо от истории.

## 4. API-контракт

API-101: GET /api/v2/limits изменяет лимит и возвращает HTTP 200 с телом ERROR при ошибке.

## 5. Приёмка и исключения

AC-101: Операция 31 января 23:55 UTC+5, обработанная 1 февраля, попадает в февраль; комиссия 100 KZT, за исключением VIP.
AC-102: Перевод из статуса CLOSED обратно в PENDING разрешён оператором.
"""
    doc = parse("synthetic_semantic.md", spec.encode("utf-8"))
    allowed = {b.block_id for b in doc.version.blocks}
    signals = build_semantic_focus_signals(doc, allowed)

    assert any("semantic_focus:timing_calendar" in s for s in signals)
    assert any("semantic_focus:state_transitions" in s for s in signals)
    assert any("semantic_focus:repeated_events" in s for s in signals)
    assert any("semantic_focus:api_consistency" in s for s in signals)
    assert any("semantic_focus:units_exceptions" in s for s in signals)


def test_long_document_cross_section_grouping_connects_distant_domain_anchors():
    # Sections 1 and 15 share concise domain anchors: UTC+5, CLOSED, KZT, HTTP 409.
    blocks: list[tuple[str, str, str]] = [
        ("B001", "Раздел 1 — Таймзона и статус", "Граница суток задаётся в UTC+5; статус CLOSED финальный; лимит 500 KZT."),
        *[
            (f"B{i:03d}", f"Раздел {i}", f"Изолированная заметка модуля номер {i} для аудита.")
            for i in range(2, 15)
        ],
        ("B015", "Раздел 15 — Приёмка", "Проверка в UTC+5 разрешает выход из CLOSED при сумме 500 KZT и коде HTTP 409."),
    ]
    src_blocks = [
        SourceBlock(
            block_id=bid,
            text=text,
            location=SourceLocation(section=sec, line_start=i, line_end=i),
        )
        for i, (bid, sec, text) in enumerate(blocks, start=1)
    ]
    doc = ParsedDocument(
        version=DocumentVersion(
            sha256=SHA,
            parser_version="test-v1",
            name="long_synthetic.md",
            format=DocumentFormat.MARKDOWN,
            line_count=len(src_blocks),
            blocks=src_blocks,
        )
    )
    reqs = [
        Requirement(
            req_id=f"R{i:03d}",
            category=RequirementCategory.FUNCTIONAL,
            summary=blk.text,
            source=EvidenceRef(
                version=SHA,
                block_id=blk.block_id,
                start=0,
                end=len(blk.text),
                quote=blk.text,
                grounding=GroundingStatus.EXACT,
            ),
        )
        for i, blk in enumerate(src_blocks, start=1)
    ]

    groups = build_cross_section_groups(reqs, doc, max_requirements_per_group=4)
    assert any({"R001", "R015"} <= set(g.requirement_ids) for g in groups)


def test_deduplicate_findings_merges_overlapping_contradiction_spans_without_collapsing_distinct_rows():
    # Overlapping spans in B001 + B002 represent the SAME contradiction detected by whole-spec and rules-acceptance.
    r1_wide = EvidenceRef(version=SHA, block_id="B001", start=0, end=40, quote="A" * 40, grounding=GroundingStatus.EXACT)
    r1_narrow = EvidenceRef(version=SHA, block_id="B001", start=10, end=30, quote="A" * 20, grounding=GroundingStatus.EXACT)
    r2_wide = EvidenceRef(version=SHA, block_id="B002", start=0, end=40, quote="B" * 40, grounding=GroundingStatus.EXACT)
    r2_narrow = EvidenceRef(version=SHA, block_id="B002", start=5, end=25, quote="B" * 20, grounding=GroundingStatus.EXACT)

    # Distinct non-overlapping row spans in the SAME blocks (e.g., another row of a table) must remain separate!
    r1_row2 = EvidenceRef(version=SHA, block_id="B001", start=60, end=90, quote="C" * 30, grounding=GroundingStatus.EXACT)
    r2_row2 = EvidenceRef(version=SHA, block_id="B002", start=60, end=90, quote="D" * 30, grounding=GroundingStatus.EXACT)

    f_whole = Finding(
        finding_id="HOL-1",
        kind=FindingType.CONTRADICTION,
        severity=Severity.MAJOR,
        claim="Конфликт даты события и даты обработки.",
        recommendation="Согласовать правило периода.",
        requirement_ids=["R001"],
        criteria=[QualityCriterion.SET_CONSISTENT],
        evidence=[r1_wide, r2_wide],
        semantic_status=SemanticStatus.UNCHECKED,
        verification_reason="Ожидает проверки.",
    )
    f_logic = Finding(
        finding_id="HOL-2",
        kind=FindingType.CONTRADICTION,
        severity=Severity.CRITICAL,
        claim="Противоречие между правилом месяца события и приёмкой по дате проводки.",
        recommendation="Зафиксировать единую дату отнесения операции.",
        requirement_ids=["R001", "R002"],
        criteria=[QualityCriterion.SRS_CONSISTENT],
        evidence=[r1_narrow, r2_narrow],
        semantic_status=SemanticStatus.UNCHECKED,
        verification_reason="Ожидает проверки.",
    )
    f_distinct_row = Finding(
        finding_id="HOL-3",
        kind=FindingType.CONTRADICTION,
        severity=Severity.CRITICAL,
        claim="Конфликт по другой строке таблицы (статус возврата).",
        recommendation="Согласовать статус.",
        requirement_ids=["R003", "R004"],
        criteria=[QualityCriterion.SET_CONSISTENT],
        evidence=[r1_row2, r2_row2],
        semantic_status=SemanticStatus.UNCHECKED,
        verification_reason="Ожидает проверки.",
    )

    dedup, id_map = deduplicate_findings(
        [f_whole, f_logic, f_distinct_row],
        block_order={"B001": 0, "B002": 1},
    )
    assert len(dedup) == 2
    assert id_map["HOL-1"] == id_map["HOL-2"]
    assert id_map["HOL-3"] != id_map["HOL-1"]
    merged = next(f for f in dedup if f.finding_id == id_map["HOL-1"])
    assert merged.severity == Severity.CRITICAL
    assert set(merged.criteria) == {QualityCriterion.SET_CONSISTENT, QualityCriterion.SRS_CONSISTENT}


def test_large_document_verification_retrieves_distant_exception_and_glossary_passages():
    # > 24,000 chars and > 20 blocks so full-doc context is off; B025 holds an explicit exception!
    filler = "Подробное техническое описание вспомогательного подпроцесса без конфликтов. " * 22
    src_blocks = [
        SourceBlock(
            block_id="B001",
            text="Для стандартных переводов комиссия составляет 1% от суммы операции.",
            location=SourceLocation(section="Тарифы", line_start=1, line_end=1),
        ),
        SourceBlock(
            block_id="B002",
            text="Для переводов внутри одного клиента комиссия составляет 0%.",
            location=SourceLocation(section="Внутренние переводы", line_start=2, line_end=2),
        ),
        *[
            SourceBlock(
                block_id=f"B{i:03d}",
                text=f"Раздел {i}: {filler}",
                location=SourceLocation(section=f"Раздел {i}", line_start=i, line_end=i),
            )
            for i in range(3, 25)
        ],
        SourceBlock(
            block_id="B025",
            text="Исключение: переводы между собственными счетами клиента освобождаются от базовой комиссии 1%.",
            location=SourceLocation(section="Исключения и глоссарий", line_start=25, line_end=25),
        ),
    ]
    doc = ParsedDocument(
        version=DocumentVersion(
            sha256=SHA,
            parser_version="test-v1",
            name="large_synthetic.md",
            format=DocumentFormat.MARKDOWN,
            line_count=len(src_blocks),
            blocks=src_blocks,
        )
    )
    f = Finding(
        finding_id="F001",
        kind=FindingType.CONTRADICTION,
        severity=Severity.MAJOR,
        claim="Разные ставки комиссии 1% и 0% для переводов.",
        recommendation="Уточнить исключение.",
        requirement_ids=["R001", "R002"],
        criteria=[QualityCriterion.SET_CONSISTENT],
        evidence=[
            EvidenceRef(version=SHA, block_id="B001", start=0, end=len(src_blocks[0].text), quote=src_blocks[0].text, grounding=GroundingStatus.EXACT),
            EvidenceRef(version=SHA, block_id="B002", start=0, end=len(src_blocks[1].text), quote=src_blocks[1].text, grounding=GroundingStatus.EXACT),
        ],
        semantic_status=SemanticStatus.UNCHECKED,
        verification_reason="Ожидает проверки.",
    )

    seen_context_blocks: list[str] = []

    def _mock_verify(stage, payload: VerificationPayload, output_type, **kwargs):
        seen_context_blocks.extend(p.block_id for p in payload.context_passages)
        return StageResult[VerificationOutput](
            stage=stage,
            output=VerificationOutput(
                reviews=[
                    SemanticReview(
                        candidate_id="F001",
                        decision="refuted",
                        reason="В блоке B025 явно зафиксировано исключение для внутренних переводов между своими счетами.",
                    )
                ]
            ),
            attempts=1,
        )

    res = verify_findings([f], doc, ProviderSettings(api_key="test"), generate_fn=_mock_verify)
    assert "B025" in seen_context_blocks
    assert res.findings[0].semantic_status == SemanticStatus.REFUTED


def test_questions_deduplicate_same_finding_overlap_and_reject_placeholder_options():
    doc = parse("q_dedup.md", "FR-1: Лимит 500 000 KZT.\n\nAC-1: Лимит 300 000 KZT.\n".encode("utf-8"))
    b1, b2 = doc.version.blocks
    f = Finding(
        finding_id="F001",
        kind=FindingType.CONTRADICTION,
        severity=Severity.CRITICAL,
        claim="Конфликт суточного лимита 500 000 KZT и 300 000 KZT.",
        recommendation="Согласовать единый лимит.",
        requirement_ids=["R001", "R002"],
        criteria=[QualityCriterion.SET_CONSISTENT],
        evidence=[
            EvidenceRef(version=doc.version.sha256, block_id=b1.block_id, start=0, end=len(b1.text), quote=b1.text, grounding=GroundingStatus.EXACT),
            EvidenceRef(version=doc.version.sha256, block_id=b2.block_id, start=0, end=len(b2.text), quote=b2.text, grounding=GroundingStatus.EXACT),
        ],
        semantic_status=SemanticStatus.SUPPORTED,
        verification_reason="Подтверждено.",
    )

    def _mock_q(stage, payload, output_type, **kwargs):
        return StageResult[QuestionGenerationOutput](
            stage=stage,
            output=QuestionGenerationOutput(
                questions=[
                    # Rejected because of placeholder option 'TBD'.
                    QuestionCandidate(
                        finding_ids=["F001"],
                        requirement_ids=["R001", "R002"],
                        evidence_block_ids=[b1.block_id],
                        uncertainty="Лимит не согласован.",
                        stakeholder=StakeholderRole.PRODUCT,
                        priority=QuestionPriority.HIGH,
                        business_impact="Разное поведение в API и приёмке.",
                        question="Какой лимит выбрать для переводов в сутки?",
                        options=[
                            QuestionOptionDraft(label="TBD", consequence="Решить позже."),
                            QuestionOptionDraft(label="300 000 KZT", consequence="Обновить FR-1."),
                        ],
                    ),
                    # Two near-duplicate questions for the same finding + owner -> must consolidate into ONE!
                    QuestionCandidate(
                        finding_ids=["F001"],
                        requirement_ids=["R001"],
                        evidence_block_ids=[b1.block_id],
                        uncertainty="Не выбран единый суточный лимит.",
                        stakeholder=StakeholderRole.PRODUCT,
                        priority=QuestionPriority.HIGH,
                        business_impact="Риск блокировки легитимных переводов.",
                        question="Какой целевой суточный лимит нужно закрепить в спецификации?",
                        options=[
                            QuestionOptionDraft(label="500 000 KZT в сутки", consequence="Обновить AC-1."),
                            QuestionOptionDraft(label="300 000 KZT в сутки", consequence="Обновить FR-1."),
                        ],
                    ),
                    QuestionCandidate(
                        finding_ids=["F001"],
                        requirement_ids=["R002"],
                        evidence_block_ids=[b2.block_id],
                        uncertainty="Не выбран единый суточный лимит.",
                        stakeholder=StakeholderRole.PRODUCT,
                        priority=QuestionPriority.BLOCKER,
                        business_impact="Блокирует приёмочные тесты и разработку лимитов.",
                        question="Какой итоговый суточный лимит утвердить между FR-1 и AC-1?",
                        options=[
                            QuestionOptionDraft(label="500 000 KZT в календарные сутки", consequence="Обновить AC-1 и автотесты."),
                            QuestionOptionDraft(label="300 000 KZT в календарные сутки", consequence="Обновить FR-1 и витрину тарифов."),
                        ],
                    ),
                ]
            ),
            attempts=1,
        )

    res = generate_questions([f], doc, ProviderSettings(api_key="test"), generate_fn=_mock_q)
    assert len(res.questions) == 1
    q = res.questions[0]
    assert q.priority == QuestionPriority.BLOCKER
    assert set(q.requirement_ids) == {"R001", "R002"}
    assert {ref.block_id for ref in q.evidence} == {b1.block_id, b2.block_id}


def test_prompts_contain_no_official_sample_hardcoding():
    all_prompts = "\n".join(
        [
            HOLISTIC_SYSTEM_PROMPT,
            HOLISTIC_LOGIC_SYSTEM_PROMPT,
            INDIVIDUAL_SYSTEM_PROMPT,
            VERIFICATION_SYSTEM_PROMPT,
            QUESTIONS_SYSTEM_PROMPT,
            IMPROVEMENT_SYSTEM_PROMPT,
        ]
    ).lower()
    # Guard against accidental hardcoding of sample-specific feature names or domain literals.
    for forbidden in ("расходы по карте", "budget-alert", "card-spend", "bcc-hub", "31 января 2025"):
        assert forbidden not in all_prompts
