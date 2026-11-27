import pytest

from src.contracts import Coverage, Finding, Severity, ValidationReport
from src.readiness import decide


@pytest.mark.parametrize(("execution", "severity", "semantic", "material", "expected"), [
    ("failed", None, None, [], "undetermined"),
    ("partial", None, None, [], "undetermined"),
    ("partial", "critical", "supported", [], "no_go"),
    ("complete", "critical", "supported", ["Уточнить лимит"], "no_go"),
    ("complete", "critical", "uncertain", [], "undetermined"),
    ("complete", "critical", "unchecked", [], "undetermined"),
    ("complete", "critical", "refuted", [], "go"),
    ("complete", None, None, ["Уточнить лимит"], "undetermined"),
    ("complete", "major", "supported", [], "conditional_go"),
    ("complete", "minor", "supported", [], "go"),
    ("complete", None, None, [], "go"),
])
def test_public_decision_table(report_data, execution, severity, semantic, material, expected):
    data = report_data["coverage"]
    data.update(execution=execution, incomplete_reasons=[] if execution == "complete" else ["Пропущена страница 3"])
    if execution == "failed":
        for scope in ("individual_requirements", "cross_section_groups", "audit_batches"):
            data[scope]["completed"] = []
    coverage = Coverage.model_validate(data)
    findings = []
    if severity:
        findings.append(Finding.model_validate(dict(report_data["findings"][0], severity=severity, semantic_status=semantic)))
    verdict = decide(coverage, findings, material)
    assert verdict.readiness == expected
    assert verdict.next_actions and verdict.rationale
    assert coverage.uncertainties == material
    if execution != "complete":
        assert "Пробелы покрытия" in verdict.rationale and "страница 3" in verdict.rationale
    if execution == "failed":
        assert "ошибкой" in verdict.rationale
    if semantic == "supported":
        assert findings[0].finding_id in verdict.next_actions[0]
    if semantic in ("unchecked", "uncertain"):
        assert findings[0].finding_id in verdict.rationale
    if material:
        assert material[0] in verdict.rationale


def test_new_material_uncertainty_survives_report_roundtrip(report):
    question = "Уточнить дату начала расчёта."
    # Use a complete snapshot with only a major defect, so uncertainty changes the result.
    for finding in report.findings:
        finding.severity = Severity.MAJOR
    report.questions = []
    report.statistics = None
    report.verdict = decide(report.coverage, report.findings, [question, question])
    restored = ValidationReport.model_validate_json(report.model_dump_json())
    assert restored.verdict.readiness == "undetermined"
    assert restored.coverage.uncertainties == [question]
    assert decide(restored.coverage, restored.findings, []) == restored.verdict


def test_unusable_question_leaves_coverage_unchanged(report):
    with pytest.raises(ValueError, match="non-empty"):
        decide(report.coverage, report.findings, ["  "])
    assert report.coverage.uncertainties == []
