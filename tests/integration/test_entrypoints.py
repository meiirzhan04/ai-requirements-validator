import subprocess
import sys
from inspect import signature
from typing import get_type_hints

from src.clarification import apply_change, propose_change
from src.contracts import Answer, EvidenceCandidate, ProposedChange, ProviderSettings
from src.evidence import ground
from src.ingest import parse
from src.pipeline import run_pipeline
from src.readiness import decide
from src.report import render_markdown


def test_public_entrypoints_accept_the_shared_types(report):
    version = report.document.version
    ref = report.requirements[0].source
    answer = Answer(question_id="Q001", version=version.sha256, option_id="A")
    candidate = EvidenceCandidate(version=version.sha256, block_id=ref.block_id, quote=ref.quote)
    change = ProposedChange(question_id="Q001", source=ref, replacement="A revised passage.")
    calls = [
        (parse, ("sample.md", b"example")), (ground, (candidate, report.document)),
        (decide, (report.coverage, report.findings, [])), (render_markdown, (report,)),
        (run_pipeline, (report.document, ProviderSettings())), (propose_change, (answer, report)),
        (apply_change, (version, change)),
    ]
    for function, args in calls:
        signature(function).bind(*args)
        assert get_type_hints(function).get("return") is not None


def test_cli_missing_input_never_creates_a_report_file(tmp_path):
    source, output = tmp_path / "in.md", tmp_path / "out.md"
    result = subprocess.run([sys.executable, "main.py", "--input", str(source), "--output", str(output)],
                            capture_output=True)
    assert result.returncode != 0 and b"FAIL" in result.stderr
    assert not output.exists()
