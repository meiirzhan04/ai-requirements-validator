"""Provider-facing data only. No canonical IDs, totals or readiness decisions."""
from typing import Literal

from pydantic import BaseModel, Field

from src.contracts import CriterionStatus, FindingType, QualityCriterion, Severity


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


class IndividualRequirementInput(BaseModel):
    req_id: str
    original_id: str | None = None
    category: str
    summary: str
    block_id: str
    section: str
    quote: str
    block_text: str


class IndividualAuditPayload(BaseModel):
    system_instruction: str
    prompt_version: str
    document_name: str
    batch_id: str
    target_req_ids: list[str]
    criteria_catalog: list[str]
    requirements: list[IndividualRequirementInput]
    repair_hint: str | None = None


class IndividualCriterionCandidate(BaseModel):
    requirement_id: str
    criterion: QualityCriterion
    status: CriterionStatus
    rationale: str
    local_finding_keys: list[str] = Field(default_factory=list)


class IndividualEvidenceQuote(BaseModel):
    block_id: str
    quote: str


class IndividualFindingCandidate(BaseModel):
    local_key: str
    requirement_id: str
    kind: FindingType
    severity: Severity
    claim: str
    recommendation: str
    criteria: list[QualityCriterion] = Field(min_length=1)
    evidence: list[IndividualEvidenceQuote] = Field(min_length=1)
    expected_information: str | None = None


class IndividualAuditOutput(BaseModel):
    evaluations: list[IndividualCriterionCandidate] = Field(default_factory=list)
    findings: list[IndividualFindingCandidate] = Field(default_factory=list)
