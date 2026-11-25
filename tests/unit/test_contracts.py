from copy import deepcopy

import pytest
from pydantic import ValidationError

from src.contracts import GroundingResult, ValidationReport


def test_fixture_round_trip(report, report_data):
    assert report.metadata.origin == "synthetic"
    assert report.metadata.request_attempts == 0
    assert report.statistics.violations == 1
    assert len(report.evaluations) == 2 * 9 + 5 + 8
    dumped = report.model_dump_json()
    assert ValidationReport.model_validate_json(dumped).model_dump_json() == dumped
    report_data.pop("statistics")
    assert ValidationReport.model_validate(report_data).statistics == report.statistics


@pytest.mark.parametrize("collection", ["requirements", "findings", "questions", "evaluations"])
def test_duplicate(report_data, collection):
    report_data[collection].append(deepcopy(report_data[collection][0]))
    with pytest.raises(ValidationError, match="Duplicate"):
        ValidationReport.model_validate(report_data)


@pytest.mark.parametrize(("path", "bad"), [
    (("findings", 0, "requirement_ids"), ["R999"]),
    (("questions", 0, "finding_ids"), ["F999"]),
    (("questions", 0, "requirement_ids"), ["R999"]),
    (("requirements", 0, "source", "version"), "a" * 64),
    (("metadata", "document_version"), "a" * 64),
    (("requirements", 0, "source", "start"), 9999),
    (("requirements", 0, "source", "end"), 9999),
    (("requirements", 0, "source", "quote"), "A different rule"),
    (("requirements", 0, "source", "grounding"), "fuzzy_candidate"),
    (("document", "version", "blocks", 0, "location", "line_end"), 1),
    (("document", "version", "blocks", 0, "location", "line_end"), 99),
    (("coverage", "extraction_blocks", "planned"), ["B001"]),
    (("coverage", "audit_batches", "completed"), []),
    (("coverage", "incomplete_reasons"), ["response truncated"]),
    (("statistics", "critical"), 0),
    (("statistics", "violations"), 2),
    (("verdict", "readiness"), "go"),
    (("evaluations", 0, "criterion"), "830.4.3.complete"),
    (("findings", 0, "kind"), "missing_information"),
    (("findings", 0, "semantic_status"), "uncertain"),
])
def test_reject_invalid_report(report_data, path, bad):
    target = report_data
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = bad
    with pytest.raises(ValidationError):
        ValidationReport.model_validate(report_data)


@pytest.mark.parametrize("index", [0, -1])
def test_missing_individual_or_set_criterion(report_data, index):
    report_data["evaluations"].pop(index)
    with pytest.raises(ValidationError, match="Missing required"):
        ValidationReport.model_validate(report_data)


@pytest.mark.parametrize("alteration", ["remove", "duplicate", "fuzzy", "overlap"])
def test_contradiction_requires_independent_anchors(report_data, alteration):
    refs = report_data["findings"][0]["evidence"]
    if alteration == "remove":
        refs.pop()
    elif alteration == "duplicate":
        refs[1] = deepcopy(refs[0])
    elif alteration == "fuzzy":
        refs[1]["grounding"] = "fuzzy_candidate"
    else:
        refs[1] = dict(refs[0], start=1, quote=refs[0]["quote"][1:])
    with pytest.raises(ValidationError):
        ValidationReport.model_validate(report_data)


def test_absence_has_scope_and_context(report_data):
    f = report_data["findings"][0]
    ref = f["evidence"][0]
    f.update(kind="missing_information", gap={
        "expected_information": "Cancellation confirmation channel",
        "context": ref, "inspected_blocks": ["B001", "B002"],
    })
    ValidationReport.model_validate(report_data)
    f["gap"]["inspected_blocks"].append("B999")
    with pytest.raises(ValidationError, match="Gap scope"):
        ValidationReport.model_validate(report_data)


def test_valid_quote_alone_cannot_confirm_a_finding(report_data):
    report_data["findings"][0]["semantic_status"] = "unchecked"
    with pytest.raises(ValidationError, match="confirmed finding"):
        ValidationReport.model_validate(report_data)


def test_answer_and_preview_do_not_resolve(report_data):
    ref = report_data["requirements"][0]["source"]
    report_data["answers"] = [{"question_id": "Q001", "version": ref["version"], "option_id": "A"}]
    report_data["proposed_changes"] = [{
        "question_id": "Q001", "source": ref, "replacement": "Send confirmation by email.",
    }]
    validated = ValidationReport.model_validate(report_data)
    assert validated.statistics.critical == 1 and validated.verdict.readiness == "no_go"
    report_data["answers"][0]["option_id"] = "unknown"
    with pytest.raises(ValidationError, match="Unknown answer option"):
        ValidationReport.model_validate(report_data)


def test_grounding_cannot_upgrade_a_fuzzy_candidate(report):
    ref = report.requirements[0].source.model_dump()
    ref["grounding"] = "fuzzy_candidate"
    with pytest.raises(ValidationError):
        GroundingResult(status="exact", reference=ref, reason="exact")


def test_pdf_missing_page_blocks_complete_report(report_data):
    doc = report_data["document"]
    doc["version"].update(format="pdf", page_count=3)
    for page, block in enumerate(doc["version"]["blocks"], start=1):
        block["location"]["page"] = page
    with pytest.raises(ValidationError, match="Page inventory"):
        ValidationReport.model_validate(report_data)
    doc["unread_pages"] = [3]
    with pytest.raises(ValidationError, match="unread input"):
        ValidationReport.model_validate(report_data)
    doc["unread_pages"] = []
    doc["blank_pages"] = [3]
    assert ValidationReport.model_validate(report_data).coverage.execution == "complete"


def test_processed_context_block_requires_explanation(report_data):
    block = deepcopy(report_data["document"]["version"]["blocks"][0])
    block.update(block_id="B003", text="Museum opening hours.")
    report_data["document"]["version"]["blocks"].append(block)
    coverage = report_data["coverage"]
    for field in ("planned", "completed"):
        coverage["extraction_blocks"][field].append("B003")
    with pytest.raises(ValidationError, match="context rationale"):
        ValidationReport.model_validate(report_data)
    coverage["context_blocks"] = {"B003": "Background, no normative rule."}
    ValidationReport.model_validate(report_data)


def test_valid_normalized_evidence_retains_original_text(report_data):
    source = report_data["requirements"][0]["source"]
    source["grounding"] = "normalized"
    assert ValidationReport.model_validate(report_data).requirements[0].source.grounded
    source["quote"] = source["quote"].replace(" ", "")
    with pytest.raises(ValidationError, match="original text"):
        ValidationReport.model_validate(report_data)
