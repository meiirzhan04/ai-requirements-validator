"""Provider-facing data only. No canonical IDs, totals or readiness decisions."""
from typing import Literal
from pydantic import BaseModel, Field
from src.contracts import CriterionStatus, FindingType, QualityCriterion, RequirementCategory, Severity


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


class HolisticPassageInput(BaseModel):
    block_id: str
    section: str
    text: str


class HolisticRequirementInput(BaseModel):
    req_id: str
    original_id: str | None = None
    category: str
    summary: str
    block_id: str
    quote: str


class HolisticGroupPayload(BaseModel):
    system_instruction: str
    prompt_version: str
    document_name: str
    group_id: str
    topic_keywords: list[str] = Field(default_factory=list)
    criteria_catalog: list[str]
    inspected_block_ids: list[str]
    has_uninspected_scope: bool = False
    requirements: list[HolisticRequirementInput]
    passages: list[HolisticPassageInput]
    repair_hint: str | None = None


class HolisticEvidenceQuote(BaseModel):
    block_id: str
    quote: str


class HolisticFindingCandidate(BaseModel):
    local_key: str
    kind: FindingType
    severity: Severity
    claim: str
    scenario: str | None = None
    conditions: str | None = None
    recommendation: str
    requirement_ids: list[str] = Field(default_factory=list)
    criteria: list[QualityCriterion] = Field(min_length=1)
    evidence: list[HolisticEvidenceQuote] = Field(default_factory=list)
    expected_information: str | None = None
    gap_context: HolisticEvidenceQuote | None = None
    inspected_blocks: list[str] = Field(default_factory=list)


class HolisticCriterionCandidate(BaseModel):
    criterion: QualityCriterion
    status: CriterionStatus
    rationale: str
    local_finding_keys: list[str] = Field(default_factory=list)


class HolisticAuditOutput(BaseModel):
    evaluations: list[HolisticCriterionCandidate] = Field(default_factory=list)
    findings: list[HolisticFindingCandidate] = Field(default_factory=list)


class VerificationCandidateInput(BaseModel):
    candidate_id: str
    kind: FindingType
    severity: Severity
    claim: str
    requirement_ids: list[str]
    evidence_quotes: list[HolisticEvidenceQuote]
    expected_information: str | None = None
    inspected_blocks: list[str] = Field(default_factory=list)


class VerificationPayload(BaseModel):
    system_instruction: str
    prompt_version: str
    document_name: str
    batch_id: str
    has_uninspected_scope: bool = False
    candidates: list[VerificationCandidateInput]
    context_passages: list[HolisticPassageInput]
    repair_hint: str | None = None


class VerificationOutput(BaseModel):
    reviews: list[SemanticReview] = Field(default_factory=list)
