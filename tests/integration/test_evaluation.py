import json

import pymupdf
import pytest

from eval_samples.evaluate import Review, evaluate, fraction, score
from eval_samples.prepare import ROOT, prepare, text_pdf


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
