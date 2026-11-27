from src.contracts import (
    INDIVIDUAL,
    CriterionStatus,
    DocumentFormat,
    DocumentVersion,
    EvidenceCandidate,
    EvidenceRef,
    FindingType,
    GroundingResult,
    GroundingStatus,
    ParsedDocument,
    PipelineStage,
    ProviderSettings,
    QualityCriterion,
    Requirement,
    RequirementCategory,
    SemanticStatus,
    Severity,
    SourceBlock,
    SourceLocation,
    StageResult,
)
from src.pipeline.individual import audit_individual_requirements
from src.provider import FailureAction, FailureReason, ProviderError
from src.provider.dto import (
    IndividualAuditOutput,
    IndividualAuditPayload,
    IndividualCriterionCandidate,
    IndividualEvidenceQuote,
    IndividualFindingCandidate,
)

SHA = "b" * 64
ORDERED_INDIVIDUAL = sorted(INDIVIDUAL, key=lambda c: c.value)


def _fixture_doc_and_reqs():
    b1 = "Система должна быстро обрабатывать заявку клиента."
    b2 = "Таймаут подтверждения 3DS составляет 120 секунд."
    doc = ParsedDocument(
        version=DocumentVersion(
            sha256=SHA,
            parser_version="test-v1",
            name="synthetic-indiv.md",
            format=DocumentFormat.MARKDOWN,
            line_count=2,
            blocks=[
                SourceBlock(
                    block_id="B001",
                    text=b1,
                    location=SourceLocation(section="Обработка", line_start=1, line_end=1),
                ),
                SourceBlock(
                    block_id="B002",
                    text=b2,
                    location=SourceLocation(section="Безопасность", line_start=2, line_end=2),
                ),
            ],
        )
    )
    r1 = Requirement(
        req_id="R001",
        category=RequirementCategory.QUALITY,
        summary=b1,
        source=EvidenceRef(
            version=SHA,
            block_id="B001",
            start=0,
            end=len(b1),
            quote=b1,
            grounding=GroundingStatus.EXACT,
        ),
    )
    r2 = Requirement(
        req_id="R002",
        category=RequirementCategory.FUNCTIONAL,
        summary=b2,
        source=EvidenceRef(
            version=SHA,
            block_id="B002",
            start=0,
            end=len(b2),
            quote=b2,
            grounding=GroundingStatus.EXACT,
        ),
    )
    return doc, [r1, r2]


def _exact_ground(candidate: EvidenceCandidate, document: ParsedDocument) -> GroundingResult:
    blocks = {b.block_id: b.text for b in document.version.blocks}
    text = blocks.get(candidate.block_id)
    if text is None:
        return GroundingResult(status=GroundingStatus.UNVERIFIED, reason="Unknown block")
    pos = text.find(candidate.quote)
    if pos == -1:
        return GroundingResult(status=GroundingStatus.FUZZY, reason="Quote not exact")
    return GroundingResult(
        status=GroundingStatus.EXACT,
        reference=EvidenceRef(
            version=document.version.sha256,
            block_id=candidate.block_id,
            start=pos,
            end=pos + len(candidate.quote),
            quote=candidate.quote,
            grounding=GroundingStatus.EXACT,
        ),
        reason="Exact match",
    )


def _all_nine_evals(
    req_id: str,
    *,
    overrides: dict[QualityCriterion, tuple[CriterionStatus, str, list[str]]] | None = None,
) -> list[IndividualCriterionCandidate]:
    overrides = overrides or {}
    items = []
    for crit in ORDERED_INDIVIDUAL:
        if crit in overrides:
            status, rationale, keys = overrides[crit]
        elif crit in (QualityCriterion.NECESSARY, QualityCriterion.FEASIBLE, QualityCriterion.CORRECT):
            status, rationale, keys = (
                CriterionStatus.UNKNOWN,
                "Внешние данные и бизнес-обоснование не приложены к ТЗ.",
                [],
            )
        elif crit == QualityCriterion.CONFORMING:
            status, rationale, keys = (
                CriterionStatus.NOT_APPLICABLE,
                "Корпоративный шаблон оформления не задан для данного документа.",
                [],
            )
        else:
            status, rationale, keys = (
                CriterionStatus.PASS,
                "Формулировка требования удовлетворяет критерию.",
                [],
            )
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


def _stage_ok(output: IndividualAuditOutput) -> StageResult[IndividualAuditOutput]:
    return StageResult[IndividualAuditOutput](
        stage=PipelineStage.INDIVIDUAL,
        output=output,
        attempts=1,
        input_tokens=100,
        output_tokens=80,
    )


def test_complete_clean_batch_with_shared_candidate_finding_and_unknown_rationales():
    doc, reqs = _fixture_doc_and_reqs()

    def fake_generate(stage, payload: IndividualAuditPayload, output_type, *, settings=None):
        assert stage == PipelineStage.INDIVIDUAL
        assert set(payload.criteria_catalog) == {c.value for c in INDIVIDUAL}
        r1_evals = _all_nine_evals(
            "R001",
            overrides={
                QualityCriterion.UNAMBIGUOUS: (
                    CriterionStatus.FAIL,
                    "Термин «быстро» допускает разные трактовки времени отклика.",
                    ["K1"],
                ),
                QualityCriterion.VERIFIABLE: (
                    CriterionStatus.FAIL,
                    "Невозможно задать автоматическую проверку без числового SLA.",
                    ["K1"],
                ),
            },
        )
        r2_evals = _all_nine_evals("R002")
        return _stage_ok(
            IndividualAuditOutput(
                evaluations=[*r1_evals, *r2_evals],
                findings=[
                    IndividualFindingCandidate(
                        local_key="K1",
                        requirement_id="R001",
                        kind=FindingType.AMBIGUITY,
                        severity=Severity.MAJOR,
                        claim="Не задано измеримое ограничение времени обработки («быстро»).",
                        recommendation="Указать числовой целевой порог времени отклика, например p95 <= 2 с.",
                        criteria=[QualityCriterion.UNAMBIGUOUS, QualityCriterion.VERIFIABLE],
                        evidence=[IndividualEvidenceQuote(block_id="B001", quote="быстро обрабатывать заявку")],
                    )
                ],
            )
        )

    result = audit_individual_requirements(
        reqs,
        doc,
        ProviderSettings(api_key="test-key"),
        generate_fn=fake_generate,
        ground_fn=_exact_ground,
    )

    assert result.individual_requirements.complete
    assert result.audit_batches.complete
    assert result.incomplete_reasons == []
    assert len(result.evaluations) == 18
    assert len(result.findings) == 1
    finding = result.findings[0]
    assert finding.finding_id == "F001"
    assert finding.semantic_status == SemanticStatus.UNCHECKED
    assert set(finding.criteria) == {QualityCriterion.UNAMBIGUOUS, QualityCriterion.VERIFIABLE}
    assert finding.evidence[0].grounded
    failed_evals = [e for e in result.evaluations if e.status == CriterionStatus.FAIL]
    assert len(failed_evals) == 2
    assert all(e.finding_ids == ["F001"] for e in failed_evals)


def test_missing_criterion_duplicate_criterion_and_foreign_id_produce_partial_batch():
    doc, reqs = _fixture_doc_and_reqs()

    def fake_generate(stage, payload: IndividualAuditPayload, output_type, *, settings=None):
        # R001 misses VERIFIABLE and duplicates SINGULAR; foreign R999 is also injected.
        r1_evals = [e for e in _all_nine_evals("R001") if e.criterion != QualityCriterion.VERIFIABLE]
        r1_evals.append(
            IndividualCriterionCandidate(
                requirement_id="R001",
                criterion=QualityCriterion.SINGULAR,
                status=CriterionStatus.PASS,
                rationale="Повторная оценка.",
            )
        )
        foreign_eval = IndividualCriterionCandidate(
            requirement_id="R999",
            criterion=QualityCriterion.SINGULAR,
            status=CriterionStatus.PASS,
            rationale="Чужое требование.",
        )
        r2_evals = _all_nine_evals("R002")
        return _stage_ok(IndividualAuditOutput(evaluations=[*r1_evals, foreign_eval, *r2_evals]))

    result = audit_individual_requirements(
        reqs,
        doc,
        ProviderSettings(api_key="test-key"),
        generate_fn=fake_generate,
        ground_fn=_exact_ground,
    )

    assert result.individual_requirements.completed == ["R002"]
    assert not result.individual_requirements.complete
    assert result.audit_batches.completed == []
    assert len(result.evaluations) == 9
    assert all(e.requirement_id == "R002" for e in result.evaluations)
    assert any("повторная оценка" in r.lower() for r in result.incomplete_reasons)
    assert any("пропущены обязательные критерии" in r.lower() for r in result.incomplete_reasons)
    assert any("R999" in u for u in result.uncertainties)


def test_invalid_or_ungrounded_evidence_ref_keeps_requirement_incomplete():
    doc, reqs = _fixture_doc_and_reqs()

    def fake_generate(stage, payload: IndividualAuditPayload, output_type, *, settings=None):
        r1_evals = _all_nine_evals(
            "R001",
            overrides={
                QualityCriterion.UNAMBIGUOUS: (
                    CriterionStatus.FAIL,
                    "Расплывчатое слово.",
                    ["K1"],
                )
            },
        )
        r2_evals = _all_nine_evals("R002")
        return _stage_ok(
            IndividualAuditOutput(
                evaluations=[*r1_evals, *r2_evals],
                findings=[
                    IndividualFindingCandidate(
                        local_key="K1",
                        requirement_id="R001",
                        kind=FindingType.AMBIGUITY,
                        severity=Severity.MAJOR,
                        claim="Нечеткий срок обработки.",
                        recommendation="Указать SLA.",
                        criteria=[QualityCriterion.UNAMBIGUOUS],
                        # Fabricated/paraphrased quote -> ground_fn returns FUZZY!
                        evidence=[IndividualEvidenceQuote(block_id="B001", quote="мгновенно обрабатывать")],
                    )
                ],
            )
        )

    result = audit_individual_requirements(
        reqs,
        doc,
        ProviderSettings(api_key="test-key"),
        generate_fn=fake_generate,
        ground_fn=_exact_ground,
    )

    assert result.individual_requirements.completed == ["R002"]
    assert result.findings == []
    assert result.audit_batches.completed == []
    assert any("не подтверждена" in r for r in result.incomplete_reasons)


def test_finding_criteria_synchronized_with_cited_fail_evaluations_and_truncation_splits():
    doc, reqs = _fixture_doc_and_reqs()
    calls = 0

    def fake_generate(stage, payload: IndividualAuditPayload, output_type, *, settings=None):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ProviderError(
                "Truncated structured response (MAX_TOKENS)",
                reason=FailureReason.TRUNCATED,
                action=FailureAction.SPLIT,
                attempts=1,
            )
        if calls == 2 and payload.repair_hint is None:
            raise ProviderError(
                "Malformed structured response",
                reason=FailureReason.MALFORMED_RESPONSE,
                action=FailureAction.REPAIR,
                attempts=1,
            )
        rid = payload.target_req_ids[0]
        if rid == "R001":
            # Only UNAMBIGUOUS fails referencing K1, even though K1 declares extra VERIFIABLE -> must sync to {UNAMBIGUOUS}!
            return _stage_ok(
                IndividualAuditOutput(
                    evaluations=_all_nine_evals(
                        "R001",
                        overrides={
                            QualityCriterion.UNAMBIGUOUS: (
                                CriterionStatus.FAIL,
                                "Слово «быстро» неизмеримо.",
                                ["K1"],
                            )
                        },
                    ),
                    findings=[
                        IndividualFindingCandidate(
                            local_key="K1",
                            requirement_id="R001",
                            kind=FindingType.AMBIGUITY,
                            severity=Severity.MINOR,
                            claim="Размытый термин «быстро».",
                            recommendation="Задать порог в миллисекундах.",
                            criteria=[QualityCriterion.UNAMBIGUOUS, QualityCriterion.VERIFIABLE],
                            evidence=[IndividualEvidenceQuote(block_id="B001", quote="быстро обрабатывать")],
                        ),
                        # Unreferenced orphan finding must never appear in canonical findings.
                        IndividualFindingCandidate(
                            local_key="ORPHAN",
                            requirement_id="R001",
                            kind=FindingType.OTHER,
                            severity=Severity.MINOR,
                            claim="Лишняя несвязанная находка.",
                            recommendation="Нет.",
                            criteria=[QualityCriterion.SINGULAR],
                            evidence=[IndividualEvidenceQuote(block_id="B001", quote="заявку клиента")],
                        ),
                    ],
                )
            )
        return _stage_ok(IndividualAuditOutput(evaluations=_all_nine_evals("R002")))

    result = audit_individual_requirements(
        reqs,
        doc,
        ProviderSettings(api_key="test-key"),
        generate_fn=fake_generate,
        ground_fn=_exact_ground,
    )

    assert calls == 4  # 1 MAX_TOKENS split + 1 malformed + 1 repair (R001) + 1 ok (R002)
    assert result.individual_requirements.complete
    assert result.audit_batches.complete
    assert len(result.findings) == 1
    assert result.findings[0].severity == Severity.MINOR
    assert result.findings[0].semantic_status == SemanticStatus.SUPPORTED
    assert result.findings[0].criteria == [QualityCriterion.UNAMBIGUOUS]
    cited = {(fid, e.criterion) for e in result.evaluations for fid in e.finding_ids}
    declared = {(f.finding_id, c) for f in result.findings for c in f.criteria}
    assert cited == declared


def test_missing_information_rejects_uninspected_block_reference():
    doc, reqs = _fixture_doc_and_reqs()

    def fake_generate(stage, payload: IndividualAuditPayload, output_type, *, settings=None):
        return _stage_ok(
            IndividualAuditOutput(
                evaluations=_all_nine_evals(
                    "R001",
                    overrides={
                        QualityCriterion.COMPLETE: (
                            CriterionStatus.FAIL,
                            "Не задан срок.",
                            ["K1"],
                        )
                    },
                ),
                findings=[
                    IndividualFindingCandidate(
                        local_key="K1",
                        requirement_id="R001",
                        kind=FindingType.MISSING_INFORMATION,
                        severity=Severity.MINOR,
                        claim="Не указан таймаут.",
                        recommendation="Задать таймаут.",
                        criteria=[QualityCriterion.COMPLETE],
                        # B002 is outside inspected_blocks={"B001"} -> must be rejected!
                        evidence=[IndividualEvidenceQuote(block_id="B002", quote="120 секунд")],
                        expected_information="Таймаут обработки заявки",
                    )
                ],
            )
        )

    result = audit_individual_requirements(
        [reqs[0]],
        doc,
        ProviderSettings(api_key="test-key"),
        inspected_blocks={"B001"},
        generate_fn=fake_generate,
        ground_fn=_exact_ground,
    )

    assert result.individual_requirements.completed == []
    assert result.findings == []
    assert any("непроверенный блок B002" in r for r in result.incomplete_reasons)
