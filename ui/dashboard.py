from hashlib import sha256
from pathlib import Path

import streamlit as st
from pydantic import ValidationError

from src import ingest, pipeline
from src.contracts import ProgressEvent, ProviderSettings, ValidationReport
from src.provider import ProviderError, load_settings
from src.report import EXECUTION, READINESS, ROLE, SEMANTIC, SEVERITY, load_json, render_json, render_markdown


STAGE = {"ingest": "Чтение документа", "extract": "Извлечение требований",
         "individual_audit": "Аудит требований", "set_audit": "Аудит документа",
         "verify": "Проверка доказательств", "clarify": "Уточнения",
         "report": "Формирование отчёта", "smoke": "Проверка модели"}
STATE = {"started": "начат", "completed": "завершён", "failed": "сбой"}


def _rules_version() -> str:
    core = Path(__file__).resolve().parents[1] / "src"
    digest = sha256()
    for directory in ("prompts", "pipeline", "readiness"):
        for path in sorted((core / directory).glob("*.py")):
            digest.update(path.name.encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()


def analyze() -> ValidationReport | None:
    settings = load_settings()
    with st.container(border=True):
        st.subheader("Новая проверка")
        upload = st.file_uploader("Спецификация", type=["md", "pdf"], max_upload_size=10,
                                  help="Markdown или PDF с текстовым слоем. До 10 МиБ и 100 страниц PDF.")
        with st.expander("Параметры Gemini"):
            model = st.text_input("Модель", value=settings.model, key="model").strip()
            timeout = st.number_input("Тайм-аут запроса, мс", min_value=1,
                                      value=settings.timeout_ms, key="timeout_ms")
        data = upload.getvalue() if upload is not None else b""
        digest = sha256(data).hexdigest()
        key = (digest, upload.name if upload is not None else "", model, timeout, _rules_version())
        history = st.session_state.setdefault("runs", {})
        st.caption("При запуске текст документа будет отправлен в Google Gemini. "
                   "Исходный файл и отчёт доступны в текущем сеансе.")
        consent = st.checkbox("Разрешаю передать этот документ в Google Gemini",
                              key=f"consent-{digest}", disabled=upload is None)
        submitted = st.button("Запустить анализ", type="primary", key="launch",
                              disabled=not (upload and model and consent) or key in history)
    failures = st.session_state.setdefault("analysis_errors", {})
    if submitted and consent and key not in history:
        failures.pop(key, None)
        if not settings.api_key:
            failures[key] = "Ключ Gemini отсутствует. Укажите GEMINI_API_KEY в окружении сервера."
            st.error(failures[key])
            return None
        with st.status("Обработка документа", expanded=True) as status:
            def progress(event: ProgressEvent) -> None:
                status.text(f"{STAGE[event.stage]} · {STATE[event.state]}")

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
            status.update(label=EXECUTION[result.coverage.execution] + " анализ",
                          state="error" if result.coverage.execution == "failed" else "complete")
    elif key in failures:
        st.error(failures[key])
    return history.get(key)


def open_saved() -> ValidationReport | None:
    upload = st.file_uploader("Открыть JSON-отчёт", type=["json"],
                              help="Локальная проверка структуры, статистики и ссылок на источники.")
    if upload is None:
        return None
    try:
        return load_json(upload.getvalue())
    except ValidationError:
        st.error("Не удалось открыть отчёт. Проверьте версию схемы, статистику и ссылки на источники.")
        return None


def show_report(report: ValidationReport, *, saved: bool) -> None:
    version = report.document.version
    if report.metadata.origin == "synthetic":
        origin = "Синтетический пример"
    else:
        origin = "Сохранённый результат запуска" if saved else "Текущий запуск Gemini"
    st.divider()
    st.caption(origin)
    st.subheader(READINESS[report.verdict.readiness])
    st.text(report.verdict.rationale)
    st.text(f"{version.name}  ·  {report.metadata.started_at:%d.%m.%Y %H:%M %z}  ·  {report.metadata.model}")
    execution = report.coverage.execution
    if execution == "failed":
        st.error("Анализ завершился с ошибкой")
    elif execution == "partial":
        st.warning("Часть документа осталась непроверенной")
    else:
        st.success("Анализ завершён")
    stats = report.statistics
    cols = st.columns(4)
    for col, label, count in zip(cols, ("Требования", "Нарушения", "Критические", "Вопросы"),
                                 (stats.requirements, stats.violations, stats.critical, stats.questions), strict=True):
        col.metric(label, count, border=True)
    reqs, findings, coverage, questions = st.tabs(["Требования", "Находки", "Покрытие", "Вопросы"])
    blocks = {b.block_id: b for b in version.blocks}

    def evidence(ref) -> None:
        loc = blocks[ref.block_id].location
        position = f"стр. {loc.page}" if loc.page else f"строки {loc.line_start}–{loc.line_end}"
        st.text(f"{loc.section} · {position} · {ref.block_id} [{ref.start}, {ref.end})")
        st.code(ref.quote, language=None, wrap_lines=True)

    with reqs:
        if report.requirements:
            st.dataframe([{"ID": r.req_id, "Исходный ID": r.original_id or "—", "Требование": r.summary}
                          for r in report.requirements], hide_index=True, width="stretch")
            selected = st.selectbox("Источник требования", [r.req_id for r in report.requirements])
            evidence(next(r.source for r in report.requirements if r.req_id == selected))
        else:
            st.info("В отчёте нет извлечённых требований.")
    with findings:
        level = st.selectbox("Серьёзность", ["Все", *SEVERITY.values()])
        matches = [f for f in report.findings if level in ("Все", SEVERITY[f.severity])]
        if not matches:
            st.info("По выбранному фильтру находок нет.")
        for f in matches:
            with st.expander(f"{f.finding_id} · {SEVERITY[f.severity]} · {SEMANTIC[f.semantic_status]}"):
                st.text(f.claim)
                st.text(f.verification_reason)
                if f.confirmed:
                    st.text(f.recommendation)
                for ref in f.evidence:
                    evidence(ref)
                if f.gap:
                    st.text(f.gap.expected_information)
                    st.text("Проверенные блоки — " + ", ".join(f.gap.inspected_blocks))
                    evidence(f.gap.context)
    with coverage:
        rows = []
        for name, work in (("Блоки", report.coverage.extraction_blocks),
                           ("Требования", report.coverage.individual_requirements),
                           ("Пакеты", report.coverage.audit_batches),
                           ("Межсекционные группы", report.coverage.cross_section_groups)):
            completed = set(work.completed)
            rows.append({"Область": name, "Выполнено": len(completed), "Запланировано": len(work.planned),
                         "Осталось": ", ".join(x for x in work.planned if x not in completed) or "—"})
        st.dataframe(rows, hide_index=True, width="stretch")
        for reason in dict.fromkeys(report.document.diagnostics + report.coverage.incomplete_reasons + report.coverage.uncertainties):
            st.text(reason)
        if report.document.unread_pages:
            st.text("Непрочитанные страницы — " + ", ".join(map(str, report.document.unread_pages)))
        st.text(f"Попытки запросов — {report.metadata.request_attempts} · Git — {report.metadata.agent_revision}")
    with questions:
        if not report.questions:
            st.info("Вопросов для уточнения нет.")
        for q in sorted(report.questions, key=lambda item: item.priority):
            st.caption(f"{q.priority} · {q.question_id} · {ROLE[q.stakeholder]}")
            st.text(q.question)
            st.text(q.business_impact)
            for option in q.options:
                st.text(f"{option.label} — {option.consequence}")
    with st.container(horizontal=True):
        st.download_button("Скачать Markdown", render_markdown(report), "report.md",
                           "text/markdown; charset=utf-8", on_click="ignore")
        st.download_button("Скачать JSON", render_json(report), "report.json",
                           "application/json", on_click="ignore")
