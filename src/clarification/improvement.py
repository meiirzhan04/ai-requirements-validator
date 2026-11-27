"""AI-assisted and stakeholder-driven requirement remediation proposal engine."""
from collections.abc import Callable, Sequence
import hashlib
import inspect
import re
from typing import Literal, Self

from pydantic import BaseModel, Field, model_validator

from src.contracts import (
    Answer,
    EvidenceRef,
    Finding,
    FindingType,
    PipelineStage,
    ProposedChange,
    ProviderSettings,
    Question,
    SemanticStatus,
    StageResult,
    ValidationReport,
)
from src.contracts.source import Contract, Count, Digest, Id, Text
from src.prompts.improvement import (
    IMPROVEMENT_PROMPT_VERSION,
    IMPROVEMENT_REPAIR_HINT,
    IMPROVEMENT_SYSTEM_PROMPT,
)
from src.provider import FailureAction, ProviderError, QuotaBudget, generate, provider_session

ProposalStatus = Literal["draft", "approved", "rejected", "needs_clarification"]
ProposalOrigin = Literal["ai_assisted", "stakeholder_decision", "deterministic_fallback"]

_REQ_ID_PATTERN = re.compile(
    r"\b(?:FR|NFR|BR|REQ|UC|AC|API|SEC|DR|TR|QR|US|R|F)-\d+[A-Za-z0-9._-]*\b"
)
_APPROVAL_REQUIRED_KINDS = frozenset({FindingType.MISSING_INFORMATION, FindingType.CONTRADICTION})


class ImprovementContextBlock(BaseModel):
    block_id: str
    section: str
    text: str


class ImprovementRequestPayload(BaseModel):
    system_instruction: str
    prompt_version: str
    document_name: str
    target_block_id: str
    target_section: str
    target_block_text: str
    target_quote: str
    target_start: int
    target_end: int
    finding_id: str | None = None
    finding_kind: str | None = None
    finding_severity: str | None = None
    finding_claim: str | None = None
    finding_recommendation: str | None = None
    question_id: str | None = None
    question_text: str | None = None
    uncertainty: str | None = None
    stakeholder_decision: str | None = None
    stakeholder_consequence: str | None = None
    related_blocks: list[ImprovementContextBlock] = Field(default_factory=list)
    repair_hint: str | None = None


class ImprovementRewriteOutput(BaseModel):
    status: Literal["ready", "needs_clarification"]
    improved_text: str | None = None
    rationale: str
    clarification_needed: str | None = None
    preserved_invariants: list[str] = Field(default_factory=list)


ImprovementGenerateFn = Callable[..., StageResult[ImprovementRewriteOutput]]


class RemediationProposal(Contract):
    """Editable, traceable remediation draft bound to an exact source span and revision."""

    proposal_id: Id
    document_version: Digest
    status: ProposalStatus
    origin: ProposalOrigin
    question_id: Id | None = None
    finding_ids: list[Id] = Field(default_factory=list)
    requirement_ids: list[Id] = Field(default_factory=list)
    source: EvidenceRef
    proposed_text: Text | None = None
    rationale: Text
    stakeholder_decision: Text | None = None
    clarification_needed: Text | None = None
    preserved_invariants: list[Text] = Field(default_factory=list)
    attempts: Count = 0
    input_tokens: Count | None = None
    output_tokens: Count | None = None

    @model_validator(mode="after")
    def validate_proposal(self) -> Self:
        if not self.source.grounded or self.source.version != self.document_version:
            raise ValueError("Proposal requires a grounded source reference matching document_version")
        if self.status in ("draft", "approved"):
            if not self.proposed_text:
                raise ValueError("Ready or approved proposal requires proposed_text")
            if self.proposed_text == self.source.quote:
                raise ValueError("Proposed text must differ from the original quote")
        if self.status == "needs_clarification":
            if self.proposed_text is not None:
                raise ValueError("Clarification-needed proposal cannot carry a ready replacement")
            if not self.clarification_needed:
                raise ValueError("Clarification-needed proposal must explain what decision is missing")
        return self

    def to_proposed_change(self, *, fallback_question_id: str | None = None) -> ProposedChange:
        """Convert a ready/approved proposal into a canonical ProposedChange."""
        if not self.proposed_text:
            raise ValueError("Предложение не содержит готового текста замены.")
        qid = self.question_id or fallback_question_id
        if not qid:
            raise ValueError("Для создания ProposedChange требуется связанный question_id.")
        return ProposedChange(question_id=qid, source=self.source, replacement=self.proposed_text)


def _check_evidence_in_report(source: ValidationReport, ref: EvidenceRef):
    version = source.document.version
    if version.sha256 != ref.version:
        raise ValueError("Цитата относится к другой версии документа.")
    block = next((item for item in version.blocks if item.block_id == ref.block_id), None)
    if (
        not ref.grounded
        or block is None
        or ref.end > len(block.text)
        or block.text[ref.start : ref.end] != ref.quote
    ):
        raise ValueError("Исходная цитата или её диапазон изменились.")
    return block


def _make_proposal_id(
    document_version: str,
    ref: EvidenceRef,
    question_id: str | None,
    finding_ids: Sequence[str],
    decision: str | None,
) -> str:
    seed = "|".join(
        [
            document_version,
            ref.block_id,
            str(ref.start),
            str(ref.end),
            question_id or "",
            ",".join(sorted(finding_ids)),
            decision or "",
        ]
    )
    return f"RP-{hashlib.sha256(seed.encode('utf-8')).hexdigest()[:8]}"


def _extract_req_ids(text: str) -> set[str]:
    return set(_REQ_ID_PATTERN.findall(text))


def _check_rewrite_invariants(original_quote: str, improved_text: str) -> str | None:
    cleaned = improved_text.strip()
    if not cleaned:
        return "Пустая предложенная формулировка."
    if cleaned == original_quote:
        return "Предложенная формулировка совпадает с исходной цитатой."
    missing_ids = sorted(_extract_req_ids(original_quote) - _extract_req_ids(cleaned))
    if missing_ids:
        return f"Потеряны идентификаторы требований: {', '.join(missing_ids)}."
    if "|" in original_quote:
        if "\n" not in original_quote and "\n" in cleaned:
            return "В однострочную строку Markdown-таблицы вставлен перенос строки."
        if cleaned.count("|") != original_quote.count("|"):
            return "Нарушено количество столбцов (разделителей '|') Markdown-таблицы."
    return None


def _estimate_tokens(text: str) -> int:
    return max(1, (len(text) + 2) // 3)


def _call_provider(
    generate_fn: ImprovementGenerateFn,
    payload: ImprovementRequestPayload,
    *,
    settings: ProviderSettings | None,
    budget: QuotaBudget | None,
    estimated_input_tokens: int,
    estimated_output_tokens: int,
) -> StageResult[ImprovementRewriteOutput]:
    params = inspect.signature(generate_fn).parameters
    accepts_var_kw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())
    kwargs: dict[str, object] = {}
    if settings is not None and (accepts_var_kw or "settings" in params):
        kwargs["settings"] = settings
    if accepts_var_kw or "budget" in params:
        kwargs["budget"] = budget
    if accepts_var_kw or "estimated_input_tokens" in params:
        kwargs["estimated_input_tokens"] = estimated_input_tokens
    if accepts_var_kw or "estimated_output_tokens" in params:
        kwargs["estimated_output_tokens"] = estimated_output_tokens
    return generate_fn(
        PipelineStage.CLARIFY,
        payload,
        ImprovementRewriteOutput,
        **kwargs,
    )


def _resolve_question_and_decision(
    source: ValidationReport,
    answer: Answer,
) -> tuple[Question, str, str | None]:
    if answer.version != source.document.version.sha256:
        raise ValueError("Ответ относится к другой версии документа.")
    question = next((q for q in source.questions if q.question_id == answer.question_id), None)
    if question is None:
        raise ValueError("Вопрос отсутствует в отчёте.")
    if answer.option_id is not None:
        option = next((o for o in question.options if o.option_id == answer.option_id), None)
        if option is None:
            raise ValueError("Вариант ответа отсутствует в вопросе.")
        return question, option.label, option.consequence
    assert answer.custom_text is not None
    return question, answer.custom_text, None


def _finding_evidence(finding: Finding) -> list[EvidenceRef]:
    refs: list[EvidenceRef] = []
    seen: set[tuple[str, str, int, int]] = set()
    for ref in [*finding.evidence, *([finding.gap.context] if finding.gap else [])]:
        if ref.grounded and ref.anchor not in seen:
            seen.add(ref.anchor)
            refs.append(ref)
    return refs


def _missing_decision_prompt(
    finding: Finding | None,
    linked_questions: Sequence[Question],
) -> str:
    if linked_questions:
        q = linked_questions[0]
        return (
            f"Требуется решение стейкхолдера ({q.stakeholder.value}) по вопросу {q.question_id}: "
            f"{q.question}"
        )
    if finding and finding.gap is not None:
        return (
            f"Недостаточно согласованных данных для правки без выдумывания правил. "
            f"Уточните: {finding.gap.expected_information}"
        )
    if finding is not None:
        return (
            f"Недостаточно согласованного решения стейкхолдера для устранения дефекта "
            f"{finding.finding_id}: {finding.claim}"
        )
    return "Требуется явное решение стейкхолдера перед изменением формулировки."


def draft_remediation_proposal(
    source: ValidationReport,
    *,
    answer: Answer | None = None,
    finding_id: str | None = None,
    target_ref: EvidenceRef | None = None,
    settings: ProviderSettings | None = None,
    budget: QuotaBudget | None = None,
    generate_fn: ImprovementGenerateFn | None = None,
    use_ai: bool = True,
    allow_fallback: bool = True,
) -> RemediationProposal:
    """Draft an editable, traceable remediation proposal without mutating source artifacts.

    Never invents unapproved business rules: when essential business decisions are absent,
    returns a proposal with status='needs_clarification' instead of fabricating policy.
    """
    if answer is None and finding_id is None:
        raise ValueError("Необходимо передать answer и/или finding_id.")

    version = source.document.version
    question: Question | None = None
    stakeholder_decision: str | None = None
    stakeholder_consequence: str | None = None
    if answer is not None:
        question, stakeholder_decision, stakeholder_consequence = _resolve_question_and_decision(
            source, answer
        )

    findings_by_id = {f.finding_id: f for f in source.findings}
    selected_findings: list[Finding] = []
    if finding_id is not None:
        finding = findings_by_id.get(finding_id)
        if finding is None:
            raise ValueError(f"Находка {finding_id} отсутствует в отчёте.")
        if finding.semantic_status == SemanticStatus.REFUTED:
            raise ValueError(f"Нельзя формировать правку по опровергнутой находке {finding_id}.")
        selected_findings.append(finding)

    if question is not None:
        for fid in question.finding_ids:
            f = findings_by_id.get(fid)
            if f is not None and f.finding_id not in {item.finding_id for item in selected_findings}:
                selected_findings.append(f)

    if not selected_findings:
        raise ValueError("Не найдено ни одной действительной находки для предложения правки.")

    primary_finding = selected_findings[0]
    finding_ids = [f.finding_id for f in selected_findings]
    requirement_ids = list(
        dict.fromkeys(
            [*(question.requirement_ids if question else []), *(r for f in selected_findings for r in f.requirement_ids)]
        )
    )

    candidate_refs: list[EvidenceRef] = []
    seen_anchors: set[tuple[str, str, int, int]] = set()
    raw_refs = list(question.evidence) if question else []
    for f in selected_findings:
        raw_refs.extend(_finding_evidence(f))
    for ref in raw_refs:
        if ref.grounded and ref.anchor not in seen_anchors:
            seen_anchors.add(ref.anchor)
            candidate_refs.append(ref)

    if not candidate_refs:
        raise ValueError("Для выбранной находки отсутствуют подтверждённые цитаты.")

    if target_ref is not None:
        target_block = _check_evidence_in_report(source, target_ref)
        allowed_blocks = {ref.block_id for ref in candidate_refs}
        if target_ref.block_id not in allowed_blocks:
            raise ValueError("Целевая цитата относится к блоку вне контекста находки/вопроса.")
        chosen_ref = target_ref
    else:
        chosen_ref = candidate_refs[0]
        if stakeholder_decision is not None:
            for ref in candidate_refs:
                if ref.quote != stakeholder_decision:
                    chosen_ref = ref
                    break
        target_block = _check_evidence_in_report(source, chosen_ref)

    linked_questions = (
        [question]
        if question is not None
        else [q for q in source.questions if set(q.finding_ids) & set(finding_ids)]
    )
    proposal_id = _make_proposal_id(
        version.sha256,
        chosen_ref,
        question.question_id if question else None,
        finding_ids,
        stakeholder_decision,
    )

    # Guardrail: never invent unapproved business rules for gaps or contradictions without an answer.
    if stakeholder_decision is None and any(f.kind in _APPROVAL_REQUIRED_KINDS for f in selected_findings):
        clarification_text = _missing_decision_prompt(primary_finding, linked_questions)
        return RemediationProposal(
            proposal_id=proposal_id,
            document_version=version.sha256,
            status="needs_clarification",
            origin="deterministic_fallback",
            question_id=linked_questions[0].question_id if linked_questions else None,
            finding_ids=finding_ids,
            requirement_ids=requirement_ids,
            source=chosen_ref,
            proposed_text=None,
            rationale=(
                "Автоматическое переписывание остановлено: устранение пробела или противоречия "
                "требует подтверждённого решения стейкхолдера, чтобы не выдумывать бизнес-правила."
            ),
            clarification_needed=clarification_text,
            preserved_invariants=sorted(_extract_req_ids(chosen_ref.quote)),
        )

    can_call_ai = use_ai and (
        generate_fn is not None or (settings is not None and settings.api_key is not None)
    )

    if not can_call_ai:
        if stakeholder_decision is None:
            return RemediationProposal(
                proposal_id=proposal_id,
                document_version=version.sha256,
                status="needs_clarification",
                origin="deterministic_fallback",
                question_id=linked_questions[0].question_id if linked_questions else None,
                finding_ids=finding_ids,
                requirement_ids=requirement_ids,
                source=chosen_ref,
                proposed_text=None,
                rationale=(
                    "AI-генерация отключена или недоступна, а решение стейкхолдера не передано. "
                    "Уточните формулировку вручную или передайте ответ на вопрос."
                ),
                clarification_needed=_missing_decision_prompt(primary_finding, linked_questions),
                preserved_invariants=sorted(_extract_req_ids(chosen_ref.quote)),
            )
        if stakeholder_decision == chosen_ref.quote:
            raise ValueError("Ответ уже совпадает с исходным текстом.")
        return RemediationProposal(
            proposal_id=proposal_id,
            document_version=version.sha256,
            status="draft",
            origin="stakeholder_decision",
            question_id=question.question_id if question else None,
            finding_ids=finding_ids,
            requirement_ids=requirement_ids,
            source=chosen_ref,
            proposed_text=stakeholder_decision,
            rationale=(
                f"Черновик правки сформирован на основе решения стейкхолдера по вопросу "
                f"{question.question_id}."
                if question
                else "Черновик правки сформирован на основе решения стейкхолдера."
            ),
            stakeholder_decision=stakeholder_decision,
            preserved_invariants=sorted(_extract_req_ids(chosen_ref.quote)),
        )

    blocks_by_id = {b.block_id: b for b in version.blocks}
    related_blocks: list[ImprovementContextBlock] = []
    seen_block_ids = {target_block.block_id}
    for ref in candidate_refs:
        if ref.block_id not in seen_block_ids and ref.block_id in blocks_by_id:
            seen_block_ids.add(ref.block_id)
            blk = blocks_by_id[ref.block_id]
            related_blocks.append(
                ImprovementContextBlock(
                    block_id=blk.block_id,
                    section=blk.location.section,
                    text=blk.text,
                )
            )

    def _build_payload(repair_hint: str | None = None) -> ImprovementRequestPayload:
        return ImprovementRequestPayload(
            system_instruction=IMPROVEMENT_SYSTEM_PROMPT,
            prompt_version=IMPROVEMENT_PROMPT_VERSION,
            document_name=version.name,
            target_block_id=target_block.block_id,
            target_section=target_block.location.section,
            target_block_text=target_block.text,
            target_quote=chosen_ref.quote,
            target_start=chosen_ref.start,
            target_end=chosen_ref.end,
            finding_id=primary_finding.finding_id,
            finding_kind=primary_finding.kind.value,
            finding_severity=primary_finding.severity.value,
            finding_claim=primary_finding.claim,
            finding_recommendation=primary_finding.recommendation,
            question_id=question.question_id if question else None,
            question_text=question.question if question else None,
            uncertainty=question.uncertainty if question else None,
            stakeholder_decision=stakeholder_decision,
            stakeholder_consequence=stakeholder_consequence,
            related_blocks=related_blocks,
            repair_hint=repair_hint,
        )

    active_generate = generate_fn or generate
    payload = _build_payload()
    est_in = _estimate_tokens(payload.model_dump_json(exclude={"system_instruction"}, exclude_none=True))
    est_out = max(180, _estimate_tokens(chosen_ref.quote) * 2 + 120)

    total_attempts = 0
    total_in = 0
    total_out = 0
    has_usage = False
    last_error: str | None = None

    for attempt_idx in range(2):
        try:
            stage_result = _call_provider(
                active_generate,
                payload,
                settings=settings,
                budget=budget,
                estimated_input_tokens=est_in,
                estimated_output_tokens=est_out,
            )
            total_attempts += stage_result.attempts
            if stage_result.input_tokens is not None and stage_result.output_tokens is not None:
                total_in += stage_result.input_tokens
                total_out += stage_result.output_tokens
                has_usage = True
        except ProviderError as exc:
            total_attempts += max(exc.attempts, 0)
            if exc.action == FailureAction.REPAIR and attempt_idx == 0:
                payload = _build_payload(repair_hint=IMPROVEMENT_REPAIR_HINT)
                continue
            if not allow_fallback:
                raise
            last_error = str(exc)
            break

        output = stage_result.output
        invariants = [item.strip() for item in output.preserved_invariants if item and item.strip()]
        rationale = output.rationale.strip() or "AI-предложение улучшенной формулировки."

        if output.status == "needs_clarification":
            clarification = (output.clarification_needed or "").strip() or _missing_decision_prompt(
                primary_finding, linked_questions
            )
            return RemediationProposal(
                proposal_id=proposal_id,
                document_version=version.sha256,
                status="needs_clarification",
                origin="ai_assisted",
                question_id=question.question_id if question else (linked_questions[0].question_id if linked_questions else None),
                finding_ids=finding_ids,
                requirement_ids=requirement_ids,
                source=chosen_ref,
                proposed_text=None,
                rationale=rationale,
                stakeholder_decision=stakeholder_decision,
                clarification_needed=clarification,
                preserved_invariants=invariants,
                attempts=total_attempts,
                input_tokens=total_in if has_usage else None,
                output_tokens=total_out if has_usage else None,
            )

        candidate_text = (output.improved_text or "").strip()
        violation = _check_rewrite_invariants(chosen_ref.quote, candidate_text)
        if violation is None:
            return RemediationProposal(
                proposal_id=proposal_id,
                document_version=version.sha256,
                status="draft",
                origin="ai_assisted",
                question_id=question.question_id if question else None,
                finding_ids=finding_ids,
                requirement_ids=requirement_ids,
                source=chosen_ref,
                proposed_text=candidate_text,
                rationale=rationale,
                stakeholder_decision=stakeholder_decision,
                preserved_invariants=invariants or sorted(_extract_req_ids(chosen_ref.quote)),
                attempts=total_attempts,
                input_tokens=total_in if has_usage else None,
                output_tokens=total_out if has_usage else None,
            )

        last_error = violation
        if attempt_idx == 0:
            payload = _build_payload(repair_hint=f"{IMPROVEMENT_REPAIR_HINT} Причина: {violation}")
            continue

    # Explicit visible deterministic fallback after provider or invariant failure.
    if stakeholder_decision is not None and stakeholder_decision != chosen_ref.quote:
        return RemediationProposal(
            proposal_id=proposal_id,
            document_version=version.sha256,
            status="draft",
            origin="deterministic_fallback",
            question_id=question.question_id if question else None,
            finding_ids=finding_ids,
            requirement_ids=requirement_ids,
            source=chosen_ref,
            proposed_text=stakeholder_decision,
            rationale=(
                f"Использован детерминированный fallback по решению стейкхолдера "
                f"(AI-переписывание недоступно или отклонено проверкой инвариантов: {last_error})."
            ),
            stakeholder_decision=stakeholder_decision,
            preserved_invariants=sorted(_extract_req_ids(chosen_ref.quote)),
            attempts=total_attempts,
            input_tokens=total_in if has_usage else None,
            output_tokens=total_out if has_usage else None,
        )

    return RemediationProposal(
        proposal_id=proposal_id,
        document_version=version.sha256,
        status="needs_clarification",
        origin="deterministic_fallback",
        question_id=question.question_id if question else (linked_questions[0].question_id if linked_questions else None),
        finding_ids=finding_ids,
        requirement_ids=requirement_ids,
        source=chosen_ref,
        proposed_text=None,
        rationale=(
            f"Не удалось безопасно сформировать AI-формулировку без нарушения инвариантов "
            f"({last_error}). Требуется ручная редакция или решение стейкхолдера."
        ),
        stakeholder_decision=stakeholder_decision,
        clarification_needed=_missing_decision_prompt(primary_finding, linked_questions),
        preserved_invariants=sorted(_extract_req_ids(chosen_ref.quote)),
        attempts=total_attempts,
        input_tokens=total_in if has_usage else None,
        output_tokens=total_out if has_usage else None,
    )


def draft_remediation_proposals(
    source: ValidationReport,
    *,
    answers: Sequence[Answer] = (),
    finding_ids: Sequence[str] = (),
    settings: ProviderSettings | None = None,
    budget: QuotaBudget | None = None,
    generate_fn: ImprovementGenerateFn | None = None,
    use_ai: bool = True,
    allow_fallback: bool = True,
) -> list[RemediationProposal]:
    """Draft remediation proposals for multiple answers and/or findings in source order."""
    if not answers and not finding_ids:
        raise ValueError("Необходимо передать хотя бы один answer или finding_id.")

    def _run() -> list[RemediationProposal]:
        proposals: list[RemediationProposal] = []
        covered_findings: set[str] = set()
        seen_ids: set[str] = set()

        for ans in answers:
            proposal = draft_remediation_proposal(
                source,
                answer=ans,
                settings=settings,
                budget=budget,
                generate_fn=generate_fn,
                use_ai=use_ai,
                allow_fallback=allow_fallback,
            )
            covered_findings.update(proposal.finding_ids)
            if proposal.proposal_id in seen_ids:
                suffix = 2
                while f"{proposal.proposal_id}-{suffix}" in seen_ids:
                    suffix += 1
                proposal = proposal.model_copy(update={"proposal_id": f"{proposal.proposal_id}-{suffix}"})
            seen_ids.add(proposal.proposal_id)
            proposals.append(proposal)

        for fid in finding_ids:
            if fid in covered_findings:
                continue
            proposal = draft_remediation_proposal(
                source,
                finding_id=fid,
                settings=settings,
                budget=budget,
                generate_fn=generate_fn,
                use_ai=use_ai,
                allow_fallback=allow_fallback,
            )
            if proposal.proposal_id in seen_ids:
                suffix = 2
                while f"{proposal.proposal_id}-{suffix}" in seen_ids:
                    suffix += 1
                proposal = proposal.model_copy(update={"proposal_id": f"{proposal.proposal_id}-{suffix}"})
            seen_ids.add(proposal.proposal_id)
            proposals.append(proposal)
        return proposals

    if use_ai and generate_fn is None and settings is not None and settings.api_key is not None:
        with provider_session(settings):
            return _run()
    return _run()


def approve_proposal(
    proposal: RemediationProposal,
    *,
    edited_text: str | None = None,
    rationale: str | None = None,
) -> RemediationProposal:
    """Approve a proposal, optionally replacing its text with stakeholder-edited wording."""
    if proposal.status == "rejected":
        raise ValueError("Отклонённое предложение нельзя утвердить повторно.")
    final_text = edited_text.strip() if edited_text is not None else proposal.proposed_text
    if not final_text:
        raise ValueError("Для утверждения предложения требуется непустой текст правки.")
    if final_text == proposal.source.quote:
        raise ValueError("Утверждаемая правка совпадает с исходной цитатой.")
    updated_rationale = rationale.strip() if rationale and rationale.strip() else proposal.rationale
    return RemediationProposal(
        **{
            **proposal.model_dump(),
            "status": "approved",
            "proposed_text": final_text,
            "rationale": updated_rationale,
            "clarification_needed": None,
        }
    )


def reject_proposal(
    proposal: RemediationProposal,
    *,
    reason: str | None = None,
) -> RemediationProposal:
    """Mark a remediation proposal as rejected without mutating the original object."""
    updated_rationale = reason.strip() if reason and reason.strip() else proposal.rationale
    return RemediationProposal(
        **{
            **proposal.model_dump(),
            "status": "rejected",
            "rationale": updated_rationale,
        }
    )
