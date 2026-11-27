"""Safe multi-edit application, full Markdown export, structured change log, and revalidation."""
from collections.abc import Callable, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import Field

from src.contracts import (
    DocumentFormat,
    DocumentVersion,
    EvidenceRef,
    ParsedDocument,
    ProgressEvent,
    ProposedChange,
    ProviderSettings,
    ValidationReport,
)
from src.contracts.source import Contract, Count, Digest, Id, Positive, Text
from src.ingest import MAX_BYTES, parse

from .improvement import ProposalOrigin, RemediationProposal


class ChangeLogEntry(Contract):
    """Single applied edit entry with coordinates, before/after text, and traceability."""

    change_id: Id
    proposal_id: Id | None = None
    question_id: Id | None = None
    finding_ids: list[Id] = Field(default_factory=list)
    requirement_ids: list[Id] = Field(default_factory=list)
    block_id: Id
    section: Text
    line_start: Positive | None = None
    line_end: Positive | None = None
    page: Positive | None = None
    start: Count
    end: Positive
    before_quote: Text
    after_text: Text
    rationale: Text
    stakeholder_decision: Text | None = None
    origin: ProposalOrigin


class DocumentChangeLog(Contract):
    """Structured revision audit trail for a set of applied specification improvements."""

    schema_version: Literal["1"] = "1"
    source_name: Text
    improved_name: Text
    parent_sha256: Digest
    improved_sha256: Digest
    applied_count: Count
    entries: list[ChangeLogEntry]
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    diagnostics: list[Text] = Field(default_factory=list)


class ImprovedDocumentBundle(Contract):
    """Revised ParsedDocument together with its complete Markdown text and structured change log."""

    document: ParsedDocument
    markdown: Text
    change_log: DocumentChangeLog


class _NormalizedEdit:
    __slots__ = (
        "proposal_id",
        "question_id",
        "finding_ids",
        "requirement_ids",
        "source",
        "replacement",
        "rationale",
        "stakeholder_decision",
        "origin",
    )

    def __init__(
        self,
        *,
        proposal_id: str | None,
        question_id: str | None,
        finding_ids: list[str],
        requirement_ids: list[str],
        source: EvidenceRef,
        replacement: str,
        rationale: str,
        stakeholder_decision: str | None,
        origin: ProposalOrigin,
    ) -> None:
        self.proposal_id = proposal_id
        self.question_id = question_id
        self.finding_ids = finding_ids
        self.requirement_ids = requirement_ids
        self.source = source
        self.replacement = replacement
        self.rationale = rationale
        self.stakeholder_decision = stakeholder_decision
        self.origin = origin


def _checked_block(version: DocumentVersion, ref: EvidenceRef):
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


def _normalize_edit(
    version: DocumentVersion,
    item: RemediationProposal | ProposedChange,
) -> _NormalizedEdit:
    if isinstance(item, RemediationProposal):
        if item.document_version != version.sha256:
            raise ValueError("Предложение правки относится к другой версии документа.")
        if item.status != "approved":
            raise ValueError(
                f"Предложение {item.proposal_id} имеет статус '{item.status}'; "
                "применять можно только утверждённые правки (status='approved')."
            )
        if not item.proposed_text:
            raise ValueError(f"Утверждённое предложение {item.proposal_id} не содержит текста замены.")
        _checked_block(version, item.source)
        if item.proposed_text == item.source.quote:
            raise ValueError("Текст замены совпадает с исходной цитатой.")
        return _NormalizedEdit(
            proposal_id=item.proposal_id,
            question_id=item.question_id,
            finding_ids=list(item.finding_ids),
            requirement_ids=list(item.requirement_ids),
            source=item.source,
            replacement=item.proposed_text,
            rationale=item.rationale,
            stakeholder_decision=item.stakeholder_decision,
            origin=item.origin,
        )
    if isinstance(item, ProposedChange):
        _checked_block(version, item.source)
        if item.replacement == item.source.quote:
            raise ValueError("Текст замены совпадает с исходной цитатой.")
        return _NormalizedEdit(
            proposal_id=None,
            question_id=item.question_id,
            finding_ids=[],
            requirement_ids=[],
            source=item.source,
            replacement=item.replacement,
            rationale=f"Применена подтверждённая правка по вопросу {item.question_id}.",
            stakeholder_decision=item.replacement,
            origin="stakeholder_decision",
        )
    raise TypeError(f"Неподдерживаемый тип правки: {type(item).__name__}")


def render_blocks_to_markdown(
    version: DocumentVersion,
    block_overrides: dict[str, str] | None = None,
) -> str:
    """Reconstruct complete UTF-8 Markdown preserving blank lines, sections, and unaffected blocks."""
    if (version.line_count or 0) > MAX_BYTES:
        raise ValueError("Рабочая копия превышает предел 10 МиБ.")
    overrides = block_overrides or {}
    parts: list[str] = []
    previous_end = 0
    for block in version.blocks:
        text = overrides.get(block.block_id, block.text)
        if version.format == DocumentFormat.MARKDOWN:
            start = block.location.line_start
            end = block.location.line_end
            assert start is not None and end is not None
            if start <= previous_end:
                raise ValueError("Диапазоны исходных строк пересекаются.")
            if start > previous_end + 1:
                parts.append("\n" * (start - previous_end - 2))
            previous_end = end
        elif parts:
            parts.append("")
        parts.append(text)
    if (
        version.format == DocumentFormat.MARKDOWN
        and version.line_count is not None
        and version.line_count > previous_end
    ):
        parts.append("\n" * (version.line_count - previous_end - 1))
    return "\n".join(parts)


def apply_changes(
    source: ParsedDocument,
    changes: Sequence[RemediationProposal | ProposedChange],
) -> ImprovedDocumentBundle:
    """Safely apply one or more non-overlapping approved edits to a copy of the document.

    Validates document revision (sha256), quote integrity, approval status, and span overlap.
    Preserves the original ParsedDocument completely untouched.
    """
    if not changes:
        raise ValueError("Не передано ни одной правки для применения.")

    version = source.version
    normalized = [_normalize_edit(version, item) for item in changes]

    blocks_by_id = {block.block_id: block for block in version.blocks}
    block_order = {block.block_id: idx for idx, block in enumerate(version.blocks)}

    edits_by_block: dict[str, list[_NormalizedEdit]] = {}
    for edit in normalized:
        edits_by_block.setdefault(edit.source.block_id, []).append(edit)

    block_overrides: dict[str, str] = {}
    for block_id, block_edits in edits_by_block.items():
        block_edits.sort(key=lambda e: (e.source.start, e.source.end))
        for prev, curr in zip(block_edits, block_edits[1:]):
            if curr.source.start < prev.source.end:
                raise ValueError(
                    f"Правки пересекаются в блоке {block_id}: "
                    f"[{prev.source.start}:{prev.source.end}] и [{curr.source.start}:{curr.source.end}]."
                )
        updated_text = blocks_by_id[block_id].text
        for edit in reversed(block_edits):
            updated_text = (
                updated_text[: edit.source.start]
                + edit.replacement
                + updated_text[edit.source.end :]
            )
        block_overrides[block_id] = updated_text

    markdown_text = render_blocks_to_markdown(version, block_overrides)
    data = markdown_text.encode("utf-8")
    if len(data) > MAX_BYTES:
        raise ValueError("Рабочая копия превышает предел 10 МиБ.")

    improved_name = Path(version.name).stem + ".md"
    child_parsed = parse(improved_name, data)
    child_version = DocumentVersion.model_validate(
        {**child_parsed.version.model_dump(), "parent_sha256": version.sha256}
    )

    diagnostics = list(source.diagnostics)
    if source.unread_pages:
        pages = ", ".join(map(str, source.unread_pages))
        diagnostics.append(f"В исходном PDF остались непрочитанные страницы: {pages}.")

    improved_document = ParsedDocument(version=child_version, diagnostics=diagnostics)
    change_log_diagnostics = list(diagnostics)
    if version.format == DocumentFormat.PDF:
        change_log_diagnostics.append(
            "Производный рабочий документ Markdown: исходный файл имел формат PDF; "
            "исходная вёрстка PDF при экспорте Markdown не сохраняется."
        )

    ordered_edits = sorted(
        normalized,
        key=lambda e: (block_order.get(e.source.block_id, 10**9), e.source.start, e.source.end),
    )
    entries: list[ChangeLogEntry] = []
    for idx, edit in enumerate(ordered_edits, start=1):
        block = blocks_by_id[edit.source.block_id]
        entries.append(
            ChangeLogEntry(
                change_id=f"CHG{idx:03d}",
                proposal_id=edit.proposal_id,
                question_id=edit.question_id,
                finding_ids=edit.finding_ids,
                requirement_ids=edit.requirement_ids,
                block_id=block.block_id,
                section=block.location.section,
                line_start=block.location.line_start,
                line_end=block.location.line_end,
                page=block.location.page,
                start=edit.source.start,
                end=edit.source.end,
                before_quote=edit.source.quote,
                after_text=edit.replacement,
                rationale=edit.rationale,
                stakeholder_decision=edit.stakeholder_decision,
                origin=edit.origin,
            )
        )

    change_log = DocumentChangeLog(
        source_name=version.name,
        improved_name=improved_name,
        parent_sha256=version.sha256,
        improved_sha256=child_version.sha256,
        applied_count=len(entries),
        entries=entries,
        diagnostics=change_log_diagnostics,
    )
    return ImprovedDocumentBundle(
        document=improved_document,
        markdown=markdown_text,
        change_log=change_log,
    )


def render_change_log_markdown(change_log: DocumentChangeLog) -> str:
    """Render a human-readable Russian Markdown audit log for applied improvements."""
    lines = [
        "## Журнал применённых изменений",
        "",
        f"- **Исходный документ:** `{change_log.source_name}` (`{change_log.parent_sha256[:12]}…`)",
        f"- **Улучшенная версия:** `{change_log.improved_name}` (`{change_log.improved_sha256[:12]}…`)",
        f"- **Применено правок:** {change_log.applied_count}",
        "",
    ]
    for entry in change_log.entries:
        if entry.line_start is not None and entry.line_end is not None:
            coords = f"блок `{entry.block_id}`, строки {entry.line_start}–{entry.line_end}"
        elif entry.page is not None:
            coords = f"блок `{entry.block_id}`, стр. {entry.page}"
        else:
            coords = f"блок `{entry.block_id}`"
        links: list[str] = []
        if entry.question_id:
            links.append(f"вопрос `{entry.question_id}`")
        if entry.finding_ids:
            links.append("находки " + ", ".join(f"`{fid}`" for fid in entry.finding_ids))
        if entry.requirement_ids:
            links.append("требования " + ", ".join(f"`{rid}`" for rid in entry.requirement_ids))
        link_line = "; ".join(links) if links else "без явных ссылок"

        lines.extend(
            [
                f"### {entry.change_id} — {entry.section} ({coords})",
                f"- **Основание:** {entry.rationale}",
                f"- **Связи:** {link_line}",
                f"- **Источник правки:** `{entry.origin}`",
                f"- **Было:** `{entry.before_quote}`",
                f"- **Стало:** `{entry.after_text}`",
                "",
            ]
        )
    if change_log.diagnostics:
        lines.append("### Диагностика исходного документа")
        lines.append("")
        for note in change_log.diagnostics:
            lines.append(f"- {note}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def export_improved_markdown(
    bundle: ImprovedDocumentBundle,
    *,
    include_change_log_appendix: bool = False,
) -> str:
    """Export the complete improved Markdown specification (optionally with change log appendix)."""
    if not include_change_log_appendix:
        return bundle.markdown
    return (
        bundle.markdown.rstrip("\n")
        + "\n\n---\n\n"
        + render_change_log_markdown(bundle.change_log)
    )


def revalidate_improved_document(
    bundle: ImprovedDocumentBundle,
    settings: ProviderSettings,
    *,
    on_progress: Callable[[ProgressEvent], None] | None = None,
    runner: Callable[..., ValidationReport] | None = None,
) -> ValidationReport:
    """Run the existing production validation pipeline on the improved document revision."""
    if bundle.document.version.sha256 != bundle.change_log.improved_sha256:
        raise ValueError("Версия документа в пакете улучшений не совпадает с журналом изменений.")
    if runner is None:
        from src.pipeline import run_pipeline

        return run_pipeline(bundle.document, settings, on_progress=on_progress)
    return runner(bundle.document, settings, on_progress=on_progress)
