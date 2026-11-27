import html
import json
import socket

import pytest
from pydantic import ValidationError

from src.report import load_json, render_json, render_markdown


def test_fixture_exports_one_source_snapshot(report, monkeypatch):
    def block_network(*args):
        pytest.fail("Rendering opened a socket")

    monkeypatch.setattr(socket, "socket", block_network)
    restored = load_json(render_json(report))
    markdown = html.unescape(render_markdown(restored))
    assert restored == report
    for section in ("Паспорт", "Сводка", "Реестр требований", "Индивидуальный аудит",
                    "Аудит документа в целом", "Критические дефекты", "Покрытие",
                    "Вопросы для уточнения", "Первоочередные действия", "Исходные цитаты"):
        assert f"## {section}" in markdown
    for field in ("model", "agent_revision", "document_version"):
        assert getattr(report.metadata, field) in markdown
    assert "Синтетический пример" in markdown
    assert f"| Подтверждённые нарушения | {restored.statistics.violations} |" in markdown
    assert "| Критические | 1 |" in markdown
    for index, req in enumerate(restored.requirements, 1):
        ref = req.source
        assert f"E{index:03}" in markdown and ref.quote in markdown
        block = next(b for b in restored.document.version.blocks if b.block_id == ref.block_id)
        assert f"[{ref.start}, {ref.end})" in markdown
        assert block.text[ref.start:ref.end] == ref.quote
    for e in restored.evaluations:
        assert e.criterion in markdown


def test_partial_escapes_content_and_preserves_outstanding_work(report_data):
    report_data["requirements"][0].update(original_id=None, summary="Цена | [a](b)\n<img> **сумма**")
    report_data["coverage"].update(execution="partial", incomplete_reasons=["Сбой пакета"])
    report_data["coverage"]["audit_batches"]["planned"].append("batch-2")
    report_data.update(findings=[], questions=[], evaluations=[
        dict(e, finding_ids=[], status="unknown") for e in report_data["evaluations"]
    ], statistics=None, verdict=dict(readiness="undetermined", rationale="Аудит неполон", next_actions=[]))
    report = load_json(json.dumps(report_data))
    markdown = render_markdown(report)
    assert "| R001 | — |" in markdown
    assert "| Подтверждённые нарушения | 0 |" in markdown
    assert "Частичный" in markdown and "Сбой пакета" in markdown and "batch-2" in markdown
    assert "<img>" not in markdown and "[a](b)" not in markdown
    assert "Цена &#124;" in markdown and "<br>&lt;img&gt;" in markdown
    assert load_json(render_json(report)) == report


def test_invalid_saved_report_rejected(report_data):
    report_data["statistics"]["violations"] += 1
    with pytest.raises(ValidationError):
        load_json(json.dumps(report_data))
