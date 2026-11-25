from typing import Literal, Self

from pydantic import Field, model_validator

from .enums import (
    CriterionStatus, ExecutionStatus, FindingType, QualityCriterion,
    QuestionPriority, Readiness, RequirementCategory, SemanticStatus,
    Severity, StakeholderRole,
)
from .source import Contract, Digest, EvidenceRef, Id, Text, unique


class Requirement(Contract):
    req_id: Id
    original_id: Text | None = None
    category: RequirementCategory
    summary: Text
    source: EvidenceRef


class CriterionEvaluation(Contract):
    # None targets the whole specification.
    requirement_id: Id | None = None
    criterion: QualityCriterion
    status: CriterionStatus
    rationale: Text
    finding_ids: list[Id] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_result(self) -> Self:
        unique(self.finding_ids, "evaluation finding")
        if bool(self.finding_ids) != (self.status == CriterionStatus.FAIL):
            raise ValueError("Only failed evaluations cite confirmed findings")
        return self


class GapEvidence(Contract):
    expected_information: Text
    context: EvidenceRef
    inspected_blocks: list[Id] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_scope(self) -> Self:
        unique(self.inspected_blocks, "inspected block")
        if self.context.block_id not in self.inspected_blocks:
            raise ValueError("Gap context must belong to inspection scope")
        return self


class Finding(Contract):
    finding_id: Id
    kind: FindingType
    severity: Severity
    claim: Text
    recommendation: Text
    requirement_ids: list[Id]
    criteria: list[QualityCriterion] = Field(min_length=1)
    evidence: list[EvidenceRef]
    gap: GapEvidence | None = None
    semantic_status: SemanticStatus
    verification_reason: Text

    @property
    def confirmed(self) -> bool:
        return self.semantic_status == SemanticStatus.SUPPORTED

    @model_validator(mode="after")
    def validate_support(self) -> Self:
        unique(self.requirement_ids, "related requirement")
        unique(self.criteria, "finding criterion")
        unique((ref.anchor for ref in self.evidence), "evidence anchor")
        if (self.kind == FindingType.MISSING_INFORMATION) != (self.gap is not None):
            raise ValueError("Missing information requires explicit gap evidence")
        if not self.evidence and not self.gap:
            raise ValueError("A finding requires document context")
        if self.kind == FindingType.CONTRADICTION:
            grounded = [ref for ref in self.evidence if ref.grounded]
            if not any(
                a.block_id != b.block_id or a.end <= b.start or b.end <= a.start
                for index, a in enumerate(grounded) for b in grounded[index + 1:]
            ):
                raise ValueError("Contradiction requires two distinct grounded references")
        refs = [*self.evidence, *([self.gap.context] if self.gap else [])]
        if self.confirmed and not all(ref.grounded for ref in refs):
            raise ValueError("Supported findings require confirmed grounding")
        return self


class WorkSet(Contract):
    planned: list[Id]
    completed: list[Id]

    @model_validator(mode="after")
    def validate_work(self) -> Self:
        unique(self.planned, "planned work")
        unique(self.completed, "completed work")
        if not set(self.completed) <= set(self.planned):
            raise ValueError("Completed work was never planned")
        return self

    @property
    def complete(self) -> bool:
        return len(self.completed) == len(self.planned)


class Coverage(Contract):
    execution: ExecutionStatus
    extraction_blocks: WorkSet
    context_blocks: dict[Id, Text] = Field(default_factory=dict)
    individual_requirements: WorkSet
    audit_batches: WorkSet
    cross_section_groups: WorkSet
    # Missing pages, skipped semantic calls, provider failures, unknown blocks, etc.
    incomplete_reasons: list[Text]
    uncertainties: list[Text]  # Material questions that could change readiness.

    @model_validator(mode="after")
    def validate_coverage(self) -> Self:
        work = (self.extraction_blocks, self.individual_requirements,
                self.audit_batches, self.cross_section_groups)
        if self.execution == ExecutionStatus.FAILED and (
            self.individual_requirements.completed or self.cross_section_groups.completed
        ):
            raise ValueError("A run with usable audits must be partial or complete")
        if self.execution == ExecutionStatus.COMPLETE:
            if self.incomplete_reasons or not all(w.planned and w.complete for w in work):
                raise ValueError("Complete analysis requires all mandatory work")
        elif not self.incomplete_reasons:
            raise ValueError("Incomplete analysis requires a diagnostic")
        return self


class QuestionOption(Contract):
    option_id: Id
    label: Text
    consequence: Text


class Question(Contract):
    question_id: Id
    finding_ids: list[Id] = Field(min_length=1)
    requirement_ids: list[Id]
    evidence: list[EvidenceRef] = Field(min_length=1)
    uncertainty: Text
    stakeholder: StakeholderRole
    priority: QuestionPriority
    business_impact: Text
    question: Text
    options: list[QuestionOption] = Field(min_length=2)
    allow_custom: Literal[True] = True

    @model_validator(mode="after")
    def validate_options(self) -> Self:
        unique((o.option_id for o in self.options), "question option")
        unique(self.finding_ids, "question finding")
        unique(self.requirement_ids, "question requirement")
        return self


class Answer(Contract):
    question_id: Id
    version: Digest
    option_id: Id | None = None
    custom_text: Text | None = None

    @model_validator(mode="after")
    def validate_choice(self) -> Self:
        if (self.option_id is None) == (self.custom_text is None):
            raise ValueError("Supply exactly one selected option or custom answer")
        return self


class ProposedChange(Contract):
    question_id: Id
    source: EvidenceRef
    replacement: Text

    @model_validator(mode="after")
    def validate_edit(self) -> Self:
        if not self.source.grounded or self.source.quote == self.replacement:
            raise ValueError("Change requires a grounded, modified passage")
        return self


class Verdict(Contract):
    readiness: Readiness
    rationale: Text
    next_actions: list[Text]


def readiness_policy(coverage: Coverage, findings: list[Finding], uncertainties: list[str]) -> Readiness:
    if coverage.execution == ExecutionStatus.FAILED:
        return Readiness.UNDETERMINED
    confirmed = [f.severity for f in findings if f.confirmed]
    if Severity.CRITICAL in confirmed:
        return Readiness.NO_GO
    potential_blocker = any(
        f.severity == Severity.CRITICAL and f.semantic_status in
        (SemanticStatus.UNCHECKED, SemanticStatus.UNCERTAIN) for f in findings
    )
    if coverage.execution != ExecutionStatus.COMPLETE or uncertainties or coverage.uncertainties or potential_blocker:
        return Readiness.UNDETERMINED
    if Severity.MAJOR in confirmed:
        return Readiness.CONDITIONAL
    return Readiness.GO
