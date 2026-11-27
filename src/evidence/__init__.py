import re

from src.contracts import (
    EvidenceCandidate, EvidenceRef, GroundingResult, GroundingStatus, ParsedDocument,
)


_SPACE = re.compile(r"[ \u00a0\r\n]+")
_VERBATIM = re.compile(r"`|^ {0,3}~{3,}|^(?: {4}|\t)", re.MULTILINE)


def _original_span(text: str, start: int, end: int) -> tuple[int, int]:
    """Map boundaries in space-folded text back into the original Python string."""
    removed_before_start = removed_before_end = 0
    for run in _SPACE.finditer(text):
        folded_position = run.start() - removed_before_end
        if folded_position >= end:
            break
        removed = run.end() - run.start() - 1
        removed_before_end += removed
        if folded_position < start:
            removed_before_start += removed
    return start + removed_before_start, end + removed_before_end


def ground(candidate: EvidenceCandidate, document: ParsedDocument) -> GroundingResult:
    """Return a reference only for a uniquely addressable original passage."""
    if candidate.version != document.version.sha256:
        raise ValueError("Evidence belongs to a different document version")
    block = next((b for b in document.version.blocks if b.block_id == candidate.block_id), None)
    if block is None:
        return GroundingResult(status=GroundingStatus.UNVERIFIED, reason="Исходный блок не найден.")

    text, quote = block.text, candidate.quote
    # Spaces and line breaks can change the meaning of a code literal or an indented block.
    verbatim = bool(_VERBATIM.search(text))
    haystack = text if verbatim else _SPACE.sub(" ", text)
    needle = quote if verbatim else _SPACE.sub(" ", quote)
    start = haystack.find(needle)
    if start < 0:
        return GroundingResult(status=GroundingStatus.UNVERIFIED, reason="Цитата отсутствует в указанном блоке.")
    if haystack.find(needle, start + 1) >= 0:
        return GroundingResult(status=GroundingStatus.UNVERIFIED, reason="Цитата встречается в блоке несколько раз.")

    exact = text.find(quote)
    if exact >= 0:
        start, end = exact, exact + len(quote)
        status = GroundingStatus.EXACT
        reason = "Цитата дословно совпадает с источником."
    else:
        start, end = _original_span(text, start, start + len(needle))
        status = GroundingStatus.NORMALIZED
        reason = "Цитата совпадает после нормализации пробелов и переносов строк."
    reference = EvidenceRef(version=candidate.version, block_id=block.block_id, start=start, end=end,
                            quote=text[start:end], grounding=status)
    return GroundingResult(status=status, reference=reference, reason=reason)
