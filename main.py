"""CLI entrypoint for static pre-development requirements validation."""
import argparse
import os
from pathlib import Path
import sys
import tempfile

from pydantic import ValidationError

from src.contracts import ExecutionStatus, ProgressEvent
from src.ingest import MAX_BYTES, parse
from src.pipeline import run_pipeline
from src.provider import ProviderError, load_settings
from src.report import render_json, render_markdown

EXIT_COMPLETE = 0
EXIT_ERROR = 1
EXIT_PARTIAL = 2
EXIT_FAILED = 3


def _resolve_json_path(md_path: Path, explicit_json: Path | None) -> Path:
    if explicit_json is not None:
        return explicit_json
    if md_path.suffix.lower() == ".json":
        raise ValueError("Markdown output path must not end with .json; pass --json-output explicitly")
    return md_path.with_suffix(".json")


def _write_pair_atomically(md_path: Path, markdown: str, json_path: Path, payload_json: str) -> None:
    """Write Markdown and JSON outputs atomically so reruns never leave a mixed pair."""
    if md_path.exists() and md_path.is_dir():
        raise OSError("Markdown output path is a directory")
    if json_path.exists() and json_path.is_dir():
        raise OSError("JSON output path is a directory")

    md_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.parent.mkdir(parents=True, exist_ok=True)

    md_tmp: Path | None = None
    json_tmp: Path | None = None
    md_backup: bytes | None = md_path.read_bytes() if md_path.is_file() else None
    md_replaced = False
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=md_path.parent,
            prefix=f".{md_path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            md_tmp = Path(handle.name)
            handle.write(markdown)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=json_path.parent,
            prefix=f".{json_path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            json_tmp = Path(handle.name)
            handle.write(payload_json)

        os.replace(md_tmp, md_path)
        md_tmp = None
        md_replaced = True
        os.replace(json_tmp, json_path)
        json_tmp = None
    except OSError:
        if md_replaced:
            try:
                if md_backup is not None:
                    md_path.write_bytes(md_backup)
                elif md_path.exists():
                    md_path.unlink()
            except OSError:
                pass
        for leftover in (md_tmp, json_tmp):
            if leftover is not None and leftover.exists():
                try:
                    leftover.unlink()
                except OSError:
                    pass
        raise


def _print_progress(event: ProgressEvent) -> None:
    print(f"STAGE {event.stage.value} [{event.state}] — {event.message}", file=sys.stderr)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Static pre-development validation of fintech requirements specifications (Markdown / PDF)."
    )
    parser.add_argument(
        "--input",
        type=Path,
        required=True,
        help="Path to the input specification (.md or .pdf, max 10 MiB / 100 pages).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="Destination path for the Russian Markdown report (matching .json is written alongside by default).",
    )
    parser.add_argument(
        "--json-output",
        type=Path,
        default=None,
        help="Optional explicit destination path for the canonical JSON ValidationReport.",
    )
    parser.add_argument(
        "--model",
        type=str,
        default=None,
        help="Optional Gemini model override (defaults to GEMINI_MODEL or gemini-3.5-flash-lite).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        input_path = args.input
        md_path = args.output
        json_path = _resolve_json_path(md_path, args.json_output)

        resolved_in = input_path.resolve()
        resolved_md = md_path.resolve()
        resolved_json = json_path.resolve()
        if resolved_in == resolved_md or resolved_in == resolved_json:
            raise ValueError("Input and output must be different files")
        if resolved_md == resolved_json:
            raise ValueError("Markdown and JSON output paths must be different files")
        if not input_path.is_file():
            raise FileNotFoundError(f"Input file not found: {input_path}")
        if input_path.stat().st_size > MAX_BYTES:
            raise ValueError("Input exceeds 10 MiB")

        raw_bytes = input_path.read_bytes()
        document = parse(input_path.name, raw_bytes)

        settings = load_settings()
        if args.model:
            settings = settings.model_copy(update={"model": args.model.strip()})

        print(
            f"CLOUD_BOUNDARY — sending shareable specification '{document.version.name}' "
            f"(sha256={document.version.sha256[:12]}) to Gemini model={settings.model}",
            file=sys.stderr,
        )
        report = run_pipeline(document, settings, on_progress=_print_progress)
        markdown = render_markdown(report)
        payload_json = render_json(report)
        _write_pair_atomically(md_path, markdown, json_path, payload_json)
    except ValidationError:
        print("FAIL — invalid configuration or report contract", file=sys.stderr)
        return EXIT_ERROR
    except (OSError, ValueError, ProviderError, NotImplementedError) as exc:
        print(f"FAIL — {exc}", file=sys.stderr)
        return EXIT_ERROR

    status = report.coverage.execution
    summary = (
        f"execution={status.value} readiness={report.verdict.readiness.value} "
        f"run_id={report.metadata.run_id} revision={report.metadata.agent_revision} "
        f"markdown={md_path} json={json_path}"
    )
    if status == ExecutionStatus.COMPLETE:
        print(f"OK — {summary}")
        return EXIT_COMPLETE
    if status == ExecutionStatus.PARTIAL:
        print(f"PARTIAL — {summary}")
        return EXIT_PARTIAL
    print(f"FAILED — {summary}")
    return EXIT_FAILED


if __name__ == "__main__":
    raise SystemExit(main())
