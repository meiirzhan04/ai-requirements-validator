"""Enterprise Fintech Streamlit dashboard for static requirements validation."""
from hashlib import sha256
import html

import streamlit as st

from src import ingest, pipeline
from src.contracts import ProgressEvent, ProviderSettings, ValidationReport
from src.provider import ProviderError, load_settings
from src.report import (
    CRITERION_STATUS,
    EXECUTION,
    READINESS,
    ROLE,
    SEMANTIC,
    SEVERITY,
    load_json,
    render_json,
    render_markdown,
)
from ui.components import (
    ProgressTracker,
    render_app_header,
    render_evidence_block,
    render_executive_verdict,
    render_remediation_panel,
    render_severity_summary,
)
from ui.state import (
    get_report_exports,
    get_report_view,
    load_saved_report_cached,
    rules_version,
    sync_active_document,
)
from ui.theme import SEVERITY_ACCENT, inject_theme

STAGE = {
    "ingest": "Чтение документа",
    "extract": "Извлечение требований",
    "individual_audit": "Аудит требований",
    "set_audit": "Аудит документа",
    "verify": "Проверка доказательств",
    "clarify": "Уточнения",
    "report": "Формирование отчёта",
    "smoke": "Проверка модели",
}
STATE = {"started": "начат", "completed": "завершён", "failed": "сбой"}
FINDING_KIND = {
    "contradiction": "Противоречие",
    "missing_information": "Пробел в требованиях",
    "ambiguity": "Неоднозначность",
    "unverifiable": "Непроверяемость",
    "other": "Прочее замечание",
}

_FINDINGS_PAGE_SIZE = 20


def render_header(active_step: int = 1) -> None:
    """Render the compact enterprise top bar and workflow indicator."""
    render_app_header(active_step=active_step)


def _rules_version() -> str:
    """Return cached backend rules digest without rereading unchanged files on every rerun."""
    return rules_version()


def analyze() -> ValidationReport | None:
    """Upload specification, confirm cloud boundary, run pipeline with real progress, and cache per session."""
    settings = load_settings()
    with st.container(border=True):
        st.markdown(
            '<p class="rv-section-title">Загрузка и запуск проверки</p>'
            '<p class="rv-section-sub">Загрузите спецификацию в формате Markdown или PDF. '
            "Файл проверяется локально до явного подтверждения отправки в Google Gemini.</p>",
            unsafe_allow_html=True,
        )
        left, right = st.columns([1.65, 1.0], gap="medium")
        with left:
            upload = st.file_uploader(
                "Спецификация",
                type=["md", "pdf"],
                max_upload_size=10,
                help="Markdown (.md) или PDF с текстовым слоем. Лимиты: до 10 МиБ и до 100 страниц PDF.",
            )
            with st.expander("Параметры Gemini"):
                c1, c2 = st.columns([2, 1])
                model = c1.text_input("Модель", value=settings.model, key="model").strip()
                timeout = c2.number_input(
                    "Тайм-аут запроса, мс",
                    min_value=1,
                    value=settings.timeout_ms,
                    key="timeout_ms",
                )
        with right:
            key_state = "Настроен в окружении" if settings.api_key else "Не задан (GEMINI_API_KEY)"
            key_color = "#15803D" if settings.api_key else "#B45309"
            st.markdown(
                '<div class="rv-spec-panel">'
                '<div class="rv-spec-panel-title">Параметры и приватность</div>'
                '<div class="rv-spec-row"><span class="rv-spec-key">Форматы</span><span class="rv-spec-val">.md, .pdf (текстовый)</span></div>'
                '<div class="rv-spec-row"><span class="rv-spec-key">Лимиты</span><span class="rv-spec-val">до 10 МиБ · 100 стр.</span></div>'
                '<div class="rv-spec-row"><span class="rv-spec-key">Облачная граница</span><span class="rv-spec-val">Только после согласия</span></div>'
                '<div class="rv-spec-row"><span class="rv-spec-key">Хранение</span><span class="rv-spec-val">Только текущий сеанс</span></div>'
                f'<div class="rv-spec-row"><span class="rv-spec-key">Ключ API</span><span class="rv-spec-val" style="color:{key_color};">{key_state}</span></div>'
                "</div>",
                unsafe_allow_html=True,
            )

        data = upload.getvalue() if upload is not None else b""
        digest = sha256(data).hexdigest()
        filename = upload.name if upload is not None else ""
        sync_active_document("analyze", f"{digest}:{filename}")
        key = (digest, filename, model, timeout, _rules_version())
        history = st.session_state.setdefault("runs", {})

        if upload is not None:
            size_kb = max(1.0, round(len(data) / 1024, 1))
            st.caption(
                f"Выбран файл: {filename} ({size_kb} КиБ · SHA-256 {digest[:12]}). "
                "Загрузка файла не отправляет данные в Gemini до нажатия кнопки запуска."
            )
            if not settings.api_key:
                st.warning("Ключ GEMINI_API_KEY отсутствует в окружении сервера. Добавьте его в .env перед запуском.")
        else:
            st.caption(
                "Перетащите файл .md или .pdf в область загрузки выше. "
                "Текст передаётся в Google Gemini только после подтверждения согласия."
            )

        consent = st.checkbox(
            "Разрешаю передать этот документ в Google Gemini",
            key=f"consent-{digest}",
            disabled=upload is None,
        )
        col_btn, col_info = st.columns([1.3, 3.7], vertical_alignment="center")
        submitted = col_btn.button(
            "Запустить анализ",
            type="primary",
            key="launch",
            disabled=not (upload and model and consent) or key in history,
            use_container_width=True,
        )
        if key in history:
            col_info.caption("Результат для этой ревизии и модели уже сохранён в текущем сеансе. Повторный запрос не нужен.")
        elif upload is not None and not consent:
            col_info.caption("Отметьте согласие на облачную обработку, чтобы активировать запуск.")

    failures = st.session_state.setdefault("analysis_errors", {})
    if submitted and consent and key not in history:
        failures.pop(key, None)
        if not settings.api_key:
            failures[key] = "Ключ Gemini отсутствует. Укажите GEMINI_API_KEY в окружении сервера."
            st.error(failures[key])
            return None
        with st.status("Обработка документа", expanded=True) as status:
            tracker = ProgressTracker(status, STAGE, STATE)

            def progress(event: ProgressEvent) -> None:
                tracker.handle(event)

            try:
                document = ingest.parse(filename, data)
                configured = ProviderSettings(api_key=settings.api_key, model=model, timeout_ms=timeout)
                result = pipeline.run_pipeline(document, configured, on_progress=progress)
                if result.metadata.origin != "live" or result.document.version.sha256 != digest:
                    raise ValueError("Report identity mismatch")
            except ProviderError as error:
                failures[key] = f"Сбой Gemini · {error.reason.value}"
            except NotImplementedError:
                failures[key] = "Модуль обработки ещё не подключён"
            except (ValueError, OSError):
                failures[key] = "Проверка прервана. Проверьте формат и целостность документа."
            except Exception:
                # Provider and parser exceptions may include credentials or private input.
                failures[key] = "Внутренняя ошибка обработки документа"
            if key in failures:
                status.update(label=failures[key], state="error")
                return None
            history[key] = result
            status.update(
                label=f"{EXECUTION[result.coverage.execution]} анализ · {tracker.elapsed_seconds:.1f} с",
                state="error" if result.coverage.execution == "failed" else "complete",
            )
    elif key in failures:
        st.error(failures[key])

    active_report = history.get(key)
    if active_report is None and upload is None and history:
        st.caption(
            f"Сохранённых прогонов в текущем сеансе: {len(history)}. "
            "Загрузите документ снова или переключитесь на «Сохранённый отчёт»."
        )
    return active_report


def open_saved() -> ValidationReport | None:
    """Open and locally validate a saved canonical JSON report with session-scoped caching."""
    with st.container(border=True):
        st.markdown(
            '<p class="rv-section-title">Просмотр сохранённого канонического отчёта</p>'
            '<p class="rv-section-sub">Локальная проверка схемы ValidationReport, статистики, вердикта и цитат. '
            "Вызовы к Gemini не выполняются.</p>",
            unsafe_allow_html=True,
        )
        upload = st.file_uploader(
            "Открыть JSON-отчёт",
            type=["json"],
            help="Выберите сохранённый JSON-отчёт из CLI, UI или demo/replay_*.json.",
        )
    if upload is None:
        sync_active_document("saved", "")
        return None
    raw = upload.getvalue()
    sync_active_document("saved", f"{sha256(raw).hexdigest()}:{upload.name}")
    report, is_invalid = load_saved_report_cached(raw, upload.name)
    if is_invalid or report is None:
        st.error("Не удалось открыть отчёт. Проверьте версию схемы, статистику и ссылки на источники.")
        return None
    return report


def show_report(report: ValidationReport, *, saved: bool) -> None:
    """Render executive verdict, KPIs, 5 structured inspection tabs, and zero-call exports."""
    meta = report.metadata
    if meta.origin == "synthetic":
        origin = "Синтетический пример"
    else:
        origin = "Сохранённый результат запуска" if saved else "Текущий запуск Gemini"

    view = get_report_view(report)
    blocks = view.blocks_by_id
    stats = report.statistics

    st.divider()
    render_executive_verdict(report, origin=origin, view=view)

    cols = st.columns(4)
    for col, label, count in zip(
        cols,
        ("Требования", "Нарушения", "Критические", "Вопросы"),
        (stats.requirements, stats.violations, stats.critical, stats.questions),
        strict=True,
    ):
        col.metric(label, count, border=True)

    ov_left, ov_right = st.columns([1.1, 1.3], gap="medium")
    with ov_left:
        render_severity_summary(report, view)
    with ov_right:
        render_remediation_panel(report)

    reqs_tab, findings_tab, standards_tab, coverage_tab, questions_tab = st.tabs(
        [
            f"Требования ({stats.requirements})",
            f"Находки и доказательства ({len(report.findings)})",
            "Стандарты ISO / IEEE",
            "Покрытие и метрики",
            f"Вопросы ({stats.questions})",
        ]
    )

    with reqs_tab:
        if report.requirements:
            st.dataframe(view.req_rows, hide_index=True, width="stretch")
            selected = st.selectbox("Источник требования", [r.req_id for r in report.requirements])
            selected_req = next(r for r in report.requirements if r.req_id == selected)
            render_evidence_block(selected_req.source, blocks, tag=selected_req.req_id)
            selected_evals = view.evals_by_req.get(selected, [])
            if selected_evals:
                with st.expander(f"Оценка {selected} по 9 критериям ISO/IEC/IEEE 29148:2018 (5.2.5)"):
                    st.dataframe(
                        [
                            {
                                "Критерий": e.criterion,
                                "Статус": CRITERION_STATUS[e.status],
                                "Находки": ", ".join(e.finding_ids) or "—",
                                "Обоснование": e.rationale,
                            }
                            for e in selected_evals
                        ],
                        hide_index=True,
                        width="stretch",
                    )
        else:
            st.info("В отчёте нет извлечённых требований.")

    with findings_tab:
        f_col1, f_col2 = st.columns([1.15, 2.25], gap="medium")
        level = f_col1.selectbox("Серьёзность", ["Все", *SEVERITY.values()])
        search_query = f_col2.text_input(
            "Поиск по тексту, ID или критерию",
            placeholder="Например: F001, R001, HTTP, порог...",
            key=f"find-search-{meta.run_id}-{meta.document_version[:8]}",
        ).strip().lower()
        semantic_filter = st.pills(
            "Статус проверки",
            ["Все", *SEMANTIC.values()],
            default="Все",
            key=f"sem-filter-{meta.run_id}-{meta.document_version[:8]}",
        )

        matches = []
        for finding in report.findings:
            if level not in ("Все", SEVERITY[finding.severity]):
                continue
            if semantic_filter and semantic_filter not in ("Все", SEMANTIC[finding.semantic_status]):
                continue
            if search_query and search_query not in view.finding_haystacks.get(finding.finding_id, ""):
                continue
            matches.append(finding)

        if not matches:
            st.info("По выбранному фильтру находок нет.")
        else:
            total_matches = len(matches)
            visible_matches = matches
            if total_matches > _FINDINGS_PAGE_SIZE:
                total_pages = (total_matches + _FINDINGS_PAGE_SIZE - 1) // _FINDINGS_PAGE_SIZE
                page_key = f"_rv_find_page_{meta.run_id}_{meta.document_version[:8]}"
                page_num = st.number_input(
                    f"Страница находок (всего {total_matches}, по {_FINDINGS_PAGE_SIZE} на странице)",
                    min_value=1,
                    max_value=total_pages,
                    value=1,
                    step=1,
                    key=page_key,
                )
                start_idx = (int(page_num) - 1) * _FINDINGS_PAGE_SIZE
                visible_matches = matches[start_idx : start_idx + _FINDINGS_PAGE_SIZE]
            else:
                st.caption(f"Показано находок: {total_matches} из {len(report.findings)}")

            for finding in visible_matches:
                kind_ru = FINDING_KIND.get(finding.kind, str(finding.kind))
                header = (
                    f"{finding.finding_id} · {SEVERITY[finding.severity]} · "
                    f"{SEMANTIC[finding.semantic_status]} · {kind_ru}"
                )
                with st.expander(header, expanded=(finding.confirmed and finding.severity == "critical")):
                    sev_color = SEVERITY_ACCENT.get(finding.severity, "#334155")
                    req_label = ", ".join(finding.requirement_ids) if finding.requirement_ids else "весь документ"
                    crit_label = ", ".join(finding.criteria)
                    st.markdown(
                        '<div class="rv-meta-line">'
                        f'<span>Серьёзность: <strong style="color:{sev_color};">{html.escape(SEVERITY[finding.severity])}</strong></span>'
                        f"<span>Тип: <strong>{html.escape(kind_ru)}</strong></span>"
                        f"<span>Проверка: <strong>{html.escape(SEMANTIC[finding.semantic_status])}</strong></span>"
                        f"<span>Требования: <strong>{html.escape(req_label)}</strong></span>"
                        f"<span>Критерии: <strong>{html.escape(crit_label)}</strong></span>"
                        "</div>",
                        unsafe_allow_html=True,
                    )
                    st.text(finding.claim)
                    st.caption("Независимая семантическая проверка")
                    st.text(finding.verification_reason)
                    if finding.confirmed:
                        st.caption("Рекомендуемое исправление")
                        st.text(finding.recommendation)

                    if len(finding.evidence) >= 2:
                        st.caption("Сравнение подтверждающих фрагментов")
                        ev_cols = st.columns(2, gap="medium")
                        for idx, ref in enumerate(finding.evidence):
                            render_evidence_block(
                                ref,
                                blocks,
                                container=ev_cols[idx % 2],
                                tag=f"Фрагмент {idx + 1}",
                            )
                    else:
                        for ref in finding.evidence:
                            render_evidence_block(ref, blocks)

                    if finding.gap:
                        st.caption("Контекст и проверенная область пропуска")
                        st.text(finding.gap.expected_information)
                        st.text(
                            f"Проверенные блоки ({len(finding.gap.inspected_blocks)}) — "
                            + ", ".join(finding.gap.inspected_blocks)
                        )
                        render_evidence_block(finding.gap.context, blocks, tag="Контекст")

    with standards_tab:
        if view.whole_eval_rows:
            w_pass, w_fail, w_other = view.whole_summary
            st.caption(
                "Аудит спецификации в целом (ISO/IEC/IEEE 29148:2018 5.2.6 и IEEE 830-1998 4.3) · "
                f"Выполнено: {w_pass} · Нарушено: {w_fail} · Неизвестно / неприменимо: {w_other}"
            )
            st.dataframe(view.whole_eval_rows, hide_index=True, width="stretch")
        else:
            st.info("Оценки спецификации в целом отсутствуют (этап целостного аудита не завершён).")

    with coverage_tab:
        st.dataframe(view.coverage_rows, hide_index=True, width="stretch")
        st.dataframe(view.passport_rows, hide_index=True, width="stretch")
        if view.diagnostics:
            st.caption("Диагностика покрытия и неопределённости")
            for reason in view.diagnostics:
                st.text(reason)
        if report.document.unread_pages:
            st.text("Непрочитанные страницы — " + ", ".join(map(str, report.document.unread_pages)))
        st.text(f"Попытки запросов — {meta.request_attempts} · Git — {meta.agent_revision}")

    with questions_tab:
        if not report.questions:
            st.info("Вопросов для уточнения нет.")
        else:
            st.caption("Для ответа на вопрос, правки рабочей копии и повторного аудита перейдите к разделу ниже.")
            for q in sorted(report.questions, key=lambda item: item.priority):
                with st.container(border=True):
                    st.caption(
                        f"{q.priority} · {q.question_id} · {ROLE[q.stakeholder]} · "
                        f"Находки: {', '.join(q.finding_ids)}"
                    )
                    st.text(q.question)
                    st.text(q.business_impact)
                    for option in q.options:
                        st.text(f"{option.label} — {option.consequence}")

    md_export, json_export = get_report_exports(report)
    with st.container(border=True):
        st.markdown(
            '<p class="rv-section-title">Экспорт отчёта</p>'
            '<p class="rv-section-sub">Канонический снимок в форматах Markdown и JSON без дополнительных обращений к Gemini API.</p>',
            unsafe_allow_html=True,
        )
        with st.container(horizontal=True):
            st.download_button(
                "Скачать Markdown",
                md_export,
                "report.md",
                "text/markdown; charset=utf-8",
                on_click="ignore",
            )
            st.download_button(
                "Скачать JSON",
                json_export,
                "report.json",
                "application/json",
                on_click="ignore",
            )
