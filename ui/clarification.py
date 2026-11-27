"""Interactive stakeholder clarification workflow and before/after revalidation diff."""
from hashlib import sha256

import streamlit as st

from src import pipeline
from src.clarification import propose_change, working_document
from src.contracts import Answer, ExecutionStatus, ProposedChange, ProviderSettings, ValidationReport
from src.provider import ProviderError
from src.report import EXECUTION, READINESS, ROLE, render_json, render_markdown
from ui.dashboard import STAGE, STATE, _rules_version


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


def render_clarification(report: ValidationReport, settings: ProviderSettings) -> None:
    if not report.questions:
        return
    st.divider()
    st.subheader("Центр уточнения требований и повторной проверки")
    version = report.document.version
    scope = (version.sha256, report.metadata.run_id, report.metadata.model, report.metadata.prompt_version)
    prefix = "clarify-" + sha256(repr(scope).encode()).hexdigest()

    f_col1, f_col2 = st.columns(2)
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
        return

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
        cols = st.columns(min(2, len(refs)))
        locations = {b.block_id: b.location for b in version.blocks}
        for index, ref in enumerate(refs.values()):
            col = cols[index % len(cols)]
            loc = locations[ref.block_id]
            position = f"стр. {loc.page}" if loc.page else f"строки {loc.line_start}–{loc.line_end}"
            col.caption(f"{loc.section} · {position} · {ref.block_id} [{ref.start}, {ref.end})")
            col.code(ref.quote, language=None, wrap_lines=True)

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
        if not custom:
            return
    else:
        st.caption("Последствия выбранного решения")
        st.text(options[option_id].consequence)

    answer = Answer(version=version.sha256, question_id=q.question_id, option_id=option_id, custom_text=custom)
    try:
        draft = propose_change(answer, report)
    except ValueError:
        st.warning("Нельзя подготовить изменение для выбранного ответа и источника.")
        return

    target = st.selectbox(
        "Изменяемый фрагмент",
        q.evidence,
        format_func=lambda r: f"{r.block_id} [{r.start}, {r.end})",
        index=q.evidence.index(draft.source),
        key=key + "-target",
    )
    edit_key = key + "-" + sha256((answer.model_dump_json() + repr(target.anchor)).encode()).hexdigest()
    before, after = st.columns(2)
    before.caption("До изменения")
    before.code(target.quote, language=None, wrap_lines=True)
    replacement = after.text_area(
        "После изменения",
        draft.replacement,
        key=edit_key + "-edit",
        help="Введите полную формулировку требования или критерия приёмки.",
    )
    current = st.session_state.get(key + "-applied")
    if st.button(
        "Применить к рабочей копии",
        key=key + "-apply",
        disabled=not replacement.strip() or replacement == target.quote,
    ):
        try:
            change = ProposedChange(question_id=q.question_id, source=target, replacement=replacement)
            child = working_document(report.document, change)
        except ValueError:
            st.error("Изменение не применено. Проверьте исходную версию, цитату и размер правки.")
            return
        current = (answer, change, child)
        st.session_state[key + "-applied"] = current
    if current is None:
        return

    applied_answer, change, child = current
    if applied_answer != answer or change.source != target or change.replacement != replacement:
        st.info("Черновик изменён. Примените текущую формулировку к рабочей копии.")
        return

    st.caption(
        f"Исходная версия {version.sha256[:12]} → рабочая версия Markdown {child.version.sha256[:12]} "
        "(исходный отчёт и статус дефектов сохраняются неизменными до повторной проверки)."
    )
    if report.metadata.origin == "synthetic":
        st.info("Локальный синтетический пример. Для повторной проверки откройте отчёт реального запуска.")
        return

    run_key = (scope, q.question_id, child.version.sha256, settings.model, settings.timeout_ms, _rules_version())
    result_store = st.session_state.setdefault("clarification_runs", {})
    consent = st.checkbox(
        "Разрешаю передать исправленный документ в Google Gemini",
        key=edit_key + "-consent-" + child.version.sha256,
    )
    if (
        st.button(
            "Повторить полный анализ",
            type="primary",
            disabled=not consent or run_key in result_store,
            key=key + "-rerun",
        )
        and consent
        and run_key not in result_store
    ):
        if not settings.api_key:
            st.error("Для повторной проверки требуется ключ Gemini.")
            return
        with st.status("Повторный анализ рабочей версии", expanded=True) as status:
            try:
                result = pipeline.run_pipeline(
                    child,
                    settings,
                    on_progress=lambda e: status.text(f"{STAGE[e.stage]} · {STATE[e.state]} — {e.message}"),
                )
                if result.document != child or result.metadata.origin != "live":
                    raise ValueError("Unexpected report version or origin")
                result_store[run_key] = result
                status.update(
                    label="Получен результат повторной проверки",
                    state="error" if result.coverage.execution == "failed" else "complete",
                )
            except ProviderError as error:
                status.update(label=f"Сбой Gemini · {error.reason.value}", state="error")
            except Exception:
                status.update(
                    label="Повторная проверка не завершена. Исходный отчёт сохранён.",
                    state="error",
                )

    if run_key in result_store:
        result = result_store[run_key]
        with st.container(border=True):
            st.caption("Сравнение до и после исправления")
            st.text(recheck_summary(report, q, change, result))
            st.text(
                f"Готовность до исправления — {READINESS[report.verdict.readiness]}. "
                f"После — {READINESS[result.verdict.readiness]}."
            )
            st.dataframe(recheck_diff(report, result), hide_index=True, width="stretch")
            st.text(result.verdict.rationale)
            with st.container(horizontal=True):
                st.download_button(
                    "Повторный отчёт Markdown",
                    render_markdown(result),
                    "revalidated.md",
                    on_click="ignore",
                )
                st.download_button(
                    "Повторный отчёт JSON",
                    render_json(result),
                    "revalidated.json",
                    on_click="ignore",
                )
