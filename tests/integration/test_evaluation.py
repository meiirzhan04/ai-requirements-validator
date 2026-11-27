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
