"""Score a validated real report against a frozen case and optional adjudication."""
import argparse
import hashlib
import json
from pathlib import Path

import pymupdf
from pydantic import BaseModel, ConfigDict, StrictBool

from src.contracts import ValidationReport
from src.ingest import parse
from .prepare import OFFICIAL_SHA, ROOT


class Review(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: str
    document_sha256: str
    # Every predicted item is explicitly matched or excluded by an empty list/null.
    requirements: dict[str, list[str]]
    findings: dict[str, str | None]
    false_alarms: dict[str, list[str]]  # Valid scenario -> confirmed finding IDs.
    # Reviewed question -> decision, owner, material trade-off, testable edit.
    questions: dict[str, tuple[StrictBool, StrictBool, StrictBool, StrictBool]]
    reviewers: list[str]


def fraction(numerator, denominator):
    return {"numerator": numerator, "denominator": denominator,
            "value": numerator / denominator if denominator else None}


def score(report, truth, review=None):
    meta = report.metadata
    confirmed = {f.finding_id: f for f in report.findings if f.confirmed}
    blocks = {b.block_id: b.text for b in report.document.version.blocks}
    references = [ref for f in confirmed.values()
                  for ref in [*f.evidence, *([f.gap.context] if f.gap else [])]]
    correct = sum(ref.version == meta.document_version and ref.grounded
                  and blocks[ref.block_id][ref.start:ref.end] == ref.quote for ref in references)
    coverage = report.coverage
    results = {
        "run": meta.model_dump(mode="json"), "execution": coverage.execution,
        "readiness": report.verdict.readiness, "input_documents": 1,
        "grounding": fraction(correct, len(references)),
        "coverage": {key: fraction(len(work.completed), len(work.planned))
                     for key in ("extraction_blocks", "individual_requirements", "audit_batches",
                                 "cross_section_groups") if (work := getattr(coverage, key))},
        "incomplete_reasons": coverage.incomplete_reasons,
        "requirement_recall": None, "finding_precision": None, "finding_recall": None,
        "critical_recall": None, "false_positive_rate": None, "question_usefulness": None,
        "reviewers": [],
    }
    if review is None:
        return results
    if (review.run_id, review.document_sha256) != (meta.run_id, meta.document_version):
        raise ValueError("Review belongs to another run or document version")
    if (review.requirements.keys() != {r.req_id for r in report.requirements}
            or review.findings.keys() != confirmed.keys()
            or review.false_alarms.keys() != truth["valid"].keys()
            or review.questions.keys() != {q.question_id for q in report.questions}):
        raise ValueError("Review must account for every prediction and valid scenario")
    extracted = {key for matches in review.requirements.values() for key in matches}
    matched = {key for key in review.findings.values() if key is not None}
    if not extracted <= truth["requirements"].keys() or not matched <= truth["defects"].keys():
        raise ValueError("Unknown annotation in review")
    for predictions in review.false_alarms.values():
        if any(fid not in confirmed or review.findings[fid] is not None for fid in predictions):
            raise ValueError("False alarm must be an unmatched confirmed finding")
    critical = {key for key, (severity, _) in truth["defects"].items() if severity == "critical"}
    reviewers = {name.strip().casefold() for name in review.reviewers if name.strip()}
    results.update(
        requirement_recall=fraction(len(extracted), len(truth["requirements"])),
        finding_precision=fraction(len(matched), len(confirmed)),
        finding_recall=fraction(len(matched), len(truth["defects"])),
        critical_recall=fraction(len(critical & matched), len(critical)),
        missed_critical=sorted(critical - matched),
        false_positive_rate=fraction(sum(bool(ids) for ids in review.false_alarms.values()),
                                     len(truth["valid"])),
        question_usefulness=(fraction(sum(all(v) for v in review.questions.values()),
                                     len(review.questions)) if len(reviewers) >= 2 else None),
        reviewers=review.reviewers,
    )
    return results


def evaluate(source, report_file, review_file=None):
    report = ValidationReport.model_validate_json(report_file.read_bytes())
    data = source.read_bytes()
    case = source.stem
    if case == "requirements":
        case = "official"
    truth = json.loads((ROOT / "truth.json").read_text(encoding="utf-8"))
    if case not in truth or report.metadata.origin != "live":
        raise ValueError("Expected a known case and a real analysis")
    if report.metadata.document_version != hashlib.sha256(data).hexdigest():
        raise ValueError("Input digest differs from report")
    if case == "official" and source.suffix.lower() == ".md":
        if report.metadata.document_version != OFFICIAL_SHA:
            raise ValueError("Unexpected organizer document version")
    elif case == "official":
        with pymupdf.open(stream=data, filetype="pdf") as pdf:
            text = "".join(page.get_text() for page in pdf).replace("ﬁ", "fi")
        digest = hashlib.sha256("".join(text.split()).encode()).hexdigest()
        if digest != "fd436585222b49b112dbed66f3d4400c7747f81acb7e81e6ef95c7ae5ed65f74":
            raise ValueError("PDF text differs from the official Markdown")
    elif data.splitlines() != (ROOT / f"{case}.md").read_bytes().splitlines():
        raise ValueError("Input differs from annotated case")
    if parse(report.document.version.name, data) != report.document:
        raise ValueError("Recorded source blocks differ from original input")
    review = Review.model_validate_json(review_file.read_bytes()) if review_file else None
    return {"case": case, "format": report.document.version.format, **score(report, truth[case], review)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("report", type=Path)
    parser.add_argument("--review", type=Path)
    args = parser.parse_args()
    print(json.dumps(evaluate(args.input, args.report, args.review), ensure_ascii=False, indent=2))
