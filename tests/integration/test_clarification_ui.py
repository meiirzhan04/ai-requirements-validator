"""Integration tests for evidence visualization and interactive clarification (#14)."""
from copy import deepcopy
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from src import pipeline
from src.clarification import apply_change, propose_change, working_document
from src.contracts import (
    Answer,
    CriterionStatus,
    ExecutionStatus,
    ProgressEvent,
    ProposedChange,
    ProviderSettings,
    Readiness,
    ValidationReport,
)
from src.report import load_json
from ui import dashboard
from ui.clarification import recheck_summary

APP = Path(__file__).resolve().parents[2] / "ui" / "app.py"


def _live_report(report: ValidationReport) -> ValidationReport:
    raw = (
        report.model_dump_json()
        .replace('"origin":"synthetic"', '"origin":"live"')
        .replace('"request_attempts":0', '"request_attempts":2')
    )
    return load_json(raw)


def _build_revalidated_report(
    base: ValidationReport,
    child_doc,
    *,
    resolved: bool = True,
    execution: ExecutionStatus = ExecutionStatus.COMPLETE,
    drop_second_req: bool = False,
    uncertainties: list[str] | None = None,
) -> ValidationReport:
    data = deepcopy(base.model_dump(mode="json"))
    child_sha = child_doc.version.sha256
    data["document"] = child_doc.model_dump(mode="json")
    data["metadata"]["document_version"] = child_sha
    data["metadata"]["origin"] = "live"
    data["metadata"]["request_attempts"] = 3
    data["metadata"]["run_id"] = "live-recheck-1"
    data.pop("statistics", None)

    blocks = child_doc.version.blocks
    reqs = []
    for idx, req in enumerate(data["requirements"]):
        if drop_second_req and idx == 1:
            continue
        blk = blocks[idx]
        req["summary"] = blk.text
        req["source"] = {
            "version": child_sha,
            "block_id": blk.block_id,
            "start": 0,
            "end": len(blk.text),
            "quote": blk.text,
            "grounding": "exact",
        }
        reqs.append(req)
    data["requirements"] = reqs
    block_ids = [b.block_id for b in blocks]
    req_ids = [r["req_id"] for r in reqs]
    data["coverage"]["extraction_blocks"] = {"planned": block_ids, "completed": block_ids}
    data["coverage"]["individual_requirements"] = {"planned": req_ids, "completed": req_ids}
    data["coverage"]["context_blocks"] = {
        b.block_id: "Неиспользованный блок." for b in blocks[len(reqs):]
    }
    data["coverage"]["execution"] = execution.value
    data["coverage"]["incomplete_reasons"] = [] if execution == ExecutionStatus.COMPLETE else ["Сбой при повторной проверке."]
    data["coverage"]["uncertainties"] = list(uncertainties or [])

    if resolved:
        data["findings"] = []
        data["questions"] = []
        new_evals = []
        for ev in data["evaluations"]:
            if ev["requirement_id"] is not None and ev["requirement_id"] not in req_ids:
                continue
            if ev["status"] == "fail":
                ev["status"] = "pass"
                ev["rationale"] = "После правки противоречие устранено."
                ev["finding_ids"] = []
            new_evals.append(ev)
        data["evaluations"] = new_evals
        readiness = Readiness.GO if (execution == ExecutionStatus.COMPLETE and not uncertainties) else Readiness.UNDETERMINED
        data["verdict"] = {
            "readiness": readiness.value,
            "rationale": "Повторный анализ подтвердил согласованность требований." if readiness == Readiness.GO else "Остались ограничения проверки.",
            "next_actions": ["Передать требования в разработку."] if readiness == Readiness.GO else ["Допроверить ограничения."],
        }
    else:
        for finding in data["findings"]:
            for idx, ref in enumerate(finding["evidence"]):
                blk = blocks[idx]
                ref["version"] = child_sha
                ref["block_id"] = blk.block_id
                ref["start"] = 0
                ref["end"] = len(blk.text)
                ref["quote"] = blk.text
        for question in data["questions"]:
            for idx, ref in enumerate(question["evidence"]):
                blk = blocks[idx]
                ref["version"] = child_sha
                ref["block_id"] = blk.block_id
                ref["start"] = 0
                ref["end"] = len(blk.text)
                ref["quote"] = blk.text
        data["verdict"] = {
            "readiness": Readiness.NO_GO.value,
            "rationale": "Противоречие сохранилось после правки.",
            "next_actions": ["Согласовать оба раздела повторно."],
        }

    return ValidationReport.model_validate(data)


def test_clarification_ui_shows_both_quotes_applies_option_and_revalidates_resolved(report, monkeypatch):
    live = _live_report(report)
    original_json = live.model_dump_json()
    rerun_calls = []

    def fake_run_pipeline(doc, settings, on_progress=None):
        rerun_calls.append((doc, settings))
        if on_progress:
            on_progress(ProgressEvent(stage="extract", state="completed", message="ok"))
        return _build_revalidated_report(live, doc, resolved=True)

    monkeypatch.setattr(dashboard, "load_settings", lambda: ProviderSettings(api_key="test-key"))
    monkeypatch.setattr(pipeline, "run_pipeline", fake_run_pipeline)

    app = AppTest.from_file(str(APP), default_timeout=15).run()
    app.radio[0].set_value("Сохранённый отчёт").run()
    app.file_uploader[0].set_value(("live.json", original_json.encode(), "application/json")).run()
    assert not app.exception

    # Both independent contradiction quotes are displayed side-by-side.
    code_texts = [c.value for c in app.code]
    assert any("получает письмо" in t for t in code_texts)
    assert any("не получает письмо" in t for t in code_texts)
    # Consequence of the selected option is shown before applying.
    assert any("Сохранить функциональное правило" in t.value for t in app.text)

    # Edit the proposed replacement into a full harmonized wording and apply to working copy.
    edit_area = next(ta for ta in app.text_area if ta.label == "После изменения")
    edit_area.set_value("При отмене заявки на экскурсию посетитель не получает письмо и видит статус отмены в личном кабинете.").run()
    apply_btn = next(b for b in app.button if b.label == "Применить к рабочей копии")
    apply_btn.click().run()
    assert not app.exception
    # Applying does NOT call Gemini yet and preserves the original report intact.
    assert len(rerun_calls) == 0
    assert live.model_dump_json() == original_json

    # Explicit cloud consent is required before running full revalidation on the child working copy.
    consent_box = next(cb for cb in app.checkbox if "исправленный документ" in cb.label)
    rerun_btn = next(b for b in app.button if b.label == "Повторить полный анализ")
    assert rerun_btn.disabled
    consent_box.check().run()
    rerun_btn = next(b for b in app.button if b.label == "Повторить полный анализ")
    rerun_btn.click().run()
    assert not app.exception
    assert len(rerun_calls) == 1
    child_doc = rerun_calls[0][0]
    assert child_doc.version.parent_sha256 == live.document.version.sha256
    assert any("Исходное нарушение не повторилось" in t.value for t in app.text)
    assert any("Готовность до исправления" in t.value and "Готово" in t.value for t in app.text)


def test_clarification_ui_custom_answer_and_remaining_defect_preserved(report, monkeypatch):
    live = _live_report(report)
    rerun_calls = []

    def fake_run_pipeline(doc, settings, on_progress=None):
        rerun_calls.append(doc)
        return _build_revalidated_report(live, doc, resolved=False)

    monkeypatch.setattr(dashboard, "load_settings", lambda: ProviderSettings(api_key="test-key"))
    monkeypatch.setattr(pipeline, "run_pipeline", fake_run_pipeline)

    app = AppTest.from_file(str(APP), default_timeout=15).run()
    app.radio[0].set_value("Сохранённый отчёт").run()
    app.file_uploader[0].set_value(("live.json", live.model_dump_json().encode(), "application/json")).run()

    # Switch to custom free-text stakeholder answer.
    answer_radio = next(r for r in app.radio if r.label == "Ответ владельца")
    answer_radio.set_value("__custom__").run()
    custom_box = next(ta for ta in app.text_area if ta.label == "Свой ответ")
    custom_box.set_value("Отправлять уведомление только по SMS при отмене заявки.").run()
    assert not app.exception

    apply_btn = next(b for b in app.button if b.label == "Применить к рабочей копии")
    apply_btn.click().run()
    consent_box = next(cb for cb in app.checkbox if "исправленный документ" in cb.label)
    consent_box.check().run()
    rerun_btn = next(b for b in app.button if b.label == "Повторить полный анализ")
    rerun_btn.click().run()
    assert not app.exception
    assert len(rerun_calls) == 1
    assert any("сохранились нарушения" in t.value for t in app.text)


def test_synthetic_report_blocks_live_revalidation_and_recheck_summary_edge_cases(report, monkeypatch):
    monkeypatch.setattr(pipeline, "run_pipeline", lambda *a, **k: pytest.fail("Synthetic report must not run Gemini"))
    app = AppTest.from_file(str(APP), default_timeout=15).run()
    app.radio[0].set_value("Сохранённый отчёт").run()
    app.file_uploader[0].set_value(("synthetic.json", report.model_dump_json().encode(), "application/json")).run()
    apply_btn = next(b for b in app.button if b.label == "Применить к рабочей копии")
    apply_btn.click().run()
    assert not app.exception
    assert any("Локальный синтетический пример" in info.value for info in app.info)

    # Verify recheck_summary edge cases: partial run, lost requirement mapping, remaining uncertainty.
    live = _live_report(report)
    q = live.questions[0]
    ans = Answer(question_id=q.question_id, version=live.document.version.sha256, custom_text="Обновлённое правило уведомления.")
    change = propose_change(ans, live)
    child = working_document(live.document, change)

    partial_res = _build_revalidated_report(live, child, resolved=True, execution=ExecutionStatus.PARTIAL)
    assert "неполная" in recheck_summary(live, q, change, partial_res)

    lost_res = _build_revalidated_report(live, child, resolved=True, drop_second_req=True)
    assert "Не удалось однозначно сопоставить" in recheck_summary(live, q, change, lost_res)

    uncertain_res = _build_revalidated_report(live, child, resolved=True, uncertainties=["Не согласован канал рассылки."])
    assert "неопределённость" in recheck_summary(live, q, change, uncertain_res)
