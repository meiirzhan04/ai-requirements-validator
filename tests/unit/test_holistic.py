from src.contracts import (
    WHOLE_SET,
    CriterionStatus,
    DocumentFormat,
    DocumentVersion,
    EvidenceCandidate,
    EvidenceRef,
    Finding,
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
from src.pipeline.holistic import (
    audit_holistic_set,
    build_cross_section_groups,
    deduplicate_findings,
)
from src.pipeline.verification import verify_findings
from src.provider import FailureAction, FailureReason, ProviderError
from src.provider.dto import (
    HolisticAuditOutput,
    HolisticCriterionCandidate,
    HolisticEvidenceQuote,
    HolisticFindingCandidate,
    HolisticGroupPayload,
    SemanticReview,
    VerificationOutput,
    VerificationPayload,
)

SHA = "c" * 64
ORDERED_WHOLE = sorted(WHOLE_SET, key=lambda c: c.value)


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


def _build_spec_doc(blocks: list[tuple[str, str, str]]) -> tuple[ParsedDocument, list[Requirement]]:
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
            name="synthetic-holistic.md",
            format=DocumentFormat.MARKDOWN,
            line_count=max(1, len(src_blocks)),
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
    return doc, reqs


def _all_13_whole_evals(
    overrides: dict[QualityCriterion, tuple[CriterionStatus, str, list[str]]] | None = None,
) -> list[HolisticCriterionCandidate]:
    overrides = overrides or {}
    out = []
    for crit in ORDERED_WHOLE:
        if crit in overrides:
            status, rationale, keys = overrides[crit]
        else:
            status, rationale, keys = (
                CriterionStatus.PASS,
                "Набор требований удовлетворяет критерию в проверенной области.",
                [],
            )
        out.append(
            HolisticCriterionCandidate(
                criterion=crit,
                status=status,
                rationale=rationale,
                local_finding_keys=keys,
            )
        )
    return out


def test_distant_contradiction_paired_across_sections_and_verified():
    # Distant blocks (section 1 and section 14) share endpoint `/api/v1/transfers` and cancellation rule.
    blocks = [
        ("B001", "Раздел 1 — API", "POST /api/v1/transfers возвращает HTTP 201 при успешном создании перевода."),
        *[(f"B{i:03d}", f"Раздел {i}", f"Служебное правило номер {i} для учёта операций.") for i in range(2, 14)],
        ("B014", "Раздел 14 — Приёмка", "При успешном создании перевода POST /api/v1/transfers возвращает HTTP 200."),
    ]
    doc, reqs = _build_spec_doc(blocks)

    groups = build_cross_section_groups(reqs, doc, max_requirements_per_group=5)
    assert any("R001" in g.requirement_ids and "R014" in g.requirement_ids for g in groups)

    def fake_holistic(stage, payload: HolisticGroupPayload, output_type, *, settings=None):
        assert stage == PipelineStage.HOLISTIC
        ids = {r.req_id for r in payload.requirements}
        if {"R001", "R014"} <= ids:
            evals = _all_13_whole_evals(
                {
                    QualityCriterion.SET_CONSISTENT: (
                        CriterionStatus.FAIL,
                        "Несовместимые коды ответа для одного API-метода.",
                        ["C1"],
                    ),
                    QualityCriterion.SRS_CONSISTENT: (
                        CriterionStatus.FAIL,
                        "Несовместимые коды ответа между разделом API и приёмкой.",
                        ["C1"],
                    ),
                }
            )
            findings = [
                HolisticFindingCandidate(
                    local_key="C1",
                    kind=FindingType.CONTRADICTION,
                    severity=Severity.CRITICAL,
                    claim="Для POST /api/v1/transfers указаны несовместимые коды HTTP 201 и HTTP 200.",
                    scenario="Успешное создание перевода через POST /api/v1/transfers",
                    recommendation="Согласовать единый код ответа HTTP 201 в разделе API и приёмке.",
                    requirement_ids=["R001", "R014"],
                    criteria=[QualityCriterion.SET_CONSISTENT, QualityCriterion.SRS_CONSISTENT],
                    evidence=[
                        HolisticEvidenceQuote(block_id="B001", quote="возвращает HTTP 201"),
                        HolisticEvidenceQuote(block_id="B014", quote="возвращает HTTP 200"),
                    ],
                )
            ]
            return StageResult[HolisticAuditOutput](
                stage=PipelineStage.HOLISTIC,
                output=HolisticAuditOutput(evaluations=evals, findings=findings),
                attempts=1,
            )
        return StageResult[HolisticAuditOutput](
            stage=PipelineStage.HOLISTIC,
            output=HolisticAuditOutput(evaluations=_all_13_whole_evals()),
            attempts=1,
        )

    holistic = audit_holistic_set(
        reqs,
        doc,
        ProviderSettings(api_key="test"),
        generate_fn=fake_holistic,
        ground_fn=_exact_ground,
        max_requirements_per_group=5,
    )

    assert len(holistic.evaluations) == 13
    assert len(holistic.findings) == 1
    cand = holistic.findings[0]
    assert cand.semantic_status == SemanticStatus.UNCHECKED
    assert len(cand.evidence) == 2
    assert {e.block_id for e in cand.evidence} == {"B001", "B014"}
    assert set(cand.criteria) == {QualityCriterion.SET_CONSISTENT, QualityCriterion.SRS_CONSISTENT}

    def fake_verify(stage, payload: VerificationPayload, output_type, *, settings=None):
        assert stage == PipelineStage.VERIFY
        return StageResult[VerificationOutput](
            stage=PipelineStage.VERIFY,
            output=VerificationOutput(
                reviews=[
                    SemanticReview(
                        candidate_id=cand.finding_id,
                        decision="supported",
                        reason="Оба раздела задают разный код ответа для одного сценария создания перевода без исключений.",
                    )
                ]
            ),
            attempts=1,
        )

    verified = verify_findings(
        holistic.findings,
        doc,
        ProviderSettings(api_key="test"),
        evaluations=holistic.evaluations,
        generate_fn=fake_verify,
    )

    assert verified.findings[0].semantic_status == SemanticStatus.SUPPORTED
    assert all(ref.grounded for ref in verified.findings[0].evidence)
    failed = [e for e in verified.evaluations if e.status == CriterionStatus.FAIL]
    assert {e.criterion for e in failed} == {
        QualityCriterion.SET_CONSISTENT,
        QualityCriterion.SRS_CONSISTENT,
    }


def test_false_conflict_under_different_conditions_is_refuted_by_verification():
    doc, reqs = _build_spec_doc(
        [
            ("B001", "Розничные клиенты", "Для физического лица комиссия за перевод составляет 0%."),
            ("B002", "Корпоративные клиенты", "Для юридического лица комиссия за перевод составляет 0.5%."),
        ]
    )

    def fake_holistic(stage, payload, output_type, *, settings=None):
        return StageResult[HolisticAuditOutput](
            stage=PipelineStage.HOLISTIC,
            output=HolisticAuditOutput(
                evaluations=_all_13_whole_evals(
                    {
                        QualityCriterion.SET_CONSISTENT: (
                            CriterionStatus.FAIL,
                            "Потенциальное расхождение ставок комиссии.",
                            ["C1"],
                        )
                    }
                ),
                findings=[
                    HolisticFindingCandidate(
                        local_key="C1",
                        kind=FindingType.CONTRADICTION,
                        severity=Severity.MAJOR,
                        claim="Разные ставки комиссии 0% и 0.5%.",
                        scenario="Перевод средств",
                        recommendation="Уточнить тариф.",
                        requirement_ids=["R001", "R002"],
                        criteria=[QualityCriterion.SET_CONSISTENT],
                        evidence=[
                            HolisticEvidenceQuote(block_id="B001", quote="комиссия за перевод составляет 0%"),
                            HolisticEvidenceQuote(block_id="B002", quote="комиссия за перевод составляет 0.5%"),
                        ],
                    )
                ],
            ),
            attempts=1,
        )

    holistic = audit_holistic_set(
        reqs, doc, ProviderSettings(api_key="test"), generate_fn=fake_holistic, ground_fn=_exact_ground
    )
    cand_id = holistic.findings[0].finding_id

    def fake_verify(stage, payload: VerificationPayload, output_type, *, settings=None):
        return StageResult[VerificationOutput](
            stage=PipelineStage.VERIFY,
            output=VerificationOutput(
                reviews=[
                    SemanticReview(
                        candidate_id=cand_id,
                        decision="refuted",
                        reason="Правила применяются к разным категориям клиентов (физлица vs юрлица) — конфликта нет.",
                    )
                ]
            ),
            attempts=1,
        )

    verified = verify_findings(
        holistic.findings,
        doc,
        ProviderSettings(api_key="test"),
        evaluations=holistic.evaluations,
        generate_fn=fake_verify,
    )

    assert verified.findings[0].semantic_status == SemanticStatus.REFUTED
    # Grounding remains intact and distinct from semantic status!
    assert all(ref.grounding == GroundingStatus.EXACT for ref in verified.findings[0].evidence)
    set_cons = next(e for e in verified.evaluations if e.criterion == QualityCriterion.SET_CONSISTENT)
    assert set_cons.status == CriterionStatus.PASS
    assert set_cons.finding_ids == []


def test_contradiction_missing_second_quote_is_rejected_and_incomplete_gap_scope_stays_uncertain():
    doc, reqs = _build_spec_doc(
        [
            ("B001", "Платежи", "Перевод выполняется после подтверждения OTP."),
            ("B002", "Отмена", "Отмена перевода доступна до списания средств."),
            ("B003", "Ошибки", "Таймаут шлюза возвращает 504."),
        ]
    )

    def fake_holistic(stage, payload: HolisticGroupPayload, output_type, *, settings=None):
        assert payload.has_uninspected_scope is True
        return StageResult[HolisticAuditOutput](
            stage=PipelineStage.HOLISTIC,
            output=HolisticAuditOutput(
                evaluations=_all_13_whole_evals(
                    {
                        QualityCriterion.SET_CONSISTENT: (
                            CriterionStatus.FAIL,
                            "Конфликт без второй цитаты.",
                            ["BAD_CONTRADICTION"],
                        ),
                        QualityCriterion.SET_COMPLETE: (
                            CriterionStatus.FAIL,
                            "Не описан обработчик таймаута шлюза.",
                            ["GAP_INCOMPLETE_SCOPE"],
                        ),
                    }
                ),
                findings=[
                    HolisticFindingCandidate(
                        local_key="BAD_CONTRADICTION",
                        kind=FindingType.CONTRADICTION,
                        severity=Severity.CRITICAL,
                        claim="Несогласованное правило подтверждения.",
                        recommendation="Исправить.",
                        requirement_ids=["R001"],
                        criteria=[QualityCriterion.SET_CONSISTENT],
                        # Only ONE quote -> must be rejected!
                        evidence=[HolisticEvidenceQuote(block_id="B001", quote="подтверждения OTP")],
                    ),
                    HolisticFindingCandidate(
                        local_key="GAP_INCOMPLETE_SCOPE",
                        kind=FindingType.MISSING_INFORMATION,
                        severity=Severity.MAJOR,
                        claim="Не указано поведение при таймауте шлюза.",
                        recommendation="Описать таймаут.",
                        requirement_ids=["R001"],
                        criteria=[QualityCriterion.SET_COMPLETE],
                        expected_information="Обработка таймаута внешнего шлюза",
                        gap_context=HolisticEvidenceQuote(block_id="B001", quote="Перевод выполняется"),
                        # B003 is NOT in inspected_blocks -> incomplete search scope!
                        inspected_blocks=["B001", "B002", "B003"],
                    ),
                ],
            ),
            attempts=1,
        )

    holistic = audit_holistic_set(
        reqs[:2],
        doc,
        ProviderSettings(api_key="test"),
        inspected_blocks={"B001", "B002"},
        generate_fn=fake_holistic,
        ground_fn=_exact_ground,
    )

    # Single-quote contradiction rejected; gap kept strictly scoped to inspected blocks and marked UNCERTAIN.
    assert len(holistic.findings) == 1
    gap_finding = holistic.findings[0]
    assert gap_finding.kind == FindingType.MISSING_INFORMATION
    assert gap_finding.gap is not None
    assert set(gap_finding.gap.inspected_blocks) == {"B001", "B002"}
    assert "B003" not in gap_finding.gap.inspected_blocks
    assert gap_finding.semantic_status == SemanticStatus.UNCERTAIN
    assert any("непроверенные фрагменты" in u for u in holistic.uncertainties)
    assert any("две независимые подтверждённые цитаты" in u for u in holistic.uncertainties)


def test_deduplication_merges_overlapping_criteria_and_preserves_single_id():
    ref1 = EvidenceRef(version=SHA, block_id="B001", start=0, end=10, quote="0123456789", grounding=GroundingStatus.EXACT)
    ref2 = EvidenceRef(version=SHA, block_id="B002", start=0, end=10, quote="abcdefghij", grounding=GroundingStatus.EXACT)
    f1 = Finding(
        finding_id="TMP-1",
        kind=FindingType.CONTRADICTION,
        severity=Severity.MAJOR,
        claim="Конфликт кодов статуса.",
        recommendation="Согласовать код.",
        requirement_ids=["R001"],
        criteria=[QualityCriterion.SET_CONSISTENT],
        evidence=[ref1, ref2],
        semantic_status=SemanticStatus.UNCHECKED,
        verification_reason="Ожидает проверки.",
    )
    f2 = Finding(
        finding_id="TMP-2",
        kind=FindingType.CONTRADICTION,
        severity=Severity.CRITICAL,
        claim="Конфликт HTTP-статусов между разделами.",
        recommendation="Согласовать единый код.",
        requirement_ids=["R002"],
        criteria=[QualityCriterion.SRS_CONSISTENT],
        evidence=[ref2, ref1],
        semantic_status=SemanticStatus.UNCHECKED,
        verification_reason="Ожидает проверки.",
    )

    merged, id_map = deduplicate_findings([f1, f2], block_order={"B001": 0, "B002": 1})

    assert len(merged) == 1
    assert merged[0].finding_id == "F001"
    assert merged[0].severity == Severity.CRITICAL
    assert set(merged[0].requirement_ids) == {"R001", "R002"}
    assert set(merged[0].criteria) == {QualityCriterion.SET_CONSISTENT, QualityCriterion.SRS_CONSISTENT}
    assert id_map == {"TMP-1": "F001", "TMP-2": "F001"}


def test_holistic_syncs_criteria_drops_orphans_splits_on_truncation_and_bounds_multigroup_gaps():
    blocks = [
        ("B001", "Раздел 1 — API", "POST /api/v1/payments возвращает HTTP 201 и комиссию 1%."),
        *[(f"B{i:03d}", f"Раздел {i}", f"Промежуточное правило учёта номер {i} для тарифа.") for i in range(2, 10)],
        ("B010", "Раздел 10 — Приёмка", "POST /api/v1/payments возвращает HTTP 200 без описания таймаута."),
    ]
    doc, reqs = _build_spec_doc(blocks)

    calls = 0

    def fake_holistic(stage, payload: HolisticGroupPayload, output_type, *, settings=None):
        nonlocal calls
        calls += 1
        if calls == 1 and len(payload.requirements) > 1:
            raise ProviderError(
                "Truncated structured response (MAX_TOKENS)",
                reason=FailureReason.TRUNCATED,
                action=FailureAction.SPLIT,
                attempts=1,
            )
        if "B010" in payload.inspected_block_ids:
            # B010 is in a partial sub-group (not all 10 blocks inspected) -> gap must be scoped & UNCERTAIN!
            # Also emit an orphan finding (ORPHAN) and extra un-failed criterion -> must be trimmed!
            assert payload.has_uninspected_scope is True
            return StageResult[HolisticAuditOutput](
                stage=PipelineStage.HOLISTIC,
                output=HolisticAuditOutput(
                    evaluations=_all_13_whole_evals(
                        {
                            QualityCriterion.SET_COMPLETE: (
                                CriterionStatus.FAIL,
                                "Не указано поведение при таймауте.",
                                ["G1"],
                            )
                        }
                    ),
                    findings=[
                        HolisticFindingCandidate(
                            local_key="G1",
                            kind=FindingType.MISSING_INFORMATION,
                            severity=Severity.MAJOR,
                            claim="Отсутствует описание таймаута платежа.",
                            recommendation="Указать таймаут.",
                            requirement_ids=["R010"],
                            criteria=[QualityCriterion.SET_COMPLETE, QualityCriterion.SRS_COMPLETE],
                            expected_information="Таймаут вызова платёжного шлюза",
                            gap_context=HolisticEvidenceQuote(block_id="B010", quote="без описания таймаута"),
                            inspected_blocks=["B001", "B010"],  # B001 not in this sub-group's passages!
                        ),
                        HolisticFindingCandidate(
                            local_key="ORPHAN",
                            kind=FindingType.AMBIGUITY,
                            severity=Severity.MINOR,
                            claim="Несвязанная находка.",
                            recommendation="Нет.",
                            requirement_ids=["R010"],
                            criteria=[QualityCriterion.SRS_UNAMBIGUOUS],
                            evidence=[HolisticEvidenceQuote(block_id="B010", quote="HTTP 200")],
                        ),
                    ],
                ),
                attempts=1,
            )
        return StageResult[HolisticAuditOutput](
            stage=PipelineStage.HOLISTIC,
            output=HolisticAuditOutput(evaluations=_all_13_whole_evals()),
            attempts=1,
        )

    holistic = audit_holistic_set(
        reqs,
        doc,
        ProviderSettings(api_key="test"),
        generate_fn=fake_holistic,
        ground_fn=_exact_ground,
        max_requirements_per_group=5,
    )

    assert holistic.cross_section_groups.complete
    assert len(holistic.findings) == 1
    gap_finding = holistic.findings[0]
    assert gap_finding.criteria == [QualityCriterion.SET_COMPLETE]
    assert gap_finding.semantic_status == SemanticStatus.UNCERTAIN
    assert gap_finding.gap is not None
    assert "B010" in gap_finding.gap.inspected_blocks
    assert any("не привязана" in u for u in holistic.uncertainties)


def test_verification_splits_on_truncation_and_keeps_failed_candidate_unchecked():
    doc, reqs = _build_spec_doc(
        [
            ("B001", "API", "POST /api/v1/a возвращает HTTP 201."),
            ("B002", "Приёмка", "POST /api/v1/a возвращает HTTP 200."),
        ]
    )
    f1 = Finding(
        finding_id="F001",
        kind=FindingType.CONTRADICTION,
        severity=Severity.CRITICAL,
        claim="Конфликт кодов 201 и 200.",
        recommendation="Согласовать код.",
        requirement_ids=["R001", "R002"],
        criteria=[QualityCriterion.SET_CONSISTENT],
        evidence=[
            EvidenceRef(version=SHA, block_id="B001", start=0, end=len(doc.version.blocks[0].text), quote=doc.version.blocks[0].text, grounding=GroundingStatus.EXACT),
            EvidenceRef(version=SHA, block_id="B002", start=0, end=len(doc.version.blocks[1].text), quote=doc.version.blocks[1].text, grounding=GroundingStatus.EXACT),
        ],
        semantic_status=SemanticStatus.UNCHECKED,
        verification_reason="Ожидает проверки.",
    )
    f2 = Finding(
        finding_id="F002",
        kind=FindingType.AMBIGUITY,
        severity=Severity.MAJOR,
        claim="Неясный формат ошибки.",
        recommendation="Задать JSON-схему.",
        requirement_ids=["R001"],
        criteria=[QualityCriterion.SRS_UNAMBIGUOUS],
        evidence=[
            EvidenceRef(version=SHA, block_id="B001", start=0, end=len(doc.version.blocks[0].text), quote=doc.version.blocks[0].text, grounding=GroundingStatus.EXACT),
        ],
        semantic_status=SemanticStatus.UNCHECKED,
        verification_reason="Ожидает проверки.",
    )
    from src.contracts import CriterionEvaluation

    evals = [
        CriterionEvaluation(requirement_id=None, criterion=QualityCriterion.SET_CONSISTENT, status=CriterionStatus.FAIL, rationale="Конфликт.", finding_ids=["F001"]),
        CriterionEvaluation(requirement_id=None, criterion=QualityCriterion.SRS_UNAMBIGUOUS, status=CriterionStatus.FAIL, rationale="Неясно.", finding_ids=["F002"]),
    ]

    calls = 0

    def fake_verify(stage, payload: VerificationPayload, output_type, *, settings=None):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ProviderError(
                "Truncated structured response (MAX_TOKENS)",
                reason=FailureReason.TRUNCATED,
                action=FailureAction.SPLIT,
                attempts=1,
            )
        if payload.candidates[0].candidate_id == "F001":
            return StageResult[VerificationOutput](
                stage=PipelineStage.VERIFY,
                output=VerificationOutput(
                    reviews=[SemanticReview(candidate_id="F001", decision="supported", reason="Подтверждено.")]
                ),
                attempts=1,
            )
        raise ProviderError("Service unavailable (HTTP 503)", reason=FailureReason.SERVER_ERROR, attempts=3)

    res = verify_findings(
        [f1, f2],
        doc,
        ProviderSettings(api_key="test"),
        evaluations=evals,
        generate_fn=fake_verify,
    )

    assert calls == 3
    by_id = {f.finding_id: f for f in res.findings}
    assert by_id["F001"].semantic_status == SemanticStatus.SUPPORTED
    assert by_id["F002"].semantic_status == SemanticStatus.UNCHECKED
    assert res.incomplete_reasons
    eval_map = {e.criterion: e for e in res.evaluations}
    assert eval_map[QualityCriterion.SET_CONSISTENT].status == CriterionStatus.FAIL
    assert eval_map[QualityCriterion.SRS_UNAMBIGUOUS].status == CriterionStatus.UNKNOWN
    assert eval_map[QualityCriterion.SRS_UNAMBIGUOUS].finding_ids == []
