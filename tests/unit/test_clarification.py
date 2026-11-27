from hashlib import sha256

import pymupdf
import pytest

from src.clarification import apply_change, propose_change, working_document
from src.contracts import Answer, EvidenceRef, ProposedChange
from src.ingest import parse


def test_option_and_free_text_are_editable_drafts(report):
    original = report.model_dump_json()
    q = report.questions[0]
    fields = dict(version=report.document.version.sha256, question_id=q.question_id)
    option = propose_change(Answer(**fields, option_id=q.options[0].option_id), report)
    assert option.replacement == q.options[0].label and option.source in q.evidence
    custom = propose_change(Answer(**fields, custom_text="Отправлять в течение минуты."), report)
    assert custom.replacement == "Отправлять в течение минуты."
    assert custom.question_id == q.question_id
    assert report.model_dump_json() == original
    for update in ({"version": "0" * 64}, {"question_id": "unknown"}, {"option_id": "unknown"}):
        values = {**fields, "option_id": q.options[0].option_id, **update}
        with pytest.raises(ValueError):
            propose_change(Answer(**values), report)


def test_applied_revision_reparses_lines_and_preserves_parent(report):
    original = "# Регламент\n\nСрок 5 дней.\n\n## Приёмка\n\nПроверить на пятый день.".encode()
    parsed = parse("regulation.md", original)
    version = parsed.version
    block = version.blocks[1]
    ref = EvidenceRef(version=version.sha256, block_id=block.block_id, start=5, end=11,
                      quote="5 дней", grounding="exact")
    change = ProposedChange(question_id="Q1", source=ref, replacement="6 рабочих дней\nс даты подачи")
    child = apply_change(version, change)
    expected = original.decode().replace("5 дней", change.replacement).encode()
    assert child.parent_sha256 == version.sha256 == sha256(original).hexdigest()
    assert child.sha256 == sha256(expected).hexdigest()
    assert child.blocks[-1].location.line_start == 8
    assert child.blocks[1].text == "Срок 6 рабочих дней\nс даты подачи."
    assert version.blocks[1].text == "Срок 5 дней."
    with pytest.raises(ValueError, match="версии"):
        apply_change(child, change)
    for update in ({"quote": "6 дней"}, {"end": 60}, {"block_id": "bad"}):
        wrong = change.model_copy(update={"source": ref.model_copy(update=update)})
        with pytest.raises(ValueError, match="цитата"):
            apply_change(version, wrong)


def test_pdf_working_copy_retains_unread_scope_without_stale_page_coordinates():
    with pymupdf.open() as pdf:
        pdf.new_page().insert_text((35, 35), "Deadline: 5 days.")
        pdf.new_page().draw_rect((15, 15, 50, 50), fill=(0.3, 0.3, 0.3))
        data = pdf.tobytes()
    doc = parse("terms.pdf", data)
    block = doc.version.blocks[0]
    change = ProposedChange(question_id="Q1", source=EvidenceRef(version=doc.version.sha256, block_id=block.block_id,
                            start=10, end=11, quote="5", grounding="exact"), replacement="6")
    child = working_document(doc, change)
    assert child.version.parent_sha256 == sha256(data).hexdigest()
    assert child.version.format == "markdown"
    assert child.version.blocks[0].text == "Deadline: 6 days."
    assert child.version.blocks[0].location.page is None
    assert doc.unread_pages == [2] and child.diagnostics
    assert "2" in child.diagnostics[-1]
