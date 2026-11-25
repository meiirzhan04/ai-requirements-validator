from typing import Literal, Self

from pydantic import Field, model_validator

from .audit import (
    Answer, Coverage, CriterionEvaluation, Finding, ProposedChange,
    Question, Requirement, Verdict, readiness_policy,
)
from .enums import INDIVIDUAL, WHOLE_SET, ExecutionStatus, QuestionPriority, Readiness, SemanticStatus, Severity
from .runtime import RunMetadata
from .source import Contract, Count, EvidenceRef, ParsedDocument, unique


class ReportStatistics(Contract):
    requirements: Count
    violations: Count
    critical: Count
    major: Count
    minor: Count
    open_hypotheses: Count
    questions: Count


class ValidationReport(Contract):
    schema_version: Literal["1"] = "1"
    metadata: RunMetadata
    document: ParsedDocument
    requirements: list[Requirement]
    evaluations: list[CriterionEvaluation]
    findings: list[Finding]
    coverage: Coverage
    questions: list[Question]
    answers: list[Answer] = Field(default_factory=list)
    proposed_changes: list[ProposedChange] = Field(default_factory=list)
    verdict: Verdict
    statistics: ReportStatistics | None = None

    @model_validator(mode="after")
    def validate_report(self) -> Self:
        self._validate_sources()
        self._validate_audits()
        self._validate_decisions()
        confirmed = [f for f in self.findings if f.confirmed]
        calculated = ReportStatistics(
            requirements=len(self.requirements), violations=len(confirmed),
            critical=sum(f.severity == Severity.CRITICAL for f in confirmed),
            major=sum(f.severity == Severity.MAJOR for f in confirmed),
            minor=sum(f.severity == Severity.MINOR for f in confirmed),
            open_hypotheses=sum(f.semantic_status in (SemanticStatus.UNCHECKED, SemanticStatus.UNCERTAIN)
                                for f in self.findings),
            questions=len(self.questions),
        )
        if self.statistics is not None and self.statistics != calculated:
            raise ValueError("Reported statistics differ from canonical data")
        self.statistics = calculated
        return self

    def _validate_sources(self) -> None:
        version = self.document.version
        blocks = {b.block_id: b.text for b in version.blocks}
        if self.metadata.document_version != version.sha256:
            raise ValueError("Run uses a different document version")

        def check(ref: EvidenceRef) -> None:
            text = blocks.get(ref.block_id)
            if ref.version != version.sha256 or text is None or ref.end > len(text):
                raise ValueError("Invalid evidence version, block or range")
            if ref.grounded and text[ref.start:ref.end] != ref.quote:
                raise ValueError("Grounded quotation must reproduce original text")

        for req in self.requirements:
            check(req.source)
            if not req.source.grounded:
                raise ValueError("Requirement needs a grounded source")
        for finding in self.findings:
            for ref in finding.evidence:
                check(ref)
            if finding.gap:
                check(finding.gap.context)
                scope = set(finding.gap.inspected_blocks)
                if not scope <= blocks.keys():
                    raise ValueError("Gap scope refers to missing blocks")
                if finding.confirmed and not scope <= set(self.coverage.extraction_blocks.completed):
                    raise ValueError("Confirmed gap includes uninspected blocks")
        for question in self.questions:
            for ref in question.evidence:
                check(ref)
        for change in self.proposed_changes:
            check(change.source)

    def _validate_audits(self) -> None:
        reqs = {r.req_id for r in self.requirements}
        findings = {f.finding_id: f for f in self.findings}
        unique((r.req_id for r in self.requirements), "requirement ID")
        unique((f.finding_id for f in self.findings), "finding ID")
        cover = self.coverage
        if set(cover.extraction_blocks.planned) != {b.block_id for b in self.document.version.blocks}:
            raise ValueError("Coverage omits or invents source blocks")
        if set(cover.individual_requirements.planned) != reqs:
            raise ValueError("Coverage omits or invents requirements")
        completed_blocks = set(cover.extraction_blocks.completed)
        cited_blocks = {r.source.block_id for r in self.requirements}
        context_blocks = set(cover.context_blocks)
        if cited_blocks & context_blocks or cited_blocks | context_blocks != completed_blocks:
            raise ValueError("Each processed block requires extracted requirements or a context rationale")
        if cover.execution == ExecutionStatus.COMPLETE:
            if self.document.unread_pages or self.document.diagnostics:
                raise ValueError("Complete analysis has unread input")
            if self.metadata.origin == "live" and not self.metadata.request_attempts:
                raise ValueError("Complete live analysis requires a provider request")
        if cover.execution == ExecutionStatus.FAILED and any(f.confirmed for f in self.findings):
            raise ValueError("Use partial for a failed run with usable confirmed findings")
        for f in self.findings:
            if not set(f.requirement_ids) <= reqs:
                raise ValueError("Unknown requirement in finding")
        seen = set()
        for e in self.evaluations:
            key = (e.requirement_id, e.criterion)
            if key in seen:
                raise ValueError("Duplicate criterion evaluation")
            seen.add(key)
            if e.requirement_id is None:
                allowed = WHOLE_SET
            else:
                if e.requirement_id not in reqs:
                    raise ValueError("Unknown evaluation target")
                allowed = INDIVIDUAL
            if e.criterion not in allowed:
                raise ValueError("Wrong criterion scope")
            for fid in e.finding_ids:
                f = findings.get(fid)
                if not f or not f.confirmed or e.criterion not in f.criteria:
                    raise ValueError("Failed criterion requires a matching confirmed finding")
                if e.requirement_id is not None and e.requirement_id not in f.requirement_ids:
                    raise ValueError("Finding belongs to another requirement")
        expected = {(r, c) for r in cover.individual_requirements.completed for c in INDIVIDUAL}
        if cover.cross_section_groups.planned and cover.cross_section_groups.complete:
            expected.update((None, c) for c in WHOLE_SET)
        if expected - seen:
            raise ValueError("Missing required criterion evaluations")
        if cover.execution == ExecutionStatus.COMPLETE:
            cited = {(fid, e.criterion) for e in self.evaluations for fid in e.finding_ids}
            declared = {(f.finding_id, c) for f in self.findings if f.confirmed for c in f.criteria}
            if cited != declared:
                raise ValueError("Confirmed finding missing from completed evaluations")

    def _validate_decisions(self) -> None:
        unique((q.question_id for q in self.questions), "question ID")
        unique((a.question_id for a in self.answers), "answer per question")
        questions = {q.question_id: q for q in self.questions}
        findings = {f.finding_id: f for f in self.findings}
        for q in self.questions:
            if not set(q.finding_ids) <= findings.keys():
                raise ValueError("Unknown question finding")
            related = [findings[fid] for fid in q.finding_ids]
            if any(f.semantic_status == SemanticStatus.REFUTED for f in related):
                raise ValueError("Question refers to a refuted hypothesis")
            if not set(q.requirement_ids) <= {r for f in related for r in f.requirement_ids}:
                raise ValueError("Question refers to unrelated requirement")
            anchors = {r.anchor for f in related for r in f.evidence}
            anchors.update(f.gap.context.anchor for f in related if f.gap)
            if any(not r.grounded or r.anchor not in anchors for r in q.evidence):
                raise ValueError("Question requires grounded evidence from its finding")
            if q.priority == QuestionPriority.BLOCKER and self.verdict.readiness in (Readiness.GO, Readiness.CONDITIONAL):
                raise ValueError("An open blocker question requires a blocker or material uncertainty")
        for answer in self.answers:
            q = questions.get(answer.question_id)
            if q is None or answer.version != self.document.version.sha256:
                raise ValueError("Answer has unknown question or stale version")
            if answer.option_id and answer.option_id not in {o.option_id for o in q.options}:
                raise ValueError("Unknown answer option")
        answered = {a.question_id for a in self.answers}
        for change in self.proposed_changes:
            if change.question_id not in answered:
                raise ValueError("Proposed change requires an answer")
            question = questions[change.question_id]
            if change.source.block_id not in {ref.block_id for ref in question.evidence}:
                raise ValueError("Proposed change targets an unrelated source block")
        if self.verdict.readiness != readiness_policy(self.coverage, self.findings, self.coverage.uncertainties):
            raise ValueError("Verdict conflicts with coverage, findings or uncertainty")
