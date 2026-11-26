import pytest

from src.contracts import (
    Answer,
    Coverage,
    DocumentFormat,
    DocumentVersion,
    EvidenceRef,
    ExecutionStatus,
    Finding,
    FindingType,
    GroundingStatus,
    ParsedDocument,
    PipelineStage,
    ProviderSettings,
    QualityCriterion,
    QuestionPriority,
    Readiness,
    SemanticStatus,
    Severity,
    SourceBlock,
    SourceLocation,
    StageResult,
    StakeholderRole,
    WorkSet,
    readiness_policy,
)
from src.pipeline.questions import generate_questions, record_answer
from src.provider.dto import (
    QuestionCandidate,
    QuestionGenerationOutput,
    QuestionGenerationPayload,
    QuestionOptionDraft,
    SkippedFindingDecision,
)

SHA = "d" * 64


def _make_doc() -> ParsedDocument:
    b1 = "При отмене перевода возврат комиссии выполняется в течение 3 дней."
    b2 = "При отмене перевода комиссия не возвращается."
    b3 = "Кнопка подтверждения выделяется цветом бренда."
    b4 = "Для VIP-клиентов установлен отдельный лимит операции."
    return ParsedDocument(
        version=DocumentVersion(
            sha256=SHA,
            parser_version="test-v1",
            name="synthetic-questions.md",
            format=DocumentFormat.MARKDOWN,
            line_count=4,
            blocks=[
                SourceBlock(
                    block_id="B001",
                    text=b1,
                    location=SourceLocation(section="Возвраты", line_start=1, line_end=1),
                ),
                SourceBlock(
                    block_id="B002",
                    text=b2,
                    location=SourceLocation(section="Тарифы", line_start=2, line_end=2),
                ),
                SourceBlock(
                    block_id="B003",
                    text=b3,
                    location=SourceLocation(section="UI", line_start=3, line_end=3),
                ),
                SourceBlock(
                    block_id="B004",
                    text=b4,
                    location=SourceLocation(section="Лимиты", line_start=4, line_end=4),
                ),
            ],
        )
    )


def _ref(doc: ParsedDocument, block_id: str) -> EvidenceRef:
    blk = next(b for b in doc.version.blocks if b.block_id == block_id)
    return EvidenceRef(
        version=SHA,
        block_id=block_id,
        start=0,
        end=len(blk.text),
        quote=blk.text,
        grounding=GroundingStatus.EXACT,
    )


def test_targeted_question_links_evidence_skips_minor_and_answer_keeps_finding_open():
    doc = _make_doc()
    f_crit = Finding(
        finding_id="F001",
        kind=FindingType.CONTRADICTION,
        severity=Severity.CRITICAL,
        claim="Разделы «Возвраты» и «Тарифы» противоречат друг другу в вопросе возврата комиссии.",
        recommendation="Согласовать единое правило возврата комиссии при отмене перевода.",
        requirement_ids=["R001", "R002"],
        criteria=[QualityCriterion.SET_CONSISTENT],
        evidence=[_ref(doc, "B001"), _ref(doc, "B002")],
        semantic_status=SemanticStatus.SUPPORTED,
        verification_reason="Оба правила относятся к одному сценарию отмены перевода без исключений.",
    )
    f_minor = Finding(
        finding_id="F002",
        kind=FindingType.OTHER,
        severity=Severity.MINOR,
        claim="Незначительная стилистическая неточность в описании цвета кнопки.",
        recommendation="Уточнить код цвета по брендбуку.",
        requirement_ids=["R003"],
        criteria=[QualityCriterion.CONFORMING],
        evidence=[_ref(doc, "B003")],
        semantic_status=SemanticStatus.SUPPORTED,
        verification_reason="Подтверждено дословной цитатой.",
    )
    f_refuted = Finding(
        finding_id="F003",
        kind=FindingType.CONTRADICTION,
        severity=Severity.MAJOR,
        claim="Ложное противоречие по VIP-лимиту.",
        recommendation="Нет действий.",
        requirement_ids=["R004"],
        criteria=[QualityCriterion.SET_CONSISTENT],
        evidence=[_ref(doc, "B001"), _ref(doc, "B004")],
        semantic_status=SemanticStatus.REFUTED,
        verification_reason="Опровергнуто: относится к разным сегментам клиентов.",
    )

    seen_input_fids: list[str] = []

    def fake_generate(stage, payload: QuestionGenerationPayload, output_type, *, settings=None):
        assert stage == PipelineStage.CLARIFY
        seen_input_fids.extend(f.finding_id for f in payload.findings)
        return StageResult[QuestionGenerationOutput](
            stage=PipelineStage.CLARIFY,
            output=QuestionGenerationOutput(
                questions=[
                    QuestionCandidate(
                        finding_ids=["F001"],
                        requirement_ids=["R001", "R002", "R999"],
                        evidence_block_ids=["B001", "B002"],
                        uncertainty="Не определена политика возврата комиссии при отмене перевода.",
                        stakeholder=StakeholderRole.PRODUCT,
                        priority=QuestionPriority.BLOCKER,
                        business_impact="Риск финансовых споров с клиентами и невозможность принять платёжную логику.",
                        question="Должна ли система возвращать комиссию при отмене перевода до списания?",
                        options=[
                            QuestionOptionDraft(
                                option_id="A",
                                label="Возвращать комиссию за 3 рабочих дня",
                                consequence="Сохранить раздел «Возвраты» и скорректировать раздел «Тарифы».",
                            ),
                            QuestionOptionDraft(
                                option_id="B",
                                label="Не возвращать комиссию",
                                consequence="Обновить раздел «Возвраты» и проинформировать клиента до подтверждения.",
                            ),
                        ],
                    )
                ]
            ),
            attempts=1,
            input_tokens=80,
            output_tokens=60,
        )

    result = generate_questions(
        [f_crit, f_minor, f_refuted],
        doc,
        ProviderSettings(api_key="test"),
        generate_fn=fake_generate,
    )

    # Minor and refuted findings were filtered before calling the provider.
    assert seen_input_fids == ["F001"]
    assert set(result.skipped_finding_ids) == {"F002", "F003"}
    assert len(result.questions) == 1
    q = result.questions[0]
    assert q.question_id == "Q001"
    assert q.stakeholder == StakeholderRole.PRODUCT
    assert q.priority == QuestionPriority.BLOCKER
    assert q.requirement_ids == ["R001", "R002"]  # Foreign R999 stripped!
    assert {e.block_id for e in q.evidence} == {"B001", "B002"}
    assert q.allow_custom is True
    assert len(q.options) == 2

    # Selecting an option OR custom text MUST keep the finding open (SUPPORTED, not closed/resolved).
    option_answer = Answer(question_id="Q001", version=SHA, option_id="A")
    saved_answer, preserved = record_answer(option_answer, result.questions, [f_crit], SHA)
    assert saved_answer.option_id == "A"
    assert preserved[0].semantic_status == SemanticStatus.SUPPORTED
    assert preserved[0].confirmed is True

    custom_answer = Answer(question_id="Q001", version=SHA, custom_text="Возвращать только в течение 24 часов.")
    saved_custom, preserved_custom = record_answer(custom_answer, result.questions, [f_crit], SHA)
    assert saved_custom.custom_text == "Возвращать только в течение 24 часов."
    assert preserved_custom[0].confirmed is True

    with pytest.raises(ValueError, match="Unknown option_id"):
        record_answer(Answer(question_id="Q001", version=SHA, option_id="Z"), result.questions, [f_crit], SHA)


def test_rejects_refuted_hypothesis_foreign_quote_and_generic_question():
    doc = _make_doc()
    f_major = Finding(
        finding_id="F001",
        kind=FindingType.AMBIGUITY,
        severity=Severity.MAJOR,
        claim="Не указан максимальный лимит для VIP-клиентов.",
        recommendation="Зафиксировать числовой лимит.",
        requirement_ids=["R004"],
        criteria=[QualityCriterion.UNAMBIGUOUS],
        evidence=[_ref(doc, "B004")],
        semantic_status=SemanticStatus.SUPPORTED,
        verification_reason="Подтверждено проверкой.",
    )
    f_refuted = Finding(
        finding_id="F002",
        kind=FindingType.AMBIGUITY,
        severity=Severity.MAJOR,
        claim="Опровергнутая гипотеза.",
        recommendation="Нет действий.",
        requirement_ids=["R001"],
        criteria=[QualityCriterion.UNAMBIGUOUS],
        evidence=[_ref(doc, "B001")],
        semantic_status=SemanticStatus.REFUTED,
        verification_reason="Опровергнуто.",
    )

    def fake_generate(stage, payload, output_type, *, settings=None):
        return StageResult[QuestionGenerationOutput](
            stage=PipelineStage.CLARIFY,
            output=QuestionGenerationOutput(
                questions=[
                    # 1. References refuted finding -> must be rejected!
                    QuestionCandidate(
                        finding_ids=["F002"],
                        requirement_ids=["R001"],
                        evidence_block_ids=["B001"],
                        uncertainty="Опровергнутая неопределённость.",
                        stakeholder=StakeholderRole.ANALYST,
                        priority=QuestionPriority.HIGH,
                        business_impact="Влияние.",
                        question="Какое правило применить для опровергнутой гипотезы?",
                        options=[
                            QuestionOptionDraft(label="Вариант 1", consequence="Последствие 1."),
                            QuestionOptionDraft(label="Вариант 2", consequence="Последствие 2."),
                        ],
                    ),
                    # 2. Foreign evidence block B001 not belonging to F001 (which is on B004) -> must be rejected!
                    QuestionCandidate(
                        finding_ids=["F001"],
                        requirement_ids=["R004"],
                        evidence_block_ids=["B001"],
                        uncertainty="Не задан лимит VIP.",
                        stakeholder=StakeholderRole.PRODUCT,
                        priority=QuestionPriority.HIGH,
                        business_impact="Риск превышения лимита.",
                        question="Какой суточный лимит установить для VIP-клиентов?",
                        options=[
                            QuestionOptionDraft(label="5 млн KZT", consequence="Обновить таблицу лимитов."),
                            QuestionOptionDraft(label="10 млн KZT", consequence="Согласовать с риск-комитетом."),
                        ],
                    ),
                    # 3. Generic question -> must be rejected!
                    QuestionCandidate(
                        finding_ids=["F001"],
                        requirement_ids=["R004"],
                        evidence_block_ids=["B004"],
                        uncertainty="Не задан лимит VIP.",
                        stakeholder=StakeholderRole.PRODUCT,
                        priority=QuestionPriority.HIGH,
                        business_impact="Риск превышения лимита.",
                        question="Уточните требование?",
                        options=[
                            QuestionOptionDraft(label="Вариант 1", consequence="Последствие 1."),
                            QuestionOptionDraft(label="Вариант 2", consequence="Последствие 2."),
                        ],
                    ),
                ]
            ),
            attempts=1,
        )

    result = generate_questions(
        [f_major, f_refuted],
        doc,
        ProviderSettings(api_key="test"),
        generate_fn=fake_generate,
    )

    assert result.questions == []
    assert any("опровергнутой гипотезе" in u for u in result.uncertainties)
    assert any("чужая ссылка" in u for u in result.uncertainties)
    assert any("общий или незаполненный вопрос" in u for u in result.uncertainties)


def test_blocking_uncertainty_on_major_finding_prevents_go_or_conditional_go():
    doc = _make_doc()
    f_major = Finding(
        finding_id="F001",
        kind=FindingType.AMBIGUITY,
        severity=Severity.MAJOR,
        claim="Не определена схема расчёта лимита для VIP-клиентов.",
        recommendation="Зафиксировать лимит и условия антифрода.",
        requirement_ids=["R004"],
        criteria=[QualityCriterion.UNAMBIGUOUS],
        evidence=[_ref(doc, "B004")],
        semantic_status=SemanticStatus.SUPPORTED,
        verification_reason="Подтверждено проверкой.",
    )

    def fake_generate(stage, payload, output_type, *, settings=None):
        return StageResult[QuestionGenerationOutput](
            stage=PipelineStage.CLARIFY,
            output=QuestionGenerationOutput(
                questions=[
                    QuestionCandidate(
                        finding_ids=["F001"],
                        requirement_ids=["R004"],
                        evidence_block_ids=["B004"],
                        uncertainty="Без согласованного лимита VIP невозможно утвердить антифрод-профиль.",
                        stakeholder=StakeholderRole.SECURITY,
                        priority=QuestionPriority.HIGH,
                        business_impact="Прямой риск финансовых потерь при запуске без лимитов.",
                        question="Какой максимальный лимит и порог усиленной проверки действует для VIP-клиентов?",
                        options=[
                            QuestionOptionDraft(
                                label="Лимит 5 млн KZT с биометрией",
                                consequence="Добавить обязательную биометрию в раздел безопасности.",
                            ),
                            QuestionOptionDraft(
                                label="Индивидуальный лимит через CRM",
                                consequence="Добавить интеграцию с CRM и fallback при недоступности.",
                            ),
                        ],
                        blocks_readiness=True,
                    )
                ],
                skipped_findings=[
                    SkippedFindingDecision(finding_id="F999", reason="ignored foreign id")
                ],
            ),
            attempts=1,
        )

    result = generate_questions(
        [f_major],
        doc,
        ProviderSettings(api_key="test"),
        generate_fn=fake_generate,
    )

    assert len(result.questions) == 1
    assert result.questions[0].priority == QuestionPriority.BLOCKER
    assert result.blocking_uncertainties == [
        "Без согласованного лимита VIP невозможно утвердить антифрод-профиль."
    ]
    # Feeding blocking_uncertainties into readiness_policy yields UNDETERMINED (never GO/CONDITIONAL_GO)!
    coverage = Coverage(
        execution=ExecutionStatus.COMPLETE,
        extraction_blocks=WorkSet(planned=["B004"], completed=["B004"]),
        individual_requirements=WorkSet(planned=["R004"], completed=["R004"]),
        audit_batches=WorkSet(planned=["batch-1"], completed=["batch-1"]),
        cross_section_groups=WorkSet(planned=["group-1"], completed=["group-1"]),
        incomplete_reasons=[],
        uncertainties=result.blocking_uncertainties,
    )
    assert readiness_policy(coverage, [f_major], result.blocking_uncertainties) == Readiness.UNDETERMINED
