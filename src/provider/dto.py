"""Provider-facing data only. No canonical IDs, totals or readiness decisions."""
from typing import Literal

from pydantic import BaseModel, Field

from src.contracts import QuestionPriority, StakeholderRole


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


class QuestionFindingEvidenceInput(BaseModel):
    block_id: str
    start: int
    end: int
    quote: str


class QuestionFindingInput(BaseModel):
    finding_id: str
    kind: str
    severity: str
    semantic_status: str
    claim: str
    recommendation: str
    requirement_ids: list[str]
    evidence: list[QuestionFindingEvidenceInput]


class QuestionGenerationPayload(BaseModel):
    system_instruction: str
    prompt_version: str
    document_name: str
    batch_id: str
    findings: list[QuestionFindingInput]
    repair_hint: str | None = None


class QuestionOptionDraft(BaseModel):
    option_id: str | None = None
    label: str
    consequence: str


class QuestionCandidate(BaseModel):
    finding_ids: list[str] = Field(min_length=1)
    requirement_ids: list[str] = Field(default_factory=list)
    evidence_block_ids: list[str] = Field(default_factory=list)
    uncertainty: str
    stakeholder: StakeholderRole
    priority: QuestionPriority
    business_impact: str
    question: str
    options: list[QuestionOptionDraft] = Field(min_length=2)
    blocks_readiness: bool = False


class SkippedFindingDecision(BaseModel):
    finding_id: str
    reason: str


class QuestionGenerationOutput(BaseModel):
    questions: list[QuestionCandidate] = Field(default_factory=list)
    skipped_findings: list[SkippedFindingDecision] = Field(default_factory=list)
