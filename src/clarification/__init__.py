from pathlib import Path

from src.contracts import (
    Answer, DocumentFormat, DocumentVersion, EvidenceRef, ParsedDocument, ProposedChange, ValidationReport,
)
from src.ingest import MAX_BYTES, parse


def _checked_block(version: DocumentVersion, ref: EvidenceRef):
    if version.sha256 != ref.version:
        raise ValueError("Цитата относится к другой версии документа.")
    block = next((item for item in version.blocks if item.block_id == ref.block_id), None)
    if (not ref.grounded or block is None or ref.end > len(block.text)
            or block.text[ref.start:ref.end] != ref.quote):
        raise ValueError("Исходная цитата или её диапазон изменились.")
    return block


def propose_change(answer: Answer, source: ValidationReport) -> ProposedChange:
    """Use the stakeholder's own wording as the editable initial replacement."""
    if answer.version != source.document.version.sha256:
        raise ValueError("Ответ относится к другой версии документа.")
    question = next((q for q in source.questions if q.question_id == answer.question_id), None)
    if question is None:
        raise ValueError("Вопрос отсутствует в отчёте.")
    replacement = answer.custom_text
    if answer.option_id is not None:
        option = next((o for o in question.options if o.option_id == answer.option_id), None)
        if option is None:
            raise ValueError("Вариант ответа отсутствует в вопросе.")
        replacement = option.label
    for ref in question.evidence:
        _checked_block(source.document.version, ref)
        if ref.quote != replacement:
            return ProposedChange(question_id=question.question_id, source=ref, replacement=replacement)
    raise ValueError("Ответ уже совпадает с исходным текстом.")


def apply_change(version: DocumentVersion, change: ProposedChange) -> DocumentVersion:
    """Create a UTF-8 Markdown working copy of the parsed text and reparse its lines."""
    target = _checked_block(version, change.source)
    if (version.line_count or 0) > MAX_BYTES:
        raise ValueError("Рабочая копия превышает предел 10 МиБ.")
    parts = []
    previous_end = 0
    for block in version.blocks:
        text = block.text
        if block.block_id == target.block_id:
            text = text[:change.source.start] + change.replacement + text[change.source.end:]
        if version.format == DocumentFormat.MARKDOWN:
            start, end = block.location.line_start, block.location.line_end
            if start <= previous_end:
                raise ValueError("Диапазоны исходных строк пересекаются.")
            if start > previous_end + 1:
                parts.append("\n" * (start - previous_end - 2))
            previous_end = end
        elif parts:
            parts.append("")
        parts.append(text)
    if version.format == DocumentFormat.MARKDOWN and version.line_count > previous_end:
        parts.append("\n" * (version.line_count - previous_end - 1))
    data = "\n".join(parts).encode("utf-8")
    name = Path(version.name).stem + ".md"
    child = parse(name, data).version
    return DocumentVersion.model_validate({**child.model_dump(), "parent_sha256": version.sha256})


def working_document(source: ParsedDocument, change: ProposedChange) -> ParsedDocument:
    version = apply_change(source.version, change)
    diagnostics = list(source.diagnostics)
    if source.unread_pages:
        pages = ", ".join(map(str, source.unread_pages))
        diagnostics.append(f"В исходном PDF остались непрочитанные страницы: {pages}.")
    return ParsedDocument(version=version, diagnostics=diagnostics)
