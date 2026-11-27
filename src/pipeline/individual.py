"""Individual requirement auditing against the 9 ISO/IEC/IEEE 29148:2018 criteria."""
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import inspect
import re

from pydantic import Field

from src.contracts import (
    INDIVIDUAL,
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
from src.prompts.individual import (
    INDIVIDUAL_PROMPT_VERSION,
    INDIVIDUAL_REPAIR_HINT,
    INDIVIDUAL_SYSTEM_PROMPT,
)
from src.provider import FailureAction, FailureReason, ProviderError, QuotaBudget, generate
from src.provider.dto import (
    IndividualAuditOutput,
    IndividualAuditPayload,
    IndividualCriterionCandidate,
    IndividualFindingCandidate,
    IndividualRequirementInput,
)

GenerateFn = Callable[..., StageResult[IndividualAuditOutput]]
GroundFn = Callable[[EvidenceCandidate, ParsedDocument], GroundingResult]

_HEADROOM_FACTOR = 0.8
_INDIVIDUAL_ORDERED = tuple(sorted(INDIVIDUAL, key=lambda c: c.value))
_WHITESPACE_RE = re.compile(r"\s+")
_STOP_ALL_REASONS = frozenset(
    {
        FailureReason.AUTH_ERROR,
        FailureReason.MODEL_UNAVAILABLE,
        FailureReason.DAILY_QUOTA_EXHAUSTED,
        FailureReason.BUDGET_EXHAUSTED,
    }
)


class IndividualAuditResult(Contract):
    evaluations: list[CriterionEvaluation]
    findings: list[Finding]
    individual_requirements: WorkSet
    audit_batches: WorkSet
    incomplete_reasons: list[Text] = Field(default_factory=list)
    uncertainties: list[Text] = Field(default_factory=list)
    attempts: Count = 0
    input_tokens: Count | None = None
    output_tokens: Count | None = None


@dataclass(frozen=True)
class _AuditBatch:
    batch_id: str
    requirements: tuple[Requirement, ...]


@dataclass
class _DraftFinding:
    requirement_id: str
    kind: FindingType
    severity: Severity
    claim: str
    recommendation: str
    criteria: set[QualityCriterion]
    evidence: list[EvidenceRef]
    gap: GapEvidence | None
    semantic_status: SemanticStatus
    verification_reason: str


def _estimate_tokens(text: str) -> int:
    return max(1, (len(text) + 2) // 3)


def _normalize_text_key(text: str) -> str:
    return _WHITESPACE_RE.sub(" ", text.strip().lower())


def plan_individual_batches(
    requirements: list[Requirement],
    document: ParsedDocument,
    *,
    max_input_tokens: int = 24_000,
    max_output_tokens: int = 12_000,
    max_requirements_per_batch: int = 10,
) -> list[_AuditBatch]:
    """Pack requirements into input/output-budgeted batches with 20% headroom."""
    if max_input_tokens < 200 or max_output_tokens < 200 or max_requirements_per_batch < 1:
        raise ValueError("Invalid individual audit batch limits")

    blocks_by_id = {b.block_id: b for b in document.version.blocks}
    prompt_overhead = _estimate_tokens(INDIVIDUAL_SYSTEM_PROMPT) + 150
    input_cap = max(128, int(max_input_tokens * _HEADROOM_FACTOR) - prompt_overhead)
    output_cap = max(256, int(max_output_tokens * _HEADROOM_FACTOR))
    # 9 concise evaluations + candidate findings per requirement in Russian JSON.
    est_output_per_req = 300

    batches: list[_AuditBatch] = []
    current: list[Requirement] = []
    current_in = 0
    current_out = 0

    for req in requirements:
        blk = blocks_by_id.get(req.source.block_id)
        block_text = blk.text if blk else req.source.quote
        req_in = _estimate_tokens(req.summary) + _estimate_tokens(req.source.quote) + _estimate_tokens(block_text) + 60
        if current and (
            len(current) >= max_requirements_per_batch
            or current_in + req_in > input_cap
            or current_out + est_output_per_req > output_cap
        ):
            batches.append(_AuditBatch(batch_id=f"indiv-{len(batches) + 1}", requirements=tuple(current)))
            current = [req]
            current_in = req_in
            current_out = est_output_per_req
        else:
            current.append(req)
            current_in += req_in
            current_out += est_output_per_req

    if current:
        batches.append(_AuditBatch(batch_id=f"indiv-{len(batches) + 1}", requirements=tuple(current)))
    return batches


def _build_payload(
    batch: _AuditBatch,
    document: ParsedDocument,
    *,
    repair_hint: str | None = None,
) -> IndividualAuditPayload:
    blocks_by_id = {b.block_id: b for b in document.version.blocks}
    req_inputs: list[IndividualRequirementInput] = []
    for req in batch.requirements:
        blk = blocks_by_id.get(req.source.block_id)
        req_inputs.append(
            IndividualRequirementInput(
                req_id=req.req_id,
                original_id=req.original_id,
                category=req.category.value,
                summary=req.summary,
                block_id=req.source.block_id,
                section=blk.location.section if blk else "Неизвестный раздел",
                quote=req.source.quote,
                block_text=blk.text if blk else req.source.quote,
            )
        )
    return IndividualAuditPayload(
        system_instruction=INDIVIDUAL_SYSTEM_PROMPT,
        prompt_version=INDIVIDUAL_PROMPT_VERSION,
        document_name=document.version.name,
        batch_id=batch.batch_id,
        target_req_ids=[r.req_id for r in batch.requirements],
        criteria_catalog=[c.value for c in _INDIVIDUAL_ORDERED],
        requirements=req_inputs,
        repair_hint=repair_hint,
    )


def _call_provider(
    generate_fn: GenerateFn,
    payload: IndividualAuditPayload,
    *,
    settings: ProviderSettings,
    budget: QuotaBudget | None,
    estimated_input_tokens: int,
    estimated_output_tokens: int,
) -> StageResult[IndividualAuditOutput]:
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
        PipelineStage.INDIVIDUAL,
        payload,
        IndividualAuditOutput,
        **kwargs,
    )


def _ground_finding_candidate(
    candidate: IndividualFindingCandidate,
    requirement: Requirement,
    document: ParsedDocument,
    ground_fn: GroundFn,
    *,
    allowed_block_ids: set[str],
) -> tuple[_DraftFinding | None, str | None, bool]:
    """Ground one individual candidate.

    Returns (draft_or_none, error_message_or_none, is_soft_contradiction_rejection).
    """
    if any(c not in INDIVIDUAL for c in candidate.criteria):
        return None, f"Находка {candidate.local_key} ({requirement.req_id}): критерий вне индивидуального каталога ISO.", False

    claim = candidate.claim.strip()
    recommendation = candidate.recommendation.strip()
    if not claim or not recommendation:
        return None, f"Находка {candidate.local_key} ({requirement.req_id}): пустое описание дефекта или рекомендации.", False

    grounded_refs: list[EvidenceRef] = []
    seen_anchors: set[tuple[str, str, int, int]] = set()
    for ev in candidate.evidence:
        if ev.block_id not in allowed_block_ids:
            return None, (
                f"Находка {candidate.local_key} ({requirement.req_id}): ссылка на неизвестный или непроверенный блок {ev.block_id}."
            ), False
        if not ev.quote.strip():
            return None, f"Находка {candidate.local_key} ({requirement.req_id}): пустая цитата доказательства.", False
        result = ground_fn(
            EvidenceCandidate(
                version=document.version.sha256,
                block_id=ev.block_id,
                quote=ev.quote,
            ),
            document,
        )
        if not result.reference or not result.reference.grounded:
            return None, (
                f"Находка {candidate.local_key} ({requirement.req_id}): цитата не подтверждена "
                f"({result.status.value}: {result.reason})."
            ), False
        if result.reference.anchor not in seen_anchors:
            seen_anchors.add(result.reference.anchor)
            grounded_refs.append(result.reference)

    if not grounded_refs:
        return None, f"Находка {candidate.local_key} ({requirement.req_id}): отсутствуют подтверждённые ссылки.", False

    if candidate.kind == FindingType.CONTRADICTION:
        has_distinct = any(
            a.block_id != b.block_id or a.end <= b.start or b.end <= a.start
            for i, a in enumerate(grounded_refs)
            for b in grounded_refs[i + 1 :]
        )
        if not has_distinct:
            # Grounded single quote cannot form a contradiction; isolate cleanly without
            # fabricating a 2nd quote or discarding the other 8 valid criterion evaluations.
            return None, (
                f"Находка {candidate.local_key} ({requirement.req_id}): одноцитатное противоречие отклонено в индивидуальном аудите "
                "(требуются две независимые подтверждённые цитаты; межтребовательные конфликты проверяются в целостном аудите)."
            ), True

    gap: GapEvidence | None = None
    if candidate.kind == FindingType.MISSING_INFORMATION:
        expected = (candidate.expected_information or "").strip() or claim
        context_ref = grounded_refs[0]
        inspected = list(dict.fromkeys([context_ref.block_id, requirement.source.block_id]))
        gap = GapEvidence(
            expected_information=expected,
            context=context_ref,
            inspected_blocks=inspected,
        )

    if candidate.severity in (Severity.CRITICAL, Severity.MAJOR):
        semantic_status = SemanticStatus.UNCHECKED
        verification_reason = "Ожидает отдельной семантической проверки контекста и исключений."
    else:
        semantic_status = SemanticStatus.SUPPORTED
        verification_reason = "Подтверждено дословной ссылкой в индивидуальном аудите (minor)."

    return (
        _DraftFinding(
            requirement_id=requirement.req_id,
            kind=candidate.kind,
            severity=candidate.severity,
            claim=claim,
            recommendation=recommendation,
            criteria=set(candidate.criteria),
            evidence=grounded_refs,
            gap=gap,
            semantic_status=semantic_status,
            verification_reason=verification_reason,
        ),
        None,
        False,
    )


def _dedup_signature(draft: _DraftFinding) -> tuple[str, str, str, tuple[tuple[str, str, int, int], ...]]:
    anchors = tuple(sorted(ref.anchor for ref in draft.evidence))
    return (draft.requirement_id, draft.kind.value, _normalize_text_key(draft.claim), anchors)


def _execute_individual_batch(
    batch: _AuditBatch,
    document: ParsedDocument,
    *,
    settings: ProviderSettings,
    budget: QuotaBudget | None,
    generate_fn: GenerateFn,
) -> tuple[StageResult[IndividualAuditOutput] | None, ProviderError | None, int]:
    payload = _build_payload(batch, document)
    est_in = _estimate_tokens(payload.model_dump_json(exclude={"system_instruction"}, exclude_none=True))
    est_out = len(batch.requirements) * 300
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
            return stage_result, None, attempts
        except ProviderError as exc:
            attempts += max(exc.attempts, 0)
            if exc.action == FailureAction.REPAIR and repair_attempt == 0:
                payload = _build_payload(batch, document, repair_hint=INDIVIDUAL_REPAIR_HINT)
                continue
            return None, exc, attempts
    return None, None, attempts


def _consume_individual_output(
    *,
    batch: _AuditBatch,
    output: IndividualAuditOutput,
    document: ParsedDocument,
    ground_fn: GroundFn,
    allowed_block_ids: set[str],
    accepted_evals_by_req: dict[str, list[tuple[QualityCriterion, CriterionStatus, str, list[tuple[str, str, str, tuple[tuple[str, str, int, int], ...]]]]]],
    completed_req_ids: list[str],
    drafts_by_sig: dict[tuple[str, str, str, tuple[tuple[str, str, int, int], ...]], _DraftFinding],
    completed_batches: list[str],
    incomplete_reasons: list[str],
    uncertainties: list[str],
) -> None:
    target_ids = [r.req_id for r in batch.requirements]
    target_set = set(target_ids)
    evals_for_req: dict[str, list[IndividualCriterionCandidate]] = {rid: [] for rid in target_ids}
    findings_for_req: dict[str, dict[str, IndividualFindingCandidate]] = {rid: {} for rid in target_ids}
    req_errors: dict[str, list[str]] = {rid: [] for rid in target_ids}

    for ev in output.evaluations:
        if ev.requirement_id not in target_set:
            uncertainties.append(
                f"Пакет {batch.batch_id}: модель вернула оценку для чужого ID {ev.requirement_id}."
            )
            continue
        evals_for_req[ev.requirement_id].append(ev)

    for fc in output.findings:
        if fc.requirement_id not in target_set:
            uncertainties.append(
                f"Пакет {batch.batch_id}: модель вернула находку для чужого ID {fc.requirement_id}."
            )
            continue
        if fc.local_key in findings_for_req[fc.requirement_id]:
            req_errors[fc.requirement_id].append(
                f"Требование {fc.requirement_id}: дубликат local_key {fc.local_key} в пакете {batch.batch_id}."
            )
            continue
        findings_for_req[fc.requirement_id][fc.local_key] = fc

    batch_clean = True
    for req in batch.requirements:
        rid = req.req_id
        raw_evals = evals_for_req[rid]
        seen_criteria: dict[QualityCriterion, IndividualCriterionCandidate] = {}

        for ev in raw_evals:
            if ev.criterion not in INDIVIDUAL:
                req_errors[rid].append(
                    f"Требование {rid}: критерий {ev.criterion.value} не относится к индивидуальному аудиту."
                )
                continue
            if ev.criterion in seen_criteria:
                req_errors[rid].append(
                    f"Требование {rid}: повторная оценка критерия {ev.criterion.value}."
                )
                continue
            if not ev.rationale.strip():
                req_errors[rid].append(
                    f"Требование {rid}: отсутствует обоснование для критерия {ev.criterion.value}."
                )
                continue
            if ev.status == CriterionStatus.FAIL and not ev.local_finding_keys:
                req_errors[rid].append(
                    f"Требование {rid}: статус fail для {ev.criterion.value} без привязанной находки."
                )
                continue
            if ev.status != CriterionStatus.FAIL and ev.local_finding_keys:
                req_errors[rid].append(
                    f"Требование {rid}: статус {ev.status.value} для {ev.criterion.value} не может ссылаться на находки."
                )
                continue
            seen_criteria[ev.criterion] = ev

        missing_criteria = [c.value for c in _INDIVIDUAL_ORDERED if c not in seen_criteria]
        if missing_criteria:
            req_errors[rid].append(
                f"Требование {rid}: пропущены обязательные критерии ({', '.join(missing_criteria)})."
            )

        referenced_keys = {
            key
            for ev in seen_criteria.values()
            if ev.status == CriterionStatus.FAIL
            for key in ev.local_finding_keys
        }
        grounded_by_key: dict[str, _DraftFinding] = {}
        soft_rejected_keys: set[str] = set()
        for key in referenced_keys:
            fc = findings_for_req[rid].get(key)
            if fc is None:
                req_errors[rid].append(
                    f"Требование {rid}: ссылка на отсутствующую находку {key}."
                )
                continue
            draft, err_msg, soft_contradiction = _ground_finding_candidate(
                fc,
                req,
                document,
                ground_fn,
                allowed_block_ids=allowed_block_ids,
            )
            if soft_contradiction:
                soft_rejected_keys.add(key)
                if err_msg:
                    uncertainties.append(err_msg)
                continue
            if err_msg is not None or draft is None:
                req_errors[rid].append(err_msg or f"Требование {rid}: невалидная находка {key}.")
                continue
            grounded_by_key[key] = draft

        cited_criteria_by_key: dict[str, set[QualityCriterion]] = {}
        for crit, ev in seen_criteria.items():
            if ev.status != CriterionStatus.FAIL:
                continue
            for key in ev.local_finding_keys:
                if key in grounded_by_key:
                    cited_criteria_by_key.setdefault(key, set()).add(crit)
        for key, draft in grounded_by_key.items():
            draft.criteria = cited_criteria_by_key.get(key, set())

        if req_errors[rid]:
            batch_clean = False
            incomplete_reasons.extend(req_errors[rid])
            continue

        req_eval_entries = []
        for crit in _INDIVIDUAL_ORDERED:
            ev = seen_criteria[crit]
            sigs: list[tuple[str, str, str, tuple[tuple[str, str, int, int], ...]]] = []
            eff_status = ev.status
            eff_rationale = ev.rationale.strip()
            if ev.status == CriterionStatus.FAIL:
                valid_keys = [k for k in ev.local_finding_keys if k in grounded_by_key]
                if not valid_keys and any(k in soft_rejected_keys for k in ev.local_finding_keys):
                    eff_status = CriterionStatus.UNKNOWN
                    eff_rationale = (
                        f"{eff_rationale} (Одноцитатное противоречие отклонено; "
                        "межтребовательная согласованность оценивается в целостном аудите.)"
                    )
                else:
                    for key in valid_keys:
                        draft = grounded_by_key[key]
                        sig = _dedup_signature(draft)
                        if sig in drafts_by_sig:
                            existing = drafts_by_sig[sig]
                            existing.criteria.update(draft.criteria)
                            if (
                                draft.severity == Severity.CRITICAL
                                or (draft.severity == Severity.MAJOR and existing.severity == Severity.MINOR)
                            ):
                                existing.severity = draft.severity
                                existing.semantic_status = SemanticStatus.UNCHECKED
                                existing.verification_reason = (
                                    "Ожидает отдельной семантической проверки контекста и исключений."
                                )
                        else:
                            drafts_by_sig[sig] = draft
                        if sig not in sigs:
                            sigs.append(sig)
            req_eval_entries.append((crit, eff_status, eff_rationale, sigs))

        accepted_evals_by_req[rid] = req_eval_entries
        completed_req_ids.append(rid)

    if batch_clean:
        completed_batches.append(batch.batch_id)


def audit_individual_requirements(
    requirements: list[Requirement],
    document: ParsedDocument,
    settings: ProviderSettings,
    *,
    inspected_blocks: set[str] | list[str] | None = None,
    budget: QuotaBudget | None = None,
    generate_fn: GenerateFn = generate,
    ground_fn: GroundFn = ground,
    max_input_tokens: int = 24_000,
    max_output_tokens: int = 12_000,
    max_requirements_per_batch: int = 10,
    finding_id_start: int = 1,
    max_concurrency: int = 1,
) -> IndividualAuditResult:
    """Audit each requirement against all 9 individual ISO criteria with strict grounding."""
    planned_reqs = [req.req_id for req in requirements]
    req_order = {req_id: idx for idx, req_id in enumerate(planned_reqs)}
    all_block_ids = {b.block_id for b in document.version.blocks}
    allowed_block_ids = (
        all_block_ids
        if inspected_blocks is None
        else ((set(inspected_blocks) & all_block_ids) | {r.source.block_id for r in requirements if r.source.block_id in all_block_ids})
    )

    if not planned_reqs:
        return IndividualAuditResult(
            evaluations=[],
            findings=[],
            individual_requirements=WorkSet(planned=[], completed=[]),
            audit_batches=WorkSet(planned=[], completed=[]),
            incomplete_reasons=["Нет требований для индивидуального аудита."],
        )

    queue = plan_individual_batches(
        requirements,
        document,
        max_input_tokens=max_input_tokens,
        max_output_tokens=max_output_tokens,
        max_requirements_per_batch=max_requirements_per_batch,
    )
    planned_batches: list[str] = [b.batch_id for b in queue]
    completed_batches: list[str] = []

    completed_req_ids: list[str] = []
    accepted_evals_by_req: dict[str, list[tuple[QualityCriterion, CriterionStatus, str, list[tuple[str, str, str, tuple[tuple[str, str, int, int], ...]]]]]] = {}
    drafts_by_sig: dict[tuple[str, str, str, tuple[tuple[str, str, int, int], ...]], _DraftFinding] = {}

    incomplete_reasons: list[str] = []
    uncertainties: list[str] = []
    total_attempts = 0
    known_in = 0
    known_out = 0
    has_known_usage = False
    workers = max(1, max_concurrency)

    pending_wave = list(queue)
    stop_all = False
    while pending_wave and not stop_all:
        if workers <= 1:
            idx = 0
            while idx < len(pending_wave):
                batch = pending_wave[idx]
                idx += 1
                stage_result, exc, batch_attempts = _execute_individual_batch(
                    batch, document, settings=settings, budget=budget, generate_fn=generate_fn
                )
                total_attempts += batch_attempts
                if exc is not None:
                    if exc.action == FailureAction.SPLIT and len(batch.requirements) > 1:
                        mid = len(batch.requirements) // 2
                        left = _AuditBatch(batch_id=f"{batch.batch_id}.1", requirements=batch.requirements[:mid])
                        right = _AuditBatch(batch_id=f"{batch.batch_id}.2", requirements=batch.requirements[mid:])
                        planned_batches.extend([left.batch_id, right.batch_id])
                        pending_wave[idx:idx] = [left, right]
                        completed_batches.append(batch.batch_id)
                        continue
                    incomplete_reasons.append(f"Пакет {batch.batch_id} индивидуального аудита не завершён: {exc}.")
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
                _consume_individual_output(
                    batch=batch,
                    output=stage_result.output,
                    document=document,
                    ground_fn=ground_fn,
                    allowed_block_ids=allowed_block_ids,
                    accepted_evals_by_req=accepted_evals_by_req,
                    completed_req_ids=completed_req_ids,
                    drafts_by_sig=drafts_by_sig,
                    completed_batches=completed_batches,
                    incomplete_reasons=incomplete_reasons,
                    uncertainties=uncertainties,
                )
            break

        next_wave: list[_AuditBatch] = []
        with ThreadPoolExecutor(max_workers=min(workers, len(pending_wave))) as pool:
            futures = [
                (
                    b,
                    pool.submit(
                        _execute_individual_batch,
                        b,
                        document,
                        settings=settings,
                        budget=budget,
                        generate_fn=generate_fn,
                    ),
                )
                for b in pending_wave
            ]
            wave_results = [(b, *fut.result()) for b, fut in futures]

        for batch, stage_result, exc, batch_attempts in wave_results:
            total_attempts += batch_attempts
            if exc is not None:
                if exc.action == FailureAction.SPLIT and len(batch.requirements) > 1:
                    mid = len(batch.requirements) // 2
                    left = _AuditBatch(batch_id=f"{batch.batch_id}.1", requirements=batch.requirements[:mid])
                    right = _AuditBatch(batch_id=f"{batch.batch_id}.2", requirements=batch.requirements[mid:])
                    planned_batches.extend([left.batch_id, right.batch_id])
                    next_wave.extend([left, right])
                    completed_batches.append(batch.batch_id)
                    continue
                incomplete_reasons.append(f"Пакет {batch.batch_id} индивидуального аудита не завершён: {exc}.")
                if exc.reason in _STOP_ALL_REASONS:
                    stop_all = True
                continue
            if stage_result is None:
                continue
            if stage_result.input_tokens is not None and stage_result.output_tokens is not None:
                known_in += stage_result.input_tokens
                known_out += stage_result.output_tokens
                has_known_usage = True
            _consume_individual_output(
                batch=batch,
                output=stage_result.output,
                document=document,
                ground_fn=ground_fn,
                allowed_block_ids=allowed_block_ids,
                accepted_evals_by_req=accepted_evals_by_req,
                completed_req_ids=completed_req_ids,
                drafts_by_sig=drafts_by_sig,
                completed_batches=completed_batches,
                incomplete_reasons=incomplete_reasons,
                uncertainties=uncertainties,
            )
        pending_wave = next_wave

    # Keep completed_req_ids in deterministic planned source order regardless of batch completion order.
    completed_set = set(completed_req_ids)
    ordered_completed_reqs = [rid for rid in planned_reqs if rid in completed_set]

    # Assign deterministic finding IDs in source requirement + anchor order.
    ordered_sigs = sorted(
        drafts_by_sig.keys(),
        key=lambda sig: (req_order[sig[0]], sig[3], sig[1], sig[2]),
    )
    sig_to_fid: dict[tuple[str, str, str, tuple[tuple[str, str, int, int], ...]], str] = {}
    findings: list[Finding] = []
    for offset, sig in enumerate(ordered_sigs):
        fid = f"F{finding_id_start + offset:03d}"
        sig_to_fid[sig] = fid
        draft = drafts_by_sig[sig]
        findings.append(
            Finding(
                finding_id=fid,
                kind=draft.kind,
                severity=draft.severity,
                claim=draft.claim,
                recommendation=draft.recommendation,
                requirement_ids=[draft.requirement_id],
                criteria=sorted(draft.criteria, key=lambda c: c.value),
                evidence=draft.evidence,
                gap=draft.gap,
                semantic_status=draft.semantic_status,
                verification_reason=draft.verification_reason,
            )
        )

    evaluations: list[CriterionEvaluation] = []
    for rid in planned_reqs:
        if rid not in accepted_evals_by_req:
            continue
        for crit, status, rationale, sigs in accepted_evals_by_req[rid]:
            fids = [sig_to_fid[sig] for sig in sigs]
            evaluations.append(
                CriterionEvaluation(
                    requirement_id=rid,
                    criterion=crit,
                    status=status,
                    rationale=rationale,
                    finding_ids=fids,
                )
            )

    return IndividualAuditResult(
        evaluations=evaluations,
        findings=findings,
        individual_requirements=WorkSet(planned=planned_reqs, completed=ordered_completed_reqs),
        audit_batches=WorkSet(planned=planned_batches, completed=completed_batches),
        incomplete_reasons=list(dict.fromkeys(incomplete_reasons)),
        uncertainties=list(dict.fromkeys(uncertainties)),
        attempts=total_attempts,
        input_tokens=known_in if has_known_usage else None,
        output_tokens=known_out if has_known_usage else None,
    )
