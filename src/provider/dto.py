"""Provider-facing data only. No canonical IDs, totals or readiness decisions."""
from typing import Literal

from pydantic import BaseModel


class ProbeRequest(BaseModel):
    instruction: str = "Return the integer 7 in the field value."


class ProbeResponse(BaseModel):
    value: int


class ExtractedItem(BaseModel):
    block_id: str
    original_quote: str
    summary: str


class ExtractionOutput(BaseModel):
    items: list[ExtractedItem]


class SemanticReview(BaseModel):
    candidate_id: str
    decision: Literal["supported", "refuted", "uncertain"]
    reason: str
