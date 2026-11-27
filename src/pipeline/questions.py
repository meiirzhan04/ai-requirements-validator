"""Targeted stakeholder clarification question engine built on validated findings."""
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
import inspect
import re

from pydantic import Field

from src.contracts import (
    Answer,
    EvidenceRef,
    Finding,
    ParsedDocument,
    PipelineStage,
    ProviderSettings,
    Question,
    QuestionOption,
    QuestionPriority,
    SemanticStatus,
    Severity,
    StageResult,
)
from src.contracts.source import Contract, Count, Text
from src.prompts.questions import (
    QUESTIONS_PROMPT_VERSION,
    QUESTIONS_REPAIR_HINT,
    QUESTIONS_SYSTEM_PROMPT,
)
from src.provider import FailureAction, FailureReason, ProviderError, QuotaBudget, generate
from src.provider.dto import (
    QuestionCandidate,
    QuestionFindingEvidenceInput,
    QuestionFindingInput,
    QuestionGenerationOutput,
    QuestionGenerationPayload,
)

GenerateFn = Callable[..., StageResult[QuestionGenerationOutput]]

_OPTION_IDS = tuple("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
_PRIORITY_ORDER = {
    QuestionPriority.BLOCKER: 0,
    QuestionPriority.HIGH: 1,
    QuestionPriority.OPTIONAL: 2,
}
_WHITESPACE_RE = re.compile(r"\s+")
_GENERIC_QUESTION_PATTERNS = (
    re.compile(r"^уточните\s+(?:требование|детали|формулировку)\s*\??$", re.IGNORECASE),
    re.compile(r"^нужно\s+уточнить\s*\??$", re.IGNORECASE),
    re.compile(r"^требуется\s+уточнение\s*\??$", re.IGNORECASE),
    re.compile(r"^please\s+clarify(?:\s+the\s+requirement)?\s*\??$", re.IGNORECASE),
    re.compile(r"^clarify\s+requirement\s*\??$", re.IGNORECASE),
)
_STOP_ALL_REASONS = frozenset(
    {
        FailureReason.AUTH_ERROR,
        FailureReason.MODEL_UNAVAILABLE,
        FailureReason.DAILY_QUOTA_EXHAUSTED,
        FailureReason.BUDGET_EXHAUSTED,
    }
)


class QuestionGenerationResult(Contract):
    questions: list[Question]
    blocking_uncertainties: list[Text] = Field(default_factory=list)
    skipped_finding_ids: list[str] = Field(default_factory=list)
    incomplete_reasons: list[Text] = Field(default_factory=list)
    uncertainties: list[Text] = Field(default_factory=list)
    attempts: Count = 0
    input_tokens: Count | None = None
    output_tokens: Count | None = None


def _normalize(text: str) -> str:
    return _WHITESPACE_RE.sub(" ", text.strip().lower())


def _is_generic_question(text: str) -> bool:
    cleaned = _normalize(text)
    if len(cleaned) < 15:
        return True
    return any(pattern.match(cleaned) for pattern in _GENERIC_QUESTION_PATTERNS)


def _finding_grounded_evidence(finding: Finding) -> list[EvidenceRef]:
    refs: list[EvidenceRef] = []
    seen: set[tuple[str, str, int, int]] = set()
    for ref in [*finding.evidence, *([finding.gap.context] if finding.gap else [])]:
        if ref.grounded and ref.anchor not in seen:
            seen.add(ref.anchor)
            refs.append(ref)
    return refs


def _is_material_finding(finding: Finding, *, include_minor: bool = False) -> bool:
    if finding.semantic_status == SemanticStatus.REFUTED:
        return False
    if not _finding_grounded_evidence(finding):
        return False
    if finding.severity in (Severity.CRITICAL, Severity.MAJOR):
        return True
    return include_minor


def _validate_and_build_options(candidate: QuestionCandidate) -> list[QuestionOption] | None:
    options: list[QuestionOption] = []
    seen_labels: set[str] = set()
    used_ids: set[str] = set()

    for idx, opt in enumerate(candidate.options):
        label = opt.label.strip()
        consequence = opt.consequence.strip()
        if not label or not consequence:
            return None
        norm_label = _normalize(label)
        if norm_label in seen_labels:
            continue
        seen_labels.add(norm_label)

        preferred = (opt.option_id or "").strip()
        if preferred and re.match(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$", preferred) and preferred not in used_ids:
            opt_id = preferred
        else:
            opt_id = _OPTION_IDS[idx] if idx < len(_OPTION_IDS) else f"OPT{idx + 1}"
            while opt_id in used_ids:
                idx += 1
                opt_id = f"OPT{idx + 1}"
        used_ids.add(opt_id)
        options.append(QuestionOption(option_id=opt_id, label=label, consequence=consequence))

    return options if len(options) >= 2 else None


def _estimate_tokens(text: str) -> int:
    return max(1, (len(text) + 2) // 3)


def _call_provider(
    generate_fn: GenerateFn,
    payload: QuestionGenerationPayload,
    *,
    settings: ProviderSettings,
    budget: QuotaBudget | None,
    estimated_input_tokens: int = 0,
    estimated_output_tokens: int = 0,
) -> StageResult[QuestionGenerationOutput]:
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
        PipelineStage.CLARIFY,
        payload,
        QuestionGenerationOutput,
        **kwargs,
    )


def _execute_question_batch(
    *,
    batch_id: str,
    batch: list[Finding],
    document: ParsedDocument,
    settings: ProviderSettings,
    budget: QuotaBudget | None,
    generate_fn: GenerateFn,
) -> tuple[StageResult[QuestionGenerationOutput] | None, ProviderError | None, int]:
    finding_inputs = [
        QuestionFindingInput(
            finding_id=f.finding_id,
            kind=f.kind.value,
            severity=f.severity.value,
            semantic_status=f.semantic_status.value,
            claim=f.claim,
            recommendation=f.recommendation,
            requirement_ids=list(f.requirement_ids),
            evidence=[
                QuestionFindingEvidenceInput(
                    block_id=ref.block_id,
                    start=ref.start,
                    end=ref.end,
                    quote=ref.quote,
                )
                for ref in _finding_grounded_evidence(f)
            ],
        )
        for f in batch
    ]

    def _make_payload(repair_hint: str | None = None) -> QuestionGenerationPayload:
        return QuestionGenerationPayload(
            system_instruction=QUESTIONS_SYSTEM_PROMPT,
            prompt_version=QUESTIONS_PROMPT_VERSION,
            document_name=document.version.name,
            batch_id=batch_id,
            findings=finding_inputs,
            repair_hint=repair_hint,
        )

    payload = _make_payload()
    est_in = _estimate_tokens(payload.model_dump_json(exclude={"system_instruction"}, exclude_none=True))
    est_out = len(batch) * 220
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
                payload = _make_payload(repair_hint=QUESTIONS_REPAIR_HINT)
                continue
            return None, exc, attempts
    return None, None, attempts


def generate_questions(
    findings: list[Finding],
    document: ParsedDocument,
    settings: ProviderSettings,
    *,
    budget: QuotaBudget | None = None,
    generate_fn: GenerateFn = generate,
    include_minor: bool = False,
    batch_size: int = 16,
    max_concurrency: int = 1,
) -> QuestionGenerationResult:
    """Generate stakeholder clarification questions grounded strictly in material findings."""
    findings_by_id = {f.finding_id: f for f in findings}
    block_order = {b.block_id: idx for idx, b in enumerate(document.version.blocks)}

    eligible: list[Finding] = []
    skipped_finding_ids: list[str] = []
    for f in findings:
        if _is_material_finding(f, include_minor=include_minor):
            eligible.append(f)
        else:
            skipped_finding_ids.append(f.finding_id)

    if not eligible:
        return QuestionGenerationResult(
            questions=[],
            skipped_finding_ids=skipped_finding_ids,
        )

    valid_candidates: list[tuple[tuple[int, int, str], dict]] = []
    blocking_uncertainties: list[str] = []
    incomplete_reasons: list[str] = []
    uncertainties: list[str] = []
    seen_question_signatures: set[tuple[tuple[str, ...], str]] = set()

    total_attempts = 0
    known_in = 0
    known_out = 0
    has_known_usage = False
    workers = max(1, max_concurrency)

    step = max(1, batch_size)
    pending_wave: list[tuple[str, list[Finding]]] = [
        (f"clarify-{batch_num}", eligible[offset : offset + step])
        for batch_num, offset in enumerate(range(0, len(eligible), step), start=1)
    ]

    def _consume_questions(batch_id: str, batch: list[Finding], output: QuestionGenerationOutput) -> None:
        batch_fids = {f.finding_id for f in batch}
        for skipped in output.skipped_findings:
            if skipped.finding_id in batch_fids and skipped.finding_id not in skipped_finding_ids:
                skipped_finding_ids.append(skipped.finding_id)

        for cand in output.questions:
            fids = list(dict.fromkeys(cand.finding_ids))
            if not fids or any(fid not in findings_by_id for fid in fids):
                uncertainties.append(
                    f"Пакет {batch_id}: отклонен вопрос с неизвестными finding_ids ({', '.join(fids)})."
                )
                continue

            related = [findings_by_id[fid] for fid in fids]
            if any(f.semantic_status == SemanticStatus.REFUTED for f in related):
                uncertainties.append(
                    f"Пакет {batch_id}: отклонен вопрос по опровергнутой гипотезе ({', '.join(fids)})."
                )
                continue

            question_text = cand.question.strip()
            uncertainty_text = cand.uncertainty.strip()
            business_impact = cand.business_impact.strip()
            if _is_generic_question(question_text) or not uncertainty_text or not business_impact:
                uncertainties.append(
                    f"Пакет {batch_id}: отклонен общий или незаполненный вопрос для {', '.join(fids)}."
                )
                continue

            options = _validate_and_build_options(cand)
            if options is None:
                uncertainties.append(
                    f"Пакет {batch_id}: вопрос для {', '.join(fids)} не содержит двух различных вариантов с последствиями."
                )
                continue

            allowed_evidence: list[EvidenceRef] = []
            seen_anchors: set[tuple[str, str, int, int]] = set()
            for f in related:
                for ref in _finding_grounded_evidence(f):
                    if ref.anchor not in seen_anchors:
                        seen_anchors.add(ref.anchor)
                        allowed_evidence.append(ref)

            allowed_block_ids = {ref.block_id for ref in allowed_evidence}
            if cand.evidence_block_ids:
                foreign_blocks = [bid for bid in cand.evidence_block_ids if bid not in allowed_block_ids]
                if foreign_blocks:
                    uncertainties.append(
                        f"Пакет {batch_id}: отклонена чужая ссылка на блоки ({', '.join(foreign_blocks)}) "
                        f"в вопросе для {', '.join(fids)}."
                    )
                    continue
                selected_blocks = set(cand.evidence_block_ids)
                evidence = [ref for ref in allowed_evidence if ref.block_id in selected_blocks]
            else:
                evidence = allowed_evidence

            if not evidence:
                uncertainties.append(
                    f"Пакет {batch_id}: у вопроса для {', '.join(fids)} отсутствует подтверждённое доказательство."
                )
                continue

            allowed_reqs = list(dict.fromkeys(r for f in related for r in f.requirement_ids))
            allowed_req_set = set(allowed_reqs)
            if cand.requirement_ids:
                req_ids = [rid for rid in dict.fromkeys(cand.requirement_ids) if rid in allowed_req_set]
                if not req_ids and allowed_reqs:
                    req_ids = allowed_reqs
            else:
                req_ids = allowed_reqs

            priority = cand.priority
            has_critical_or_open_blocker = any(
                f.severity == Severity.CRITICAL
                and (f.confirmed or f.semantic_status in (SemanticStatus.UNCHECKED, SemanticStatus.UNCERTAIN))
                for f in related
            )
            if cand.blocks_readiness and priority != QuestionPriority.BLOCKER:
                priority = QuestionPriority.BLOCKER

            if priority == QuestionPriority.BLOCKER and not has_critical_or_open_blocker:
                blocking_uncertainties.append(uncertainty_text)

            sig = (tuple(sorted(fids)), _normalize(question_text))
            if sig in seen_question_signatures:
                continue
            seen_question_signatures.add(sig)

            first_block_idx = min(block_order.get(ref.block_id, 10**9) for ref in evidence)
            sort_key = (_PRIORITY_ORDER[priority], first_block_idx, fids[0])
            valid_candidates.append(
                (
                    sort_key,
                    {
                        "finding_ids": fids,
                        "requirement_ids": req_ids,
                        "evidence": evidence,
                        "uncertainty": uncertainty_text,
                        "stakeholder": cand.stakeholder,
                        "priority": priority,
                        "business_impact": business_impact,
                        "question": question_text,
                        "options": options,
                        "allow_custom": True,
                    },
                )
            )

    stop_all = False
    while pending_wave and not stop_all:
        if workers <= 1 or len(pending_wave) <= 1:
            q_idx = 0
            while q_idx < len(pending_wave):
                batch_id, batch = pending_wave[q_idx]
                q_idx += 1
                stage_result, exc, batch_attempts = _execute_question_batch(
                    batch_id=batch_id,
                    batch=batch,
                    document=document,
                    settings=settings,
                    budget=budget,
                    generate_fn=generate_fn,
                )
                total_attempts += batch_attempts
                if exc is not None:
                    if exc.action == FailureAction.SPLIT and len(batch) > 1:
                        mid = len(batch) // 2
                        pending_wave[q_idx:q_idx] = [
                            (f"{batch_id}.1", batch[:mid]),
                            (f"{batch_id}.2", batch[mid:]),
                        ]
                        continue
                    incomplete_reasons.append(f"Пакет генерации вопросов {batch_id} не завершён: {exc}.")
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
                _consume_questions(batch_id, batch, stage_result.output)
            break

        next_wave: list[tuple[str, list[Finding]]] = []
        with ThreadPoolExecutor(max_workers=min(workers, len(pending_wave))) as pool:
            futures = [
                (
                    bid,
                    b,
                    pool.submit(
                        _execute_question_batch,
                        batch_id=bid,
                        batch=b,
                        document=document,
                        settings=settings,
                        budget=budget,
                        generate_fn=generate_fn,
                    ),
                )
                for bid, b in pending_wave
            ]
            wave_results = [(bid, b, *fut.result()) for bid, b, fut in futures]

        for batch_id, batch, stage_result, exc, batch_attempts in wave_results:
            total_attempts += batch_attempts
            if exc is not None:
                if exc.action == FailureAction.SPLIT and len(batch) > 1:
                    mid = len(batch) // 2
                    next_wave.extend(
                        [(f"{batch_id}.1", batch[:mid]), (f"{batch_id}.2", batch[mid:])]
                    )
                    continue
                incomplete_reasons.append(f"Пакет генерации вопросов {batch_id} не завершён: {exc}.")
                if exc.reason in _STOP_ALL_REASONS:
                    stop_all = True
                continue
            if stage_result is None:
                continue
            if stage_result.input_tokens is not None and stage_result.output_tokens is not None:
                known_in += stage_result.input_tokens
                known_out += stage_result.output_tokens
                has_known_usage = True
            _consume_questions(batch_id, batch, stage_result.output)
        pending_wave = next_wave

    valid_candidates.sort(key=lambda item: item[0])
    questions = [
        Question(question_id=f"Q{seq:03d}", **payload_fields)
        for seq, (_, payload_fields) in enumerate(valid_candidates, start=1)
    ]

    return QuestionGenerationResult(
        questions=questions,
        blocking_uncertainties=list(dict.fromkeys(blocking_uncertainties)),
        skipped_finding_ids=list(dict.fromkeys(skipped_finding_ids)),
        incomplete_reasons=list(dict.fromkeys(incomplete_reasons)),
        uncertainties=list(dict.fromkeys(uncertainties)),
        attempts=total_attempts,
        input_tokens=known_in if has_known_usage else None,
        output_tokens=known_out if has_known_usage else None,
    )


def record_answer(
    answer: Answer,
    questions: list[Question],
    findings: list[Finding],
    document_version: str,
) -> tuple[Answer, list[Finding]]:
    """Validate a stakeholder answer against questions while keeping all findings open.

    Selecting an option or supplying a custom answer records the stakeholder's decision
    without closing or mutating any finding until the specification is revised and re-audited.
    """
    question_map = {q.question_id: q for q in questions}
    question = question_map.get(answer.question_id)
    if question is None:
        raise ValueError(f"Unknown question_id: {answer.question_id}")
    if answer.version != document_version:
        raise ValueError("Answer targets a stale document version")
    if answer.option_id is not None:
        valid_option_ids = {opt.option_id for opt in question.options}
        if answer.option_id not in valid_option_ids:
            raise ValueError(f"Unknown option_id '{answer.option_id}' for question {answer.question_id}")

    # Return defensive copies proving findings remain strictly unchanged and open.
    preserved_findings = [f.model_copy(deep=True) for f in findings]
    return answer, preserved_findings
