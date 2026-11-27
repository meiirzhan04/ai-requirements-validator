"""Enterprise Fintech Streamlit dashboard for static requirements validation."""
from hashlib import sha256
import html
from pathlib import Path

import streamlit as st
from pydantic import ValidationError

from src import ingest, pipeline
from src.contracts import ProgressEvent, ProviderSettings, ValidationReport
from src.provider import ProviderError, load_settings
from src.report import (
    CATEGORY,
    CRITERION_STATUS,
    EXECUTION,
    GROUNDING,
    READINESS,
    ROLE,
    SEMANTIC,
    SEVERITY,
    load_json,
    render_json,
    render_markdown,
)

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

_READINESS_STYLE = {
    "go": ("#15803D", "rgba(22, 163, 74, 0.12)", "#16A34A"),
    "conditional_go": ("#B45309", "rgba(217, 119, 6, 0.14)", "#D97706"),
    "no_go": ("#B91C1C", "rgba(220, 38, 38, 0.12)", "#DC2626"),
    "undetermined": ("#1D4ED8", "rgba(37, 99, 235, 0.12)", "#2563EB"),
}
_SEV_COLOR = {
    "critical": "#DC2626",
    "major": "#D97706",
    "minor": "#0284C7",
}


def inject_theme() -> None:
    """Apply restrained Enterprise Fintech typography, spacing, and contrast rules (light & dark safe)."""
    st.markdown(
        """
        <style>
        .block-container {
            padding-top: 3.4rem;
            padding-bottom: 3rem;
            max-width: 1280px;
        }
        .req-hero {
            border: 1px solid rgba(148, 163, 184, 0.35);
            background: var(--secondary-background-color);
            border-left: 4px solid #0F4C81;
            border-radius: 10px;
            padding: 1.05rem 1.35rem;
            margin-bottom: 1rem;
        }
        .req-hero-title {
            font-size: 1.4rem;
            font-weight: 700;
            color: var(--text-color);
            margin: 0;
            letter-spacing: -0.015em;
        }
        .req-hero-sub {
            font-size: 0.9rem;
            color: var(--text-color);
            opacity: 0.8;
            margin: 0.3rem 0 0 0;
        }
        .req-verdict-banner {
            border-radius: 10px;
            padding: 1rem 1.25rem;
            margin: 0.5rem 0 1rem 0;
            border: 1px solid rgba(148, 163, 184, 0.25);
            border-left: 5px solid;
        }
        .req-badge {
            display: inline-block;
            font-size: 0.78rem;
            font-weight: 600;
            padding: 0.2rem 0.6rem;
            border-radius: 999px;
            margin-right: 0.35rem;
            margin-bottom: 0.25rem;
            border: 1px solid rgba(148, 163, 184, 0.4);
            background: var(--background-color);
            color: var(--text-color);
        }
        .req-passport-strip {
            display: flex;
            flex-wrap: wrap;
            gap: 0.4rem;
            margin: 0.65rem 0 0.1rem 0;
        }
        .req-dist-bar {
            display: flex;
            height: 10px;
            width: 100%;
            border-radius: 6px;
            overflow: hidden;
            background: rgba(148, 163, 184, 0.3);
            margin: 0.6rem 0 0.7rem 0;
        }
        .req-action-card {
            border: 1px solid rgba(148, 163, 184, 0.3);
            border-radius: 8px;
            padding: 0.55rem 0.85rem;
            margin-bottom: 0.45rem;
            background: var(--background-color);
            color: var(--text-color);
            font-size: 0.88rem;
            line-height: 1.45;
        }
        .req-meta-card {
            border: 1px solid rgba(148, 163, 184, 0.35);
            border-radius: 10px;
            padding: 0.9rem 1.05rem;
            background: var(--secondary-background-color);
            color: var(--text-color);
            margin-bottom: 0.75rem;
        }
        .req-card-title {
            font-size: 0.95rem;
            font-weight: 700;
            color: var(--text-color);
            margin-bottom: 0.35rem;
        }
        div[data-testid="stMetric"] {
            background: var(--secondary-background-color);
            border-radius: 10px;
            padding: 0.75rem 0.95rem;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def render_header() -> None:
    st.markdown(
        """
        <div class="req-hero">
            <p class="req-hero-title">Аудит требований · Requirements Intelligence</p>
            <p class="req-hero-sub">
                Предразработочная статическая проверка финтех-ТЗ по ISO/IEC/IEEE 29148 и IEEE 830
                с дословной привязкой доказательств, независимой семантической проверкой и адресными уточнениями.
            </p>
        </div>
        """,
        unsafe_allow_html=True,
    )


def _rules_version() -> str:
    core = Path(__file__).resolve().parents[1] / "src"
    digest = sha256()
    for directory in ("contracts", "ingest", "evidence", "prompts", "pipeline", "provider", "readiness"):
        for path in sorted((core / directory).glob("*.py")):
            digest.update(f"{directory}/{path.name}".encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()


def analyze() -> ValidationReport | None:
    settings = load_settings()
    with st.container(border=True):
        st.subheader("Загрузка и запуск проверки")
        upload = st.file_uploader(
            "Спецификация",
            type=["md", "pdf"],
            max_upload_size=10,
            help="Markdown (.md) или PDF с текстовым слоем. Ограничения: до 10 МиБ и до 100 страниц PDF.",
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
            if settings.api_key:
                st.caption("Ключ Gemini обнаружен в окружении сервера (значение скрыто).")
            else:
                st.warning("Ключ GEMINI_API_KEY не задан. Запустите локальную проверку или задайте ключ в .env.")

        data = upload.getvalue() if upload is not None else b""
        digest = sha256(data).hexdigest()
        key = (digest, upload.name if upload is not None else "", model, timeout, _rules_version())
        history = st.session_state.setdefault("runs", {})

        if upload is not None:
            size_kb = max(1, round(len(data) / 1024, 1))
            st.caption(
                f"Выбран файл: {upload.name} ({size_kb} КиБ · SHA-256 {digest[:12]}). "
                "Загрузка файла не отправляет данные во внешний API до явного запуска."
            )
        else:
            st.caption(
                "При запуске текст документа будет отправлен в Google Gemini. "
                "Исходный файл и отчёт хранятся только в текущем сеансе."
            )

        consent = st.checkbox(
            "Разрешаю передать этот документ в Google Gemini",
            key=f"consent-{digest}",
            disabled=upload is None,
        )
        col_btn, col_info = st.columns([1.4, 3.6])
        submitted = col_btn.button(
            "Запустить анализ",
            type="primary",
            key="launch",
            disabled=not (upload and model and consent) or key in history,
            use_container_width=True,
        )
        if key in history:
            col_info.caption("Результат для этой ревизии и конфигурации уже доступен в текущем сеансе.")

    failures = st.session_state.setdefault("analysis_errors", {})
    if submitted and consent and key not in history:
        failures.pop(key, None)
        if not settings.api_key:
            failures[key] = "Ключ Gemini отсутствует. Укажите GEMINI_API_KEY в окружении сервера."
            st.error(failures[key])
            return None
        with st.status("Обработка документа", expanded=True) as status:
            def progress(event: ProgressEvent) -> None:
                status.text(f"{STAGE[event.stage]} · {STATE[event.state]} — {event.message}")

            try:
                document = ingest.parse(upload.name, data)
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
                label=EXECUTION[result.coverage.execution] + " анализ",
                state="error" if result.coverage.execution == "failed" else "complete",
            )
    elif key in failures:
        st.error(failures[key])

    active_report = history.get(key)
    if active_report is None and upload is None and history:
        st.caption(f"Сохранённых прогонов в текущем сеансе: {len(history)}. Загрузите файл или откройте JSON-отчёт.")
    return active_report


def open_saved() -> ValidationReport | None:
    with st.container(border=True):
        st.subheader("Просмотр сохранённого канонического отчёта")
        upload = st.file_uploader(
            "Открыть JSON-отчёт",
            type=["json"],
            help="Локальная проверка схемы, статистики, вердикта и привязки цитат. Вызовы к Gemini не выполняются.",
        )
    if upload is None:
        return None
    try:
        return load_json(upload.getvalue())
    except ValidationError:
        st.error("Не удалось открыть отчёт. Проверьте версию схемы, статистику и ссылки на источники.")
        return None


def _render_severity_distribution(report: ValidationReport) -> None:
    stats = report.statistics
    refuted = sum(1 for f in report.findings if f.semantic_status == "refuted")
    total_bar = stats.critical + stats.major + stats.minor + stats.open_hypotheses
    if total_bar > 0:
        seg_crit = stats.critical / total_bar * 100
        seg_maj = stats.major / total_bar * 100
        seg_min = stats.minor / total_bar * 100
        seg_open = stats.open_hypotheses / total_bar * 100
        bar_html = (
            '<div class="req-dist-bar">'
            f'<div style="width:{seg_crit:.1f}%;background:#DC2626;" title="Критические"></div>'
            f'<div style="width:{seg_maj:.1f}%;background:#D97706;" title="Существенные"></div>'
            f'<div style="width:{seg_min:.1f}%;background:#0284C7;" title="Незначительные"></div>'
            f'<div style="width:{seg_open:.1f}%;background:#64748B;" title="Открытые гипотезы"></div>'
            "</div>"
        )
    else:
        bar_html = '<div class="req-dist-bar"><div style="width:100%;background:#16A34A;"></div></div>'

    st.markdown(
        '<div class="req-meta-card">'
        '<div class="req-card-title">Распределение подтверждённых дефектов и гипотез</div>'
        f"{bar_html}"
        f'<span class="req-badge" style="color:#DC2626;">Критические: {stats.critical}</span>'
        f'<span class="req-badge" style="color:#D97706;">Существенные: {stats.major}</span>'
        f'<span class="req-badge" style="color:#0284C7;">Незначительные: {stats.minor}</span>'
        f'<span class="req-badge">Открытые гипотезы: {stats.open_hypotheses}</span>'
        f'<span class="req-badge" style="color:#16A34A;">Опровергнуто проверкой: {refuted}</span>'
        "</div>",
        unsafe_allow_html=True,
    )


def _render_remediation_roadmap(report: ValidationReport) -> None:
    actions = report.verdict.next_actions
    items_html = ""
    if not actions:
        items_html = '<div class="req-action-card">Первоочередные исправления не требуются.</div>'
    else:
        for idx, action in enumerate(actions[:6], start=1):
            items_html += (
                f'<div class="req-action-card"><strong>{idx}.</strong> {html.escape(action)}</div>'
            )
        if len(actions) > 6:
            items_html += (
                f'<div style="font-size:0.82rem;opacity:0.75;">Ещё действий в отчёте: {len(actions) - 6}</div>'
            )
    st.markdown(
        '<div class="req-meta-card">'
        '<div class="req-card-title">Первоочередной план устранения</div>'
        f'<div style="margin-top:0.5rem;">{items_html}</div>'
        "</div>",
        unsafe_allow_html=True,
    )


def show_report(report: ValidationReport, *, saved: bool) -> None:
    version = report.document.version
    meta = report.metadata
    if meta.origin == "synthetic":
        origin = "Синтетический пример"
    else:
        origin = "Сохранённый результат запуска" if saved else "Текущий запуск Gemini"

    st.divider()
    st.caption(origin)

    fg, bg, accent = _READINESS_STYLE[report.verdict.readiness]
    execution = report.coverage.execution
    tokens_label = (
        f"{meta.input_tokens:,} / {meta.output_tokens:,}"
        if meta.input_tokens is not None and meta.output_tokens is not None
        else "нет данных"
    )
    st.markdown(
        f'<div class="req-verdict-banner" style="background:{bg};border-left-color:{accent};">'
        f'<div style="font-size:0.78rem;text-transform:uppercase;letter-spacing:0.05em;font-weight:700;color:{fg};">'
        f'Вердикт готовности · Исполнение: {html.escape(EXECUTION[execution])}</div>'
        f'<div style="font-size:1.35rem;font-weight:700;margin-top:0.2rem;color:var(--text-color);">'
        f'{html.escape(READINESS[report.verdict.readiness])}</div>'
        f'<div class="req-passport-strip">'
        f'<span class="req-badge">Документ: {html.escape(version.name)}</span>'
        f'<span class="req-badge">Модель: {html.escape(meta.model)}</span>'
        f'<span class="req-badge">Время: {meta.duration_seconds:.1f} с</span>'
        f'<span class="req-badge">Запросы API: {meta.request_attempts}</span>'
        f'<span class="req-badge">Токены (вх/вых): {html.escape(tokens_label)}</span>'
        f'<span class="req-badge">Git: {html.escape(meta.agent_revision)}</span>'
        f"</div></div>",
        unsafe_allow_html=True,
    )

    st.text(report.verdict.rationale)
    st.text(f"{version.name}  ·  {meta.started_at:%d.%m.%Y %H:%M %z}  ·  {meta.model}")

    if execution == "failed":
        st.error("Анализ завершился с ошибкой — пригодная оценка отсутствует.")
    elif execution == "partial":
        st.warning("Часть документа осталась непроверенной — вердикт GO заблокирован политикой безопасности.")
    else:
        st.success("Анализ завершён полностью по всем запланированным блокам и критериям.")

    stats = report.statistics
    cols = st.columns(4)
    for col, label, count in zip(
        cols,
        ("Требования", "Нарушения", "Критические", "Вопросы"),
        (stats.requirements, stats.violations, stats.critical, stats.questions),
        strict=True,
    ):
        col.metric(label, count, border=True)

    ov_left, ov_right = st.columns([1.15, 1.35])
    with ov_left:
        _render_severity_distribution(report)
    with ov_right:
        _render_remediation_roadmap(report)

    reqs_tab, findings_tab, standards_tab, coverage_tab, questions_tab = st.tabs(
        [
            f"Требования ({stats.requirements})",
            f"Находки и доказательства ({len(report.findings)})",
            "Стандарты ISO / IEEE",
            "Покрытие и метрики",
            f"Вопросы ({stats.questions})",
        ]
    )
    blocks = {b.block_id: b for b in version.blocks}

    def render_evidence(ref, target_container=st) -> None:
        loc = blocks[ref.block_id].location
        position = f"стр. {loc.page}" if loc.page else f"строки {loc.line_start}–{loc.line_end}"
        grounding_label = GROUNDING[ref.grounding]
        target_container.text(
            f"{loc.section} · {position} · {ref.block_id} [{ref.start}, {ref.end}) · {grounding_label}"
        )
        target_container.code(ref.quote, language=None, wrap_lines=True)

    with reqs_tab:
        if report.requirements:
            evals_by_req: dict[str, list] = {}
            for ev in report.evaluations:
                if ev.requirement_id is not None:
                    evals_by_req.setdefault(ev.requirement_id, []).append(ev)

            req_rows = []
            for r in report.requirements:
                req_evals = evals_by_req.get(r.req_id, [])
                fails = sum(1 for e in req_evals if e.status == "fail")
                passes = sum(1 for e in req_evals if e.status == "pass")
                req_rows.append(
                    {
                        "ID": r.req_id,
                        "Исходный ID": r.original_id or "—",
                        "Категория": CATEGORY[r.category],
                        "Раздел": blocks[r.source.block_id].location.section,
                        "ISO (OK / Наруш.)": f"{passes} / {fails}" if req_evals else "не проверено",
                        "Требование": r.summary,
                    }
                )
            st.dataframe(req_rows, hide_index=True, width="stretch")
            selected = st.selectbox("Источник требования", [r.req_id for r in report.requirements])
            selected_req = next(r for r in report.requirements if r.req_id == selected)
            render_evidence(selected_req.source)
            selected_evals = evals_by_req.get(selected, [])
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
        f_col1, f_col2 = st.columns([1.2, 2.2])
        level = f_col1.selectbox("Серьёзность", ["Все", *SEVERITY.values()])
        search_query = f_col2.text_input(
            "Поиск по тексту или ID",
            placeholder="Например: F001, HTTP, порог...",
            key=f"find-search-{meta.run_id}",
        ).strip().lower()
        semantic_filter = st.pills(
            "Статус проверки",
            ["Все", *SEMANTIC.values()],
            default="Все",
            key=f"sem-filter-{meta.run_id}",
        )

        matches = []
        for f in report.findings:
            if level not in ("Все", SEVERITY[f.severity]):
                continue
            if semantic_filter and semantic_filter not in ("Все", SEMANTIC[f.semantic_status]):
                continue
            if search_query:
                haystack = f"{f.finding_id} {f.claim} {f.recommendation} {' '.join(f.requirement_ids)}".lower()
                if search_query not in haystack:
                    continue
            matches.append(f)

        if not matches:
            st.info("По выбранному фильтру находок нет.")
        for f in matches:
            kind_ru = FINDING_KIND.get(f.kind, str(f.kind))
            header = f"{f.finding_id} · {SEVERITY[f.severity]} · {SEMANTIC[f.semantic_status]} · {kind_ru}"
            with st.expander(header, expanded=(f.confirmed and f.severity == "critical")):
                sev_color = _SEV_COLOR.get(f.severity, "#334155")
                req_label = ", ".join(f.requirement_ids) if f.requirement_ids else "весь документ"
                crit_label = ", ".join(f.criteria)
                st.markdown(
                    f'<span class="req-badge" style="color:{sev_color};">{html.escape(SEVERITY[f.severity])}</span>'
                    f'<span class="req-badge">{html.escape(kind_ru)}</span>'
                    f'<span class="req-badge">{html.escape(SEMANTIC[f.semantic_status])}</span>'
                    f'<span class="req-badge">Требования: {html.escape(req_label)}</span>'
                    f'<span class="req-badge">Критерии: {html.escape(crit_label)}</span>',
                    unsafe_allow_html=True,
                )
                st.text(f.claim)
                st.caption("Независимая семантическая проверка")
                st.text(f.verification_reason)
                if f.confirmed:
                    st.caption("Рекомендуемое исправление")
                    st.text(f.recommendation)

                if len(f.evidence) >= 2:
                    st.caption("Сравнение подтверждающих фрагментов")
                    ev_cols = st.columns(2)
                    for idx, ref in enumerate(f.evidence):
                        render_evidence(ref, target_container=ev_cols[idx % 2])
                else:
                    for ref in f.evidence:
                        render_evidence(ref)

                if f.gap:
                    st.caption("Контекст и проверенная область пропуска")
                    st.text(f.gap.expected_information)
                    st.text(
                        f"Проверенные блоки ({len(f.gap.inspected_blocks)}) — "
                        + ", ".join(f.gap.inspected_blocks)
                    )
                    render_evidence(f.gap.context)

    with standards_tab:
        whole_evals = [e for e in report.evaluations if e.requirement_id is None]
        if whole_evals:
            st.caption("Аудит спецификации в целом: ISO/IEC/IEEE 29148:2018 (5.2.6) и IEEE 830-1998 (4.3)")
            st.dataframe(
                [
                    {
                        "Стандарт / Критерий": e.criterion,
                        "Результат": CRITERION_STATUS[e.status],
                        "Связанные находки": ", ".join(e.finding_ids) or "—",
                        "Обоснование": e.rationale,
                    }
                    for e in whole_evals
                ],
                hide_index=True,
                width="stretch",
            )
        else:
            st.info("Оценки спецификации в целом отсутствуют (этап целостного аудита не завершён).")

    with coverage_tab:
        rows = []
        for name, work in (
            ("Блоки", report.coverage.extraction_blocks),
            ("Требования", report.coverage.individual_requirements),
            ("Пакеты", report.coverage.audit_batches),
            ("Межсекционные группы", report.coverage.cross_section_groups),
        ):
            completed = set(work.completed)
            pct = f"{round(len(completed) / len(work.planned) * 100)}%" if work.planned else "—"
            rows.append(
                {
                    "Область": name,
                    "Выполнено": len(completed),
                    "Запланировано": len(work.planned),
                    "Покрытие": pct,
                    "Осталось": ", ".join(x for x in work.planned if x not in completed) or "—",
                }
            )
        st.dataframe(rows, hide_index=True, width="stretch")

        perf_rows = [
            {"Параметр": "ID прогона", "Значение": meta.run_id},
            {"Параметр": "Модель", "Значение": meta.model},
            {"Параметр": "Версия правил", "Значение": meta.prompt_version},
            {"Параметр": "Git-ревизия", "Значение": meta.agent_revision},
            {"Параметр": "Длительность, с", "Значение": f"{meta.duration_seconds:.3f}"},
            {"Параметр": "Попытки запросов API", "Значение": str(meta.request_attempts)},
            {
                "Параметр": "Входные / выходные токены",
                "Значение": tokens_label,
            },
            {"Параметр": "SHA-256 документа", "Значение": version.sha256},
        ]
        st.dataframe(perf_rows, hide_index=True, width="stretch")

        diagnostics = list(
            dict.fromkeys(
                report.document.diagnostics
                + report.coverage.incomplete_reasons
                + report.coverage.uncertainties
            )
        )
        if diagnostics:
            st.caption("Диагностика покрытия и неопределённости")
            for reason in diagnostics:
                st.text(reason)
        if report.document.unread_pages:
            st.text("Непрочитанные страницы — " + ", ".join(map(str, report.document.unread_pages)))
        st.text(f"Попытки запросов — {meta.request_attempts} · Git — {meta.agent_revision}")

    with questions_tab:
        if not report.questions:
            st.info("Вопросов для уточнения нет.")
        for q in sorted(report.questions, key=lambda item: item.priority):
            with st.container(border=True):
                st.caption(f"{q.priority} · {q.question_id} · {ROLE[q.stakeholder]} · Находки: {', '.join(q.finding_ids)}")
                st.text(q.question)
                st.text(q.business_impact)
                for option in q.options:
                    st.text(f"{option.label} — {option.consequence}")

    st.divider()
    with st.container(horizontal=True):
        st.download_button(
            "Скачать Markdown",
            render_markdown(report),
            "report.md",
            "text/markdown; charset=utf-8",
            on_click="ignore",
        )
        st.download_button(
            "Скачать JSON",
            render_json(report),
            "report.json",
            "application/json",
            on_click="ignore",
        )
