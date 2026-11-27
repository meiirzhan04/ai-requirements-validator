"""Provider-facing data only. No canonical IDs, totals or readiness decisions."""
from typing import Literal

from pydantic import BaseModel, Field

from src.contracts import RequirementCategory


class ProbeRequest(BaseModel):
    instruction: str = "Return the integer 7 in the field value."


class ProbeResponse(BaseModel):
    value: int


class ExtractionBlockInput(BaseModel):
    block_id: str
    section: str
    text: str
    is_target: bool = True
    table_header: str | None = None


class ExtractionBatchPayload(BaseModel):
    system_instruction: str
    prompt_version: str
    document_name: str
    batch_id: str
    target_block_ids: list[str]
    blocks: list[ExtractionBlockInput]
    repair_hint: str | None = None


class ExtractedItem(BaseModel):
    block_id: str
    original_quote: str
    summary: str
    original_id: str | None = None
    category: RequirementCategory = RequirementCategory.FUNCTIONAL


class ContextBlockDisposition(BaseModel):
    block_id: str
    reason: str


class ExtractionOutput(BaseModel):
    items: list[ExtractedItem] = Field(default_factory=list)
    context_blocks: list[ContextBlockDisposition] = Field(default_factory=list)


class SemanticReview(BaseModel):
    candidate_id: str
    decision: Literal["supported", "refuted", "uncertain"]
    reason: str
