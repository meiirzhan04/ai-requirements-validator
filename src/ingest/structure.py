"""Local structural inspection and reconciliation of extraction coverage."""
from collections.abc import Mapping, Sequence
import re
from typing import Literal

from src.contracts import EvidenceRef, GroundingStatus, ParsedDocument, Requirement, SourceBlock, WorkSet
from src.contracts.source import Contract, Id, Text


class StructuralSignal(Contract):
    kind: Literal["placeholder", "empty_field", "empty_cell", "empty_table", "empty_section"]
    source: EvidenceRef


class ExtractionScope(Contract):
    extraction_blocks: WorkSet
    context_blocks: dict[Id, Text]
    outstanding_blocks: list[Id]
    incomplete_reasons: list[Text]


_PLACEHOLDER = re.compile(
    r"(?<!\w)(?:TBD|TBC|TODO|FIXME|уточнить|будет\s+уточнено|заполня(?:ется|ются)\s+\S+|не\s+заполнен[оаы]?)(?!\w)|\?{3,}",
    re.I,
)
_FIELD = re.compile(r"^\s*(?:[-*+]\s+)?[\w][^|:\r\n]{0,100}[:：]\s*$")
_HEADING = re.compile(r"^ {0,3}(#{1,6})(?:\s+.*)?$")
_FENCE = re.compile(r"^ {0,3}(?:\x60{3,}|~{3,})")
_CELL = re.compile(r"(?:\\.|[^\\|])+|\|")


def _pipes(line: str) -> list[int]:
    return [m.start() for m in _CELL.finditer(line) if m[0] == "|"]


def _cells(line: str) -> list[tuple[int, int]]:
    pipes = _pipes(line)
    if not pipes:
        return []
    edges = [-1, *pipes, len(line)]
    cells = [(left + 1, right) for left, right in zip(edges, edges[1:])]
    if not line[:pipes[0]].strip():
        cells.pop(0)
    if not line[pipes[-1] + 1:].strip():
        cells.pop()
    return cells


def _heading_level(text: str) -> int | None:
    lines = text.splitlines()
    if len(lines) == 1 and (match := _HEADING.fullmatch(lines[0])):
        return len(match[1])
    if len(lines) == 2 and re.fullmatch(r" {0,3}(?:=+|-+)[ \t]*", lines[1]):
        return 1 if lines[1].strip().startswith("=") else 2
    return None


def scan_structure(document: ParsedDocument) -> list[StructuralSignal]:
    """Signals are located inspection candidates; only a subsequent audit can confirm a defect."""
    signals = []

    def add(kind, block: SourceBlock, start: int, end: int):
        source = EvidenceRef(version=document.version.sha256, block_id=block.block_id,
                             start=start, end=end, quote=block.text[start:end], grounding=GroundingStatus.EXACT)
        signals.append(StructuralSignal(kind=kind, source=source))

    pending_headings = []
    for block in document.version.blocks:
        level = _heading_level(block.text)
        if block.location.page and block.location.section == block.text.strip():
            level = 1
        if level is not None:
            while pending_headings and pending_headings[-1][0] >= level:
                _, empty = pending_headings.pop()
                add("empty_section", empty, 0, len(empty.text))
            pending_headings.append((level, block))
        else:
            pending_headings.clear()
        if _FENCE.match(block.text):
            continue
        offset = 0
        table_header = None
        expected_columns = 0
        table_has_rows = False
        previous_cells = []
        for raw in block.text.splitlines(keepends=True):
            line = raw.rstrip("\r\n")
            for match in _PLACEHOLDER.finditer(line):
                add("placeholder", block, offset + match.start(), offset + match.end())
            if _FIELD.fullmatch(line):
                add("empty_field", block, offset, offset + len(line))
            cells = _cells(line)
            if (cells and len(previous_cells) == len(cells) and
                    all(re.fullmatch(r"\s*:?-{3,}:?\s*", line[a:b]) for a, b in cells)):
                table_header = (offset, offset + len(line))
                expected_columns = len(cells)
                table_has_rows = False
            elif table_header:
                if cells:
                    table_has_rows = True
                    if len(cells) < expected_columns:
                        add("empty_cell", block, offset, offset + len(line))
                    for a, b in cells:
                        if not line[a:b].strip():
                            # Anchor the delimiter, so even a zero-width cell has an original span.
                            anchor = a - 1 if a else b
                            add("empty_cell", block, offset + anchor, offset + anchor + 1)
                else:
                    if not table_has_rows:
                        add("empty_table", block, *table_header)
                    table_header = None
            previous_cells = cells
            offset += len(raw)
        if table_header and not table_has_rows:
            add("empty_table", block, *table_header)
    for _, empty in pending_headings:
        add("empty_section", empty, 0, len(empty.text))
    return signals


def reconcile_scope(document: ParsedDocument, processed: WorkSet, requirements: Sequence[Requirement],
                    context_blocks: Mapping[str, str]) -> ExtractionScope:
    """Only acknowledge completed blocks backed by a grounded requirement or justified context."""
    blocks = {block.block_id: block for block in document.version.blocks}
    if set(processed.planned) - blocks.keys() or context_blocks.keys() - blocks.keys():
        raise ValueError("Extraction scope names an unknown source block")
    cited = set()
    for requirement in requirements:
        ref = requirement.source
        block = blocks.get(ref.block_id)
        if (not block or not ref.grounded or ref.version != document.version.sha256 or
                ref.end > len(block.text) or block.text[ref.start:ref.end] != ref.quote):
            raise ValueError("Requirement has an invalid source anchor")
        cited.add(ref.block_id)
    if cited & context_blocks.keys():
        raise ValueError("A requirement block cannot also be context only")
    justified = {bid: reason for bid, reason in context_blocks.items() if reason.strip()}
    accepted = set(processed.completed) & (cited | justified.keys())
    completed = [bid for bid in blocks if bid in accepted]
    outstanding = [bid for bid in blocks if bid not in accepted]
    reasons = list(document.diagnostics)
    if document.unread_pages:
        pages = ", ".join(map(str, document.unread_pages))
        reasons.append(f"Непрочитанные полностью или частично страницы: {pages}.")
    if outstanding:
        reasons.append("Не завершена обработка блоков: " + ", ".join(outstanding) + ".")
    if not blocks:
        reasons.append("Нет доступных для проверки исходных блоков.")
    return ExtractionScope(
        extraction_blocks=WorkSet(planned=list(blocks), completed=completed),
        context_blocks={bid: justified[bid] for bid in completed if bid in justified},
        outstanding_blocks=outstanding, incomplete_reasons=reasons,
    )
