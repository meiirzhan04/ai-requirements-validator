from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re

import pymupdf
import pytest
from pydantic import ValidationError

from eval_samples import prepare as prepare_mod
from eval_samples.evaluate import Review, build_review_template, evaluate, fraction, score
from eval_samples.prepare import ROOT, prepare, text_pdf
from src.contracts import (
    INDIVIDUAL,
    WHOLE_SET,
    Coverage,
    CriterionEvaluation,
    CriterionStatus,
    EvidenceRef,
    ExecutionStatus,
    GroundingStatus,
    Readiness,
    Requirement,
    RequirementCategory,
    RunMetadata,
    ValidationReport,
    Verdict,
    WorkSet,
)
from src.ingest import parse


def _make_clean_live_report(doc, run_id: str) -> ValidationReport:
    blocks = doc.version.blocks
    req_block = blocks[-1]
    req = Requirement(
        req_id="R001",
        original_id="R1",
        category=RequirementCategory.FUNCTIONAL,
        summary=req_block.text,
        source=EvidenceRef(
            version=doc.version.sha256,
            block_id=req_block.block_id,
            start=0,
            end=len(req_block.text),
            quote=req_block.text,
            grounding=GroundingStatus.EXACT,
        ),
    )
    evaluations = [
        CriterionEvaluation(requirement_id="R001", criterion=c, status=CriterionStatus.PASS, rationale="Выполнен")
        for c in sorted(INDIVIDUAL)
    ] + [
        CriterionEvaluation(requirement_id=None, criterion=c, status=CriterionStatus.PASS, rationale="Выполнен")
        for c in sorted(WHOLE_SET)
    ]
    all_block_ids = [b.block_id for b in blocks]
    context_blocks = {b.block_id: "Контекстный блок" for b in blocks if b.block_id != req_block.block_id}
    return ValidationReport(
        metadata=RunMetadata(
            run_id=run_id,
            document_version=doc.version.sha256,
            started_at=datetime(2026, 11, 27, 10, 0, tzinfo=timezone.utc),
            model="gemini-3.5-flash-lite",
            agent_revision="rev123",
            prompt_version="pipeline-v2",
            origin="live",
            duration_seconds=1.2,
            request_attempts=3,
        ),
        document=doc,
        requirements=[req],
        evaluations=evaluations,
        findings=[],
        coverage=Coverage(
            execution=ExecutionStatus.COMPLETE,
            extraction_blocks=WorkSet(planned=all_block_ids, completed=all_block_ids),
            context_blocks=context_blocks,
            individual_requirements=WorkSet(planned=["R001"], completed=["R001"]),
            audit_batches=WorkSet(planned=["extract-1", "indiv-1"], completed=["extract-1", "indiv-1"]),
            cross_section_groups=WorkSet(planned=["whole-spec"], completed=["whole-spec"]),
            incomplete_reasons=[],
            uncertainties=[],
        ),
        questions=[],
        verdict=Verdict(readiness=Readiness.GO, rationale="Обязательные проверки завершены.", next_actions=[]),
    )


def test_counts_duplicate_and_unmeasured(report):
    duplicate = report.findings[0].model_copy(update={"finding_id": "F002"})
    report.findings.append(duplicate)
    truth = {"requirements": {"a": "", "b": ""}, "defects": {"x": ["critical", ""]},
             "valid": {"ok": "known-valid scenario"}}
    review = Review(run_id=report.metadata.run_id, document_sha256=report.metadata.document_version,
                    requirements={"R001": ["a"], "R002": []},
                    findings={"F001": "x", "F002": "x"}, false_alarms={"ok": []},
                    questions={"Q001": [True] * 4}, reviewers=[])
    result = score(report, truth, review)
    assert result["finding_precision"] == fraction(1, 2)
    assert result["requirement_recall"] == fraction(1, 2)
    assert result["critical_recall"] == fraction(1, 1)
    assert result["false_positive_rate"] == fraction(0, 1)
    assert result["question_usefulness"] is None
    assert result["grounding"] == fraction(4, 4)
    assert fraction(0, 0)["value"] is None
    assert score(report, truth)["finding_recall"] is None
    review.run_id = "other"
    with pytest.raises(ValueError, match="another run"):
        score(report, truth, review)


def test_rejects_fixture_and_changed_official(report, tmp_path):
    path = tmp_path / "report.json"
    path.write_text(report.model_dump_json(), encoding="utf-8")
    with pytest.raises(ValueError, match="real analysis"):
        evaluate(ROOT / "clean.md", path)
    with pytest.raises(ValueError, match="pinned"):
        prepare(tmp_path, b"changed")


def test_pdf_selectable_unicode(tmp_path):
    path = tmp_path / "example.pdf"
    text_pdf("Порог ≥ 50 ₸\nКарта: 7", path)
    with pymupdf.open(path) as doc:
        assert "".join(doc[0].get_text().split()) == "Порог≥50₸Карта:7"
    truth = json.loads((ROOT / "truth.json").read_text(encoding="utf-8"))
    assert set(truth) == {"official", "clean", "defective", "untrusted"}


def test_prepare_generates_invalid_and_mixed_inputs_with_safe_rejections(tmp_path):
    from eval_samples.prepare import OFFICIAL_SHA
    from src.contracts import ExecutionStatus, PipelineStage, ProviderSettings, Readiness
    from src.ingest import parse
    from src.pipeline import execute_pipeline
    from src.provider import FailureReason, ProviderError

    # Create valid synthetic official bytes matching or test invalid/mixed directly via prepare helper pieces.
    # Verify empty, corrupt PDF, and unsupported TXT fail before provider contact.
    for name, raw in (("empty.md", b""), ("corrupt.pdf", b"%PDF-invalid"), ("unsupported.txt", b"Example")):
        with pytest.raises(ValueError):
            parse(name, raw)

    # Mixed readable + raster-only PDF preserves unread_pages and never yields GO.
    mixed_path = tmp_path / "mixed.pdf"
    with pymupdf.open() as doc:
        doc.new_page().insert_text((30, 40), "A booking expires after 30 minutes.")
        raster = doc[0].get_pixmap()
        doc.new_page().insert_image(doc[0].rect, pixmap=raster)
        doc.save(mixed_path)

    parsed_mixed = parse("mixed.pdf", mixed_path.read_bytes())
    assert parsed_mixed.unread_pages == [2]
    assert parsed_mixed.diagnostics

    def failing_provider(stage, payload, output_type, *, settings=None, **kwargs):
        raise ProviderError("Rate limit exceeded (HTTP 429)", reason=FailureReason.RATE_LIMITED, attempts=3)

    failed_report = execute_pipeline(
        parsed_mixed,
        ProviderSettings(api_key="test"),
        generate_fn=failing_provider,
    )
    assert failed_report.coverage.execution == ExecutionStatus.FAILED
    assert failed_report.verdict.readiness == Readiness.UNDETERMINED
    assert OFFICIAL_SHA


def test_two_reviewer_adjudication_unlocks_question_usefulness(report):
    truth = {
        "requirements": {"a": "rule a", "b": "rule b"},
        "defects": {"x": ["critical", "conflict"]},
        "valid": {"ok": "valid scenario"},
    }
    review = Review(
        run_id=report.metadata.run_id,
        document_sha256=report.metadata.document_version,
        requirements={"R001": ["a"], "R002": ["b"]},
        findings={"F001": "x"},
        false_alarms={"ok": []},
        questions={"Q001": (True, True, True, True)},
        reviewers=["Reviewer A", "Reviewer B"],
    )
    scored = score(report, truth, review)
    assert scored["requirement_recall"] == fraction(2, 2)
    assert scored["finding_precision"] == fraction(1, 1)
    assert scored["critical_recall"] == fraction(1, 1)
    assert scored["false_positive_rate"] == fraction(0, 1)
    assert scored["question_usefulness"] == fraction(1, 1)
    assert scored["reviewers"] == ["Reviewer A", "Reviewer B"]


def test_prepare_creates_all_six_evaluation_input_groups(tmp_path, monkeypatch):
    sample_official = "# Официальный образец\n\nR1. Порог уведомления ≥ 1 000 KZT.\n".encode("utf-8")
    monkeypatch.setattr(prepare_mod, "OFFICIAL_SHA", hashlib.sha256(sample_official).hexdigest())

    prepare(tmp_path, sample_official)

    expected = {
        "official.md",
        "official.pdf",
        "clean.md",
        "defective.md",
        "untrusted.md",
        "empty.md",
        "corrupt.pdf",
        "unsupported.txt",
        "mixed.pdf",
    }
    assert expected <= {path.name for path in tmp_path.iterdir()}

    parsed_md = parse("official.md", (tmp_path / "official.md").read_bytes())
    parsed_pdf = parse("official.pdf", (tmp_path / "official.pdf").read_bytes())
    assert parsed_md.version.blocks and parsed_pdf.version.blocks
    assert not parsed_pdf.unread_pages

    parsed_mixed = parse("mixed.pdf", (tmp_path / "mixed.pdf").read_bytes())
    assert parsed_mixed.unread_pages == [2]

    for invalid_name in ("empty.md", "corrupt.pdf", "unsupported.txt"):
        with pytest.raises(ValueError):
            parse(invalid_name, (tmp_path / invalid_name).read_bytes())


def test_zero_denominators_missed_critical_false_alarms_and_reviewer_dedup(report):
    # Clean report with zero findings/defects/questions must report null ratios cleanly.
    clean_doc = parse("clean.md", (ROOT / "clean.md").read_bytes())
    clean_report = _make_clean_live_report(clean_doc, "run-clean-eval")
    clean_truth = {"requirements": {"empty": "r1"}, "defects": {}, "valid": {"ok": "v1"}}
    clean_review = Review(
        run_id="run-clean-eval",
        document_sha256=clean_doc.version.sha256,
        requirements={"R001": ["empty"]},
        findings={},
        false_alarms={"ok": []},
        questions={},
        reviewers=["Parasat", "Meiirzhan"],
    )
    clean_scored = score(clean_report, clean_truth, clean_review)
    assert clean_scored["grounding"] == fraction(0, 0)
    assert clean_scored["finding_precision"] == fraction(0, 0)
    assert clean_scored["finding_recall"] == fraction(0, 0)
    assert clean_scored["critical_recall"] == fraction(0, 0)
    assert clean_scored["question_usefulness"] == fraction(0, 0)
    assert clean_scored["false_positive_rate"] == fraction(0, 1)
    assert clean_scored["missed_critical"] == []

    # Missed critical defect, false alarm on one of two valid scenarios, and duplicate reviewer names.
    extra_fp = report.findings[0].model_copy(update={"finding_id": "F002"})
    report.findings.append(extra_fp)
    truth = {
        "requirements": {"a": "rule a", "b": "rule b"},
        "defects": {
            "crit_hit": ["critical", "matched conflict"],
            "crit_miss": ["critical", "missed conflict"],
        },
        "valid": {"v1": "valid one", "v2": "valid two"},
    }
    review = Review(
        run_id=report.metadata.run_id,
        document_sha256=report.metadata.document_version,
        requirements={"R001": ["a"], "R002": ["b"]},
        findings={"F001": "crit_hit", "F002": None},
        false_alarms={"v1": ["F002", "F002"], "v2": []},
        questions={"Q001": (True, True, True, True)},
        reviewers=["Parasat", " parasat "],
    )
    scored = score(report, truth, review)
    assert scored["finding_precision"] == fraction(1, 2)
    assert scored["finding_recall"] == fraction(1, 2)
    assert scored["critical_recall"] == fraction(1, 2)
    assert scored["missed_critical"] == ["crit_miss"]
    assert scored["false_positive_rate"] == fraction(1, 2)
    # Same reviewer listed twice in different case/whitespace does not count as two reviewers.
    assert scored["question_usefulness"] is None


def test_review_validation_rejects_inconsistent_annotations(report):
    truth = {
        "requirements": {"a": "rule a"},
        "defects": {"x": ["critical", "conflict"]},
        "valid": {"ok": "valid scenario"},
    }
    base = dict(
        run_id=report.metadata.run_id,
        document_sha256=report.metadata.document_version,
        requirements={"R001": ["a"], "R002": []},
        findings={"F001": "x"},
        false_alarms={"ok": []},
        questions={"Q001": (True, True, True, True)},
        reviewers=["Reviewer A", "Reviewer B"],
    )

    with pytest.raises(ValidationError):
        Review(**base, extra_field="forbidden")

    with pytest.raises(ValueError, match="must account"):
        score(report, truth, Review(**{**base, "requirements": {"R001": ["a"]}}))

    with pytest.raises(ValueError, match="Unknown annotation"):
        score(report, truth, Review(**{**base, "requirements": {"R001": ["unknown"], "R002": []}}))

    with pytest.raises(ValueError, match="Unknown annotation"):
        score(report, truth, Review(**{**base, "findings": {"F001": "unknown"}}))

    with pytest.raises(ValueError, match="unmatched confirmed finding"):
        score(report, truth, Review(**{**base, "false_alarms": {"ok": ["F001"]}}))

    with pytest.raises(ValueError, match="unmatched confirmed finding"):
        score(report, truth, Review(**{**base, "findings": {"F001": None}, "false_alarms": {"ok": ["F999"]}}))


def test_evaluate_round_trip_rejects_tampered_inputs_and_checks_official_pdf_parity(tmp_path):
    source = tmp_path / "clean.md"
    raw = (ROOT / "clean.md").read_bytes()
    source.write_bytes(raw)
    doc = parse("clean.md", raw)
    live_report = _make_clean_live_report(doc, "run-live-clean")
    report_path = tmp_path / "clean_report.json"
    report_path.write_text(live_report.model_dump_json(), encoding="utf-8")

    evaluated = evaluate(source, report_path)
    assert evaluated["case"] == "clean"
    assert evaluated["format"] == "markdown"
    assert evaluated["run"]["model"] == "gemini-3.5-flash-lite"
    assert evaluated["run"]["agent_revision"] == "rev123"
    assert evaluated["coverage"]["extraction_blocks"] == fraction(3, 3)
    assert evaluated["requirement_recall"] is None

    # Tampered input bytes fail digest check.
    tampered = tmp_path / "clean.md"
    tampered.write_bytes(raw + b"\nExtra\n")
    with pytest.raises(ValueError, match="Input digest differs"):
        evaluate(tampered, report_path)

    # Altered PDF under official case fails text-parity digest check.
    bad_pdf = tmp_path / "official.pdf"
    text_pdf("Not the pinned official specification", bad_pdf)
    bad_pdf_bytes = bad_pdf.read_bytes()
    bad_pdf_doc = parse("official.pdf", bad_pdf_bytes)
    bad_pdf_report = _make_clean_live_report(bad_pdf_doc, "run-live-bad-pdf")
    bad_pdf_report_path = tmp_path / "official_pdf_report.json"
    bad_pdf_report_path.write_text(bad_pdf_report.model_dump_json(), encoding="utf-8")
    with pytest.raises(ValueError, match="PDF text differs"):
        evaluate(bad_pdf, bad_pdf_report_path)


def test_review_template_is_valid_and_has_no_fabricated_adjudication(report):
    truth = {
        "requirements": {"a": "rule a", "b": "rule b"},
        "defects": {"x": ["critical", "conflict"]},
        "valid": {"ok": "valid scenario"},
    }
    raw_template = build_review_template(report, truth)
    validated = Review.model_validate(raw_template)
    assert validated.reviewers == []
    assert set(validated.requirements) == {"R001", "R002"}
    assert set(validated.findings) == {"F001"}
    assert set(validated.false_alarms) == {"ok"}
    assert set(validated.questions) == {"Q001"}

    scored = score(report, truth, validated)
    assert scored["question_usefulness"] is None
    assert scored["finding_precision"] == fraction(0, 1)


def test_eval_samples_isolated_from_runtime_code():
    repo_root = ROOT.parent
    runtime_files = [
        repo_root / "main.py",
        *(repo_root / "src").rglob("*.py"),
        *(repo_root / "ui").rglob("*.py"),
    ]
    forbidden = re.compile(r"\beval_samples\b|truth\.json")
    for path in runtime_files:
        text = path.read_text(encoding="utf-8")
        assert not forbidden.search(text), f"Runtime file {path} must not reference evaluation ground truth"
