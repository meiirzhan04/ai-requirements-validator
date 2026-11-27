"""Holistic cross-section audit against the 5 ISO set criteria and 8 IEEE criteria."""
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
import re

from pydantic import Field

from src.contracts import (
    WHOLE_SET,
    CriterionEvaluation,
    CriterionStatus,
    EvidenceCandidate,
    EvidenceRef,
    Finding,
    FindingType,
    GapEvidence,
    GroundingResult,
    ParsedDocument,
    PipelineStage,
    ProviderSettings,
    QualityCriterion,
    Requirement,
    SemanticStatus,
    Severity,
    StageResult,
    WorkSet,
)
from src.contracts.source import Contract, Count, Id, Text
from src.evidence import ground
from src.prompts.holistic import (
    HOLISTIC_PROMPT_VERSION,
    HOLISTIC_REPAIR_HINT,
    HOLISTIC_SYSTEM_PROMPT,
)
from src.provider import FailureAction, FailureReason, ProviderError, QuotaBudget, generate
from src.provider.dto import (
    HolisticAuditOutput,
    HolisticCriterionCandidate,
    HolisticEvidenceQuote,
    HolisticFindingCandidate,
    HolisticGroupPayload,
    HolisticPassageInput,
    HolisticRequirementInput,
)

GenerateFn = Callable[..., StageResult[HolisticAuditOutput]]
GroundFn = Callable[[EvidenceCandidate, ParsedDocument], GroundingResult]

_WHOLE_SET_ORDERED = tuple(sorted(WHOLE_SET, key=lambda c: c.value))
_TOKEN_RE = re.compile(r"[A-Za-zА-Яа-яЁё0-9/_-]{4,}")
_API_RE = re.compile(r"(?:/[A-Za-z0-9_{}.-]+)+|\b(?:GET|POST|PUT|PATCH|DELETE|HTTP\s*\d{3})\b", re.IGNORECASE)
_WHITESPACE_RE = re.compile(r"\s+")
_STOP_WORDS = frozenset(
    {
        "система",
        "должна",
        "должен",
        "должны",
        "может",
        "пользователь",
        "требование",
        "требования",
        "данные",
        "данных",
        "если",
        "при",
        "для",
        "или",
        "после",
        "перед",
        "через",
        "более",
        "менее",
        "также",
        "должно",
        "быть",
        "the",
        "must",
        "shall",
        "should",
        "with",
        "from",
        "that",
        "this",
        "when",
        "system",
        "user",
    }
)
_STOP_ALL_REASONS = frozenset(
    {
        FailureReason.AUTH_ERROR,
        FailureReason.MODEL_UNAVAILABLE,
        FailureReason.DAILY_QUOTA_EXHAUSTED,
        FailureReason.BUDGET_EXHAUSTED,
    }
)


class HolisticAuditResult(Contract):
    evaluations: list[CriterionEvaluation]
    findings: list[Finding]
    cross_section_groups: WorkSet
    incomplete_reasons: list[Text] = Field(default_factory=list)
    uncertainties: list[Text] = Field(default_factory=list)
    attempts: Count = 0
    input_tokens: Count | None = None
    output_tokens: Count | None = None


@dataclass(frozen=True)
class CrossSectionGroup:
    group_id: str
    topic_keywords: tuple[str, ...]
    requirement_ids: tuple[str, ...]
    block_ids: tuple[str, ...]


@dataclass
class _DraftHolisticFinding:
    kind: FindingType
    severity: Severity
    claim: str
    conditions_key: str
    recommendation: str
    requirement_ids: list[str]
    criteria: set[QualityCriterion]
    evidence: list[EvidenceRef]
    gap: GapEvidence | None
    semantic_status: SemanticStatus
    verification_reason: str


def _normalize_key(text: str) -> str:
    return _WHITESPACE_RE.sub(" ", text.strip().lower())


def _extract_index_terms(text: str) -> set[str]:
    terms: set[str] = set()
    for match in _API_RE.findall(text):
        terms.add(_normalize_key(match))
    for tok in _TOKEN_RE.findall(text.lower()):
        cleaned = tok.strip("_-/")
        if len(cleaned) < 4 or cleaned in _STOP_WORDS or cleaned.isdigit():
            continue
        # Light prefix stem for Russian/English inflections without external deps.
        stem = cleaned[:6] if len(cleaned) >= 7 else cleaned
        terms.add(stem)
    return terms


def _has_independent_anchors(refs: list[EvidenceRef]) -> bool:
    grounded = [ref for ref in refs if ref.grounded]
    return any(
        a.block_id != b.block_id or a.end <= b.start or b.end <= a.start
        for idx, a in enumerate(grounded)
        for b in grounded[idx + 1 :]
    )


def build_cross_section_groups(
    requirements: list[Requirement],
    document: ParsedDocument,
    *,
    max_requirements_per_group: int = 12,
    max_passages_per_group: int = 16,
) -> list[CrossSectionGroup]:
    """Index entities/endpoints/conditions and build cross-section comparison groups."""
    if not requirements:
        return []

    block_order = {b.block_id: idx for idx, b in enumerate(document.version.blocks)}
    block_section = {b.block_id: b.location.section for b in document.version.blocks}

    # Small document optimization: one whole-specification group fits in a single pass.
    unique_blocks = sorted(
        {r.source.block_id for r in requirements if r.source.block_id in block_order},
        key=lambda bid: block_order[bid],
    )
    if len(requirements) <= max_requirements_per_group and len(unique_blocks) <= max_passages_per_group:
        return [
            CrossSectionGroup(
                group_id="whole-spec",
                topic_keywords=("full-document",),
                requirement_ids=tuple(r.req_id for r in requirements),
                block_ids=tuple(unique_blocks),
            )
        ]

    # Long document indexing: connect distant sections sharing entities, endpoints, or terms.
    term_to_reqs: dict[str, list[Requirement]] = defaultdict(list)
    for req in requirements:
        terms = _extract_index_terms(f"{req.summary} {req.source.quote}")
        for term in terms:
            term_to_reqs[term].append(req)

    # Prioritize terms spanning multiple sections (distant rules/API/acceptance comparisons).
    ranked_terms: list[tuple[int, int, str]] = []
    for term, matched in term_to_reqs.items():
        if len(matched) < 2:
            continue
        sections = {block_section.get(r.source.block_id, "") for r in matched}
        if len(sections) >= 2:
            ranked_terms.append((len(sections), len(matched), term))
    ranked_terms.sort(key=lambda item: (-item[0], -item[1], item[2]))

    groups: list[CrossSectionGroup] = []
    covered_reqs: set[str] = set()
    seen_req_sets: set[frozenset[str]] = set()

    for _, _, term in ranked_terms:
        matched = term_to_reqs[term][:max_requirements_per_group]
        req_ids = tuple(dict.fromkeys(r.req_id for r in matched))
        req_set = frozenset(req_ids)
        if req_set in seen_req_sets:
            continue
        # Avoid creating redundant sub-groups already fully covered unless introducing new pairs.
        if any(req_set <= existing for existing in seen_req_sets):
            continue
        seen_req_sets.add(req_set)
        covered_reqs.update(req_ids)
        blk_ids = tuple(
            sorted(
                {r.source.block_id for r in matched if r.source.block_id in block_order},
                key=lambda bid: block_order[bid],
            )[:max_passages_per_group]
        )
        groups.append(
            CrossSectionGroup(
                group_id=f"cross-{len(groups) + 1}",
                topic_keywords=(term,),
                requirement_ids=req_ids,
                block_ids=blk_ids,
            )
        )

    # Ensure every requirement participates in at least one group.
    remaining = [r for r in requirements if r.req_id not in covered_reqs]
    for offset in range(0, len(remaining), max_requirements_per_group):
        chunk = remaining[offset : offset + max_requirements_per_group]
        req_ids = tuple(r.req_id for r in chunk)
        blk_ids = tuple(
            sorted(
                {r.source.block_id for r in chunk if r.source.block_id in block_order},
                key=lambda bid: block_order[bid],
            )[:max_passages_per_group]
        )
        groups.append(
            CrossSectionGroup(
                group_id=f"scope-{len(groups) + 1}",
                topic_keywords=("residual-scope",),
                requirement_ids=req_ids,
                block_ids=blk_ids,
            )
        )

    return groups


def deduplicate_findings(
    findings: list[Finding],
    *,
    finding_id_start: int = 1,
    block_order: dict[str, int] | None = None,
) -> tuple[list[Finding], dict[str, str]]:
    """Deduplicate findings by kind, normalized claim, and grounded evidence anchors.

    Returns (deduplicated_findings, old_id_to_new_id_map).
    """
    severity_rank = {Severity.CRITICAL: 3, Severity.MAJOR: 2, Severity.MINOR: 1}
    semantic_rank = {
        SemanticStatus.SUPPORTED: 4,
        SemanticStatus.UNCERTAIN: 3,
        SemanticStatus.UNCHECKED: 2,
        SemanticStatus.REFUTED: 1,
    }

    groups: dict[tuple[str, tuple[tuple[str, str, int, int], ...], str], list[Finding]] = {}
    for finding in findings:
        anchors = tuple(sorted(ref.anchor for ref in finding.evidence))
        if not anchors and finding.gap is not None:
            anchors = (finding.gap.context.anchor,)
        # Contradictions with identical pair of anchors represent the same underlying conflict;
        # other finding types key on (kind, anchors, normalized claim).
        claim_part = "" if finding.kind == FindingType.CONTRADICTION else _normalize_key(finding.claim)
        sig = (finding.kind.value, anchors, claim_part)
        groups.setdefault(sig, []).append(finding)

    merged_items: list[tuple[tuple[int, int, str], Finding, list[str]]] = []
    for sig, bucket in groups.items():
        primary = max(bucket, key=lambda f: (severity_rank[f.severity], semantic_rank[f.semantic_status], len(f.claim)))
        all_req_ids = list(dict.fromkeys(rid for f in bucket for rid in f.requirement_ids))
        all_criteria = sorted({c for f in bucket for c in f.criteria}, key=lambda c: c.value)
        seen_anchors: set[tuple[str, str, int, int]] = set()
        all_evidence: list[EvidenceRef] = []
        for f in bucket:
            for ref in f.evidence:
                if ref.anchor not in seen_anchors:
                    seen_anchors.add(ref.anchor)
                    all_evidence.append(ref)
        gap = primary.gap
        if gap is not None:
            combined_inspected = list(
                dict.fromkeys(bid for f in bucket if f.gap for bid in f.gap.inspected_blocks)
            )
            gap = GapEvidence(
                expected_information=gap.expected_information,
                context=gap.context,
                inspected_blocks=combined_inspected,
            )

        first_ref = all_evidence[0] if all_evidence else (gap.context if gap else None)
        order_key = (
            block_order.get(first_ref.block_id, 10**9) if (block_order and first_ref) else 0,
            first_ref.start if first_ref else 0,
            primary.claim,
        )
        merged = primary.model_copy(
            update={
                "requirement_ids": all_req_ids,
                "criteria": all_criteria,
                "evidence": all_evidence,
                "gap": gap,
            }
        )
        merged_items.append((order_key, merged, [f.finding_id for f in bucket]))

    merged_items.sort(key=lambda item: item[0])
    result: list[Finding] = []
    id_map: dict[str, str] = {}
    for offset, (_, item, old_ids) in enumerate(merged_items):
        new_id = f"F{finding_id_start + offset:03d}"
        for old_id in old_ids:
            id_map[old_id] = new_id
        result.append(item.model_copy(update={"finding_id": new_id}))
    return result, id_map


def _ground_quote(
    quote_obj: HolisticEvidenceQuote,
    document: ParsedDocument,
    valid_blocks: set[str],
    ground_fn: GroundFn,
) -> tuple[EvidenceRef | None, str | None]:
    if quote_obj.block_id not in valid_blocks:
        return None, f"Неизвестный блок {quote_obj.block_id}."
    if not quote_obj.quote.strip():
        return None, f"Пустая цитата в блоке {quote_obj.block_id}."
    res = ground_fn(
        EvidenceCandidate(
            version=document.version.sha256,
            block_id=quote_obj.block_id,
            quote=quote_obj.quote,
        ),
        document,
    )
    if not res.reference or not res.reference.grounded:
        return None, f"Цитата в блоке {quote_obj.block_id} не подтверждена ({res.status.value}: {res.reason})."
    return res.reference, None


def _build_draft_finding(
    candidate: HolisticFindingCandidate,
    document: ParsedDocument,
    valid_req_ids: set[str],
    inspected_blocks: set[str],
    has_uninspected_scope: bool,
    ground_fn: GroundFn,
) -> tuple[_DraftHolisticFinding | None, str | None, str | None]:
    """Validate and ground one holistic candidate.

    Returns (draft_or_none, rejection_reason_or_none, uncertainty_note_or_none).
    """
    valid_blocks = {b.block_id for b in document.version.blocks}
    if any(c not in WHOLE_SET for c in candidate.criteria):
        return None, f"Находка {candidate.local_key}: критерий вне каталога целостного аудита.", None

    claim = candidate.claim.strip()
    recommendation = candidate.recommendation.strip()
    if not claim or not recommendation:
        return None, f"Находка {candidate.local_key}: пустое описание дефекта или рекомендации.", None

    req_ids = [rid for rid in dict.fromkeys(candidate.requirement_ids) if rid in valid_req_ids]

    grounded_refs: list[EvidenceRef] = []
    seen_anchors: set[tuple[str, str, int, int]] = set()
    for ev in candidate.evidence:
        ref, err = _ground_quote(ev, document, valid_blocks, ground_fn)
        if err or ref is None:
            return None, f"Находка {candidate.local_key}: {err}", None
        if ref.anchor not in seen_anchors:
            seen_anchors.add(ref.anchor)
            grounded_refs.append(ref)

    uncertainty_note: str | None = None
    gap: GapEvidence | None = None

    if candidate.kind == FindingType.CONTRADICTION:
        if not _has_independent_anchors(grounded_refs):
            return (
                None,
                f"Находка {candidate.local_key}: противоречие отклонено — требуются две независимые подтверждённые цитаты.",
                None,
            )
        if candidate.scenario and candidate.scenario.strip():
            scenario = candidate.scenario.strip()
            if _normalize_key(scenario) not in _normalize_key(claim):
                claim = f"{claim} (Сценарий: {scenario})"

    if candidate.kind == FindingType.MISSING_INFORMATION:
        expected = (candidate.expected_information or "").strip()
        if not expected:
            return None, f"Находка {candidate.local_key}: для пропуска не указана ожидаемая информация.", None
        ctx_ref: EvidenceRef | None = None
        if candidate.gap_context is not None:
            ctx_ref, err = _ground_quote(candidate.gap_context, document, valid_blocks, ground_fn)
            if err or ctx_ref is None:
                return None, f"Находка {candidate.local_key} (контекст пропуска): {err}", None
        elif grounded_refs:
            ctx_ref = grounded_refs[0]
        else:
            return None, f"Находка {candidate.local_key}: для пропуска отсутствует подтверждённый контекстный якорь.", None

        if ctx_ref.block_id not in inspected_blocks:
            return (
                None,
                f"Находка {candidate.local_key}: контекстный блок {ctx_ref.block_id} не входит в проверенную область.",
                None,
            )

        declared_scope = list(dict.fromkeys(candidate.inspected_blocks)) if candidate.inspected_blocks else sorted(inspected_blocks)
        outside_scope = [bid for bid in declared_scope if bid not in inspected_blocks]
        scoped_inspected = [bid for bid in declared_scope if bid in inspected_blocks]
        if ctx_ref.block_id not in scoped_inspected:
            scoped_inspected.insert(0, ctx_ref.block_id)

        gap = GapEvidence(
            expected_information=expected,
            context=ctx_ref,
            inspected_blocks=scoped_inspected,
        )
        if outside_scope or has_uninspected_scope:
            uncertainty_note = (
                f"Заявление об отсутствии «{expected}» ограничено проверенными блоками ({len(scoped_inspected)}); "
                f"в документе остались непроверенные фрагменты."
            )
    else:
        if not grounded_refs:
            return None, f"Находка {candidate.local_key}: отсутствуют подтверждённые доказательства.", None

    if candidate.kind == FindingType.MISSING_INFORMATION and uncertainty_note is not None:
        semantic_status = SemanticStatus.UNCERTAIN
        verification_reason = uncertainty_note
    elif candidate.severity in (Severity.CRITICAL, Severity.MAJOR):
        semantic_status = SemanticStatus.UNCHECKED
        verification_reason = "Ожидает отдельной семантической проверки контекста и исключений."
    else:
        semantic_status = SemanticStatus.SUPPORTED
        verification_reason = "Подтверждено дословной ссылкой в целостном аудите (minor)."

    conditions_key = _normalize_key(candidate.conditions or candidate.scenario or "")
    return (
        _DraftHolisticFinding(
            kind=candidate.kind,
            severity=candidate.severity,
            claim=claim,
            conditions_key=conditions_key,
            recommendation=recommendation,
            requirement_ids=req_ids,
            criteria=set(candidate.criteria),
            evidence=grounded_refs,
            gap=gap,
            semantic_status=semantic_status,
            verification_reason=verification_reason,
        ),
        None,
        uncertainty_note,
    )


def _call_provider(
    generate_fn: GenerateFn,
    payload: HolisticGroupPayload,
    *,
    settings: ProviderSettings,
    budget: QuotaBudget | None,
) -> StageResult[HolisticAuditOutput]:
    if generate_fn is generate:
        return generate_fn(
            PipelineStage.HOLISTIC,
            payload,
            HolisticAuditOutput,
            settings=settings,
            budget=budget,
        )
    return generate_fn(
        PipelineStage.HOLISTIC,
        payload,
        HolisticAuditOutput,
        settings=settings,
    )


def audit_holistic_set(
    requirements: list[Requirement],
    document: ParsedDocument,
    settings: ProviderSettings,
    *,
    inspected_blocks: set[str] | list[str] | None = None,
    budget: QuotaBudget | None = None,
    generate_fn: GenerateFn = generate,
    ground_fn: GroundFn = ground,
    max_requirements_per_group: int = 12,
    finding_id_start: int = 1,
) -> HolisticAuditResult:
    """Audit whole-specification ISO/IEEE properties across cross-section groups."""
    all_block_ids = [b.block_id for b in document.version.blocks]
    block_order = {bid: idx for idx, bid in enumerate(all_block_ids)}
    block_map = {b.block_id: b for b in document.version.blocks}
    req_map = {r.req_id: r for r in requirements}
    valid_req_ids = set(req_map)

    effective_inspected = (
        set(all_block_ids)
        if inspected_blocks is None
        else (set(inspected_blocks) & set(all_block_ids))
    )
    has_uninspected_scope = (
        bool(document.unread_pages)
        or effective_inspected != set(all_block_ids)
    )

    groups = build_cross_section_groups(
        requirements,
        document,
        max_requirements_per_group=max_requirements_per_group,
    )
    planned_groups = [g.group_id for g in groups]
    if not groups:
        return HolisticAuditResult(
            evaluations=[],
            findings=[],
            cross_section_groups=WorkSet(planned=[], completed=[]),
            incomplete_reasons=["Нет требований для целостного аудита набора."],
        )

    completed_groups: list[str] = []
    incomplete_reasons: list[str] = []
    uncertainties: list[str] = []

    raw_findings: list[Finding] = []
    criterion_observations: dict[QualityCriterion, list[tuple[CriterionStatus, str, list[str]]]] = defaultdict(list)

    total_attempts = 0
    known_in = 0
    known_out = 0
    has_known_usage = False
    temp_finding_seq = 0

    for group in groups:
        group_inspected = sorted(
            effective_inspected & set(group.block_ids) if group.block_ids else effective_inspected,
            key=lambda bid: block_order[bid],
        )
        # Include all inspected blocks in scope metadata when running whole-spec audit.
        scope_blocks = sorted(effective_inspected, key=lambda bid: block_order[bid])
        passages = [
            HolisticPassageInput(
                block_id=bid,
                section=block_map[bid].location.section,
                text=block_map[bid].text,
            )
            for bid in group.block_ids
            if bid in block_map
        ]
        req_inputs = [
            HolisticRequirementInput(
                req_id=req_map[rid].req_id,
                original_id=req_map[rid].original_id,
                category=req_map[rid].category.value,
                summary=req_map[rid].summary,
                block_id=req_map[rid].source.block_id,
                quote=req_map[rid].source.quote,
            )
            for rid in group.requirement_ids
            if rid in req_map
        ]

        def _make_payload(repair_hint: str | None = None) -> HolisticGroupPayload:
            return HolisticGroupPayload(
                system_instruction=HOLISTIC_SYSTEM_PROMPT,
                prompt_version=HOLISTIC_PROMPT_VERSION,
                document_name=document.version.name,
                group_id=group.group_id,
                topic_keywords=list(group.topic_keywords),
                criteria_catalog=[c.value for c in _WHOLE_SET_ORDERED],
                inspected_block_ids=scope_blocks if group.group_id == "whole-spec" else group_inspected,
                has_uninspected_scope=has_uninspected_scope,
                requirements=req_inputs,
                passages=passages,
                repair_hint=repair_hint,
            )

        payload = _make_payload()
        stage_result: StageResult[HolisticAuditOutput] | None = None
        stop_all = False

        for repair_attempt in range(2):
            try:
                stage_result = _call_provider(
                    generate_fn,
                    payload,
                    settings=settings,
                    budget=budget,
                )
                total_attempts += stage_result.attempts
                if stage_result.input_tokens is not None and stage_result.output_tokens is not None:
                    known_in += stage_result.input_tokens
                    known_out += stage_result.output_tokens
                    has_known_usage = True
                break
            except ProviderError as exc:
                total_attempts += max(exc.attempts, 0)
                if exc.action == FailureAction.REPAIR and repair_attempt == 0:
                    payload = _make_payload(repair_hint=HOLISTIC_REPAIR_HINT)
                    continue
                incomplete_reasons.append(f"Группа {group.group_id} целостного аудита не завершена: {exc}.")
                if exc.reason in _STOP_ALL_REASONS:
                    stop_all = True
                break

        if stop_all:
            break
        if stage_result is None:
            continue

        output = stage_result.output
        group_clean = True
        local_key_to_temp_id: dict[str, str] = {}
        local_findings_by_key: dict[str, Finding] = {}

        for fc in output.findings:
            if fc.local_key in local_key_to_temp_id:
                incomplete_reasons.append(f"Группа {group.group_id}: дубликат local_key {fc.local_key}.")
                group_clean = False
                continue
            draft, rejection, uncertainty_note = _build_draft_finding(
                fc,
                document,
                valid_req_ids,
                effective_inspected,
                has_uninspected_scope,
                ground_fn,
            )
            if uncertainty_note:
                uncertainties.append(uncertainty_note)
            if rejection or draft is None:
                uncertainties.append(rejection or f"Группа {group.group_id}: отклонена находка {fc.local_key}.")
                continue
            temp_finding_seq += 1
            temp_id = f"TMP-{temp_finding_seq:04d}"
            finding = Finding(
                finding_id=temp_id,
                kind=draft.kind,
                severity=draft.severity,
                claim=draft.claim,
                recommendation=draft.recommendation,
                requirement_ids=draft.requirement_ids,
                criteria=sorted(draft.criteria, key=lambda c: c.value),
                evidence=draft.evidence,
                gap=draft.gap,
                semantic_status=draft.semantic_status,
                verification_reason=draft.verification_reason,
            )
            local_key_to_temp_id[fc.local_key] = temp_id
            local_findings_by_key[fc.local_key] = finding
            raw_findings.append(finding)

        seen_group_criteria: dict[QualityCriterion, HolisticCriterionCandidate] = {}
        for ev in output.evaluations:
            if ev.criterion not in WHOLE_SET:
                incomplete_reasons.append(
                    f"Группа {group.group_id}: критерий {ev.criterion.value} вне каталога целостного аудита."
                )
                group_clean = False
                continue
            if ev.criterion in seen_group_criteria:
                incomplete_reasons.append(
                    f"Группа {group.group_id}: повторная оценка критерия {ev.criterion.value}."
                )
                group_clean = False
                continue
            if not ev.rationale.strip():
                incomplete_reasons.append(
                    f"Группа {group.group_id}: отсутствует обоснование для критерия {ev.criterion.value}."
                )
                group_clean = False
                continue
            seen_group_criteria[ev.criterion] = ev

        missing_criteria = [c.value for c in _WHOLE_SET_ORDERED if c not in seen_group_criteria]
        if missing_criteria:
            incomplete_reasons.append(
                f"Группа {group.group_id}: пропущены критерии набора ({', '.join(missing_criteria)})."
            )
            group_clean = False

        for crit, ev in seen_group_criteria.items():
            resolved_ids = [
                local_key_to_temp_id[k]
                for k in ev.local_finding_keys
                if k in local_key_to_temp_id
            ]
            # Ensure every linked finding declares this criterion.
            for k in ev.local_finding_keys:
                f = local_findings_by_key.get(k)
                if f is not None and crit not in f.criteria:
                    idx_f = raw_findings.index(f)
                    updated = f.model_copy(
                        update={"criteria": sorted({*f.criteria, crit}, key=lambda c: c.value)}
                    )
                    raw_findings[idx_f] = updated
                    local_findings_by_key[k] = updated

            if ev.status == CriterionStatus.FAIL and not resolved_ids:
                # Candidate defect was rejected (e.g. missing 2nd quote) -> cannot keep unsupported FAIL.
                criterion_observations[crit].append(
                    (
                        CriterionStatus.UNKNOWN,
                        f"{ev.rationale.strip()} (Кандидат дефекта не прошёл проверку оснований.)",
                        [],
                    )
                )
            elif ev.status != CriterionStatus.FAIL:
                criterion_observations[crit].append((ev.status, ev.rationale.strip(), []))
            else:
                criterion_observations[crit].append((CriterionStatus.FAIL, ev.rationale.strip(), resolved_ids))

        if group_clean:
            completed_groups.append(group.group_id)

    dedup_findings, id_map = deduplicate_findings(
        raw_findings,
        finding_id_start=finding_id_start,
        block_order=block_order,
    )

    # Aggregate exactly one whole-set evaluation per criterion when all groups completed (or for observed criteria).
    evaluations: list[CriterionEvaluation] = []
    for crit in _WHOLE_SET_ORDERED:
        obs = criterion_observations.get(crit)
        if not obs:
            continue
        fail_ids = list(
            dict.fromkeys(
                id_map[tid]
                for status, _, tids in obs
                if status == CriterionStatus.FAIL
                for tid in tids
                if tid in id_map
            )
        )
        if fail_ids:
            rationales = [rat for status, rat, _ in obs if status == CriterionStatus.FAIL]
            evaluations.append(
                CriterionEvaluation(
                    requirement_id=None,
                    criterion=crit,
                    status=CriterionStatus.FAIL,
                    rationale=rationales[0],
                    finding_ids=fail_ids,
                )
            )
        elif any(status == CriterionStatus.UNKNOWN for status, _, _ in obs):
            rationales = [rat for status, rat, _ in obs if status == CriterionStatus.UNKNOWN]
            evaluations.append(
                CriterionEvaluation(
                    requirement_id=None,
                    criterion=crit,
                    status=CriterionStatus.UNKNOWN,
                    rationale=rationales[0],
                    finding_ids=[],
                )
            )
        elif any(status == CriterionStatus.PASS for status, _, _ in obs):
            rationales = [rat for status, rat, _ in obs if status == CriterionStatus.PASS]
            evaluations.append(
                CriterionEvaluation(
                    requirement_id=None,
                    criterion=crit,
                    status=CriterionStatus.PASS,
                    rationale=rationales[0],
                    finding_ids=[],
                )
            )
        else:
            evaluations.append(
                CriterionEvaluation(
                    requirement_id=None,
                    criterion=crit,
                    status=CriterionStatus.NOT_APPLICABLE,
                    rationale=obs[0][1],
                    finding_ids=[],
                )
            )

    return HolisticAuditResult(
        evaluations=evaluations,
        findings=dedup_findings,
        cross_section_groups=WorkSet(planned=planned_groups, completed=completed_groups),
        incomplete_reasons=list(dict.fromkeys(incomplete_reasons)),
        uncertainties=list(dict.fromkeys(uncertainties)),
        attempts=total_attempts,
        input_tokens=known_in if has_known_usage else None,
        output_tokens=known_out if has_known_usage else None,
    )
