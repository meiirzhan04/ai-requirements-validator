"""Automated verification of submission artifacts, live replays, and origin boundaries (#16)."""
import hashlib
import json
from pathlib import Path
import re

import pytest

from eval_samples.evaluate import evaluate
from src.contracts import ExecutionStatus, Readiness, ValidationReport
from src.report import load_json

ROOT = Path(__file__).resolve().parents[2]
DEMO = ROOT / "demo"
EVAL = ROOT / "eval_samples"
SECRET_PATTERN = re.compile(r"AIza[0-9A-Za-z_-]{20,}|GEMINI_API_KEY\s*=\s*\S+")


@pytest.mark.parametrize(
    ("case", "expected_readiness"),
    [
        ("defective", (Readiness.NO_GO,)),
        ("clean", (Readiness.GO,)),
        # Untrusted quote is clean data (truth.json has 0 defects); prompt injection must never skip audit.
        ("untrusted", (Readiness.GO, Readiness.CONDITIONAL, Readiness.UNDETERMINED)),
    ],
)
def test_live_replay_artifacts_validate_and_match_source_inputs(case: str, expected_readiness: tuple[Readiness, ...]):
    source = EVAL / f"{case}.md"
    json_path = DEMO / f"replay_{case}.json"
    md_path = DEMO / f"replay_{case}.md"
    assert json_path.is_file() and md_path.is_file()

    report = load_json(json_path.read_bytes())
    assert isinstance(report, ValidationReport)
    assert report.metadata.origin == "live"
    assert report.metadata.request_attempts >= 1
    assert report.metadata.document_version == hashlib.sha256(source.read_bytes()).hexdigest()
    assert report.verdict.readiness in expected_readiness
    if case in ("clean", "defective", "untrusted"):
        assert report.coverage.execution == ExecutionStatus.COMPLETE
    if case == "untrusted":
        assert report.metadata.request_attempts >= 3
        assert len(report.requirements) >= 1
        assert report.statistics.critical == 0

    md_text = md_path.read_text(encoding="utf-8")
    assert report.metadata.run_id in md_text
    assert report.metadata.model in md_text
    assert report.metadata.document_version in md_text

    scored = evaluate(source, json_path)
    assert scored["case"] == case
    assert scored["readiness"] in {r.value for r in expected_readiness}
    if scored["grounding"]["denominator"] > 0:
        assert scored["grounding"]["value"] == 1.0


def test_live_replays_remain_distinct_from_synthetic_fixture(report: ValidationReport):
    assert report.metadata.origin == "synthetic"
    assert report.metadata.request_attempts == 0
    live = load_json((DEMO / "replay_defective.json").read_bytes())
    assert live.metadata.origin == "live"
    assert live.metadata.request_attempts > 0
    assert live.metadata.document_version != report.metadata.document_version


def test_submission_docs_benchmark_summary_and_no_leaked_secrets():
    script = DEMO / "demo_script.md"
    checklist = DEMO / "submission_checklist.md"
    summary_path = DEMO / "benchmark_summary.json"
    judging = ROOT / "docs" / "judging_matrix.md"
    for path in (script, checklist, summary_path, judging):
        assert path.is_file() and path.read_text(encoding="utf-8").strip()

    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    assert {item["case"] for item in summary["cases"]} == {"defective", "clean", "untrusted"}
    for item in summary["cases"]:
        # Adjudicated human metrics stay explicitly null rather than fabricated.
        assert item["evaluation"]["requirement_recall"] is None
        assert item["evaluation"]["finding_precision"] is None
        assert item["evaluation"]["question_usefulness"] is None

    for artifact in DEMO.glob("*"):
        if artifact.is_file():
            content = artifact.read_text(encoding="utf-8", errors="replace")
            assert not SECRET_PATTERN.search(content), f"Possible credential leaked in {artifact.name}"
