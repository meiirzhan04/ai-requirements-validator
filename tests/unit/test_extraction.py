import pytest

from src.contracts import (
    DocumentFormat,
    DocumentVersion,
    EvidenceCandidate,
    EvidenceRef,
    GroundingResult,
    GroundingStatus,
    ParsedDocument,
    PipelineStage,
    ProviderSettings,
    RequirementCategory,
    SourceBlock,
    SourceLocation,
)
from src.pipeline.extraction import extract_requirements, plan_extraction_batches
from src.provider import FailureAction, FailureReason, ProviderError
from src.provider.dto import (
    ContextBlockDisposition,
    ExtractedItem,
    ExtractionBatchPayload,
    ExtractionOutput,
)

SHA = "a" * 64


def _doc(blocks: list[tuple[str, str, str]]) -> ParsedDocument:
    source_blocks = [
        SourceBlock(
            block_id=bid,
            text=text,
            location=SourceLocation(section=section, line_start=idx, line_end=idx),
        )
        for idx, (bid, section, text) in enumerate(blocks, start=1)
    ]
    return ParsedDocument(
        version=DocumentVersion(
            sha256=SHA,
            parser_version="test-v1",
            name="synthetic-spec.md",
            format=DocumentFormat.MARKDOWN,
            line_count=max(1, len(source_blocks)),
            blocks=source_blocks,
        )
    )


def _exact_ground(candidate: EvidenceCandidate, document: ParsedDocument) -> GroundingResult:
    block_map = {b.block_id: b.text for b in document.version.blocks}
    text = block_map.get(candidate.block_id)
    if text is None:
        return GroundingResult(status=GroundingStatus.UNVERIFIED, reason="Missing block")
    pos = text.find(candidate.quote)
    if pos == -1:
        return GroundingResult(status=GroundingStatus.FUZZY, reason="Quote not found verbatim")
    return GroundingResult(
        status=GroundingStatus.EXACT,
        reference=EvidenceRef(
            version=document.version.sha256,
            block_id=candidate.block_id,
            start=pos,
            end=pos + len(candidate.quote),
            quote=candidate.quote,
            grounding=GroundingStatus.EXACT,
        ),
        reason="Exact substring match",
    )


def _settings() -> ProviderSettings:
    return ProviderSettings(api_key="unit-test-key")


def test_table_repeated_original_ids_and_stable_source_order():
    table_text = (
        "| ID | Операция | Порог |\n"
        "| --- | --- | --- |\n"
        "| REQ-1 | Перевод P2P | Не более 500 000 KZT |\n"
        "| REQ-1 | Оплата QR | Не более 200 000 KZT |"
    )
    document = _doc(
        [
            ("B001", "Введение", "Документ описывает платёжный модуль для мобильного приложения."),
            ("B002", "Лимиты", table_text),
            ("B003", "Приёмка", "При превышении лимита операция отклоняется с кодом 422."),
        ]
    )

    def fake_generate(stage, payload: ExtractionBatchPayload, output_type, *, settings=None):
        assert stage == PipelineStage.EXTRACT
        # Deliberately return items out of source order to verify Python ordering and ID assignment.
        return StageResult_ok(
            ExtractionOutput(
                items=[
                    ExtractedItem(
                        block_id="B003",
                        original_id=None,
                        category=RequirementCategory.ACCEPTANCE,
                        original_quote="При превышении лимита операция отклоняется с кодом 422.",
                        summary="Отклонять операцию с кодом 422 при превышении лимита.",
                    ),
                    ExtractedItem(
                        block_id="B002",
                        original_id="REQ-1",
                        category=RequirementCategory.FUNCTIONAL,
                        original_quote="| REQ-1 | Оплата QR | Не более 200 000 KZT |",
                        summary="Лимит операции Оплата QR не превышает 200 000 KZT.",
                    ),
                    ExtractedItem(
                        block_id="B002",
                        original_id="REQ-1",
                        category=RequirementCategory.FUNCTIONAL,
                        original_quote="| REQ-1 | Перевод P2P | Не более 500 000 KZT |",
                        summary="Лимит операции Перевод P2P не превышает 500 000 KZT.",
                    ),
                ],
                context_blocks=[
                    ContextBlockDisposition(
                        block_id="B001",
                        reason="Общее описание назначения документа без нормативных правил.",
                    )
                ],
            )
        )

    result = extract_requirements(
        document,
        _settings(),
        generate_fn=fake_generate,
        ground_fn=_exact_ground,
    )

    assert [r.req_id for r in result.requirements] == ["R001", "R002", "R003"]
    assert [r.original_id for r in result.requirements] == ["REQ-1", "REQ-1", None]
    assert [r.source.block_id for r in result.requirements] == ["B002", "B002", "B003"]
    assert result.requirements[0].source.start < result.requirements[1].source.start
    assert result.extraction_blocks.complete
    assert result.context_blocks == {
        "B001": "Общее описание назначения документа без нормативных правил."
    }
    assert result.incomplete_reasons == []


def test_truncation_splits_table_and_repeats_header_without_losing_rows():
    row1 = "| A1 | Подтверждение SMS | Код действует 60 секунд |"
    row2 = "| A2 | Блокировка ввода | После 3 ошибок на 15 минут |"
    row3 = "| A3 | Повторная отправка | Не чаще 1 раза в 30 секунд |"
    row4 = "| A4 | Журналирование | Хранить событие 5 лет |"
    table = "| ID | Сценарий | Условие |\n| --- | --- | --- |\n" + "\n".join([row1, row2, row3, row4])
    document = _doc([("B001", "Приёмка OTP", table)])

    seen_headers: list[str | None] = []
    calls = 0

    def fake_generate(stage, payload: ExtractionBatchPayload, output_type, *, settings=None):
        nonlocal calls
        calls += 1
        target_block = next(b for b in payload.blocks if b.is_target)
        seen_headers.append(target_block.table_header)
        if calls == 1:
            raise ProviderError(
                "Truncated structured response (MAX_TOKENS)",
                reason=FailureReason.TRUNCATED,
                action=FailureAction.SPLIT,
                attempts=1,
            )
        items = []
        for row in (row1, row2, row3, row4):
            if row in target_block.text:
                row_id = row.split("|")[1].strip()
                items.append(
                    ExtractedItem(
                        block_id="B001",
                        original_id=row_id,
                        category=RequirementCategory.ACCEPTANCE,
                        original_quote=row,
                        summary=f"Правило таблицы {row_id}.",
                    )
                )
        return StageResult_ok(ExtractionOutput(items=items))

    result = extract_requirements(
        document,
        _settings(),
        generate_fn=fake_generate,
        ground_fn=_exact_ground,
    )

    assert calls == 3
    assert len(result.requirements) == 4
    assert [r.original_id for r in result.requirements] == ["A1", "A2", "A3", "A4"]
    assert all(h == "| ID | Сценарий | Условие |\n| --- | --- | --- |" for h in seen_headers)
    assert result.extraction_blocks.complete
    assert result.attempts == 3


def test_boundary_context_and_overlap_deduplication_preserve_all_requirements():
    b1 = "Раздел правил комиссии: для переводов свыше 100 000 KZT взимается комиссия 1%."
    b2 = "Для переводов до 100 000 KZT комиссия составляет 0% при условии активного тарифа."
    b3 = "Возврат комиссии выполняется в течение 3 рабочих дней после отмены платежа."
    document = _doc(
        [
            ("B001", "Комиссии", b1),
            ("B002", "Комиссии", b2),
            ("B003", "Комиссии", b3),
        ]
    )

    batches = plan_extraction_batches(document, max_input_tokens=450, max_output_tokens=200)
    assert len(batches) >= 2

    seen_payloads: list[ExtractionBatchPayload] = []

    def fake_generate(stage, payload: ExtractionBatchPayload, output_type, *, settings=None):
        seen_payloads.append(payload)
        items = []
        for blk in payload.blocks:
            if blk.is_target:
                items.append(
                    ExtractedItem(
                        block_id=blk.block_id,
                        original_quote=blk.text,
                        summary=f"Требование из {blk.block_id}.",
                    )
                )
        return StageResult_ok(ExtractionOutput(items=items))

    result = extract_requirements(
        document,
        _settings(),
        generate_fn=fake_generate,
        ground_fn=_exact_ground,
        max_input_tokens=450,
        max_output_tokens=200,
    )

    # Every non-first batch receives the preceding boundary block as read-only context.
    assert any(
        any(not b.is_target for b in p.blocks) for p in seen_payloads[1:]
    )
    assert [r.req_id for r in result.requirements] == ["R001", "R002", "R003"]
    assert [r.source.block_id for r in result.requirements] == ["B001", "B002", "B003"]
    assert result.extraction_blocks.complete


def test_omitted_block_fuzzy_quote_and_single_malformed_repair():
    document = _doc(
        [
            ("B001", "Авторизация", "Сессия пользователя завершается через 15 минут бездействия."),
            ("B002", "Уведомления", "Push-уведомление отправляется не позднее 5 секунд."),
            ("B003", "Аудит", "Все входы фиксируются с IP-адресом и временем UTC."),
        ]
    )
    calls = 0

    def fake_generate(stage, payload: ExtractionBatchPayload, output_type, *, settings=None):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ProviderError(
                "Malformed structured response",
                reason=FailureReason.MALFORMED_RESPONSE,
                action=FailureAction.REPAIR,
                attempts=1,
            )
        assert payload.repair_hint is not None
        return StageResult_ok(
            ExtractionOutput(
                items=[
                    ExtractedItem(
                        block_id="B001",
                        original_quote="Сессия пользователя завершается через 15 минут бездействия.",
                        summary="Таймаут неактивной сессии 15 минут.",
                    ),
                    # Ungrounded/paraphrased quote for B002 -> must NEVER be accepted as confirmed!
                    ExtractedItem(
                        block_id="B002",
                        original_quote="Push отправляется за 5 сек.",
                        summary="Отправка push-уведомления.",
                    ),
                    # B003 intentionally omitted by model -> must remain outstanding!
                ]
            )
        )

    result = extract_requirements(
        document,
        _settings(),
        generate_fn=fake_generate,
        ground_fn=_exact_ground,
    )

    assert calls == 2
    assert [r.req_id for r in result.requirements] == ["R001"]
    assert result.requirements[0].source.block_id == "B001"
    assert result.extraction_blocks.completed == ["B001"]
    assert not result.extraction_blocks.complete
    assert any("B002" in reason and "не подтверждена" in reason for reason in result.incomplete_reasons)
    assert any("B003" in reason and "пропущен" in reason for reason in result.incomplete_reasons)


def test_partially_invalid_block_preserves_completed_block_coverage_invariant():
    b1 = "Система поддерживает вход по биометрии."
    b2 = "Лимит операции 500 000 KZT. Комиссия рассчитывается автоматически."
    document = _doc([("B001", "Вход", b1), ("B002", "Лимиты", b2)])

    def fake_generate(stage, payload: ExtractionBatchPayload, output_type, *, settings=None):
        return StageResult_ok(
            ExtractionOutput(
                items=[
                    ExtractedItem(
                        block_id="B001",
                        original_quote=b1,
                        summary="Поддержка биометрии.",
                    ),
                    # B002 has one valid quote AND one ungrounded quote -> B002 must stay incomplete
                    # and must not leave dangling requirements outside completed_blocks!
                    ExtractedItem(
                        block_id="B002",
                        original_quote="Лимит операции 500 000 KZT.",
                        summary="Лимит операции 500 000 KZT.",
                    ),
                    ExtractedItem(
                        block_id="B002",
                        original_quote="Несуществующая цитата о комиссии",
                        summary="Комиссия.",
                    ),
                ]
            )
        )

    result = extract_requirements(
        document,
        _settings(),
        generate_fn=fake_generate,
        ground_fn=_exact_ground,
    )

    cited = {r.source.block_id for r in result.requirements}
    completed = set(result.extraction_blocks.completed)
    context = set(result.context_blocks)
    assert completed == {"B001"}
    assert cited | context == completed
    assert not (cited & context)
    assert any("B002" in r for r in result.incomplete_reasons)


def test_permanent_provider_stop_preserves_prior_batches_and_marks_rest_incomplete():
    document = _doc(
        [
            ("B001", "Раздел 1", "Правило один: обязательная проверка подписи транзакции перед отправкой."),
            ("B002", "Раздел 2", "Правило два: при ошибке валидации возвращать HTTP 400 с кодом поля."),
            ("B003", "Раздел 3", "Правило три: хранить журнал аудита операций не менее 5 лет."),
        ]
    )
    calls = 0

    def fake_generate(stage, payload: ExtractionBatchPayload, output_type, *, settings=None):
        nonlocal calls
        calls += 1
        if calls == 1:
            items = [
                ExtractedItem(block_id=b.block_id, original_quote=b.text, summary=f"Правило {b.block_id}.")
                for b in payload.blocks
                if b.is_target
            ]
            return StageResult_ok(ExtractionOutput(items=items))
        raise ProviderError(
            "Request attempt budget exhausted",
            reason=FailureReason.BUDGET_EXHAUSTED,
            action=FailureAction.STOP,
            attempts=0,
        )

    result = extract_requirements(
        document,
        _settings(),
        generate_fn=fake_generate,
        ground_fn=_exact_ground,
        max_input_tokens=450,
        max_output_tokens=200,
    )

    assert calls == 2
    assert len(result.extraction_blocks.completed) >= 1
    assert not result.extraction_blocks.complete
    assert {r.source.block_id for r in result.requirements} == set(result.extraction_blocks.completed)
    assert any("budget exhausted" in r.lower() for r in result.incomplete_reasons)


def test_empty_document_returns_incomplete_diagnostic():
    empty_doc = ParsedDocument(
        version=DocumentVersion(
            sha256=SHA,
            parser_version="test-v1",
            name="empty.pdf",
            format=DocumentFormat.PDF,
            page_count=1,
            blocks=[],
        ),
        unread_pages=[1],
        diagnostics=["Страница 1: графическая страница без извлекаемого текста."],
    )
    result = extract_requirements(empty_doc, _settings(), generate_fn=lambda *a, **k: pytest.fail("no call"))
    assert result.requirements == []
    assert result.attempts == 0
    assert result.incomplete_reasons


def StageResult_ok(output: ExtractionOutput):
    from src.contracts import StageResult

    return StageResult[ExtractionOutput](
        stage=PipelineStage.EXTRACT,
        output=output,
        attempts=1,
        input_tokens=40,
        output_tokens=20,
    )
