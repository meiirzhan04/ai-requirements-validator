from enum import StrEnum


class DocumentFormat(StrEnum):
    MARKDOWN = "markdown"
    PDF = "pdf"


class RequirementCategory(StrEnum):
    BUSINESS = "business"
    FUNCTIONAL = "functional"
    QUALITY = "quality"
    DATA = "data"
    INTERFACE = "interface"
    ACCEPTANCE = "acceptance"
    GOVERNANCE = "governance"


class QualityCriterion(StrEnum):
    # ISO/IEC/IEEE 29148:2018, clause 5.2.5 (individual).
    NECESSARY = "29148.5.2.5.necessary"
    APPROPRIATE = "29148.5.2.5.appropriate"
    UNAMBIGUOUS = "29148.5.2.5.unambiguous"
    COMPLETE = "29148.5.2.5.complete"
    SINGULAR = "29148.5.2.5.singular"
    FEASIBLE = "29148.5.2.5.feasible"
    VERIFIABLE = "29148.5.2.5.verifiable"
    CORRECT = "29148.5.2.5.correct"
    CONFORMING = "29148.5.2.5.conforming"

    SET_COMPLETE = "29148.5.2.6.complete"
    SET_CONSISTENT = "29148.5.2.6.consistent"
    SET_FEASIBLE = "29148.5.2.6.feasible"
    SET_COMPREHENSIBLE = "29148.5.2.6.comprehensible"
    SET_VALIDATABLE = "29148.5.2.6.validatable"

    SRS_CORRECT = "830.4.3.correct"
    SRS_UNAMBIGUOUS = "830.4.3.unambiguous"
    SRS_COMPLETE = "830.4.3.complete"
    SRS_CONSISTENT = "830.4.3.consistent"
    SRS_RANKED = "830.4.3.ranked"
    SRS_VERIFIABLE = "830.4.3.verifiable"
    SRS_MODIFIABLE = "830.4.3.modifiable"
    SRS_TRACEABLE = "830.4.3.traceable"


INDIVIDUAL = frozenset(c for c in QualityCriterion if c.startswith("29148.5.2.5."))
WHOLE_SET = frozenset(QualityCriterion) - INDIVIDUAL


class FindingType(StrEnum):
    CONTRADICTION = "contradiction"
    MISSING_INFORMATION = "missing_information"
    AMBIGUITY = "ambiguity"
    UNVERIFIABLE = "unverifiable"
    OTHER = "other"


class Severity(StrEnum):
    CRITICAL = "critical"
    MAJOR = "major"
    MINOR = "minor"


class CriterionStatus(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    UNKNOWN = "unknown"
    NOT_APPLICABLE = "not_applicable"


class GroundingStatus(StrEnum):
    EXACT = "exact"
    NORMALIZED = "normalized"
    FUZZY = "fuzzy_candidate"
    UNVERIFIED = "unverified"


class SemanticStatus(StrEnum):
    SUPPORTED = "supported"
    REFUTED = "refuted"
    UNCERTAIN = "uncertain"
    UNCHECKED = "unchecked"


class ExecutionStatus(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    FAILED = "failed"


class Readiness(StrEnum):
    GO = "go"
    CONDITIONAL = "conditional_go"
    NO_GO = "no_go"
    UNDETERMINED = "undetermined"


class StakeholderRole(StrEnum):
    PRODUCT = "product_owner"
    ANALYST = "analyst"
    ARCHITECT = "architect"
    ENGINEER = "engineer"
    QA = "qa"
    SECURITY = "security"


class QuestionPriority(StrEnum):
    BLOCKER = "P0"
    HIGH = "P1"
    OPTIONAL = "P2"


class PipelineStage(StrEnum):
    INGEST = "ingest"
    EXTRACT = "extract"
    INDIVIDUAL = "individual_audit"
    HOLISTIC = "set_audit"
    VERIFY = "verify"
    CLARIFY = "clarify"
    REPORT = "report"
    SMOKE = "smoke"
