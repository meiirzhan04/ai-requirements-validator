import pytest

from src.contracts import DocumentVersion, EvidenceRef, ParsedDocument, Requirement, SourceBlock, SourceLocation, WorkSet
from src.ingest.structure import reconcile_scope, scan_structure


def document(*texts, unread=False):
    blocks = []
    for i, text in enumerate(texts, 1):
        blocks.append(SourceBlock(block_id=f"B{i}", text=text,
                                  location=SourceLocation(section="Раздел", page=1)))
    return ParsedDocument(version=DocumentVersion(sha256="a" * 64, parser_version="test",
                          format="pdf", name="sample.pdf", page_count=2 if unread else 1, blocks=blocks),
                          unread_pages=[2] if unread else [], blank_pages=[] if blocks else [1])


def requirement(doc, i=0):
    block = doc.version.blocks[i]
    return Requirement(req_id="R1", summary="Требование", category="functional",
                       source=EvidenceRef(version=doc.version.sha256, block_id=block.block_id,
                                          start=0, end=len(block.text), quote=block.text, grounding="exact"))


def test_signals_have_reproducible_spans_and_preserve_hierarchy():
    doc = document("# Переводы", "## Сроки", "## Правила", "Поле:\r\nTBD",
                   "| A | B |\r\n| --- | --- |\r\n| R1 | |", "Вопросы\n---")
    signals = scan_structure(doc)
    assert [(s.kind, s.source.block_id) for s in signals] == [
        ("empty_section", "B2"), ("empty_field", "B4"), ("placeholder", "B4"),
        ("empty_cell", "B5"), ("empty_section", "B6")]
    for s in signals:
        block = next(b for b in doc.version.blocks if b.block_id == s.source.block_id)
        assert s.source.version == doc.version.sha256
        assert block.text[s.source.start:s.source.end] == s.source.quote


def test_complete_table_code_and_unnumbered_requirement_are_clean():
    doc = document("# Правила", "Срок перевода равен двум дням.",
                   "| ID | Описание |\n| :---: | --- |\n| R1 | A\\|B |",
                   "~~~md\n# Пример\nTBD\n~~~")
    assert scan_structure(doc) == []


def test_header_only_and_ragged_table_remain_candidates():
    doc = document("A | B | C\n--- | --- | ---", "A | B | C\n--- | --- | ---\n1 | 2")
    assert [signal.kind for signal in scan_structure(doc)] == ["empty_table", "empty_cell"]


def test_reconciles_entire_inventory_with_context_and_unread_pages():
    doc = document("Платёж отправляется", "Преамбула", "Пропущено", unread=True)
    processed = WorkSet(planned=["B2", "B1"], completed=["B2", "B1"])
    scope = reconcile_scope(doc, processed, [requirement(doc)], {"B2": "Определение термина."})
    assert scope.extraction_blocks == WorkSet(planned=["B1", "B2", "B3"], completed=["B1", "B2"])
    assert scope.context_blocks == {"B2": "Определение термина."}
    assert scope.outstanding_blocks == ["B3"]
    assert any("страницы: 2" in reason for reason in scope.incomplete_reasons)
    assert any("B3" in reason for reason in scope.incomplete_reasons)


def test_missing_rationale_and_partial_block_never_complete_by_accident():
    doc = document("A", "B")
    work = WorkSet(planned=["B1", "B2"], completed=["B2"])
    scope = reconcile_scope(doc, work, [requirement(doc)], {"B2": " "})
    assert scope.extraction_blocks.completed == []
    assert scope.context_blocks == {}
    assert scope.outstanding_blocks == ["B1", "B2"]
    assert scope.incomplete_reasons


def test_signals_never_make_a_completed_block_a_defect_or_outstanding():
    doc = document("TBD")
    work = WorkSet(planned=["B1"], completed=["B1"])
    assert scan_structure(doc)
    scope = reconcile_scope(doc, work, [], {"B1": "Литерал в словаре."})
    assert scope.extraction_blocks.complete and not scope.incomplete_reasons


def test_reject_contradictory_foreign_or_stale_disposition():
    doc = document("A")
    work = WorkSet(planned=["B1"], completed=["B1"])
    req = requirement(doc)
    with pytest.raises(ValueError, match="context only"):
        reconcile_scope(doc, work, [req], {"B1": "Context"})
    with pytest.raises(ValueError, match="unknown"):
        reconcile_scope(doc, work, [], {"B2": "Foreign"})
    req.source.version = "b" * 64
    with pytest.raises(ValueError, match="source anchor"):
        reconcile_scope(doc, work, [req], {})


def test_blank_or_unread_only_document_has_explicit_incomplete_scope():
    scope = reconcile_scope(document(unread=True), WorkSet(planned=[], completed=[]), [], {})
    assert not scope.outstanding_blocks
    assert scope.incomplete_reasons
