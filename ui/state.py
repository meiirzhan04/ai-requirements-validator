"""Session-scoped caching and state lifecycle helpers for fast, leak-free Streamlit reruns."""
from collections import OrderedDict
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any

import streamlit as st

from src.contracts import SourceBlock, ValidationReport
from src.report import CATEGORY, CRITERION_STATUS, load_json, render_json, render_markdown

_RULES_DIRS = ("contracts", "ingest", "evidence", "prompts", "pipeline", "provider", "readiness")
_RULES_CACHE: tuple[tuple[tuple[str, int, int], ...], str] | None = None

_MAX_SAVED_CACHE = 8
_MAX_VIEW_CACHE = 12
_MAX_EXPORT_CACHE = 12


@dataclass(frozen=True)
class ReportView:
    """Precomputed canonical tables and indexes derived once per report snapshot."""

    blocks_by_id: dict[str, SourceBlock]
    evals_by_req: dict[str, list[Any]]
    req_rows: list[dict[str, str]]
    whole_eval_rows: list[dict[str, str]]
    whole_summary: tuple[int, int, int]  # (pass, fail, other)
    coverage_rows: list[dict[str, Any]]
    passport_rows: list[dict[str, str]]
    finding_haystacks: dict[str, str]
    refuted_count: int
    tokens_label: str
    block_coverage_label: str
    diagnostics: list[str]


def rules_version() -> str:
    """Compute a deterministic digest of backend source modules without rereading unchanged files."""
    global _RULES_CACHE
    core = Path(__file__).resolve().parents[1] / "src"
    entries: list[tuple[str, Path, int, int]] = []
    for directory in _RULES_DIRS:
        dir_path = core / directory
        if not dir_path.is_dir():
            continue
        for path in sorted(dir_path.glob("*.py")):
            stat = path.stat()
            entries.append((f"{directory}/{path.name}", path, stat.st_mtime_ns, stat.st_size))
    fingerprint = tuple((rel, mtime_ns, size) for rel, _, mtime_ns, size in entries)
    if _RULES_CACHE is not None and _RULES_CACHE[0] == fingerprint:
        return _RULES_CACHE[1]
    digest = sha256()
    for rel, path, _, _ in entries:
        digest.update(rel.encode())
        digest.update(path.read_bytes())
    value = digest.hexdigest()
    _RULES_CACHE = (fingerprint, value)
    return value


def _lru_store(store_key: str) -> OrderedDict[Any, Any]:
    store = st.session_state.get(store_key)
    if not isinstance(store, OrderedDict):
        store = OrderedDict()
        st.session_state[store_key] = store
    return store


def _lru_put(store: OrderedDict[Any, Any], key: Any, value: Any, max_entries: int) -> None:
    store[key] = value
    store.move_to_end(key)
    while len(store) > max_entries:
        store.popitem(last=False)


def report_cache_key(report: ValidationReport) -> tuple[Any, ...]:
    """Identify a canonical report snapshot within the current user session."""
    meta = report.metadata
    return (
        meta.run_id,
        meta.document_version,
        meta.origin,
        meta.model,
        meta.prompt_version,
        meta.agent_revision,
        meta.request_attempts,
        report.coverage.execution,
        report.verdict.readiness,
        len(report.requirements),
        len(report.findings),
        len(report.questions),
        len(report.answers),
        len(report.proposed_changes),
    )


def load_saved_report_cached(raw: bytes, filename: str) -> tuple[ValidationReport | None, bool]:
    """Parse and validate an uploaded JSON report once per (sha256, filename) inside session_state.

    Returns (report, is_invalid). Never shares uploaded data across Streamlit sessions.
    """
    digest = sha256(raw).hexdigest()
    cache_key = (digest, filename)
    store = _lru_store("_rv_saved_reports")
    if cache_key in store:
        store.move_to_end(cache_key)
        return store[cache_key]
    try:
        parsed = load_json(raw)
        entry: tuple[ValidationReport | None, bool] = (parsed, False)
    except Exception:
        entry = (None, True)
    _lru_put(store, cache_key, entry, _MAX_SAVED_CACHE)
    return entry


def get_report_exports(report: ValidationReport) -> tuple[str, str]:
    """Return cached (markdown, json) exports for one report snapshot inside session_state."""
    key = report_cache_key(report)
    store = _lru_store("_rv_report_exports")
    if key in store:
        store.move_to_end(key)
        return store[key]
    pair = (render_markdown(report), render_json(report))
    _lru_put(store, key, pair, _MAX_EXPORT_CACHE)
    return pair


def get_report_view(report: ValidationReport) -> ReportView:
    """Build and cache derived UI tables and lookup indexes once per report snapshot in session_state."""
    key = report_cache_key(report)
    store = _lru_store("_rv_report_views")
    if key in store:
        store.move_to_end(key)
        return store[key]

    version = report.document.version
    meta = report.metadata
    blocks_by_id = {b.block_id: b for b in version.blocks}

    evals_by_req: dict[str, list[Any]] = {}
    whole_evals = []
    for ev in report.evaluations:
        if ev.requirement_id is None:
            whole_evals.append(ev)
        else:
            evals_by_req.setdefault(ev.requirement_id, []).append(ev)

    req_rows = []
    for req in report.requirements:
        req_evals = evals_by_req.get(req.req_id, [])
        passes = sum(1 for e in req_evals if e.status == "pass")
        fails = sum(1 for e in req_evals if e.status == "fail")
        req_rows.append(
            {
                "ID": req.req_id,
                "Исходный ID": req.original_id or "—",
                "Категория": CATEGORY[req.category],
                "Раздел": blocks_by_id[req.source.block_id].location.section,
                "ISO (OK / Наруш.)": f"{passes} / {fails}" if req_evals else "не проверено",
                "Требование": req.summary,
            }
        )

    whole_eval_rows = [
        {
            "Стандарт / Критерий": e.criterion,
            "Результат": CRITERION_STATUS[e.status],
            "Связанные находки": ", ".join(e.finding_ids) or "—",
            "Обоснование": e.rationale,
        }
        for e in whole_evals
    ]
    whole_pass = sum(1 for e in whole_evals if e.status == "pass")
    whole_fail = sum(1 for e in whole_evals if e.status == "fail")
    whole_other = len(whole_evals) - whole_pass - whole_fail

    coverage_rows = []
    for name, work in (
        ("Блоки", report.coverage.extraction_blocks),
        ("Требования", report.coverage.individual_requirements),
        ("Пакеты", report.coverage.audit_batches),
        ("Межсекционные группы", report.coverage.cross_section_groups),
    ):
        completed = set(work.completed)
        pct = f"{round(len(completed) / len(work.planned) * 100)}%" if work.planned else "—"
        coverage_rows.append(
            {
                "Область": name,
                "Выполнено": len(completed),
                "Запланировано": len(work.planned),
                "Покрытие": pct,
                "Осталось": ", ".join(x for x in work.planned if x not in completed) or "—",
            }
        )

    tokens_label = (
        f"{meta.input_tokens:,} / {meta.output_tokens:,}"
        if meta.input_tokens is not None and meta.output_tokens is not None
        else "нет данных"
    )
    passport_rows = [
        {"Параметр": "ID прогона", "Значение": meta.run_id},
        {"Параметр": "Модель", "Значение": meta.model},
        {"Параметр": "Версия правил", "Значение": meta.prompt_version},
        {"Параметр": "Git-ревизия", "Значение": meta.agent_revision},
        {"Параметр": "Длительность, с", "Значение": f"{meta.duration_seconds:.3f}"},
        {"Параметр": "Попытки запросов API", "Значение": str(meta.request_attempts)},
        {"Параметр": "Входные / выходные токены", "Значение": tokens_label},
        {"Параметр": "SHA-256 документа", "Значение": version.sha256},
    ]

    finding_haystacks = {
        f.finding_id: f"{f.finding_id} {f.claim} {f.recommendation} {' '.join(f.requirement_ids)} {' '.join(f.criteria)}".lower()
        for f in report.findings
    }
    refuted_count = sum(1 for f in report.findings if f.semantic_status == "refuted")

    eb = report.coverage.extraction_blocks
    block_cov = f"{len(set(eb.completed))}/{len(eb.planned)} блоков" if eb.planned else "нет блоков"

    diagnostics = list(
        dict.fromkeys(
            report.document.diagnostics
            + report.coverage.incomplete_reasons
            + report.coverage.uncertainties
        )
    )

    view = ReportView(
        blocks_by_id=blocks_by_id,
        evals_by_req=evals_by_req,
        req_rows=req_rows,
        whole_eval_rows=whole_eval_rows,
        whole_summary=(whole_pass, whole_fail, whole_other),
        coverage_rows=coverage_rows,
        passport_rows=passport_rows,
        finding_haystacks=finding_haystacks,
        refuted_count=refuted_count,
        tokens_label=tokens_label,
        block_coverage_label=block_cov,
        diagnostics=diagnostics,
    )
    _lru_put(store, key, view, _MAX_VIEW_CACHE)
    return view


def sync_active_document(scope_key: str, token: str) -> bool:
    """Track active input identity and clear stale per-document UI state when the file changes."""
    tracker_key = f"_rv_active_{scope_key}"
    previous = st.session_state.get(tracker_key)
    if previous == token:
        return False
    st.session_state[tracker_key] = token
    # Clear stale search/pagination keys from a different document revision.
    stale_prefixes = ("_rv_find_page_", "_rv_active_finding_")
    for k in list(st.session_state.keys()):
        if any(k.startswith(pref) for pref in stale_prefixes):
            st.session_state.pop(k, None)
    return True
