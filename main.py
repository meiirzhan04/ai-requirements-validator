import argparse
import sys
from pathlib import Path

from pydantic import ValidationError

from src.ingest import parse
from src.pipeline import run_pipeline
from src.provider import ProviderError, load_settings
from src.report import render_markdown


def main() -> int:
    parser = argparse.ArgumentParser(description="Static requirements validation (phase 1 foundation)")
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.input.resolve() == args.output.resolve():
            raise ValueError("Input and output must be different files")
        if args.input.stat().st_size > 10 * 1024 * 1024:
            raise ValueError("Input exceeds 10 MiB")
        document = parse(args.input.name, args.input.read_bytes())
        report = run_pipeline(document, load_settings())
        markdown = render_markdown(report)
        args.output.write_text(markdown, encoding="utf-8")
    except ValidationError:
        print("FAIL — invalid configuration or report contract", file=sys.stderr)
        return 1
    except (OSError, ValueError, ProviderError, NotImplementedError) as exc:
        print(f"FAIL — {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
