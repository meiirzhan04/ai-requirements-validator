"""Prepare public evaluation inputs outside runtime. Run as python -m eval_samples.prepare."""
import argparse
import hashlib
import html
from pathlib import Path
from urllib.request import urlopen

import pymupdf

ROOT = Path(__file__).parent
OFFICIAL_SHA = "e7dae55e5453a250d93e335f5ff9c7f775f4457ccddd333f4a67e47b20d74381"
OFFICIAL_URL = (
    "https://raw.githubusercontent.com/BCC-HUB/ai-requirements-validator-task/"
    "928c1aa364265426105b69ba7a3444eac11b22db/docs/requirements.md"
)


def text_pdf(source: str, destination: Path) -> None:
    # Paragraphs wrap across pages, leaving all source characters selectable.
    story = pymupdf.Story("".join(f"<p>{html.escape(line)}</p>" for line in source.splitlines()),
                          user_css="p { font-size: 9pt; margin: 2pt 0; }")
    page = pymupdf.paper_rect("a4")
    writer = pymupdf.DocumentWriter(str(destination))
    try:
        story.write(writer, lambda *_: (page, page + (32, 32, -32, -32), None))
    finally:
        writer.close()


def prepare(destination: Path, official: bytes) -> None:
    if hashlib.sha256(official).hexdigest() != OFFICIAL_SHA:
        raise ValueError("Official Markdown differs from pinned organizer input")
    destination.mkdir(parents=True, exist_ok=True)
    (destination / "official.md").write_bytes(official)
    text_pdf(official.decode("utf-8"), destination / "official.pdf")
    for stem in ("clean", "defective", "untrusted"):
        (destination / f"{stem}.md").write_bytes((ROOT / f"{stem}.md").read_bytes())
    (destination / "empty.md").write_bytes(b"")
    (destination / "corrupt.pdf").write_bytes(b"%PDF-invalid")
    (destination / "unsupported.txt").write_bytes(b"Example")
    with pymupdf.open() as doc:
        doc.new_page().insert_text((30, 40), "A booking expires after 30 minutes.")
        # Only a raster illustration on page two, no extractable text.
        raster = doc[0].get_pixmap()
        page = doc.new_page()
        page.insert_image(page.rect, pixmap=raster)
        doc.save(destination / "mixed.pdf")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--official", type=Path, help="Use a local byte-identical organizer input")
    args = parser.parse_args()
    if args.official:
        data = args.official.read_bytes()
    else:
        with urlopen(OFFICIAL_URL, timeout=30) as response:
            data = response.read()
    prepare(args.out, data)
