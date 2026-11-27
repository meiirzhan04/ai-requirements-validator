"""Holistic cross-section audit against the 5 ISO set criteria and 8 IEEE criteria."""
from collections import defaultdict
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import inspect
import logging
import re
from typing import Any

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
    RequirementCategory,
    SemanticStatus,
    Severity,
    StageResult,
    WorkSet,
)
from src.contracts.source import Contract, Count, Id, Text
from src.evidence import ground
from src.pipeline.diagnostics import log_safe_exception
from src.prompts.holistic import (
    HOLISTIC_LOGIC_SYSTEM_PROMPT,
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
logger = logging.getLogger(__name__)

_WHOLE_SET_ORDERED = tuple(sorted(WHOLE_SET, key=lambda c: c.value))
_TOKEN_RE = re.compile(r"[A-Za-zА-Яа-яЁё0-9/_-]{4,}")
_API_RE = re.compile(r"(?:/[A-Za-z0-9_{}.-]+)+|\b(?:GET|POST|PUT|PATCH|DELETE|HTTP\s*\d{3})\b", re.IGNORECASE)
_DOMAIN_ANCHOR_RE = re.compile(
    r"\b(?:UTC(?:[+-]\d{1,2}(?::\d{2})?)?|GMT(?:[+-]\d{1,2})?|"
    r"KZT|USD|EUR|RUB|GBP|CNY|SLA|TTL|OTP|API|REST|JSON|ISO|"
    r"NEW|DRAFT|PENDING|ACTIVE|PAID|FAILED|CLOSED|CANCELLED|CANCELED|BLOCKED|EXPIRED|ERROR|SUCCESS|OK)\b",
)
_HTTP_STATUS_CONTEXT_RE = re.compile(
    r"(?:HTTP|статус|код|status|code)[^\n\r\d]{0,12}\b([1-5]\d{2})\b",
    re.IGNORECASE,
)
_SEMANTIC_FOCUS_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "timing_calendar",
        re.compile(
            r"\b(?:UTC|GMT|часов\w*\s+пояс\w*|таймзон\w*|дата\s+(?:операц|событ|обработ|провод|расч)|"
            r"время\s+(?:событ|операц|обработ)|календарн\w*|рабоч\w*\s+дн\w*|расч[её]тн\w*\s+(?:период|месяц)|"
            r"конец\s+месяц|полноч|23:59|00:00|включая|исключительно|не\s+позднее|в\s+течение)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "state_transitions",
        re.compile(
            r"(?:->|→|\b(?:статус\w*|состояни\w*|переход\w*|терминальн\w*|финальн\w*|отмен\w*|блокир\w*|"
            r"NEW|DRAFT|PENDING|ACTIVE|PAID|FAILED|CLOSED|CANCELLED|CANCELED|BLOCKED|EXPIRED)\b)",
            re.IGNORECASE,
        ),
    ),
    (
        "repeated_events",
        re.compile(
            r"\b(?:не\s+более\s+(?:одного|1)\s+раз|единожды|повторн\w*|сброс\w*|пересч[её]т\w*|"
            r"независимо\s+от|каждый\s+раз|идемпотент\w*|дубликат\w*|изменени\w*\s+(?:порог|лимит|параметр)|"
            r"rate\s*limit|idempoten\w*|re-?trigger\w*)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "api_consistency",
        re.compile(
            r"(?:/[A-Za-z0-9_{}.-]+){2,}|\b(?:GET|POST|PUT|PATCH|DELETE|HTTP\s*[1-5]\d{2}|"
            r"эндпоинт\w*|endpoint\w*|webhook\w*|payload|JSON|OpenAPI|Swagger|ERROR)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "units_exceptions",
        re.compile(
            r"\b(?:KZT|USD|EUR|RUB|тенге|доллар\w*|евро|руб\w*|процент\w*|мс|сек\w*|мин\w*|"
            r"за\s+исключением|кроме|исключая|только\s+для|только\s+если|без\s+исключений)\b|%",
            re.IGNORECASE,
        ),
    ),
)
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
    for anchor in _DOMAIN_ANCHOR_RE.findall(text):
        terms.add(f"anchor:{anchor.lower()}")
    for status_code in _HTTP_STATUS_CONTEXT_RE.findall(text):
        terms.add(f"http:{status_code}")
    for tok in _TOKEN_RE.findall(text.lower()):
        cleaned = tok.strip("_-/")
        if len(cleaned) < 4 or cleaned in _STOP_WORDS or cleaned.isdigit():
            continue
        # Light prefix stem for Russian/English inflections without external deps.
        stem = cleaned[:6] if len(cleaned) >= 7 else cleaned
        terms.add(stem)
    return terms


def build_semantic_focus_signals(
    document: ParsedDocument,
    allowed_blocks: set[str],
    *,
    allowed_categories: set[str] | None = None,
) -> list[str]:
    """Produce compact generalized cross-section semantic focus hints without hardcoding any sample."""
    hits_by_category: dict[str, list[str]] = defaultdict(list)
    sections_by_category: dict[str, set[str]] = defaultdict(set)
    for block in document.version.blocks:
        if block.block_id not in allowed_blocks:
            continue
        for category, pattern in _SEMANTIC_FOCUS_PATTERNS:
            if allowed_categories is not None and category not in allowed_categories:
                continue
            if pattern.search(block.text):
                hits_by_category[category].append(block.block_id)
                sections_by_category[category].add(block.location.section)

    signals: list[str] = []
    for category, _ in _SEMANTIC_FOCUS_PATTERNS:
        block_ids = hits_by_category.get(category)
        if not block_ids:
            continue
        # Emit focus hint when >= 2 blocks (or API contract markers) warrant cross-checking.
        if len(block_ids) >= 2 or (category == "api_consistency" and len(block_ids) >= 1):
            preview = ", ".join(block_ids[:10])
            if len(block_ids) > 10:
                preview += f" (+{len(block_ids) - 10})"
            signals.append(
                f"semantic_focus:{category} ({len(sections_by_category[category])} sections) @ [{preview}]"
            )
    return signals


def _has_independent_anchors(refs: list[EvidenceRef]) -> bool:
    grounded = [ref for ref in refs if ref.grounded]
    return any(
        a.block_id != b.block_id or a.end <= b.start or b.end <= a.start
        for idx, a in enumerate(grounded)
        for b in grounded[idx + 1 :]
    )


def _estimate_tokens(text: str) -> int:
    return max(1, (len(text) + 2) // 3)


def build_cross_section_groups(
    requirements: list[Requirement],
    document: ParsedDocument,
    *,
    max_requirements_per_group: int = 80,
    max_passages_per_group: int = 120,
    max_cross_groups: int = 8,
    max_whole_spec_tokens: int = 24_000,
) -> list[CrossSectionGroup]:
    """Index entities/endpoints/conditions and build cross-section comparison groups."""
    if not requirements:
        return []

    block_order = {b.block_id: idx for idx, b in enumerate(document.version.blocks)}
    block_section = {b.block_id: b.location.section for b in document.version.blocks}

    # Whole-specification optimization: keep small/medium documents in one full-visibility pass
    # so cross-section contradictions and gaps see the entire document with zero artificial scope splits.
    unique_blocks = sorted(
        {r.source.block_id for r in requirements if r.source.block_id in block_order},
        key=lambda bid: block_order[bid],
    )
    est_doc_tokens = _estimate_tokens("".join(b.text for b in document.version.blocks)) + sum(
        _estimate_tokens(r.summary) + _estimate_tokens(r.source.quote) for r in requirements
    )
    if (
        len(requirements) <= max_requirements_per_group
        and len(unique_blocks) <= max_passages_per_group
        and est_doc_tokens <= max_whole_spec_tokens
    ):
        whole_groups = [
            CrossSectionGroup(
                group_id="whole-spec",
                topic_keywords=("full-document",),
                requirement_ids=tuple(r.req_id for r in requirements),
                block_ids=tuple(unique_blocks),
            )
        ]
        # For non-trivial specs that mix functional/business rules with acceptance/interface rules,
        # add a dedicated focused cross-rule consistency pass (runs in parallel with whole-spec)
        # so acceptance-vs-rule and pairwise rule contradictions are not diluted by API/schema tables.
        core_categories = (
            RequirementCategory.BUSINESS,
            RequirementCategory.FUNCTIONAL,
            RequirementCategory.QUALITY,
            RequirementCategory.DATA,
        )
        core_reqs = [r for r in requirements if r.category in core_categories]
        acceptance_reqs = [r for r in requirements if r.category == RequirementCategory.ACCEPTANCE]
        if len(requirements) >= 8 and len(core_reqs) >= 2 and acceptance_reqs:
            focused_reqs = [
                r for r in requirements if r.category in (*core_categories, RequirementCategory.ACCEPTANCE)
            ]
            focused_blocks = tuple(
                sorted(
                    {r.source.block_id for r in focused_reqs if r.source.block_id in block_order},
                    key=lambda bid: block_order[bid],
                )
            )
            if len(focused_blocks) < len(unique_blocks):
                whole_groups.append(
                    CrossSectionGroup(
                        group_id="rules-acceptance",
                        topic_keywords=("functional-vs-acceptance", "pairwise-rules"),
                        requirement_ids=tuple(r.req_id for r in focused_reqs),
                        block_ids=focused_blocks,
                    )
                )
        return whole_groups

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

    # Greedily cluster cross-section term matches to keep API call count bounded while preserving distant pairs.
    clusters: list[tuple[list[str], list[str], list[str]]] = []
    seen_req_sets: set[frozenset[str]] = set()
    req_by_id = {r.req_id: r for r in requirements}

    for _, _, term in ranked_terms:
        matched = term_to_reqs[term][:max_requirements_per_group]
        req_ids = list(dict.fromkeys(r.req_id for r in matched))
        req_set = frozenset(req_ids)
        if req_set in seen_req_sets or any(req_set <= existing for existing in seen_req_sets):
            continue

        merged = False
        for keywords, c_reqs, c_blks in clusters:
            combined_reqs = list(dict.fromkeys([*c_reqs, *req_ids]))
            combined_blks = sorted(
                {req_by_id[rid].source.block_id for rid in combined_reqs if req_by_id[rid].source.block_id in block_order},
                key=lambda bid: block_order[bid],
            )
            if len(combined_reqs) <= max_requirements_per_group and len(combined_blks) <= max_passages_per_group:
                if term not in keywords:
                    keywords.append(term)
                c_reqs[:] = combined_reqs
                c_blks[:] = combined_blks
                seen_req_sets.add(frozenset(combined_reqs))
                merged = True
                break
        if not merged and len(clusters) < max(1, max_cross_groups):
            blk_ids = sorted(
                {r.source.block_id for r in matched if r.source.block_id in block_order},
                key=lambda bid: block_order[bid],
            )[:max_passages_per_group]
            clusters.append(([term], req_ids, blk_ids))
            seen_req_sets.add(req_set)

    groups: list[CrossSectionGroup] = []
    covered_reqs: set[str] = set()
    for keywords, req_ids, blk_ids in clusters:
        covered_reqs.update(req_ids)
        groups.append(
            CrossSectionGroup(
                group_id=f"cross-{len(groups) + 1}",
                topic_keywords=tuple(keywords),
                requirement_ids=tuple(req_ids),
                block_ids=tuple(blk_ids),
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


def _contradiction_anchors_overlap(left: list[EvidenceRef], right: list[EvidenceRef]) -> bool:
    """Return True when two contradictions cite the same blocks with overlapping spans on every block."""
    left_by_block: dict[str, list[EvidenceRef]] = defaultdict(list)
    right_by_block: dict[str, list[EvidenceRef]] = defaultdict(list)
    for ref in left:
        left_by_block[ref.block_id].append(ref)
    for ref in right:
        right_by_block[ref.block_id].append(ref)
    if not left_by_block or set(left_by_block) != set(right_by_block):
        return False
    for bid, l_refs in left_by_block.items():
        r_refs = right_by_block[bid]
        for lr in l_refs:
            if not any(lr.start < rr.end and rr.start < lr.end for rr in r_refs):
                return False
        for rr in r_refs:
            if not any(rr.start < lr.end and lr.start < rr.end for lr in l_refs):
                return False
    return True


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
    contradiction_buckets: list[list[Finding]] = []
    for finding in findings:
        if finding.kind == FindingType.CONTRADICTION:
            matched_bucket = None
            for bucket in contradiction_buckets:
                if any(_contradiction_anchors_overlap(finding.evidence, existing.evidence) for existing in bucket):
                    matched_bucket = bucket
                    break
            if matched_bucket is not None:
                matched_bucket.append(finding)
            else:
                contradiction_buckets.append([finding])
            continue
        anchors = tuple(sorted(ref.anchor for ref in finding.evidence))
        if not anchors and finding.gap is not None:
            anchors = (finding.gap.context.anchor,)
        sig = (finding.kind.value, anchors, _normalize_key(finding.claim))
        groups.setdefault(sig, []).append(finding)

    all_buckets: list[list[Finding]] = [*contradiction_buckets, *groups.values()]
    merged_items: list[tuple[tuple[int, int, str], Finding, list[str]]] = []
    for bucket in all_buckets:
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
    estimated_input_tokens: int = 0,
    estimated_output_tokens: int = 0,
) -> StageResult[HolisticAuditOutput]:
    params = inspect.signature(generate_fn).parameters
    accepts_var_kw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())
    kwargs: dict[str, object] = {"settings": settings}
    if accepts_var_kw or "budget" in params:
        kwargs["budget"] = budget
    if accepts_var_kw or "estimated_input_tokens" in params:
        kwargs["estimated_input_tokens"] = estimated_input_tokens
    if accepts_var_kw or "estimated_output_tokens" in params:
        kwargs["estimated_output_tokens"] = estimated_output_tokens
    return generate_fn(
        PipelineStage.HOLISTIC,
        payload,
        HolisticAuditOutput,
        **kwargs,
    )


def _split_group(
    group: CrossSectionGroup,
    req_map: dict[str, Requirement],
    block_order: dict[str, int],
) -> list[CrossSectionGroup] | None:
    if len(group.requirement_ids) <= 1:
        return None
    mid = len(group.requirement_ids) // 2
    parts: list[CrossSectionGroup] = []
    for idx, sub_reqs in enumerate((group.requirement_ids[:mid], group.requirement_ids[mid:]), start=1):
        sub_blks = tuple(
            sorted(
                {req_map[rid].source.block_id for rid in sub_reqs if rid in req_map and req_map[rid].source.block_id in block_order},
                key=lambda bid: block_order[bid],
            )
        )
        parts.append(
            CrossSectionGroup(
                group_id=f"{group.group_id}.{idx}",
                topic_keywords=group.topic_keywords,
                requirement_ids=sub_reqs,
                block_ids=sub_blks,
            )
        )
    return parts


def _format_structural_signals(
    structural_signals: list[Any] | None,
    allowed_blocks: set[str],
    *,
    limit: int = 25,
) -> list[str]:
    if not structural_signals:
        return []
    seen: set[tuple[str, str]] = set()
    formatted: list[str] = []
    for sig in structural_signals:
        src = getattr(sig, "source", None)
        kind = getattr(sig, "kind", None)
        if src is None or kind is None or src.block_id not in allowed_blocks:
            continue
        key = (str(kind), src.block_id)
        if key in seen:
            continue
        seen.add(key)
        snippet = _WHITESPACE_RE.sub(" ", src.quote).strip()[:80]
        formatted.append(f"{kind} @ {src.block_id}: {snippet}")
        if len(formatted) >= limit:
            break
    return formatted


def _execute_holistic_group(
    *,
    group: CrossSectionGroup,
    document: ParsedDocument,
    req_map: dict[str, Requirement],
    block_map: dict[str, Any],
    block_order: dict[str, int],
    all_block_ids: list[str],
    effective_inspected: set[str],
    has_uninspected_scope: bool,
    max_passages_per_group: int,
    structural_signals: list[Any] | None,
    settings: ProviderSettings,
    budget: QuotaBudget | None,
    generate_fn: GenerateFn,
    run_id: str = "unspecified",
) -> tuple[StageResult[HolisticAuditOutput] | None, ProviderError | None, int, set[str], bool]:
    if group.group_id == "whole-spec":
        scope_candidates = sorted(effective_inspected, key=lambda bid: block_order[bid])
        if len(scope_candidates) <= max_passages_per_group:
            selected_passage_ids = scope_candidates
        else:
            req_blks = sorted(effective_inspected & set(group.block_ids), key=lambda bid: block_order[bid])
            selected_passage_ids = req_blks[:max_passages_per_group]
    else:
        selected_passage_ids = sorted(
            effective_inspected & set(group.block_ids) if group.block_ids else effective_inspected,
            key=lambda bid: block_order[bid],
        )[:max_passages_per_group]

    group_inspected_set = set(selected_passage_ids)
    group_has_uninspected_scope = (
        has_uninspected_scope or group_inspected_set != set(all_block_ids)
    )
    passages = [
        HolisticPassageInput(
            block_id=bid,
            section=block_map[bid].location.section,
            text=block_map[bid].text,
        )
        for bid in selected_passage_ids
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
    if group.group_id == "rules-acceptance":
        group_signals = build_semantic_focus_signals(
            document,
            group_inspected_set,
            allowed_categories={"timing_calendar", "state_transitions", "repeated_events", "units_exceptions"},
        )
    else:
        group_signals = [
            *_format_structural_signals(structural_signals, group_inspected_set),
            *build_semantic_focus_signals(document, group_inspected_set),
        ]
    system_prompt = (
        HOLISTIC_LOGIC_SYSTEM_PROMPT
        if group.group_id == "rules-acceptance"
        else HOLISTIC_SYSTEM_PROMPT
    )

    def _make_payload(repair_hint: str | None = None) -> HolisticGroupPayload:
        return HolisticGroupPayload(
            system_instruction=system_prompt,
            prompt_version=HOLISTIC_PROMPT_VERSION,
            document_name=document.version.name,
            group_id=group.group_id,
            topic_keywords=list(group.topic_keywords),
            criteria_catalog=[c.value for c in _WHOLE_SET_ORDERED],
            inspected_block_ids=selected_passage_ids,
            has_uninspected_scope=group_has_uninspected_scope,
            structural_signals=group_signals,
            requirements=req_inputs,
            passages=passages,
            repair_hint=repair_hint,
        )

    payload = _make_payload()
    est_in = _estimate_tokens(payload.model_dump_json(exclude={"system_instruction"}, exclude_none=True))
    est_out = 2600
    attempts = 0
    for repair_attempt in range(2):
        try:
            stage_result = _call_provider(
                generate_fn,
                payload,
                settings=settings,
                budget=budget,
                estimated_input_tokens=est_in,
                estimated_output_tokens=est_out,
            )
            attempts += stage_result.attempts
            return stage_result, None, attempts, group_inspected_set, group_has_uninspected_scope
        except ProviderError as exc:
            attempts += max(exc.attempts, 0)
            if exc.action == FailureAction.REPAIR and repair_attempt == 0:
                payload = _make_payload(repair_hint=HOLISTIC_REPAIR_HINT)
                continue
            return None, exc, attempts, group_inspected_set, group_has_uninspected_scope
        except Exception as exc:
            log_safe_exception(
                logger,
                "holistic_group_unexpected_error",
                exc,
                run_id=run_id,
                stage=PipelineStage.HOLISTIC.value,
            )
            return (
                None,
                ProviderError(
                    f"Внутренняя ошибка группы ({type(exc).__name__})",
                    reason=FailureReason.API_ERROR,
                    action=FailureAction.STOP,
                    attempts=max(attempts, 0),
                ),
                attempts,
                group_inspected_set,
                group_has_uninspected_scope,
            )
    return None, None, attempts, group_inspected_set, group_has_uninspected_scope


def audit_holistic_set(
    requirements: list[Requirement],
    document: ParsedDocument,
    settings: ProviderSettings,
    *,
    inspected_blocks: set[str] | list[str] | None = None,
    structural_signals: list[Any] | None = None,
    budget: QuotaBudget | None = None,
    generate_fn: GenerateFn = generate,
    ground_fn: GroundFn = ground,
    max_requirements_per_group: int = 80,
    max_passages_per_group: int = 120,
    finding_id_start: int = 1,
    max_concurrency: int = 1,
    run_id: str | None = None,
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
        max_passages_per_group=max_passages_per_group,
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
    workers = max(1, max_concurrency)
    diag_run_id = run_id or "unspecified"

    def _consume_group_output(
        group: CrossSectionGroup,
        output: HolisticAuditOutput,
        group_inspected_set: set[str],
        group_has_uninspected_scope: bool,
    ) -> None:
        nonlocal temp_finding_seq
        group_clean = True
        local_key_to_draft: dict[str, _DraftHolisticFinding] = {}

        for fc in output.findings:
            if fc.local_key in local_key_to_draft:
                incomplete_reasons.append(f"Группа {group.group_id}: дубликат local_key {fc.local_key}.")
                group_clean = False
                continue
            draft, rejection, uncertainty_note = _build_draft_finding(
                fc,
                document,
                valid_req_ids,
                group_inspected_set,
                group_has_uninspected_scope,
                ground_fn,
            )
            if uncertainty_note:
                uncertainties.append(uncertainty_note)
            if rejection or draft is None:
                uncertainties.append(rejection or f"Группа {group.group_id}: отклонена находка {fc.local_key}.")
                continue
            local_key_to_draft[fc.local_key] = draft

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

        cited_criteria_by_key: dict[str, set[QualityCriterion]] = defaultdict(set)
        for crit, ev in seen_group_criteria.items():
            if ev.status != CriterionStatus.FAIL:
                continue
            for k in ev.local_finding_keys:
                if k in local_key_to_draft:
                    cited_criteria_by_key[k].add(crit)

        local_key_to_temp_id: dict[str, str] = {}
        for key, draft in local_key_to_draft.items():
            synced_criteria = cited_criteria_by_key.get(key)
            if not synced_criteria:
                uncertainties.append(
                    f"Группа {group.group_id}: находка {key} не привязана ни к одному критерию со статусом fail и исключена."
                )
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
                criteria=sorted(synced_criteria, key=lambda c: c.value),
                evidence=draft.evidence,
                gap=draft.gap,
                semantic_status=draft.semantic_status,
                verification_reason=draft.verification_reason,
            )
            local_key_to_temp_id[key] = temp_id
            raw_findings.append(finding)

        for crit, ev in seen_group_criteria.items():
            resolved_ids = [
                local_key_to_temp_id[k]
                for k in ev.local_finding_keys
                if k in local_key_to_temp_id
            ]
            if ev.status == CriterionStatus.FAIL and not resolved_ids:
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

    pending_wave = list(groups)
    stop_all = False
    while pending_wave and not stop_all:
        if workers <= 1 or len(pending_wave) <= 1:
            q_idx = 0
            while q_idx < len(pending_wave):
                group = pending_wave[q_idx]
                q_idx += 1
                stage_result, exc, grp_attempts, grp_inspected, grp_has_uninspected = _execute_holistic_group(
                    group=group,
                    document=document,
                    req_map=req_map,
                    block_map=block_map,
                    block_order=block_order,
                    all_block_ids=all_block_ids,
                    effective_inspected=effective_inspected,
                    has_uninspected_scope=has_uninspected_scope,
                    max_passages_per_group=max_passages_per_group,
                    structural_signals=structural_signals,
                    settings=settings,
                    budget=budget,
                    generate_fn=generate_fn,
                    run_id=diag_run_id,
                )
                total_attempts += grp_attempts
                if exc is not None:
                    if exc.action == FailureAction.SPLIT:
                        sub_groups = _split_group(group, req_map, block_order)
                        if sub_groups is not None:
                            planned_groups.extend(sg.group_id for sg in sub_groups)
                            pending_wave[q_idx:q_idx] = sub_groups
                            completed_groups.append(group.group_id)
                            continue
                    incomplete_reasons.append(f"Группа {group.group_id} целостного аудита не завершена: {exc}.")
                    if exc.reason in _STOP_ALL_REASONS:
                        stop_all = True
                        break
                    continue
                if stage_result is None:
                    continue
                if stage_result.input_tokens is not None and stage_result.output_tokens is not None:
                    known_in += stage_result.input_tokens
                    known_out += stage_result.output_tokens
                    has_known_usage = True
                _consume_group_output(group, stage_result.output, grp_inspected, grp_has_uninspected)
            break

        next_wave: list[CrossSectionGroup] = []
        with ThreadPoolExecutor(max_workers=min(workers, len(pending_wave))) as pool:
            futures = [
                (
                    g,
                    pool.submit(
                        _execute_holistic_group,
                        group=g,
                        document=document,
                        req_map=req_map,
                        block_map=block_map,
                        block_order=block_order,
                        all_block_ids=all_block_ids,
                        effective_inspected=effective_inspected,
                        has_uninspected_scope=has_uninspected_scope,
                        max_passages_per_group=max_passages_per_group,
                        structural_signals=structural_signals,
                        settings=settings,
                        budget=budget,
                        generate_fn=generate_fn,
                        run_id=diag_run_id,
                    ),
                )
                for g in pending_wave
            ]
            wave_results = [(g, *fut.result()) for g, fut in futures]

        for group, stage_result, exc, grp_attempts, grp_inspected, grp_has_uninspected in wave_results:
            total_attempts += grp_attempts
            if exc is not None:
                if exc.action == FailureAction.SPLIT:
                    sub_groups = _split_group(group, req_map, block_order)
                    if sub_groups is not None:
                        planned_groups.extend(sg.group_id for sg in sub_groups)
                        next_wave.extend(sub_groups)
                        completed_groups.append(group.group_id)
                        continue
                incomplete_reasons.append(f"Группа {group.group_id} целостного аудита не завершена: {exc}.")
                if exc.reason in _STOP_ALL_REASONS:
                    stop_all = True
                continue
            if stage_result is None:
                continue
            if stage_result.input_tokens is not None and stage_result.output_tokens is not None:
                known_in += stage_result.input_tokens
                known_out += stage_result.output_tokens
                has_known_usage = True
            _consume_group_output(group, stage_result.output, grp_inspected, grp_has_uninspected)
        pending_wave = next_wave

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
