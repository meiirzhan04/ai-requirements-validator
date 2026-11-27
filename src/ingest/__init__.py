"""Parse shareable source bytes locally into versioned, addressable blocks."""
import hashlib
from pathlib import Path
import re
from statistics import median

import pymupdf

from src.contracts import DocumentFormat, DocumentVersion, ParsedDocument, SourceBlock, SourceLocation

MAX_BYTES = 10 * 1024 * 1024
MAX_PAGES = 100
PARSER_VERSION = "ingest-1"
_HEADING = re.compile(r"^ {0,3}#{1,6}(?:[ \t]+(.*?)|[ \t]*)$")
_FENCE = re.compile(r"^ {0,3}(\x60{3,}|~{3,})")
_SETEXT = re.compile(r"^ {0,3}(?:=+|-+)[ \t]*$")


def _append(blocks: list[SourceBlock], text: str, section: str, **location) -> None:
    blocks.append(SourceBlock(block_id=f"B{len(blocks) + 1:05d}", text=text,
                              location=SourceLocation(section=section, **location)))


def _markdown(name: str, data: bytes, digest: str) -> ParsedDocument:
    try:
        source = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ValueError("Markdown должен быть файлом UTF-8.") from None
    if source.lstrip().startswith("%PDF-") or re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", source):
        raise ValueError("Содержимое файла не соответствует формату Markdown.")
    # Keep original CR, CRLF and LF inside a block for exact quote recovery.
    lines = source.splitlines(keepends=True)
    blocks = []
    section = "Начало документа"
    i = 0
    while i < len(lines):
        start = i
        line = lines[i].rstrip("\r\n")
        i += 1
        if not line.strip():
            continue
        fence = _FENCE.match(line)
        heading = _HEADING.fullmatch(line)
        if fence:
            marker = fence[1]
            close = re.compile(r"^ {0,3}" + marker[0] + "{" + str(len(marker)) + r",}[ \t]*$")
            while i < len(lines):
                i += 1
                if close.fullmatch(lines[i - 1].rstrip("\r\n")):
                    break
        elif heading:
            section = re.sub(r"[ \t]+#+[ \t]*$", "", heading[1] or "").strip() or line.strip()
        elif i < len(lines) and _SETEXT.fullmatch(lines[i].rstrip("\r\n")):
            section = line.strip()
            i += 1
        else:
            while i < len(lines) and lines[i].strip():
                following = lines[i].rstrip("\r\n")
                if _FENCE.match(following) or _HEADING.fullmatch(following):
                    break
                if ("|" in line) != ("|" in following):
                    break
                if i + 1 < len(lines) and _SETEXT.fullmatch(lines[i + 1].rstrip("\r\n")):
                    break
                i += 1
        text = "".join(lines[start:i]).rstrip("\r\n")
        _append(blocks, text, section, line_start=start + 1, line_end=i)
    if not blocks:
        raise ValueError("Документ Markdown пуст.")
    version = DocumentVersion(sha256=digest, name=name, format=DocumentFormat.MARKDOWN,
                              parser_version=PARSER_VERSION, line_count=len(lines), blocks=blocks)
    return ParsedDocument(version=version)


def _pdf_passages(page):
    """Group physical lines by ruled table cell/row or original paragraph."""
    drawings = page.get_drawings()
    table_error = False
    try:
        tables = page.find_tables(paths=drawings).tables if drawings else []
    except (RuntimeError, ValueError, IndexError):
        tables = []
        table_error = True
    rows = [row for table in tables for row in table.rows]
    flags = pymupdf.TEXTFLAGS_DICT & ~pymupdf.TEXT_PRESERVE_IMAGES & ~pymupdf.TEXT_USE_CID_FOR_UNKNOWN_UNICODE
    text_blocks = page.get_text("dict", flags=flags)
    sizes = [span["size"] for block in text_blocks["blocks"] for line in block.get("lines", [])
             for span in line["spans"] if span["text"].strip()]
    body_size = median(sizes) if sizes else 0
    titles = set()
    groups = {}
    for index, block in enumerate(text_blocks["blocks"]):
        for line in block.get("lines", []):
            text = "".join(span["text"] for span in line["spans"])
            if not text.strip():
                continue
            if (len(block["lines"]) == 1 and
                    min(s["size"] for s in line["spans"] if s["text"].strip()) >= 1.2 * body_size):
                titles.add(text.strip())
            box = pymupdf.Rect(line["bbox"])
            midpoint = (box.tl + box.br) / 2
            key = ("paragraph", index)
            cell_index = 0
            for row_index, row in enumerate(rows):
                if midpoint in pymupdf.Rect(row.bbox):
                    key = ("row", row_index)
                    cell_index = next((n for n, cell in enumerate(row.cells)
                                       if cell and midpoint in pymupdf.Rect(cell)), 0)
                    break
            groups.setdefault(key, []).append((cell_index, box, text))
    passages = []
    for (kind, _), items in groups.items():
        box = pymupdf.Rect(items[0][1])
        if kind == "row":
            items.sort(key=lambda item: (item[0], item[1].y0, item[1].x0))
        parts = []
        for i, (cell, rect, text) in enumerate(items):
            box |= rect
            if i:
                parts.append("\t" if kind == "row" and items[i - 1][0] != cell else "\n")
            parts.append(text)
        passages.append((box, "".join(parts)))
    passages.sort(key=lambda item: (item[0].y0, item[0].x0))
    return passages, titles, drawings, table_error


def _white_background(path) -> bool:
    colors = [path.get(k) for k in ("fill", "color") if path.get(k) is not None]
    return bool(colors) and all(all(channel >= 0.999 for channel in color) for color in colors)


def _pdf(name: str, data: bytes, digest: str) -> ParsedDocument:
    if not data.lstrip().startswith(b"%PDF-"):
        raise ValueError("Отсутствует заголовок PDF.")
    try:
        pdf = pymupdf.open(stream=data, filetype="pdf")
    except (RuntimeError, ValueError):
        raise ValueError("Файл PDF повреждён.") from None
    blocks, unread, blank, diagnostics = [], [], [], []
    with pdf:
        if pdf.needs_pass or (pdf.metadata or {}).get("encryption"):
            raise ValueError("Защищённый PDF не поддерживается.")
        if pdf.is_repaired or not pdf.page_count:
            raise ValueError("Файл PDF повреждён или пуст.")
        count = pdf.page_count
        if count > MAX_PAGES:
            raise ValueError("Превышен предел PDF в 100 страниц.")
        headings = {title.strip() for _, title, _ in pdf.get_toc() if title.strip()}
        section = "Начало документа"
        for n in range(1, count + 1):
            try:
                page = pdf[n - 1]
                passages, titles, drawings, table_error = _pdf_passages(page)
                for box, text in passages:
                    if text.strip() in headings or text.strip() in titles:
                        section = text.strip()
                    _append(blocks, text, section, page=n, bbox=tuple(box))
                if table_error:
                    reason = "не удалось восстановить структуру таблицы"
                elif page.get_image_info() or page.first_annot or page.first_widget:
                    reason = "изображения или аннотации требуют просмотра"
                elif not passages and any(not _white_background(path) for path in drawings):
                    reason = "графическая страница без извлекаемого текста"
                elif any("\ufffd" in text for _, text in passages):
                    reason = "часть символов не удалось декодировать"
                else:
                    if not passages:
                        blank.append(n)
                    continue
            except (RuntimeError, ValueError, IndexError):
                reason = "не удалось извлечь содержимое"
            unread.append(n)
            diagnostics.append(f"Страница {n}: {reason}.")
    version = DocumentVersion(sha256=digest, name=name, format=DocumentFormat.PDF,
                              parser_version=PARSER_VERSION, page_count=count, blocks=blocks)
    return ParsedDocument(version=version, blank_pages=blank, unread_pages=unread,
                          diagnostics=diagnostics)


def parse(name: str, data: bytes) -> ParsedDocument:
    """Build original source anchors without reading paths or contacting a provider."""
    if not data or len(data) > MAX_BYTES:
        raise ValueError("Файл пуст или превышает предел 10 МиБ.")
    suffix = Path(name).suffix.lower()
    digest = hashlib.sha256(data).hexdigest()
    if suffix == ".md":
        return _markdown(name, data, digest)
    if suffix == ".pdf":
        return _pdf(name, data, digest)
    raise ValueError("Поддерживаются только файлы .md и .pdf.")
