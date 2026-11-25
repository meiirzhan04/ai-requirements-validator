import json
from pathlib import Path

import pytest

from src.contracts import ValidationReport


@pytest.fixture
def report_data():
    path = Path(__file__).parent / "fixtures" / "sample_report.json"
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture
def report(report_data):
    return ValidationReport.model_validate(report_data)
