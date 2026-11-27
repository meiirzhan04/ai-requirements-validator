"""Interactive stakeholder clarification, AI document improvement, export, and revalidation UI."""
from hashlib import sha256
import html
import logging
import re

import streamlit as st

from src import pipeline
from src.clarification import (
    ImprovedDocumentBundle,
    RemediationProposal,
    apply_changes,
    approve_proposal,
    draft_remediation_proposal,
    export_improved_markdown,
    propose_change,
    reject_proposal,
    render_change_log_markdown,
    revalidate_improved_document,
)
from src.contracts import (
    Answer,
    DocumentFormat,
    EvidenceRef,
    ExecutionStatus,
    Finding,
    ProposedChange,
    ProviderSettings,
    SemanticStatus,
    ValidationReport,
)
from src.provider import ProviderError
from src.report import EXECUTION, READINESS, ROLE, SEVERITY
from ui.components import ProgressTracker, render_evidence_block, run_with_progress
from ui.dashboard import STAGE, STATE, _rules_version
from ui.state import get_report_exports, get_report_view

logger = logging.getLogger(__name__)

_PROPOSAL_STATUS_LABEL = {
    "draft": "Черновик",
    "approved": "Утверждено",
    "rejected": "Отклонено",
    "needs_clarification": "Требуется решение",
}
_PROPOSAL_ORIGIN_LABEL = {
    "ai_assisted": "AI-предложение",
    "stakeholder_decision": "Решение стейкхолдера",
    "deterministic_fallback": "Детерминированный черновик",
}
_WORD_RE = re.compile(r"[A-Za-zА-Яа-яЁё0-9]{3,}")


def _finding_tokens(finding: Finding) -> set[str]:
    text = f"{finding.claim} {' '.join(r.quote for r in finding.evidence)}"
    return set(_WORD_RE.findall(text.lower()))


def _findings_match(old: Finding, new: Finding) -> bool:
    """Match two findings across document revisions by defect kind, overlap, and evidence similarity."""
    if old.kind != new.kind:
        return False
    old_quotes = {r.quote.strip().lower() for r in old.evidence if r.quote.strip()}
    new_quotes = {r.quote.strip().lower() for r in new.evidence if r.quote.strip()}
    if old_quotes and new_quotes and (old_quotes & new_quotes):
        return True
    toks_old = _finding_tokens(old)
    toks_new = _finding_tokens(new)
    if not toks_old or not toks_new:
        return False
    overlap = len(toks_old & toks_new) / len(toks_old | toks_new)
    return overlap >= 0.45


def compare_reports(
    previous: ValidationReport,
    result: ValidationReport,
    *,
    applied_changes: int = 0,
) -> dict[str, object]:
    """Compare original and revalidated reports honestly without claiming resolution on partial runs."""
    prev_confirmed = [f for f in previous.findings if f.confirmed]
    next_confirmed = [f for f in result.findings if f.confirmed]
    complete = result.coverage.execution == ExecutionStatus.COMPLETE

    matched_prev_ids: set[str] = set()
    matched_next_ids: set[str] = set()
    remaining: list[Finding] = []
    for old in prev_confirmed:
        match = next(
            (cand for cand in next_confirmed if cand.finding_id not in matched_next_ids and _findings_match(old, cand)),
            None,
        )
        if match is not None:
            matched_prev_ids.add(old.finding_id)
            matched_next_ids.add(match.finding_id)
            remaining.append(match)

    unmatched_prev = [f for f in prev_confirmed if f.finding_id not in matched_prev_ids]
    new_defects = [f for f in next_confirmed if f.finding_id not in matched_next_ids]

    # Never claim a defect is resolved unless mandatory revalidation finished completely.
    resolved = unmatched_prev if complete else []
    unverified_prev = [] if complete else unmatched_prev

    open_hypotheses = [
        f for f in result.findings if f.semantic_status in (SemanticStatus.UNCHECKED, SemanticStatus.UNCERTAIN)
    ]
    return {
        "complete": complete,
        "original_readiness": previous.verdict.readiness,
        "improved_readiness": result.verdict.readiness,
        "original_execution": previous.coverage.execution,
        "improved_execution": result.coverage.execution,
        "applied_changes": applied_changes,
        "resolved": resolved,
        "unverified_previous": unverified_prev,
        "remaining": remaining,
        "new_defects": new_defects,
        "unresolved_uncertainties": list(result.coverage.uncertainties),
        "open_hypotheses": open_hypotheses,
    }


def recheck_summary(previous, question, change, result):
    if result.coverage.execution != ExecutionStatus.COMPLETE:
        return "Повторная проверка неполная. Устранение дефекта не подтверждено."
    affected = set(question.requirement_ids)
    evidence = [*question.evidence, *(r.source for r in previous.requirements if r.req_id in affected)]
    requirements = set()
    for ref in evidence:
        quote = ref.quote
        if ref.block_id == change.source.block_id:
            if ref.start <= change.source.start and change.source.end <= ref.end:
                start, end = change.source.start - ref.start, change.source.end - ref.start
                quote = quote[:start] + change.replacement + quote[end:]
            elif ref.start < change.source.end and change.source.start < ref.end:
                return "Текст связанных источников изменился. Сопоставьте сценарий с новым отчётом."
        matches = [r for r in result.requirements if quote in r.source.quote]
        if len(matches) != 1:
            return "Не удалось однозначно сопоставить исправленное правило и связанные условия приёмки."
        requirements.add(matches[0].req_id)

    criteria = {c for f in previous.findings if f.finding_id in question.finding_ids for c in f.criteria}
    evaluations = [
        e
        for e in result.evaluations
        if e.requirement_id in requirements or (e.requirement_id is None and e.criterion in criteria)
    ]
    if any(e.status == "fail" for e in evaluations):
        return "В повторном аудите связанного сценария сохранились нарушения."
    addressed = [e for e in evaluations if e.criterion in criteria]
    unsettled = any(
        f.semantic_status in ("unchecked", "uncertain")
        and (not f.requirement_ids or requirements.intersection(f.requirement_ids))
        for f in result.findings
    )
    if (
        set(e.criterion for e in addressed) != criteria
        or result.coverage.uncertainties
        or unsettled
        or any(e.status != "pass" for e in addressed)
    ):
        return "В связанном сценарии осталась неопределённость. Проверьте оценки нового отчёта."
    return "Исправленное правило и связанные требования найдены и проверены. Исходное нарушение не повторилось."


def recheck_diff(previous: ValidationReport, result: ValidationReport) -> list[dict[str, str]]:
    """Build a canonical before-and-after comparison table between revisions."""
    prev_s = previous.statistics
    next_s = result.statistics
    return [
        {
            "Показатель": "Готовность",
            "До исправления": READINESS[previous.verdict.readiness],
            "После повторной проверки": READINESS[result.verdict.readiness],
        },
        {
            "Показатель": "Статус покрытия",
            "До исправления": EXECUTION[previous.coverage.execution],
            "После повторной проверки": EXECUTION[result.coverage.execution],
        },
        {
            "Показатель": "Подтверждённые нарушения",
            "До исправления": str(prev_s.violations),
            "После повторной проверки": str(next_s.violations),
        },
        {
            "Показатель": "Критические дефекты",
            "До исправления": str(prev_s.critical),
            "После повторной проверки": str(next_s.critical),
        },
        {
            "Показатель": "Открытые гипотезы",
            "До исправления": str(prev_s.open_hypotheses),
            "После повторной проверки": str(next_s.open_hypotheses),
        },
        {
            "Показатель": "Открытые вопросы",
            "До исправления": str(prev_s.questions),
            "После повторной проверки": str(next_s.questions),
        },
    ]


def _make_local_proposal(
    report: ValidationReport,
    *,
    source: EvidenceRef,
    proposed_text: str | None,
    rationale: str,
    status: str = "draft",
    question_id: str | None = None,
    finding_ids: list[str] | None = None,
    requirement_ids: list[str] | None = None,
    stakeholder_decision: str | None = None,
    origin: str = "stakeholder_decision",
) -> RemediationProposal:
    version = report.document.version.sha256
    seed = f"{version}|{source.block_id}|{source.start}|{source.end}|{question_id or ''}|{','.join(sorted(finding_ids or []))}"
    pid = f"RP-{sha256(seed.encode('utf-8')).hexdigest()[:8]}"
    return RemediationProposal(
        proposal_id=pid,
        document_version=version,
        status=status,
        origin=origin,
        question_id=question_id,
        finding_ids=list(finding_ids or []),
        requirement_ids=list(requirement_ids or []),
        source=source,
        proposed_text=proposed_text,
        rationale=rationale,
        stakeholder_decision=stakeholder_decision,
    )


def _overlaps_approved(
    candidate_ref: EvidenceRef,
    proposals: dict[str, RemediationProposal],
    *,
    ignore_id: str | None = None,
) -> RemediationProposal | None:
    for pid, prop in proposals.items():
        if pid == ignore_id or prop.status != "approved":
            continue
        if prop.source.block_id != candidate_ref.block_id:
            continue
        if candidate_ref.start < prop.source.end and prop.source.start < candidate_ref.end:
            return prop
    return None


def _render_proposal_status(proposal: RemediationProposal) -> None:
    pill_cls = "rv-pill"
    if proposal.status == "approved":
        pill_cls += " rv-pill--approved"
    elif proposal.status == "rejected":
        pill_cls += " rv-pill--rejected"
    elif proposal.status == "needs_clarification":
        pill_cls += " rv-pill--warn"
    status_ru = _PROPOSAL_STATUS_LABEL.get(proposal.status, proposal.status)
    origin_ru = _PROPOSAL_ORIGIN_LABEL.get(proposal.origin, proposal.origin)
    st.markdown(
        f'<span class="{pill_cls}">{html.escape(status_ru)}</span> '
        f'<span class="rv-pill">{html.escape(origin_ru)}</span> '
        f'<span class="rv-pill">{html.escape(proposal.proposal_id)}</span>',
        unsafe_allow_html=True,
    )
    st.caption(proposal.rationale)
    if proposal.status == "needs_clarification" and proposal.clarification_needed:
        st.warning(proposal.clarification_needed)


def _render_bundle_and_revalidation(
    report: ValidationReport,
    settings: ProviderSettings,
    *,
    scope: tuple[str, str, str, str],
    prefix: str,
    bundle: ImprovedDocumentBundle,
    single_context: tuple[object, ProposedChange] | None,
) -> None:
    version = report.document.version
    child = bundle.document
    improved_md = export_improved_markdown(bundle)
    change_log_md = render_change_log_markdown(bundle.change_log)
    md_hash = sha256(improved_md.encode("utf-8")).hexdigest()

    st.caption(
        f"Исходная версия {version.sha256[:12]} → рабочая версия Markdown {child.version.sha256[:12]} "
        f"(применено правок: {bundle.change_log.applied_count}; "
        "исходный отчёт и статус дефектов сохраняются неизменными до повторной проверки)."
    )
    if version.format == DocumentFormat.PDF:
        st.info(
            "Исходный документ был в формате PDF. Экспортируемый файл requirements_improved.md "
            "является производной рабочей копией в формате Markdown — исходная вёрстка PDF не сохраняется."
        )
    if md_hash != child.version.sha256:
        st.error("Несоответствие SHA-256 экспортируемого документа. Примените правки повторно.")
        return

    with st.container(border=True):
        st.markdown(
            '<p class="rv-section-title">Улучшенный рабочий документ и журнал изменений</p>'
            '<p class="rv-section-sub">Полная спецификация с сохранёнными незатронутыми разделами, таблицами '
            "и идентификаторами требований. Скачивание не выполняет вызовов к Gemini.</p>",
            unsafe_allow_html=True,
        )
        with st.container(horizontal=True):
            st.download_button(
                "Скачать улучшенный документ (.md)",
                improved_md,
                "requirements_improved.md",
                "text/markdown; charset=utf-8",
                key=f"{prefix}-dl-improved-{child.version.sha256[:10]}",
                on_click="ignore",
            )
            st.download_button(
                "Скачать журнал изменений (.md)",
                change_log_md,
                "changes.md",
                "text/markdown; charset=utf-8",
                key=f"{prefix}-dl-changelog-{child.version.sha256[:10]}",
                on_click="ignore",
            )
        with st.expander("Предпросмотр полного улучшенного документа и журнала изменений", expanded=False):
            prev_tab, log_tab = st.tabs(["Полный документ (Markdown)", "Журнал изменений"])
            with prev_tab:
                st.code(improved_md, language="markdown")
            with log_tab:
                st.markdown(change_log_md)

    if report.metadata.origin == "synthetic":
        st.info("Локальный синтетический пример. Для повторной проверки откройте отчёт реального запуска.")
        return

    focus_id = single_context[0].question_id if single_context is not None else "batch"
    run_key = (scope, focus_id, child.version.sha256, settings.model, settings.timeout_ms, _rules_version())
    result_store = st.session_state.setdefault("clarification_runs", {})
    consent = st.checkbox(
        "Разрешаю передать исправленный документ в Google Gemini",
        key=f"{prefix}-reval-consent-{child.version.sha256}",
    )
    rerun_btn_key = f"{prefix}-{focus_id}-rerun"
    if (
        st.button(
            "Повторить полный анализ",
            type="primary",
            disabled=not consent or run_key in result_store,
            key=rerun_btn_key,
        )
        and consent
        and run_key not in result_store
    ):
        if not settings.api_key:
            st.error("Для повторной проверки требуется ключ Gemini.")
            return
        with st.status("Повторный анализ рабочей версии", expanded=True) as status:
            tracker = ProgressTracker(status, STAGE, STATE)
            try:
                result = run_with_progress(
                    tracker,
                    revalidate_improved_document,
                    bundle,
                    settings,
                    runner=pipeline.run_pipeline,
                )
                if result.document != child or result.metadata.origin != "live":
                    raise ValueError("Unexpected report version or origin")
                result_store[run_key] = result
                status.update(
                    label=f"Получен результат повторной проверки · {tracker.elapsed_seconds:.1f} с",
                    state="error" if result.coverage.execution == "failed" else "complete",
                )
            except ProviderError as error:
                logger.warning("ui_revalidation_provider_error reason=%s", error.reason.value)
                status.update(label=f"Сбой Gemini · {error.reason.value}", state="error")
            except Exception as error:
                logger.error("ui_revalidation_internal_error error_type=%s", type(error).__name__)
                status.update(
                    label="Повторная проверка не завершена. Исходный отчёт сохранён.",
                    state="error",
                )

    if run_key in result_store:
        result = result_store[run_key]
        comparison = compare_reports(report, result, applied_changes=bundle.change_log.applied_count)
        orig_md, orig_json = get_report_exports(report)
        reval_md, reval_json = get_report_exports(result)

        with st.container(border=True):
            st.caption("Сравнение до и после исправления")
            if single_context is not None:
                st.text(recheck_summary(report, single_context[0], single_context[1], result))
            elif not comparison["complete"]:
                st.text("Повторная проверка неполная. Устранение дефектов не подтверждено.")
            else:
                st.text(
                    f"Повторная проверка завершена полностью. Применено правок: {comparison['applied_changes']}, "
                    f"устранено дефектов: {len(comparison['resolved'])}, "
                    f"осталось: {len(comparison['remaining'])}, новых: {len(comparison['new_defects'])}."
                )
            st.text(
                f"Готовность до исправления — {READINESS[report.verdict.readiness]}. "
                f"После — {READINESS[result.verdict.readiness]}."
            )
            st.dataframe(recheck_diff(report, result), hide_index=True, width="stretch")

            c1, c2, c3, c4 = st.columns(4)
            c1.metric("Применено правок", comparison["applied_changes"], border=True)
            c2.metric("Устранено дефектов", len(comparison["resolved"]), border=True)
            c3.metric("Осталось дефектов", len(comparison["remaining"]), border=True)
            c4.metric("Новых дефектов", len(comparison["new_defects"]), border=True)

            if comparison["resolved"]:
                st.caption(
                    "Подтверждённо устранённые дефекты: "
                    + ", ".join(f.finding_id for f in comparison["resolved"])
                )
            if comparison["unverified_previous"]:
                st.warning(
                    "Неполная повторная проверка не позволяет подтвердить устранение: "
                    + ", ".join(f.finding_id for f in comparison["unverified_previous"])
                )
            if comparison["remaining"]:
                st.caption(
                    "Сохранившиеся дефекты: "
                    + ", ".join(f"{f.finding_id} ({SEVERITY[f.severity]})" for f in comparison["remaining"])
                )
            if comparison["new_defects"]:
                st.warning(
                    "Новые дефекты после правки: "
                    + ", ".join(f"{f.finding_id} ({SEVERITY[f.severity]})" for f in comparison["new_defects"])
                )
            if comparison["unresolved_uncertainties"]:
                st.caption(f"Неразрешённых неопределённостей: {len(comparison['unresolved_uncertainties'])}")

            st.text(result.verdict.rationale)
            st.caption("Комплект файлов для скачивания (исходный отчёт, улучшенное ТЗ, журнал и повторный отчёт)")
            with st.container(horizontal=True):
                st.download_button(
                    "Исходный отчёт Markdown",
                    orig_md,
                    "report_original.md",
                    "text/markdown; charset=utf-8",
                    key=f"{prefix}-dl-orig-md-{result.metadata.run_id}",
                    on_click="ignore",
                )
                st.download_button(
                    "Исходный отчёт JSON",
                    orig_json,
                    "report_original.json",
                    "application/json",
                    key=f"{prefix}-dl-orig-json-{result.metadata.run_id}",
                    on_click="ignore",
                )
                st.download_button(
                    "Повторный отчёт Markdown",
                    reval_md,
                    "report_improved.md",
                    "text/markdown; charset=utf-8",
                    key=f"{prefix}-dl-imp-md-{result.metadata.run_id}",
                    on_click="ignore",
                )
                st.download_button(
                    "Повторный отчёт JSON",
                    reval_json,
                    "report_improved.json",
                    "application/json",
                    key=f"{prefix}-dl-imp-json-{result.metadata.run_id}",
                    on_click="ignore",
                )


def _render_finding_improvement_panel(
    report: ValidationReport,
    settings: ProviderSettings,
    *,
    prefix: str,
    blocks: dict,
    proposals_store: dict[str, RemediationProposal],
) -> None:
    confirmed = [f for f in report.findings if f.confirmed]
    if not confirmed:
        return
    with st.expander(f"Улучшение по любой подтверждённой находке ({len(confirmed)})", expanded=False):
        finding = st.selectbox(
            "Подтверждённая находка",
            confirmed,
            format_func=lambda item: f"{item.finding_id} · {SEVERITY[item.severity]} · {item.claim[:72]}",
            key=prefix + "-finding-select",
        )
        refs = list(finding.evidence)
        if finding.gap and finding.gap.context.anchor not in {r.anchor for r in refs}:
            refs.append(finding.gap.context)
        if not refs:
            st.info("Для выбранной находки нет подтверждённого текстового якоря.")
            return
        for idx, ref in enumerate(refs[:2]):
            render_evidence_block(ref, blocks, tag=f"Источник {idx + 1}" if len(refs) > 1 else None)

        f_target = st.selectbox(
            "Целевой фрагмент находки",
            refs,
            format_func=lambda r: f"{r.block_id} [{r.start}, {r.end})",
            key=f"{prefix}-finding-target-{finding.finding_id}",
        )
        fkey = f"{prefix}-f-{finding.finding_id}-{sha256(repr(f_target.anchor).encode()).hexdigest()[:8]}"
        decision_text = st.text_area(
            "Решение стейкхолдера по находке (опционально, обязательно для пробелов и противоречий)",
            key=fkey + "-decision",
            help="Если находка — пробел или противоречие, укажите подтверждённое решение, чтобы агент не выдумывал правила.",
        ).strip()
        linked_q = next((q for q in report.questions if finding.finding_id in q.finding_ids), None)

        f_draft_key = fkey + "-draft-obj"
        f_edit_key = fkey + "-edit"
        can_use_ai = report.metadata.origin != "synthetic" and settings.api_key is not None
        f_ai_consent = st.checkbox(
            "Разрешаю передать фрагмент находки в Gemini для черновика формулировки",
            key=fkey + "-ai-consent",
            disabled=not can_use_ai,
        )
        if st.button("Подготовить предложение по находке", key=fkey + "-draft-btn"):
            ans = (
                Answer(version=report.document.version.sha256, question_id=linked_q.question_id, custom_text=decision_text)
                if (linked_q is not None and decision_text)
                else None
            )
            try:
                prop = draft_remediation_proposal(
                    report,
                    answer=ans,
                    finding_id=finding.finding_id,
                    target_ref=f_target,
                    settings=settings if (can_use_ai and f_ai_consent) else None,
                    use_ai=bool(can_use_ai and f_ai_consent),
                    allow_fallback=True,
                )
                st.session_state[f_draft_key] = prop
                if prop.proposed_text:
                    st.session_state[f_edit_key] = prop.proposed_text
            except (ValueError, ProviderError) as exc:
                logger.warning("ui_finding_draft_failed error_type=%s", type(exc).__name__)
                st.warning("Не удалось сформировать автоматический черновик для выбранного фрагмента.")

        current_f_draft: RemediationProposal | None = st.session_state.get(f_draft_key)
        if current_f_draft is not None:
            _render_proposal_status(current_f_draft)

        default_val = (
            current_f_draft.proposed_text
            if (current_f_draft is not None and current_f_draft.proposed_text)
            else (decision_text or f_target.quote)
        )
        if f_edit_key not in st.session_state:
            st.session_state[f_edit_key] = default_val
        edited = st.text_area(
            "Редактируемая формулировка по находке",
            key=f_edit_key,
            help="Отредактируйте итоговый текст замены перед утверждением в пакет правок.",
        )
        b_col1, b_col2 = st.columns(2)
        if b_col1.button(
            "Утвердить правку по находке в пакет",
            key=fkey + "-approve",
            disabled=not edited.strip() or edited.strip() == f_target.quote,
        ):
            base_prop = current_f_draft
            if base_prop is None or base_prop.status == "rejected":
                base_prop = _make_local_proposal(
                    report,
                    source=f_target,
                    proposed_text=edited.strip(),
                    rationale=f"Правка по подтверждённой находке {finding.finding_id}.",
                    question_id=linked_q.question_id if linked_q else None,
                    finding_ids=[finding.finding_id],
                    requirement_ids=list(finding.requirement_ids),
                    stakeholder_decision=decision_text or None,
                )
            overlap = _overlaps_approved(f_target, proposals_store, ignore_id=base_prop.proposal_id)
            if overlap is not None:
                st.error(
                    f"Диапазон пересекается с уже утверждённой правкой {overlap.proposal_id} "
                    f"в блоке {overlap.source.block_id}. Сначала удалите или отклоните конфликтующую правку."
                )
            else:
                try:
                    approved = approve_proposal(base_prop, edited_text=edited.strip())
                    proposals_store[approved.proposal_id] = approved
                    st.session_state[f_draft_key] = approved
                    st.success(f"Правка {approved.proposal_id} утверждена и добавлена в пакет изменений.")
                except ValueError as exc:
                    st.error(str(exc))
        if b_col2.button("Отклонить правку по находке", key=fkey + "-reject"):
            if current_f_draft is not None:
                rejected = reject_proposal(current_f_draft, reason="Отклонено пользователем.")
            else:
                rejected = _make_local_proposal(
                    report,
                    source=f_target,
                    proposed_text=None,
                    status="rejected",
                    rationale=f"Предложение по находке {finding.finding_id} отклонено пользователем.",
                    question_id=linked_q.question_id if linked_q else None,
                    finding_ids=[finding.finding_id],
                    requirement_ids=list(finding.requirement_ids),
                )
            proposals_store[rejected.proposal_id] = rejected
            st.session_state[f_draft_key] = rejected
            st.info(f"Предложение {rejected.proposal_id} помечено как отклонённое.")


def render_clarification(report: ValidationReport, settings: ProviderSettings) -> None:
    """Render stakeholder clarification, AI improvement drafting, multi-edit export, and revalidation."""
    confirmed_findings = [f for f in report.findings if f.confirmed]
    if not report.questions and not confirmed_findings:
        return

    st.divider()
    st.markdown(
        '<p class="rv-section-title">Центр уточнения требований, AI-улучшения и повторной проверки</p>'
        '<p class="rv-section-sub">Зафиксируйте решение стейкхолдера, при желании сгенерируйте AI-формулировку, '
        "отредактируйте и утвердите правки, скачайте полный улучшенный документ и подтвердите устранение дефектов "
        "полным повторным анализом.</p>",
        unsafe_allow_html=True,
    )

    version = report.document.version
    view = get_report_view(report)
    blocks = view.blocks_by_id
    scope = (version.sha256, report.metadata.run_id, report.metadata.model, report.metadata.prompt_version)
    prefix = "clarify-" + sha256(repr(scope).encode()).hexdigest()

    proposals_store: dict[str, RemediationProposal] = st.session_state.setdefault(prefix + "-proposals", {})
    ai_drafts: dict[str, RemediationProposal] = st.session_state.setdefault(prefix + "-ai-drafts", {})
    batch_bundle_key = prefix + "-batch-bundle"

    active_bundle: ImprovedDocumentBundle | None = st.session_state.get(batch_bundle_key)
    single_context: tuple[object, ProposedChange] | None = None

    if report.questions:
        f_col1, f_col2 = st.columns([1.0, 1.4], gap="medium")
        priorities = sorted({q.priority for q in report.questions})
        priority = f_col1.selectbox("Приоритет вопроса", ["Все", *priorities], key=prefix + "-priority")
        roles = sorted({q.stakeholder for q in report.questions})
        role_choice = f_col2.pills(
            "Стейкхолдер",
            ["Все", *[ROLE[r] for r in roles]],
            default="Все",
            key=prefix + "-role",
        )

        queue = sorted(
            (
                q
                for q in report.questions
                if (priority == "Все" or q.priority == priority)
                and (not role_choice or role_choice in ("Все", ROLE[q.stakeholder]))
            ),
            key=lambda q: q.priority,
        )
        if not queue:
            st.info("По выбранным фильтрам вопросов нет.")
        else:
            q = st.selectbox(
                "Очередь вопросов",
                queue,
                format_func=lambda item: f"{item.priority} · {item.question_id} · {ROLE[item.stakeholder]}",
                key=prefix + "-question",
            )
            key = prefix + "-" + q.question_id

            with st.container(border=True):
                st.caption(f"{q.priority} · {ROLE[q.stakeholder]} · Находки: {', '.join(q.finding_ids)}")
                st.text(q.question)
                st.caption("Неопределённость и бизнес-влияние")
                st.text(q.uncertainty)
                st.text(q.business_impact)

                refs = {r.anchor: r for r in q.evidence}
                for finding in report.findings:
                    if finding.kind == "contradiction" and finding.finding_id in q.finding_ids:
                        refs.update((r.anchor, r) for r in finding.evidence)
                cols = st.columns(min(2, len(refs)), gap="medium")
                for index, ref in enumerate(refs.values()):
                    render_evidence_block(
                        ref,
                        blocks,
                        container=cols[index % len(cols)],
                        tag=f"Цитата {index + 1}" if len(refs) > 1 else None,
                    )

            with st.container(border=True):
                options = {o.option_id: o for o in q.options}
                custom_sentinel = "__custom__"
                choice = st.radio(
                    "Ответ владельца",
                    [*options, custom_sentinel],
                    format_func=lambda i: options[i].label if i != custom_sentinel else "Свой ответ",
                    key=key + "-choice",
                )
                custom = None
                option_id = None if choice == custom_sentinel else choice
                if option_id is None:
                    custom = st.text_area("Свой ответ", key=key + "-custom").strip()
                else:
                    st.caption("Последствия выбранного решения")
                    st.text(options[option_id].consequence)

                if option_id is not None or custom:
                    answer = Answer(
                        version=version.sha256,
                        question_id=q.question_id,
                        option_id=option_id,
                        custom_text=custom,
                    )
                    try:
                        draft = propose_change(answer, report)
                    except ValueError:
                        st.warning("Нельзя подготовить изменение для выбранного ответа и источника.")
                        draft = None

                    if draft is not None:
                        target = st.selectbox(
                            "Изменяемый фрагмент",
                            q.evidence,
                            format_func=lambda r: f"{r.block_id} [{r.start}, {r.end})",
                            index=q.evidence.index(draft.source),
                            key=key + "-target",
                        )
                        edit_key = key + "-" + sha256((answer.model_dump_json() + repr(target.anchor)).encode()).hexdigest()

                        # Optional explicit AI-assisted requirement formulation drafting.
                        can_ai_draft = report.metadata.origin != "synthetic" and settings.api_key is not None
                        ai_col1, ai_col2 = st.columns([2.1, 1.2], vertical_alignment="center")
                        ai_consent = ai_col1.checkbox(
                            "Разрешаю отправить фрагмент и решение в Gemini для AI-формулировки",
                            key=edit_key + "-ai-consent",
                            disabled=not can_ai_draft,
                        )
                        if ai_col2.button(
                            "Сгенерировать AI-формулировку",
                            key=edit_key + "-ai-btn",
                            disabled=not (can_ai_draft and ai_consent),
                            use_container_width=True,
                        ):
                            try:
                                proposal = draft_remediation_proposal(
                                    report,
                                    answer=answer,
                                    target_ref=target,
                                    settings=settings,
                                    use_ai=True,
                                    allow_fallback=True,
                                )
                                ai_drafts[edit_key] = proposal
                                if proposal.proposed_text:
                                    st.session_state[edit_key + "-edit"] = proposal.proposed_text
                            except (ValueError, ProviderError) as exc:
                                logger.warning("ui_ai_draft_failed error_type=%s", type(exc).__name__)
                                st.error("Не удалось сгенерировать AI-формулировку. Отредактируйте текст вручную.")

                        active_ai_proposal = ai_drafts.get(edit_key)
                        if active_ai_proposal is not None:
                            _render_proposal_status(active_ai_proposal)

                        before, after = st.columns(2, gap="medium")
                        before.caption("До изменения")
                        before.code(target.quote, language=None, wrap_lines=True)
                        replacement = after.text_area(
                            "После изменения",
                            draft.replacement,
                            key=edit_key + "-edit",
                            help="Введите или отредактируйте полную формулировку требования или критерия приёмки.",
                        )

                        btn_apply, btn_approve, btn_reject = st.columns([1.3, 1.3, 1.0], gap="small")
                        current = st.session_state.get(key + "-applied")
                        can_apply_edit = bool(replacement.strip()) and replacement != target.quote
                        if btn_apply.button(
                            "Применить к рабочей копии",
                            key=key + "-apply",
                            disabled=not can_apply_edit,
                            use_container_width=True,
                        ):
                            try:
                                change = ProposedChange(question_id=q.question_id, source=target, replacement=replacement)
                                single_bundle = apply_changes(report.document, [change])
                            except ValueError:
                                st.error("Изменение не применено. Проверьте исходную версию, цитату и размер правки.")
                                single_bundle = None
                            if single_bundle is not None:
                                current = (answer, change, single_bundle)
                                st.session_state[key + "-applied"] = current
                                st.session_state.pop(batch_bundle_key, None)

                        if btn_approve.button(
                            "Утвердить в пакет правок",
                            key=key + "-approve",
                            disabled=not can_apply_edit,
                            use_container_width=True,
                        ):
                            base_prop = active_ai_proposal
                            if base_prop is None or base_prop.status == "rejected":
                                base_prop = _make_local_proposal(
                                    report,
                                    source=target,
                                    proposed_text=replacement.strip(),
                                    rationale=f"Утверждённая правка по вопросу {q.question_id}.",
                                    question_id=q.question_id,
                                    finding_ids=list(q.finding_ids),
                                    requirement_ids=list(q.requirement_ids),
                                    stakeholder_decision=answer.custom_text or options[option_id].label,
                                )
                            overlap = _overlaps_approved(target, proposals_store, ignore_id=base_prop.proposal_id)
                            if overlap is not None:
                                st.error(
                                    f"Правка пересекается с утверждённым предложением {overlap.proposal_id} "
                                    f"в блоке {overlap.source.block_id}."
                                )
                            else:
                                try:
                                    approved = approve_proposal(base_prop, edited_text=replacement.strip())
                                    proposals_store[approved.proposal_id] = approved
                                    ai_drafts[edit_key] = approved
                                    st.success(f"Правка {approved.proposal_id} добавлена в пакет утверждённых изменений.")
                                except ValueError as exc:
                                    st.error(str(exc))

                        if btn_reject.button(
                            "Отклонить",
                            key=key + "-reject",
                            use_container_width=True,
                        ):
                            if active_ai_proposal is not None:
                                rejected = reject_proposal(active_ai_proposal, reason="Отклонено пользователем.")
                            else:
                                rejected = _make_local_proposal(
                                    report,
                                    source=target,
                                    proposed_text=None,
                                    status="rejected",
                                    rationale=f"Предложение по вопросу {q.question_id} отклонено пользователем.",
                                    question_id=q.question_id,
                                    finding_ids=list(q.finding_ids),
                                    requirement_ids=list(q.requirement_ids),
                                )
                            proposals_store[rejected.proposal_id] = rejected
                            ai_drafts[edit_key] = rejected
                            st.info(f"Предложение {rejected.proposal_id} отклонено.")

                        if current is not None:
                            applied_answer, change, single_bundle = current
                            if applied_answer != answer or change.source != target or change.replacement != replacement:
                                st.info("Черновик изменён. Примените текущую формулировку к рабочей копии.")
                            elif active_bundle is None:
                                active_bundle = single_bundle
                                single_context = (q, change)

    _render_finding_improvement_panel(
        report,
        settings,
        prefix=prefix,
        blocks=blocks,
        proposals_store=proposals_store,
    )

    if proposals_store:
        with st.container(border=True):
            approved_list = [p for p in proposals_store.values() if p.status == "approved"]
            rejected_list = [p for p in proposals_store.values() if p.status == "rejected"]
            st.markdown(
                '<p class="rv-section-title">Пакет предложений по улучшению документа</p>'
                f'<p class="rv-section-sub">Утверждено: {len(approved_list)} · Отклонено: {len(rejected_list)}. '
                "В рабочий документ применяются только явно утверждённые непересекающиеся правки.</p>",
                unsafe_allow_html=True,
            )
            st.dataframe(
                [
                    {
                        "ID": p.proposal_id,
                        "Статус": _PROPOSAL_STATUS_LABEL.get(p.status, p.status),
                        "Источник": _PROPOSAL_ORIGIN_LABEL.get(p.origin, p.origin),
                        "Блок": f"{p.source.block_id} [{p.source.start}, {p.source.end})",
                        "Связи": ", ".join(p.finding_ids) or (p.question_id or "—"),
                        "Новая формулировка": p.proposed_text or "—",
                    }
                    for p in proposals_store.values()
                ],
                hide_index=True,
                width="stretch",
            )
            p_col1, p_col2 = st.columns([1.8, 1.0])
            if p_col1.button(
                f"Применить утверждённые правки ({len(approved_list)})",
                type="primary",
                key=prefix + "-apply-batch",
                disabled=not approved_list,
                use_container_width=True,
            ):
                try:
                    batch_bundle = apply_changes(report.document, approved_list)
                    st.session_state[batch_bundle_key] = batch_bundle
                    active_bundle = batch_bundle
                    single_context = None
                except ValueError as exc:
                    st.error(f"Не удалось применить пакет правок: {exc}")
            if p_col2.button("Очистить пакет", key=prefix + "-clear-batch", use_container_width=True):
                proposals_store.clear()
                st.session_state.pop(batch_bundle_key, None)
                active_bundle = None
                st.rerun()

    if active_bundle is not None:
        _render_bundle_and_revalidation(
            report,
            settings,
            scope=scope,
            prefix=prefix,
            bundle=active_bundle,
            single_context=single_context,
        )
