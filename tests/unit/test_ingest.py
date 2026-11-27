import hashlib

import pymupdf
import pytest

from src.ingest import MAX_BYTES, parse


def test_markdown_lines_blocks_digest_and_revision():
    source = "\ufeff# Оплата\r\n\r\nПервый абзац.  \r\nВторая строка.\r\n| ID | Валюта |\r\n| -- | -- |\r\n| F1 | ₸ |\r\n~~~py\r\n# code\r\n\r\nx=1\r\n~~~\r\nНовая глава\r\n===\r\nТекст."
    data = source.encode()
    doc = parse("spec.MD", data)
    assert doc == parse("spec.MD", data)
    assert doc.version.sha256 == hashlib.sha256(data).hexdigest()
    assert doc.version.line_count == 15
    lines = source.lstrip("\ufeff").splitlines(keepends=True)
    for b in doc.version.blocks:
        assert b.text == "".join(lines[b.location.line_start - 1:b.location.line_end]).rstrip("\r\n")
    assert [b.location.line_start for b in doc.version.blocks] == [1, 3, 5, 8, 13, 15]
    assert [b.location.section for b in doc.version.blocks] == ["Оплата"] * 4 + ["Новая глава"] * 2
    assert parse("spec.MD", data + b"!").version.sha256 != doc.version.sha256


def test_pair_preserves_logical_table_row_order_and_bounds():
    md = parse("table.md", b"# Transfers\n| ID | Limit |\n| -- | -- |\n| R1 | 50 USD |")
    with pymupdf.open() as pdf:
        page = pdf.new_page()
        page.insert_text((72, 54), "Transfers")
        for x in (70, 150, 250):
            page.draw_line((x, 70), (x, 130))
        for y in (70, 100, 130):
            page.draw_line((70, y), (250, y))
        # Cell creation order deliberately differs from reading order.
        for x, y, text in [(160, 120, "50 USD"), (80, 90, "ID"), (160, 90, "Limit"), (80, 120, "R1")]:
            page.insert_text((x, y), text)
        pdf.set_toc([[1, "Transfers", 1]])
        data = pdf.tobytes()
    parsed = parse("table.pdf", data)
    blocks = parsed.version.blocks
    assert [b.text for b in blocks] == ["Transfers", "ID\tLimit", "R1\t50 USD"]
    assert all(token in md.version.blocks[1].text for token in blocks[2].text.split("\t"))
    assert all(b.location.page == 1 and b.location.bbox and b.location.section == "Transfers" for b in blocks)
    assert parsed == parse("table.pdf", data)
    assert parsed.version.sha256 == hashlib.sha256(data).hexdigest()


def test_blank_scanned_and_partially_readable_pages():
    with pymupdf.open() as pdf:
        background = pdf.new_page()
        background.draw_rect(background.rect, fill=(1, 1, 1), color=None)
        pix = pymupdf.Pixmap(pymupdf.csRGB, (0, 0, 20, 20), 0)
        pix.clear_with(42)
        pdf.new_page().insert_image((40, 40, 100, 100), pixmap=pix)
        mixed = pdf.new_page()
        mixed.insert_text((40, 35), "Readable title")
        mixed.insert_image((40, 40, 100, 100), pixmap=pix)
        doc = parse("mixed.pdf", pdf.tobytes())
    assert doc.blank_pages == [1]
    assert doc.unread_pages == [2, 3]
    assert [b.text for b in doc.version.blocks] == ["Readable title"]
    assert all(str(n) in msg for n, msg in zip(doc.unread_pages, doc.diagnostics, strict=True))


@pytest.mark.parametrize(("name", "data"), [
    ("empty.md", b""), ("space.md", b"  \r\n"), ("bad.pdf", b"ordinary text"),
    ("broken.pdf", b"%PDF-1.5\n???"), ("binary.md", b"\xff"), ("nul.md", b"a\x00b"),
    ("renamed.md", b"%PDF-1.7\n"), ("file.txt", b"abc"), ("large.md", b"x" * (MAX_BYTES + 1)),
])
def test_invalid_input(name, data):
    with pytest.raises(ValueError):
        parse(name, data)


def test_byte_limit_is_inclusive():
    data = b"a" * MAX_BYTES
    assert len(parse("at-limit.md", data).version.blocks[0].text) == MAX_BYTES


def test_pdf_limits_and_password():
    with pymupdf.open() as pdf:
        pdf.new_page()
        protected = pdf.tobytes(encryption=pymupdf.PDF_ENCRYPT_AES_256, owner_pw="owner", user_pw="reader")
        with pytest.raises(ValueError, match="Защищённый"):
            parse("protected.pdf", protected)
        for _ in range(99):
            pdf.new_page()
        assert len(parse("100.pdf", pdf.tobytes()).blank_pages) == 100
        pdf.new_page()
        with pytest.raises(ValueError, match="100"):
            parse("101.pdf", pdf.tobytes())
