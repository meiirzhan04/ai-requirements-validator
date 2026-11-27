"""Independent semantic verification pass for major and critical finding candidates."""
from collections.abc import Callable
import inspect

from pydantic import Field

from src.contracts import (
    CriterionEvaluation,
    CriterionStatus,
    Finding,
    FindingType,
    ParsedDocument,
    PipelineStage,
    ProviderSettings,
    SemanticStatus,
    Severity,
    StageResult,
)
from src.contracts.source import Contract, Count, Text
from src.prompts.verification import (
    VERIFICATION_PROMPT_VERSION,
    VERIFICATION_REPAIR_HINT,
    VERIFICATION_SYSTEM_PROMPT,
)
from src.provider import FailureAction, FailureReason, ProviderError, QuotaBudget, generate
from src.provider.dto import (
    HolisticEvidenceQuote,
    HolisticPassageInput,
    SemanticReview,
    VerificationCandidateInput,
    VerificationOutput,
    VerificationPayload,
)

GenerateFn = Callable[..., StageResult[VerificationOutput]]

_STOP_ALL_REASONS = frozenset(
    {
        FailureReason.AUTH_ERROR,
        FailureReason.MODEL_UNAVAILABLE,
        FailureReason.DAILY_QUOTA_EXHAUSTED,
        FailureReason.BUDGET_EXHAUSTED,
    }
)


class VerificationResult(Contract):
    findings: list[Finding]
    evaluations: list[CriterionEvaluation] = Field(default_factory=list)
    incomplete_reasons: list[Text] = Field(default_factory=list)
    uncertainties: list[Text] = Field(default_factory=list)
    attempts: Count = 0
    input_tokens: Count | None = None
    output_tokens: Count | None = None


def _needs_semantic_verification(finding: Finding) -> bool:
    return (
        finding.severity in (Severity.CRITICAL, Severity.MAJOR)
        and finding.semantic_status == SemanticStatus.UNCHECKED
    )


def _grounding_prerequisites_met(finding: Finding, inspected_blocks: set[str]) -> tuple[bool, str | None]:
    refs = [*finding.evidence, *([finding.gap.context] if finding.gap else [])]
    if not refs or not all(ref.grounded for ref in refs):
        return False, "Нельзя подтвердить находку без подтверждённого дословного обоснования."
    if finding.kind == FindingType.CONTRADICTION:
        has_distinct = any(
            a.block_id != b.block_id or a.end <= b.start or b.end <= a.start
            for i, a in enumerate(finding.evidence)
            for b in finding.evidence[i + 1 :]
        )
        if not has_distinct:
            return False, "Противоречие не может быть подтверждено без двух независимых цитат."
    if finding.gap is not None:
        if not set(finding.gap.inspected_blocks) <= inspected_blocks:
            return False, "Заявление об отсутствии охватывает непроверенные блоки документа."
    return True, None


def reconcile_evaluations_with_findings(
    evaluations: list[CriterionEvaluation],
    findings: list[Finding],
) -> list[CriterionEvaluation]:
    """Keep CriterionEvaluation consistent with canonical confirmed-finding invariants."""
    findings_by_id = {f.finding_id: f for f in findings}
    reconciled: list[CriterionEvaluation] = []
    for ev in evaluations:
        if ev.status != CriterionStatus.FAIL:
            reconciled.append(ev)
            continue
        linked = [findings_by_id[fid] for fid in ev.finding_ids if fid in findings_by_id]
        confirmed_ids = [
            f.finding_id
            for f in linked
            if f.confirmed
            and ev.criterion in f.criteria
            and (ev.requirement_id is None or ev.requirement_id in f.requirement_ids)
        ]
        if confirmed_ids:
            reconciled.append(ev.model_copy(update={"finding_ids": confirmed_ids}))
        elif linked and all(f.semantic_status == SemanticStatus.REFUTED for f in linked):
            reconciled.append(
                CriterionEvaluation(
                    requirement_id=ev.requirement_id,
                    criterion=ev.criterion,
                    status=CriterionStatus.PASS,
                    rationale="Кандидат дефекта опровергнут независимой семантической проверкой контекста и исключений.",
                    finding_ids=[],
                )
            )
        else:
            reconciled.append(
                CriterionEvaluation(
                    requirement_id=ev.requirement_id,
                    criterion=ev.criterion,
                    status=CriterionStatus.UNKNOWN,
                    rationale="Гипотеза дефекта осталась неподтверждённой после семантической проверки и требует уточнения.",
                    finding_ids=[],
                )
            )
    return reconciled


def _estimate_tokens(text: str) -> int:
    return max(1, (len(text) + 2) // 3)


def _call_provider(
    generate_fn: GenerateFn,
    payload: VerificationPayload,
    *,
    settings: ProviderSettings,
    budget: QuotaBudget | None,
    estimated_input_tokens: int = 0,
    estimated_output_tokens: int = 0,
) -> StageResult[VerificationOutput]:
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
        PipelineStage.VERIFY,
        payload,
        VerificationOutput,
        **kwargs,
    )


def verify_findings(
    findings: list[Finding],
    document: ParsedDocument,
    settings: ProviderSettings,
    *,
    evaluations: list[CriterionEvaluation] | None = None,
    inspected_blocks: set[str] | list[str] | None = None,
    budget: QuotaBudget | None = None,
    generate_fn: GenerateFn = generate,
    batch_size: int = 8,
) -> VerificationResult:
    """Verify major/critical finding candidates separately from quote grounding."""
    all_block_ids = [b.block_id for b in document.version.blocks]
    block_order = {bid: idx for idx, bid in enumerate(all_block_ids)}
    block_map = {b.block_id: b for b in document.version.blocks}
    effective_inspected = (
        set(all_block_ids)
        if inspected_blocks is None
        else (set(inspected_blocks) & set(all_block_ids))
    )
    has_uninspected_scope = bool(document.unread_pages) or effective_inspected != set(all_block_ids)

    targets = [f for f in findings if _needs_semantic_verification(f)]
    if not targets:
        reconciled = reconcile_evaluations_with_findings(evaluations or [], findings)
        return VerificationResult(findings=list(findings), evaluations=reconciled)

    reviews_by_id: dict[str, SemanticReview] = {}
    incomplete_reasons: list[str] = []
    uncertainties: list[str] = []
    total_attempts = 0
    known_in = 0
    known_out = 0
    has_known_usage = False

    step = max(1, batch_size)
    queue: list[tuple[str, list[Finding]]] = [
        (f"verify-{batch_num}", targets[offset : offset + step])
        for batch_num, offset in enumerate(range(0, len(targets), step), start=1)
    ]

    q_idx = 0
    while q_idx < len(queue):
        batch_id, batch = queue[q_idx]
        q_idx += 1
        target_ids = {f.finding_id for f in batch}

        # Collect cited blocks + neighboring blocks from same sections to expose exceptions/conditions.
        context_block_ids: set[str] = set()
        cited_sections: set[str] = set()
        for f in batch:
            for ref in f.evidence:
                context_block_ids.add(ref.block_id)
                if ref.block_id in block_map:
                    cited_sections.add(block_map[ref.block_id].location.section)
            if f.gap:
                context_block_ids.add(f.gap.context.block_id)
                context_block_ids.update(f.gap.inspected_blocks)
        for blk in document.version.blocks:
            if blk.location.section in cited_sections or len(document.version.blocks) <= 20:
                context_block_ids.add(blk.block_id)

        passages = [
            HolisticPassageInput(
                block_id=bid,
                section=block_map[bid].location.section,
                text=block_map[bid].text,
            )
            for bid in sorted(context_block_ids, key=lambda b: block_order.get(b, 10**9))
            if bid in block_map
        ]

        candidates_input = [
            VerificationCandidateInput(
                candidate_id=f.finding_id,
                kind=f.kind,
                severity=f.severity,
                claim=f.claim,
                requirement_ids=list(f.requirement_ids),
                evidence_quotes=[
                    HolisticEvidenceQuote(block_id=ref.block_id, quote=ref.quote) for ref in f.evidence
                ],
                expected_information=f.gap.expected_information if f.gap else None,
                inspected_blocks=list(f.gap.inspected_blocks) if f.gap else [],
            )
            for f in batch
        ]

        def _make_payload(repair_hint: str | None = None) -> VerificationPayload:
            return VerificationPayload(
                system_instruction=VERIFICATION_SYSTEM_PROMPT,
                prompt_version=VERIFICATION_PROMPT_VERSION,
                document_name=document.version.name,
                batch_id=batch_id,
                has_uninspected_scope=has_uninspected_scope,
                candidates=candidates_input,
                context_passages=passages,
                repair_hint=repair_hint,
            )

        payload = _make_payload()
        est_in = _estimate_tokens(payload.model_dump_json())
        est_out = len(batch) * 180
        stage_result: StageResult[VerificationOutput] | None = None
        stop_all = False

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
                total_attempts += stage_result.attempts
                if stage_result.input_tokens is not None and stage_result.output_tokens is not None:
                    known_in += stage_result.input_tokens
                    known_out += stage_result.output_tokens
                    has_known_usage = True
                break
            except ProviderError as exc:
                total_attempts += max(exc.attempts, 0)
                if exc.action == FailureAction.REPAIR and repair_attempt == 0:
                    payload = _make_payload(repair_hint=VERIFICATION_REPAIR_HINT)
                    continue
                if exc.action == FailureAction.SPLIT and len(batch) > 1:
                    mid = len(batch) // 2
                    queue[q_idx:q_idx] = [
                        (f"{batch_id}.1", batch[:mid]),
                        (f"{batch_id}.2", batch[mid:]),
                    ]
                    stage_result = None
                    break
                incomplete_reasons.append(f"Пакет семантической проверки {batch_id} не завершён: {exc}.")
                if exc.reason in _STOP_ALL_REASONS:
                    stop_all = True
                break

        if stop_all:
            break
        if stage_result is None:
            continue

        for review in stage_result.output.reviews:
            if review.candidate_id not in target_ids:
                uncertainties.append(
                    f"Пакет {batch_id}: семантическая проверка вернула неизвестный candidate_id {review.candidate_id}."
                )
                continue
            if review.candidate_id in reviews_by_id:
                uncertainties.append(
                    f"Пакет {batch_id}: дубликат результата проверки для {review.candidate_id}."
                )
                continue
            reviews_by_id[review.candidate_id] = review

        for f in batch:
            if f.finding_id not in reviews_by_id:
                incomplete_reasons.append(
                    f"Семантическая проверка не вернула вердикт для кандидата {f.finding_id}."
                )

    updated_findings: list[Finding] = []
    for f in findings:
        if not _needs_semantic_verification(f):
            updated_findings.append(f)
            continue
        review = reviews_by_id.get(f.finding_id)
        if review is None:
            updated_findings.append(f)
            uncertainties.append(f"Гипотеза {f.finding_id} осталась непроверенной (unchecked).")
            continue

        reason = review.reason.strip() or "Результат независимой семантической проверки."
        decision_map = {
            "supported": SemanticStatus.SUPPORTED,
            "refuted": SemanticStatus.REFUTED,
            "uncertain": SemanticStatus.UNCERTAIN,
        }
        desired_status = decision_map[review.decision]

        if desired_status == SemanticStatus.SUPPORTED:
            ok, guard_reason = _grounding_prerequisites_met(f, effective_inspected)
            if not ok:
                desired_status = SemanticStatus.UNCERTAIN
                reason = guard_reason or reason
                uncertainties.append(f"Кандидат {f.finding_id} не может быть подтверждён: {reason}")

        if desired_status == SemanticStatus.UNCERTAIN:
            uncertainties.append(f"Неопределённая гипотеза {f.finding_id}: {reason}")

        updated_findings.append(
            f.model_copy(
                update={
                    "semantic_status": desired_status,
                    "verification_reason": reason,
                }
            )
        )

    reconciled = reconcile_evaluations_with_findings(evaluations or [], updated_findings)
    return VerificationResult(
        findings=updated_findings,
        evaluations=reconciled,
        incomplete_reasons=list(dict.fromkeys(incomplete_reasons)),
        uncertainties=list(dict.fromkeys(uncertainties)),
        attempts=total_attempts,
        input_tokens=known_in if has_known_usage else None,
        output_tokens=known_out if has_known_usage else None,
    )
