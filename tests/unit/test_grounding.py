import pytest

from src.contracts import DocumentVersion, EvidenceCandidate, ParsedDocument, SourceBlock, SourceLocation
from src.evidence import ground


def document(*texts):
    blocks = []
    line = 1
    for index, text in enumerate(texts, 1):
        last = line + text.count("\n")
        blocks.append(SourceBlock(block_id=f"B{index}", text=text,
                                  location=SourceLocation(section="S", line_start=line, line_end=last)))
        line = last + 1
    return ParsedDocument(version=DocumentVersion(name="rules.md", sha256="a" * 64, parser_version="test",
                                                  format="markdown", line_count=line - 1, blocks=blocks))


def lookup(doc, quote, block="B1", version=None):
    return ground(EvidenceCandidate(version=version or doc.version.sha256, block_id=block, quote=quote), doc)


def test_addresses_remain_distinct_and_reproduce_original_unicode():
    original = "🪙  Сумма\u00a0>=  50\r\nтенге."
    doc = document(original, "Сумма >= 50 тенге.")
    normalized = lookup(doc, "Сумма >= 50 тенге.").reference
    exact = lookup(doc, "Сумма >= 50 тенге.", "B2").reference
    assert (normalized.start, normalized.end, normalized.grounding) == (3, len(original), "normalized")
    assert normalized.quote == original[normalized.start:normalized.end]
    assert (exact.start, exact.end, exact.grounding) == (0, len(exact.quote), "exact")
    assert normalized.version == exact.version == doc.version.sha256
    assert normalized.block_id != exact.block_id


@pytest.mark.parametrize(("text", "quote"), [
    ("aaaa", "aaa"), ("x y / x\u00a0y", "x y"), ("размер 15 МБ", "размер 5 МБ"),
    ("не разрешать", "не запрещать"), ("нельзя отменять", "можно отменять"),
    ("платёж не подтверждён", "платёж подтверждён"), ("срок <= 1 ч", "срок < 1 ч"),
    ("срок <= 1 ч", "срок <= 1 с"), ("a\tb", "a b"), ("a\u200bb", "ab"),
    ("код `X  Y`", "код `X Y`"), ("~~~\nif x:\n    y()\n~~~", "if x: y()"),
])
def test_changed_or_ambiguous_quote_has_no_verified_reference(text, quote):
    result = lookup(document(text), quote)
    assert result.status == "unverified" and result.reference is None
    assert result.reason


def test_named_block_and_version_are_mandatory():
    doc = document("перевод", "комиссия")
    assert lookup(doc, "комиссия", "B1").reference is None
    assert lookup(doc, "комиссия", "missing").reference is None
    with pytest.raises(ValueError, match="version"):
        lookup(doc, "перевод", version="b" * 64)


def test_exact_code_and_whitespace_edges():
    code = "    send(  amount)"
    assert lookup(document(code), "send(  amount)").status == "exact"
    doc = document("🪙  x\r\ny\u00a0\u00a0z.")
    span = lookup(doc, "\u00a0x y ").reference
    assert (span.start, span.end, span.quote) == (1, 9, "  x\r\ny\u00a0\u00a0")
