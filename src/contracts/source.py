from collections.abc import Iterable
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from .enums import DocumentFormat, GroundingStatus

Id = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")]
Text = Annotated[str, StringConstraints(min_length=1, pattern=r"\S")]
Digest = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
Positive = Annotated[int, Field(ge=1)]
Count = Annotated[int, Field(ge=0)]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


def unique(values: Iterable[object], label: str) -> None:
    items = list(values)
    if len(set(items)) != len(items):
        raise ValueError(f"Duplicate {label}")


class SourceLocation(Contract):
    section: Text
    page: Positive | None = None
    line_start: Positive | None = None
    line_end: Positive | None = None
    bbox: tuple[float, float, float, float] | None = None

    @model_validator(mode="after")
    def validate_location(self) -> Self:
        if (self.line_start is None) != (self.line_end is None):
            raise ValueError("Supply both ends of a line range")
        if self.line_start is not None and self.line_end < self.line_start:
            raise ValueError("Reversed line range")
        if self.bbox:
            x0, y0, x1, y1 = self.bbox
            if self.page is None or x1 <= x0 or y1 <= y0:
                raise ValueError("Invalid page rectangle")
        return self


class SourceBlock(Contract):
    block_id: Id
    text: Text
    location: SourceLocation


class DocumentVersion(Contract):
    # SHA-256 of input bytes (or of the edited UTF-8 working copy).
    sha256: Digest
    parent_sha256: Digest | None = None
    parser_version: Id
    name: Text
    format: DocumentFormat
    page_count: Positive | None = None
    line_count: Count | None = None
    blocks: list[SourceBlock]

    @model_validator(mode="after")
    def validate_blocks(self) -> Self:
        unique((b.block_id for b in self.blocks), "source block")
        if self.parent_sha256 == self.sha256:
            raise ValueError("A revision cannot be its own parent")
        if (self.format == DocumentFormat.PDF) != (self.page_count is not None):
            raise ValueError("Only PDF revisions declare a page count")
        if self.format == DocumentFormat.MARKDOWN and self.line_count is None:
            raise ValueError("Markdown requires the original line count")
        for block in self.blocks:
            loc = block.location
            if self.format == DocumentFormat.PDF:
                if loc.page is None or loc.page > self.page_count:
                    raise ValueError("PDF block outside document pages")
            elif loc.page is not None or loc.line_start is None or loc.line_end > self.line_count:
                raise ValueError("Markdown block requires source lines")
        return self


class ParsedDocument(Contract):
    version: DocumentVersion
    unread_pages: list[Positive] = Field(default_factory=list)
    blank_pages: list[Positive] = Field(default_factory=list)
    diagnostics: list[Text] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_pages(self) -> Self:
        unique([*self.unread_pages, *self.blank_pages], "page disposition")
        pages = {b.location.page for b in self.version.blocks if b.location.page is not None}
        if pages.intersection(self.blank_pages):
            raise ValueError("A blank page contains source blocks")
        accounted = pages.union(self.unread_pages, self.blank_pages)
        if accounted != set(range(1, (self.version.page_count or 0) + 1)):
            raise ValueError("Page inventory does not match the source document")
        return self


class EvidenceRef(Contract):
    version: Digest
    block_id: Id
    start: Count
    end: Positive
    quote: Text
    grounding: GroundingStatus

    @model_validator(mode="after")
    def validate_range(self) -> Self:
        if self.end <= self.start:
            raise ValueError("Empty or reversed source range")
        return self

    @property
    def grounded(self) -> bool:
        return self.grounding in (GroundingStatus.EXACT, GroundingStatus.NORMALIZED)

    @property
    def anchor(self) -> tuple[str, str, int, int]:
        return self.version, self.block_id, self.start, self.end


class EvidenceCandidate(Contract):
    version: Digest
    block_id: Id
    quote: Text


class GroundingResult(Contract):
    status: GroundingStatus
    reference: EvidenceRef | None = None
    reason: Text

    @model_validator(mode="after")
    def validate_status(self) -> Self:
        if self.status in (GroundingStatus.EXACT, GroundingStatus.NORMALIZED) and not self.reference:
            raise ValueError("Confirmed grounding requires a source reference")
        if self.reference and self.reference.grounding != self.status:
            raise ValueError("Reference and result disagree on grounding")
        return self
