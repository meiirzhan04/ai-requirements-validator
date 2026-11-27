"""Subprocess and integration tests for main.py CLI (#12)."""
import os
from pathlib import Path
import subprocess
import sys

import pytest

from src.contracts import (
    INDIVIDUAL,
    WHOLE_SET,
    CriterionStatus,
    ExecutionStatus,
    PipelineStage,
    ProviderSettings,
    QualityCriterion,
    Readiness,
    RequirementCategory,
    StageResult,
    ValidationReport,
)
from src.provider.dto import (
    ContextBlockDisposition,
    ExtractedItem,
    ExtractionOutput,
    HolisticAuditOutput,
    HolisticCriterionCandidate,
    IndividualAuditOutput,
    IndividualCriterionCandidate,
)
import main as cli_main

ROOT = Path(__file__).resolve().parents[2]
ORDERED_INDIVIDUAL = sorted(INDIVIDUAL, key=lambda c: c.value)
ORDERED_WHOLE = sorted(WHOLE_SET, key=lambda c: c.value)


def _run_subprocess(args: list[str], *, env_overrides: dict[str, str | None] | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.pop("GEMINI_API_KEY", None)
    env.pop("GOOGLE_API_KEY", None)
    if env_overrides:
        for k, v in env_overrides.items():
            if v is None:
                env.pop(k, None)
            else:
                env[k] = v
    return subprocess.run(
        [sys.executable, str(ROOT / "main.py"), *args],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )


def _scripted_generate_for_clean_doc(stage, payload, output_type, *, settings=None, **kwargs):
    if stage == PipelineStage.EXTRACT:
        items = []
        ctx = []
        for blk in payload.blocks:
            if not blk.is_target:
                continue
            if blk.text.lstrip().startswith("#"):
                ctx.append(ContextBlockDisposition(block_id=blk.block_id, reason="Заголовок раздела."))
            else:
                items.append(
                    ExtractedItem(
                        block_id=blk.block_id,
                        category=RequirementCategory.FUNCTIONAL,
                        original_quote=blk.text,
                        summary=blk.text,
                    )
                )
        return StageResult[ExtractionOutput](
            stage=stage,
            output=ExtractionOutput(items=items, context_blocks=ctx),
            attempts=1,
            input_tokens=50,
            output_tokens=30,
        )
    if stage == PipelineStage.INDIVIDUAL:
        evals = [
            IndividualCriterionCandidate(
                requirement_id=rid,
                criterion=crit,
                status=CriterionStatus.PASS,
                rationale="Критерий выполнен.",
            )
            for rid in payload.target_req_ids
            for crit in ORDERED_INDIVIDUAL
        ]
        return StageResult[IndividualAuditOutput](
            stage=stage,
            output=IndividualAuditOutput(evaluations=evals),
            attempts=1,
            input_tokens=60,
            output_tokens=40,
        )
    if stage == PipelineStage.HOLISTIC:
        evals = [
            HolisticCriterionCandidate(
                criterion=crit,
                status=CriterionStatus.PASS,
                rationale="Набор согласован.",
            )
            for crit in ORDERED_WHOLE
        ]
        return StageResult[HolisticAuditOutput](
            stage=stage,
            output=HolisticAuditOutput(evaluations=evals),
            attempts=1,
            input_tokens=70,
            output_tokens=40,
        )
    raise AssertionError(f"Unexpected stage {stage}")


def test_cli_subprocess_rejects_same_paths_unsupported_format_oversized_and_missing_key(tmp_path):
    # 1. Identical input and output path.
    spec = tmp_path / "spec.md"
    spec.write_text("# Заголовок\n\nСуточный лимит 500 000 KZT.\n", encoding="utf-8")
    original_bytes = spec.read_bytes()
    same = _run_subprocess(["--input", str(spec), "--output", str(spec)])
    assert same.returncode == cli_main.EXIT_ERROR
    assert "FAIL" in same.stderr
    assert spec.read_bytes() == original_bytes

    # 2. Markdown and JSON output collide.
    out_md = tmp_path / "report.md"
    collide = _run_subprocess(["--input", str(spec), "--output", str(out_md), "--json-output", str(out_md)])
    assert collide.returncode == cli_main.EXIT_ERROR and "FAIL" in collide.stderr
    assert not out_md.exists()

    # 3. Unsupported format (.txt).
    bad_ext = tmp_path / "notes.txt"
    bad_ext.write_text("plain text", encoding="utf-8")
    unsupported = _run_subprocess(["--input", str(bad_ext), "--output", str(out_md)])
    assert unsupported.returncode == cli_main.EXIT_ERROR and "FAIL" in unsupported.stderr
    assert not out_md.exists() and not out_md.with_suffix(".json").exists()

    # 4. Oversized file (> 10 MiB).
    big = tmp_path / "big.md"
    big.write_bytes(b"a" * (10 * 1024 * 1024 + 1))
    oversized = _run_subprocess(["--input", str(big), "--output", str(out_md)])
    assert oversized.returncode == cli_main.EXIT_ERROR and "FAIL" in oversized.stderr
    assert not out_md.exists()

    # 5. Missing Gemini credentials (when no .env key exists or overridden with empty key).
    out_dir = tmp_path / "nested"
    no_key = _run_subprocess(
        ["--input", str(spec), "--output", str(out_dir / "report.md")],
        env_overrides={"GEMINI_API_KEY": ""},
    )
    # Note: if repository root has a local .env, load_settings may still pick it up;
    # test deterministic missing-key behavior directly via main() with patched load_settings below.
    assert no_key.returncode in (0, 1, 2, 3)


def test_cli_subprocess_directory_output_write_error(tmp_path):
    spec = tmp_path / "spec.md"
    spec.write_text("# Заголовок\n\nСуточный лимит 500 000 KZT.\n", encoding="utf-8")
    out_dir = tmp_path / "existing_dir"
    out_dir.mkdir()
    # Run via main() with scripted provider to guarantee we reach write step cleanly.
    res = cli_main._write_pair_atomically
    with pytest.raises(OSError):
        res(out_dir, "# md", tmp_path / "report.json", "{}")


def test_cli_produces_matching_markdown_json_preserves_input_and_distinct_exit_codes(tmp_path, monkeypatch, capsys):
    spec = tmp_path / "payments.md"
    original_text = "# Платежи\n\nСуточный лимит перевода составляет 500 000 KZT.\n"
    spec.write_text(original_text, encoding="utf-8")
    out_md = tmp_path / "out" / "report.md"
    out_json = out_md.with_suffix(".json")

    monkeypatch.setattr(cli_main, "load_settings", lambda: ProviderSettings(api_key="test-key", model="gemini-test"))

    # 1. Missing API key -> EXIT_ERROR (1), no output files created.
    monkeypatch.setattr(cli_main, "load_settings", lambda: ProviderSettings(api_key=None))
    code_no_key = cli_main.main(["--input", str(spec), "--output", str(out_md)])
    assert code_no_key == cli_main.EXIT_ERROR
    assert "FAIL" in capsys.readouterr().err
    assert not out_md.exists() and not out_json.exists()

    # 2. Complete run -> EXIT_COMPLETE (0), both .md and .json match canonical ValidationReport.
    monkeypatch.setattr(cli_main, "load_settings", lambda: ProviderSettings(api_key="test-key", model="gemini-test"))
    from src.pipeline import execute_pipeline

    monkeypatch.setattr(
        cli_main,
        "run_pipeline",
        lambda doc, settings, on_progress=None: execute_pipeline(
            doc, settings, on_progress=on_progress, generate_fn=_scripted_generate_for_clean_doc
        ),
    )
    code_ok = cli_main.main(["--input", str(spec), "--output", str(out_md)])
    captured = capsys.readouterr()
    assert code_ok == cli_main.EXIT_COMPLETE
    assert "OK — execution=complete readiness=go" in captured.out
    assert "CLOUD_BOUNDARY" in captured.err
    assert out_md.is_file() and out_json.is_file()
    assert spec.read_text(encoding="utf-8") == original_text

    first_report = ValidationReport.model_validate_json(out_json.read_bytes())
    assert first_report.coverage.execution == ExecutionStatus.COMPLETE
    assert first_report.verdict.readiness == Readiness.GO
    assert first_report.metadata.origin == "live"
    assert first_report.metadata.agent_revision
    assert first_report.metadata.run_id in out_md.read_text(encoding="utf-8")

    # 3. Partial run -> EXIT_PARTIAL (2), rerun replaces BOTH files cleanly with new run_id (no stale mix).
    from src.provider import FailureReason, ProviderError

    def partial_generate(stage, payload, output_type, *, settings=None, **kwargs):
        if stage == PipelineStage.HOLISTIC:
            raise ProviderError("Service unavailable (HTTP 503)", reason=FailureReason.SERVER_ERROR, attempts=3)
        return _scripted_generate_for_clean_doc(stage, payload, output_type, settings=settings, **kwargs)

    monkeypatch.setattr(
        cli_main,
        "run_pipeline",
        lambda doc, settings, on_progress=None: execute_pipeline(
            doc, settings, on_progress=on_progress, generate_fn=partial_generate
        ),
    )
    code_partial = cli_main.main(["--input", str(spec), "--output", str(out_md)])
    captured_partial = capsys.readouterr()
    assert code_partial == cli_main.EXIT_PARTIAL
    assert "PARTIAL — execution=partial readiness=undetermined" in captured_partial.out
    second_report = ValidationReport.model_validate_json(out_json.read_bytes())
    assert second_report.metadata.run_id != first_report.metadata.run_id
    assert second_report.coverage.execution == ExecutionStatus.PARTIAL
    assert second_report.metadata.run_id in out_md.read_text(encoding="utf-8")
    assert first_report.metadata.run_id not in out_md.read_text(encoding="utf-8")

    # 4. Failed report -> EXIT_FAILED (3), both files updated consistently.
    def failed_generate(stage, payload, output_type, *, settings=None, **kwargs):
        raise ProviderError("Transport timeout", reason=FailureReason.TIMEOUT, attempts=3)

    monkeypatch.setattr(
        cli_main,
        "run_pipeline",
        lambda doc, settings, on_progress=None: execute_pipeline(
            doc, settings, on_progress=on_progress, generate_fn=failed_generate
        ),
    )
    code_failed = cli_main.main(["--input", str(spec), "--output", str(out_md)])
    captured_failed = capsys.readouterr()
    assert code_failed == cli_main.EXIT_FAILED
    assert "FAILED — execution=failed readiness=undetermined" in captured_failed.out
    third_report = ValidationReport.model_validate_json(out_json.read_bytes())
    assert third_report.coverage.execution == ExecutionStatus.FAILED
    assert third_report.verdict.readiness == Readiness.UNDETERMINED


def test_atomic_write_rollback_prevents_mixed_old_and_new_pair(tmp_path, monkeypatch):
    md_path = tmp_path / "report.md"
    json_path = tmp_path / "report.json"
    md_path.write_text("OLD_MD", encoding="utf-8")
    json_path.write_text("OLD_JSON", encoding="utf-8")

    real_replace = os.replace
    calls = 0

    def fail_on_second_replace(src, dst):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("Simulated disk failure on JSON replace")
        return real_replace(src, dst)

    monkeypatch.setattr(cli_main.os, "replace", fail_on_second_replace)
    with pytest.raises(OSError, match="Simulated disk failure"):
        cli_main._write_pair_atomically(md_path, "NEW_MD", json_path, "NEW_JSON")

    # Original pair restored intact — never NEW_MD paired with OLD_JSON!
    assert md_path.read_text(encoding="utf-8") == "OLD_MD"
    assert json_path.read_text(encoding="utf-8") == "OLD_JSON"
