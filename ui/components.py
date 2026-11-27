"""Reusable Enterprise Fintech UI components and thread-safe ProgressEvent tracker."""
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
import html
import logging
import queue
import threading
import time
from typing import TypeVar

import streamlit as st

from src.contracts import EvidenceRef, ProgressEvent, SourceBlock, ValidationReport
from src.report import EXECUTION, GROUNDING, READINESS
from ui.state import ReportView
from ui.theme import EXECUTION_TOKENS, READINESS_TOKENS

logger = logging.getLogger(__name__)
T = TypeVar("T")

WORKFLOW_STEPS = (
    (1, "Загрузка"),
    (2, "Анализ"),
    (3, "Вердикт"),
    (4, "Находки"),
    (5, "Уточнение"),
    (6, "Экспорт"),
)

_RETRY_HINTS = ("повтор", "retry", "ожидан", "503", "429", "квот", "backoff", "rate")


def render_app_header(active_step: int = 1) -> None:
    """Render a compact enterprise top bar and linear workflow indicator (zero emojis)."""
    st.markdown(
        """
        <div class="rv-topbar">
            <div>
                <p class="rv-brand-kicker">BCC-HUB × MNU · Предразработочный аудит</p>
                <h1 class="rv-brand-title">Агент проверки требований</h1>
                <p class="rv-brand-desc">
                    Статический аудит финтех-спецификаций с дословным заземлением цитат,
                    независимой семантической проверкой и адресными уточнениями.
                </p>
            </div>
            <div class="rv-standards">
                <span class="rv-std-chip">ISO/IEC/IEEE 29148</span>
                <span class="rv-std-chip">IEEE 830</span>
                <span class="rv-std-chip">MD / PDF</span>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    parts: list[str] = []
    for idx, (num, label) in enumerate(WORKFLOW_STEPS):
        if num < active_step:
            cls = "rv-wf-step rv-wf-step--done"
        elif num == active_step:
            cls = "rv-wf-step rv-wf-step--active"
        else:
            cls = "rv-wf-step"
        parts.append(
            f'<span class="{cls}"><span class="rv-wf-num">{num}</span>{html.escape(label)}</span>'
        )
        if idx < len(WORKFLOW_STEPS) - 1:
            parts.append('<span class="rv-wf-sep" aria-hidden="true">/</span>')
    st.markdown(f'<div class="rv-workflow">{"".join(parts)}</div>', unsafe_allow_html=True)


def render_evidence_block(
    ref: EvidenceRef,
    blocks_by_id: dict[str, SourceBlock],
    *,
    container=st,
    tag: str | None = None,
) -> None:
    """Render a GitHub-diff-inspired source coordinate header and verbatim quote."""
    loc = blocks_by_id[ref.block_id].location
    position = f"стр. {loc.page}" if loc.page else f"строки {loc.line_start}–{loc.line_end}"
    grounding_label = GROUNDING[ref.grounding]
    prefix = f"{html.escape(tag)} · " if tag else ""
    container.markdown(
        '<div class="rv-evidence-bar">'
        f'<span class="rv-evidence-loc">{prefix}{html.escape(loc.section)} · {html.escape(position)}</span>'
        f'<span>{html.escape(ref.block_id)} [{ref.start}, {ref.end}) · {html.escape(grounding_label)}</span>'
        "</div>",
        unsafe_allow_html=True,
    )
    container.code(ref.quote, language=None, wrap_lines=True)


def render_executive_verdict(report: ValidationReport, *, origin: str, view: ReportView) -> None:
    """Display clearly separated Document Readiness and Execution Completeness plus run passport."""
    version = report.document.version
    meta = report.metadata
    readiness = report.verdict.readiness
    execution = report.coverage.execution

    r_tok = READINESS_TOKENS[readiness]
    e_tok = EXECUTION_TOKENS[execution]

    if execution == "complete":
        exec_note = f"Проверены все запланированные области ({view.block_coverage_label})."
    elif execution == "partial":
        exec_note = (
            f"Часть объёма не завершена ({view.block_coverage_label}). "
            "Политика безопасности блокирует вердикт GO."
        )
    else:
        exec_note = "Пригодная оценка не получена. Подробности указаны во вкладке покрытия."

    rationale_full = report.verdict.rationale
    rationale_brief = rationale_full
    has_long_rationale = len(rationale_full) > 280
    if has_long_rationale:
        first_break = rationale_full.find(". ", 40, 260)
        if first_break > 0:
            rationale_brief = rationale_full[: first_break + 1]
        else:
            rationale_brief = rationale_full[:260].rstrip() + "…"

    st.caption(origin)
    st.markdown(
        '<div class="rv-exec-grid">'
        f'<div class="rv-verdict-box" style="background:{r_tok["tint"]};border-color:{r_tok["border"]};border-left-color:{r_tok["accent"]};">'
        f'<div class="rv-kicker" style="color:{r_tok["accent"]};">Вердикт готовности документа · {html.escape(r_tok["label"])}</div>'
        f'<div class="rv-verdict-value">{html.escape(READINESS[readiness])}</div>'
        f'<div class="rv-exec-detail">{html.escape(rationale_brief)}</div>'
        "</div>"
        f'<div class="rv-exec-box" style="background:{e_tok["tint"]};border-left-color:{e_tok["accent"]};">'
        f'<div class="rv-kicker" style="color:{e_tok["accent"]};">Полнота выполнения аудита</div>'
        f'<div class="rv-exec-value">{html.escape(EXECUTION[execution])} анализ</div>'
        f'<div class="rv-exec-detail">{html.escape(exec_note)}</div>'
        "</div>"
        "</div>"
        '<div class="rv-passport">'
        f'<div class="rv-pass-item"><div class="rv-pass-label">Документ</div><div class="rv-pass-value" title="{html.escape(version.name)}">{html.escape(version.name)}</div></div>'
        f'<div class="rv-pass-item"><div class="rv-pass-label">Модель</div><div class="rv-pass-value">{html.escape(meta.model)}</div></div>'
        f'<div class="rv-pass-item"><div class="rv-pass-label">Длительность</div><div class="rv-pass-value">{meta.duration_seconds:.1f} с</div></div>'
        f'<div class="rv-pass-item"><div class="rv-pass-label">Запросы API</div><div class="rv-pass-value">{meta.request_attempts}</div></div>'
        f'<div class="rv-pass-item"><div class="rv-pass-label">Токены (вх / вых)</div><div class="rv-pass-value">{html.escape(view.tokens_label)}</div></div>'
        f'<div class="rv-pass-item"><div class="rv-pass-label">Ревизия Git</div><div class="rv-pass-value">{html.escape(meta.agent_revision)}</div></div>'
        "</div>",
        unsafe_allow_html=True,
    )
    if has_long_rationale or view.diagnostics:
        with st.expander("Подробное обоснование вердикта и техническая диагностика", expanded=False):
            st.text(rationale_full)
            if view.diagnostics:
                st.caption(f"Уникальные диагностические записи и неопределённости ({len(view.diagnostics)})")
                for item in view.diagnostics:
                    st.text(f"• {item}")
    st.text(f"{version.name}  ·  {meta.started_at:%d.%m.%Y %H:%M %z}  ·  {meta.model}")


def render_severity_summary(report: ValidationReport, view: ReportView) -> None:
    """Render a compact, non-noisy defect breakdown card."""
    stats = report.statistics
    total = stats.critical + stats.major + stats.minor + stats.open_hypotheses
    if total > 0:
        segments = (
            f'<div style="width:{stats.critical / total * 100:.1f}%;background:#B91C1C;" title="Критические"></div>'
            f'<div style="width:{stats.major / total * 100:.1f}%;background:#B45309;" title="Существенные"></div>'
            f'<div style="width:{stats.minor / total * 100:.1f}%;background:#0F4C81;" title="Незначительные"></div>'
            f'<div style="width:{stats.open_hypotheses / total * 100:.1f}%;background:#64748B;" title="Открытые гипотезы"></div>'
        )
    else:
        segments = '<div style="width:100%;background:#15803D;" title="Нарушений не выявлено"></div>'

    rows = (
        ("#B91C1C", "Критические", stats.critical),
        ("#B45309", "Существенные", stats.major),
        ("#0F4C81", "Незначительные", stats.minor),
        ("#64748B", "Открытые гипотезы", stats.open_hypotheses),
        ("#15803D", "Опровергнуто", view.refuted_count),
    )
    items_html = "".join(
        f'<div class="rv-break-item"><span><span class="rv-dot" style="background:{color};"></span>{label}</span>'
        f"<strong>{count}</strong></div>"
        for color, label, count in rows
    )
    st.markdown(
        '<div class="rv-card">'
        '<div class="rv-card-header">Структура подтверждённых нарушений и гипотез</div>'
        f'<div class="rv-bar">{segments}</div>'
        f'<div class="rv-breakdown">{items_html}</div>'
        "</div>",
        unsafe_allow_html=True,
    )


def render_remediation_panel(report: ValidationReport) -> None:
    """Render prioritized next actions as a clean numbered operational checklist."""
    actions = report.verdict.next_actions
    if not actions:
        body = '<div class="rv-action-item">Первоочередные исправления не требуются.</div>'
    else:
        body = "".join(
            f'<li class="rv-action-item"><span class="rv-action-idx">{idx}.</span>'
            f"<span>{html.escape(action)}</span></li>"
            for idx, action in enumerate(actions[:6], start=1)
        )
        if len(actions) > 6:
            body += (
                f'<li class="rv-action-item" style="color:var(--rv-muted);">'
                f"Ещё действий в полном отчёте: {len(actions) - 6}</li>"
            )
        body = f'<ol class="rv-action-list">{body}</ol>'
    st.markdown(
        '<div class="rv-card">'
        '<div class="rv-card-header">Первоочередные действия</div>'
        f"{body}"
        "</div>",
        unsafe_allow_html=True,
    )


class ProgressTracker:
    """Thread-safe ProgressEvent renderer that only mutates Streamlit widgets on the script thread."""

    def __init__(self, status_container, stage_labels: dict[str, str], state_labels: dict[str, str]) -> None:
        self._status = status_container
        self._stage_labels = stage_labels
        self._state_labels = state_labels
        self._started = time.monotonic()
        self._owner_thread_id = threading.get_ident()
        self._lock = threading.Lock()
        self._pending: queue.SimpleQueue[ProgressEvent] = queue.SimpleQueue()
        self._summary_slot = status_container.empty()
        self._stages: dict[str, tuple[str, str, float]] = {}
        self.events: list[ProgressEvent] = []
        self.retry_notices: list[str] = []

    @property
    def elapsed_seconds(self) -> float:
        return max(0.0, time.monotonic() - self._started)

    def _render_event(self, event: ProgressEvent) -> None:
        elapsed = self.elapsed_seconds
        self.events.append(event)
        stage_key = str(event.stage)
        stage_name = self._stage_labels.get(stage_key, stage_key)
        state_name = self._state_labels.get(event.state, event.state)
        self._stages[stage_key] = (event.state, event.message, elapsed)

        lower_msg = event.message.lower()
        if any(hint in lower_msg for hint in _RETRY_HINTS):
            self.retry_notices.append(f"{stage_name}: {event.message}")

        completed = sum(1 for st_val, _, _ in self._stages.values() if st_val == "completed")
        rows = []
        for key, (st_val, _msg, ts) in self._stages.items():
            label = self._stage_labels.get(key, key)
            st_ru = self._state_labels.get(st_val, st_val)
            rows.append(
                f'<div class="rv-stage-row"><span><strong>{html.escape(label)}</strong> · {html.escape(st_ru)}</span>'
                f"<span>{ts:.1f} с</span></div>"
            )
        retry_html = ""
        if self.retry_notices:
            retry_html = (
                '<div style="margin-top:0.4rem;font-size:0.8rem;color:#B45309;">'
                f"Ожидание / повтор провайдера: {html.escape(self.retry_notices[-1])}</div>"
            )
        try:
            # Emit plain factual stage line for visibility and deterministic test assertions.
            self._status.text(f"{stage_name} · {state_name} ({elapsed:.1f} с) — {event.message}")
            self._summary_slot.markdown(
                '<div style="font-size:0.82rem;color:var(--rv-muted);">'
                f"Фактическое время: <strong>{elapsed:.1f} с</strong> · "
                f"Завершено этапов: <strong>{completed}</strong> · "
                f"Активный этап: <strong>{html.escape(stage_name)}</strong>"
                "</div>"
                f'<div class="rv-stage-list">{"".join(rows)}</div>'
                f"{retry_html}",
                unsafe_allow_html=True,
            )
        except Exception as exc:
            # Never allow transient UI rendering issues to abort backend processing.
            logger.warning("ui_progress_render_failed stage=%s error_type=%s", stage_key, type(exc).__name__)

    def flush(self) -> None:
        """Drain queued worker-thread events strictly on the owning Streamlit script thread."""
        if threading.get_ident() != self._owner_thread_id:
            return
        with self._lock:
            while True:
                try:
                    pending_event = self._pending.get_nowait()
                except queue.Empty:
                    break
                self._render_event(pending_event)

    def handle(self, event: ProgressEvent) -> None:
        if threading.get_ident() != self._owner_thread_id:
            self._pending.put(event)
            return
        self.flush()
        with self._lock:
            self._render_event(event)


def run_with_progress(tracker: ProgressTracker, fn: Callable[..., T], *args, **kwargs) -> T:
    """Execute a blocking pipeline call in a worker thread and render ProgressEvents on the script thread."""
    event_queue: queue.SimpleQueue[ProgressEvent] = queue.SimpleQueue()

    def _enqueue(event: ProgressEvent) -> None:
        event_queue.put(event)

    def _drain() -> None:
        while True:
            try:
                queued = event_queue.get_nowait()
            except queue.Empty:
                break
            tracker.handle(queued)

    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="rv-pipeline") as pool:
        future = pool.submit(fn, *args, on_progress=_enqueue, **kwargs)
        while not future.done():
            try:
                queued = event_queue.get(timeout=0.05)
                tracker.handle(queued)
            except queue.Empty:
                pass
            _drain()
        _drain()
        tracker.flush()
        return future.result()
