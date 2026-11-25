"""Executable acceptance rows for Dulat's decide implementation."""
import pytest

from src.contracts import Coverage, Finding, readiness_policy


@pytest.mark.parametrize(("execution", "severity", "semantic", "uncertainties", "expected"), [
    ("failed", None, None, [], "undetermined"),
    ("partial", "critical", "supported", [], "no_go"),
    ("complete", "critical", "supported", ["pending owner"], "no_go"),
    ("partial", None, None, [], "undetermined"),
    ("complete", "critical", "uncertain", [], "undetermined"),
    ("complete", "critical", "unchecked", [], "undetermined"),
    ("complete", None, None, ["pending owner"], "undetermined"),
    ("complete", "major", "supported", [], "conditional_go"),
    ("complete", "minor", "supported", [], "go"),
    ("complete", "critical", "refuted", [], "go"),
    ("complete", None, None, [], "go"),
])
def test_decision_table(report_data, execution, severity, semantic, uncertainties, expected):
    data = report_data["coverage"]
    data.update(execution=execution, incomplete_reasons=[] if execution == "complete" else ["provider failure"])
    if execution == "failed":
        for work in ("individual_requirements", "cross_section_groups", "audit_batches"):
            data[work]["completed"] = []
    findings = []
    if severity:
        findings.append(Finding.model_validate(dict(report_data["findings"][0], severity=severity, semantic_status=semantic)))
    assert readiness_policy(Coverage.model_validate(data), findings, uncertainties) == expected
