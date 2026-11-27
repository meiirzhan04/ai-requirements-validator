"""Atomic requirement extraction with budget-aware batching and evidence grounding."""
from collections.abc import Callable
from dataclasses import dataclass
import re

from pydantic import Field

from src.contracts import (
    EvidenceCandidate,
    EvidenceRef,
    GroundingResult,
    ParsedDocument,
    PipelineStage,
    ProviderSettings,
    Requirement,
    SourceBlock,
    StageResult,
    WorkSet,
)
from src.contracts.source import Contract, Count, Id, Text
from src.evidence import ground
from src.prompts.extraction import (
    EXTRACTION_PROMPT_VERSION,
    EXTRACTION_REPAIR_HINT,
    EXTRACTION_SYSTEM_PROMPT,
)
from src.provider import FailureAction, FailureReason, ProviderError, QuotaBudget, generate
from src.provider.dto import (
    ExtractionBatchPayload,
    ExtractionBlockInput,
    ExtractionOutput,
)

GenerateFn = Callable[..., StageResult[ExtractionOutput]]
GroundFn = Callable[[EvidenceCandidate, ParsedDocument], GroundingResult]

_HEADROOM_FACTOR = 0.8
_TABLE_SEP_RE = re.compile(r"^\s*\|?(?:\s*:?-{3,}:?\s*\|)+\s*:?-{3,}:?\s*\|?\s*$")
_STOP_ALL_REASONS = frozenset(
    {
        FailureReason.AUTH_ERROR,
        FailureReason.MODEL_UNAVAILABLE,
        FailureReason.DAILY_QUOTA_EXHAUSTED,
        FailureReason.BUDGET_EXHAUSTED,
    }
)


class ExtractionResult(Contract):
    requirements: list[Requirement]
    extraction_blocks: WorkSet
    context_blocks: dict[Id, Text] = Field(default_factory=dict)
    batches: WorkSet
    incomplete_reasons: list[Text] = Field(default_factory=list)
    uncertainties: list[Text] = Field(default_factory=list)
    attempts: Count = 0
    input_tokens: Count | None = None
    output_tokens: Count | None = None


@dataclass(frozen=True)
class _TargetUnit:
    block: SourceBlock
    text: str
    table_header: str | None = None


@dataclass(frozen=True)
class _ExtractionBatch:
    batch_id: str
    units: tuple[_TargetUnit, ...]
    leading_context: SourceBlock | None = None
    trailing_context: SourceBlock | None = None


def _estimate_tokens(text: str) -> int:
    # Conservative character-to-token ratio (~3 chars/token for mixed Cyrillic/Latin/JSON).
    return max(1, (len(text) + 2) // 3)


def _extract_markdown_table_header(text: str) -> tuple[str | None, list[str]]:
    lines = text.splitlines()
    for idx in range(len(lines) - 1):
        if "|" in lines[idx] and _TABLE_SEP_RE.match(lines[idx + 1]):
            header = f"{lines[idx]}\n{lines[idx + 1]}"
            body_lines = lines[idx + 2 :]
            return header, body_lines
    return None, lines


def _estimate_unit_output_tokens(unit: _TargetUnit) -> int:
    non_empty = [line for line in unit.text.splitlines() if line.strip()]
    header, body_lines = _extract_markdown_table_header(unit.text)
    if header is not None:
        rows = [line for line in body_lines if line.strip()]
        estimated_items = max(1, len(rows))
    else:
        estimated_items = max(1, len(non_empty))
    # Estimate structured JSON overhead + verbatim quote + Russian summary per item.
    return _estimate_tokens(unit.text) + estimated_items * 65


def _estimate_unit_input_tokens(unit: _TargetUnit) -> int:
    overhead = len(unit.block.block_id) + len(unit.block.location.section) + 40
    header_len = len(unit.table_header) if unit.table_header else 0
    return _estimate_tokens(unit.text) + (overhead + header_len) // 3


def _split_unit_into_chunks(
    unit: _TargetUnit,
    *,
    max_input_tokens: int,
    max_output_tokens: int,
    force_split: bool = False,
) -> list[_TargetUnit]:
    header, body_lines = _extract_markdown_table_header(unit.text)
    if header is not None and len(body_lines) > 1:
        effective_header = unit.table_header or header
        lines_to_split = body_lines
    else:
        effective_header = unit.table_header
        lines_to_split = unit.text.splitlines()

    non_empty_indices = [i for i, line in enumerate(lines_to_split) if line.strip()]
    if len(non_empty_indices) <= 1:
        if not force_split:
            return [unit]
        # Fall back to sentence/character bisection only if explicitly forced.
        text = unit.text.strip()
        if len(text) < 40:
            return [unit]
        mid = len(text) // 2
        split_at = text.rfind(" ", 0, mid + 1)
        if split_at <= 0:
            split_at = mid
        left, right = text[:split_at].strip(), text[split_at:].strip()
        if not left or not right:
            return [unit]
        return [
            _TargetUnit(block=unit.block, text=left, table_header=effective_header),
            _TargetUnit(block=unit.block, text=right, table_header=effective_header),
        ]

    if force_split:
        mid = len(lines_to_split) // 2
        left_lines = lines_to_split[:mid]
        right_lines = lines_to_split[mid:]
        parts = []
        for group in (left_lines, right_lines):
            chunk_body = "\n".join(group).strip()
            if not chunk_body:
                continue
            chunk_text = f"{effective_header}\n{chunk_body}" if (header is not None and effective_header) else chunk_body
            parts.append(_TargetUnit(block=unit.block, text=chunk_text, table_header=effective_header))
        return parts or [unit]

    chunks: list[_TargetUnit] = []
    current: list[str] = []
    for line in lines_to_split:
        candidate_lines = [*current, line]
        body_text = "\n".join(candidate_lines)
        full_text = f"{effective_header}\n{body_text}" if (header is not None and effective_header) else body_text
        candidate = _TargetUnit(block=unit.block, text=full_text, table_header=effective_header)
        if current and (
            _estimate_unit_input_tokens(candidate) > max_input_tokens
            or _estimate_unit_output_tokens(candidate) > max_output_tokens
        ):
            prev_body = "\n".join(current)
            prev_text = f"{effective_header}\n{prev_body}" if (header is not None and effective_header) else prev_body
            chunks.append(_TargetUnit(block=unit.block, text=prev_text, table_header=effective_header))
            current = [line]
        else:
            current = candidate_lines
    if current:
        body_text = "\n".join(current)
        full_text = f"{effective_header}\n{body_text}" if (header is not None and effective_header) else body_text
        chunks.append(_TargetUnit(block=unit.block, text=full_text, table_header=effective_header))
    return chunks or [unit]


def _detect_inherited_table_header(block: SourceBlock, last_header: str | None) -> tuple[str | None, str | None]:
    header, _ = _extract_markdown_table_header(block.text)
    if header is not None:
        return header, header
    lines = [line.strip() for line in block.text.splitlines() if line.strip()]
    if last_header and lines and all("|" in line for line in lines):
        return last_header, last_header
    return None, None


def plan_extraction_batches(
    document: ParsedDocument,
    *,
    max_input_tokens: int = 12_000,
    max_output_tokens: int = 4_000,
    include_boundary_context: bool = True,
) -> list[_ExtractionBatch]:
    """Pack source blocks into input/output-budgeted batches with 20% headroom."""
    if max_input_tokens < 100 or max_output_tokens < 100:
        raise ValueError("Batch token budgets must be at least 100")

    prompt_overhead = _estimate_tokens(EXTRACTION_SYSTEM_PROMPT) + 120
    input_cap = max(64, int(max_input_tokens * _HEADROOM_FACTOR) - prompt_overhead)
    output_cap = max(64, int(max_output_tokens * _HEADROOM_FACTOR))

    units: list[_TargetUnit] = []
    last_table_header: str | None = None
    last_section: str | None = None
    for block in document.version.blocks:
        if block.location.section != last_section:
            last_table_header = None
            last_section = block.location.section
        unit_header, last_table_header = _detect_inherited_table_header(block, last_table_header)
        base_unit = _TargetUnit(block=block, text=block.text, table_header=unit_header)
        if (
            _estimate_unit_input_tokens(base_unit) > input_cap
            or _estimate_unit_output_tokens(base_unit) > output_cap
        ):
            units.extend(
                _split_unit_into_chunks(
                    base_unit,
                    max_input_tokens=input_cap,
                    max_output_tokens=output_cap,
                )
            )
        else:
            units.append(base_unit)

    raw_groups: list[list[_TargetUnit]] = []
    current_group: list[_TargetUnit] = []
    current_in = 0
    current_out = 0
    for unit in units:
        u_in = _estimate_unit_input_tokens(unit)
        u_out = _estimate_unit_output_tokens(unit)
        if current_group and (current_in + u_in > input_cap or current_out + u_out > output_cap):
            raw_groups.append(current_group)
            current_group = [unit]
            current_in = u_in
            current_out = u_out
        else:
            current_group.append(unit)
            current_in += u_in
            current_out += u_out
    if current_group:
        raw_groups.append(current_group)

    blocks_by_id = {b.block_id: idx for idx, b in enumerate(document.version.blocks)}
    all_blocks = document.version.blocks
    batches: list[_ExtractionBatch] = []
    for idx, group in enumerate(raw_groups, start=1):
        first_block_idx = blocks_by_id[group[0].block.block_id]
        last_block_idx = blocks_by_id[group[-1].block.block_id]
        leading = all_blocks[first_block_idx - 1] if (include_boundary_context and first_block_idx > 0) else None
        trailing = (
            all_blocks[last_block_idx + 1]
            if (include_boundary_context and last_block_idx + 1 < len(all_blocks))
            else None
        )
        batches.append(
            _ExtractionBatch(
                batch_id=f"extract-{idx}",
                units=tuple(group),
                leading_context=leading,
                trailing_context=trailing,
            )
        )
    return batches


def _build_payload(
    document: ParsedDocument,
    batch: _ExtractionBatch,
    *,
    repair_hint: str | None = None,
) -> ExtractionBatchPayload:
    block_inputs: list[ExtractionBlockInput] = []
    target_ids: list[str] = []
    seen_targets: set[str] = set()

    if batch.leading_context is not None:
        block_inputs.append(
            ExtractionBlockInput(
                block_id=batch.leading_context.block_id,
                section=batch.leading_context.location.section,
                text=batch.leading_context.text,
                is_target=False,
            )
        )
    for unit in batch.units:
        if unit.block.block_id not in seen_targets:
            seen_targets.add(unit.block.block_id)
            target_ids.append(unit.block.block_id)
        block_inputs.append(
            ExtractionBlockInput(
                block_id=unit.block.block_id,
                section=unit.block.location.section,
                text=unit.text,
                is_target=True,
                table_header=unit.table_header,
            )
        )
    if batch.trailing_context is not None:
        block_inputs.append(
            ExtractionBlockInput(
                block_id=batch.trailing_context.block_id,
                section=batch.trailing_context.location.section,
                text=batch.trailing_context.text,
                is_target=False,
            )
        )

    return ExtractionBatchPayload(
        system_instruction=EXTRACTION_SYSTEM_PROMPT,
        prompt_version=EXTRACTION_PROMPT_VERSION,
        document_name=document.version.name,
        batch_id=batch.batch_id,
        target_block_ids=target_ids,
        blocks=block_inputs,
        repair_hint=repair_hint,
    )


def _split_batch_on_truncation(batch: _ExtractionBatch) -> list[_ExtractionBatch] | None:
    if len(batch.units) > 1:
        mid = len(batch.units) // 2
        left_units = batch.units[:mid]
        right_units = batch.units[mid:]
        return [
            _ExtractionBatch(
                batch_id=f"{batch.batch_id}.1",
                units=left_units,
                leading_context=batch.leading_context,
                trailing_context=right_units[0].block,
            ),
            _ExtractionBatch(
                batch_id=f"{batch.batch_id}.2",
                units=right_units,
                leading_context=left_units[-1].block,
                trailing_context=batch.trailing_context,
            ),
        ]
    unit = batch.units[0]
    sub_units = _split_unit_into_chunks(
        unit,
        max_input_tokens=max(64, _estimate_unit_input_tokens(unit) // 2),
        max_output_tokens=max(64, _estimate_unit_output_tokens(unit) // 2),
        force_split=True,
    )
    if len(sub_units) <= 1:
        return None
    return [
        _ExtractionBatch(
            batch_id=f"{batch.batch_id}.{idx}",
            units=(sub_unit,),
            leading_context=batch.leading_context if idx == 1 else None,
            trailing_context=batch.trailing_context if idx == len(sub_units) else None,
        )
        for idx, sub_unit in enumerate(sub_units, start=1)
    ]


def _normalize_optional_id(raw: str | None) -> str | None:
    if raw is None:
        return None
    cleaned = raw.strip()
    return cleaned or None


def _call_provider(
    generate_fn: GenerateFn,
    payload: ExtractionBatchPayload,
    *,
    settings: ProviderSettings,
    budget: QuotaBudget | None,
    estimated_input_tokens: int,
    estimated_output_tokens: int,
) -> StageResult[ExtractionOutput]:
    if generate_fn is generate:
        return generate_fn(
            PipelineStage.EXTRACT,
            payload,
            ExtractionOutput,
            settings=settings,
            budget=budget,
            estimated_input_tokens=estimated_input_tokens,
            estimated_output_tokens=estimated_output_tokens,
        )
    return generate_fn(
        PipelineStage.EXTRACT,
        payload,
        ExtractionOutput,
        settings=settings,
    )


def extract_requirements(
    document: ParsedDocument,
    settings: ProviderSettings,
    *,
    budget: QuotaBudget | None = None,
    generate_fn: GenerateFn = generate,
    ground_fn: GroundFn = ground,
    max_input_tokens: int = 12_000,
    max_output_tokens: int = 4_000,
    include_boundary_context: bool = True,
) -> ExtractionResult:
    """Extract grounded atomic requirements and per-block coverage in source order."""
    planned_blocks = [block.block_id for block in document.version.blocks]
    block_order = {block_id: index for index, block_id in enumerate(planned_blocks)}
    if not planned_blocks:
        return ExtractionResult(
            requirements=[],
            extraction_blocks=WorkSet(planned=[], completed=[]),
            context_blocks={},
            batches=WorkSet(planned=[], completed=[]),
            incomplete_reasons=["Документ не содержит текстовых блоков для извлечения."],
        )

    queue = plan_extraction_batches(
        document,
        max_input_tokens=max_input_tokens,
        max_output_tokens=max_output_tokens,
        include_boundary_context=include_boundary_context,
    )
    planned_batches: list[str] = [b.batch_id for b in queue]
    completed_batches: list[str] = []

    # Track per-block extraction state across chunks/batches.
    block_planned_chunks: dict[str, int] = {}
    for batch in queue:
        for unit in batch.units:
            block_planned_chunks[unit.block.block_id] = block_planned_chunks.get(unit.block.block_id, 0) + 1
    block_completed_chunks: dict[str, int] = {bid: 0 for bid in planned_blocks}
    block_failed: set[str] = set()

    grounded_by_anchor: dict[tuple[str, str, int, int], tuple[str | None, str, str, EvidenceRef]] = {}
    context_reasons: dict[str, str] = {}
    incomplete_reasons: list[str] = []
    uncertainties: list[str] = []

    total_attempts = 0
    known_in = 0
    known_out = 0
    has_known_usage = False

    idx = 0
    while idx < len(queue):
        batch = queue[idx]
        idx += 1
        target_ids = list(dict.fromkeys(unit.block.block_id for unit in batch.units))
        est_in = sum(_estimate_unit_input_tokens(u) for u in batch.units)
        est_out = sum(_estimate_unit_output_tokens(u) for u in batch.units)

        payload = _build_payload(document, batch)
        stage_result: StageResult[ExtractionOutput] | None = None
        stop_all = False

        for repair_attempt in range(2):
            try:
                stage_result = _call_provider(
                    generate_fn,
                    payload,
                    settings=settings,
                    budget=budget,
                    estimated_input_tokens=est_in,
                    estimated_output_tokens=est_out,
                )
                total_attempts += stage_result.attempts
                if stage_result.input_tokens is not None and stage_result.output_tokens is not None:
                    known_in += stage_result.input_tokens
                    known_out += stage_result.output_tokens
                    has_known_usage = True
                break
            except ProviderError as exc:
                total_attempts += max(exc.attempts, 0)
                if exc.action == FailureAction.REPAIR and repair_attempt == 0:
                    payload = _build_payload(document, batch, repair_hint=EXTRACTION_REPAIR_HINT)
                    continue
                if exc.action == FailureAction.SPLIT:
                    split_batches = _split_batch_on_truncation(batch)
                    if split_batches is not None:
                        # Update chunk accounting for the split batch.
                        for unit in batch.units:
                            block_planned_chunks[unit.block.block_id] -= 1
                        for sub in split_batches:
                            planned_batches.append(sub.batch_id)
                            for unit in sub.units:
                                block_planned_chunks[unit.block.block_id] = (
                                    block_planned_chunks.get(unit.block.block_id, 0) + 1
                                )
                        queue[idx:idx] = split_batches
                        completed_batches.append(batch.batch_id)
                        stage_result = None
                        break
                    incomplete_reasons.append(
                        f"Пакет {batch.batch_id} усечён (MAX_TOKENS) и не может быть разбит дальше."
                    )
                    block_failed.update(target_ids)
                    break

                incomplete_reasons.append(f"Пакет {batch.batch_id} не завершён: {exc}.")
                block_failed.update(target_ids)
                if exc.reason in _STOP_ALL_REASONS:
                    stop_all = True
                break

        if stop_all:
            for remaining_batch in queue[idx:]:
                for unit in remaining_batch.units:
                    block_failed.add(unit.block.block_id)
            break
        if stage_result is None:
            continue

        output = stage_result.output
        target_set = set(target_ids)
        batch_grounded_blocks: set[str] = set()
        batch_context_blocks: set[str] = set()
        batch_invalid_blocks: set[str] = set()

        for item in output.items:
            if item.block_id not in target_set:
                uncertainties.append(
                    f"Пакет {batch.batch_id}: модель вернула элемент для внецелевого блока {item.block_id}."
                )
                continue
            quote = item.original_quote.strip()
            summary = item.summary.strip()
            if not quote or not summary:
                batch_invalid_blocks.add(item.block_id)
                incomplete_reasons.append(
                    f"Блок {item.block_id} ({batch.batch_id}): пустая цитата или формулировка требования."
                )
                continue
            candidate = EvidenceCandidate(
                version=document.version.sha256,
                block_id=item.block_id,
                quote=item.original_quote,
            )
            grounded = ground_fn(candidate, document)
            if not grounded.reference or not grounded.reference.grounded:
                batch_invalid_blocks.add(item.block_id)
                incomplete_reasons.append(
                    f"Блок {item.block_id} ({batch.batch_id}): цитата не подтверждена ({grounded.status.value}: {grounded.reason})."
                )
                continue
            ref = grounded.reference
            anchor = ref.anchor
            orig_id = _normalize_optional_id(item.original_id)
            if anchor not in grounded_by_anchor:
                grounded_by_anchor[anchor] = (orig_id, item.category.value, summary, ref)
            else:
                existing_orig, existing_cat, existing_summary, existing_ref = grounded_by_anchor[anchor]
                if existing_orig is None and orig_id is not None:
                    grounded_by_anchor[anchor] = (orig_id, existing_cat, existing_summary, existing_ref)
            batch_grounded_blocks.add(item.block_id)

        for ctx in output.context_blocks:
            if ctx.block_id not in target_set:
                continue
            reason = ctx.reason.strip()
            if not reason:
                batch_invalid_blocks.add(ctx.block_id)
                incomplete_reasons.append(
                    f"Блок {ctx.block_id} ({batch.batch_id}): указан контекстный блок без обоснования."
                )
                continue
            context_reasons.setdefault(ctx.block_id, reason)
            batch_context_blocks.add(ctx.block_id)

        batch_fully_accounted = True
        for unit in batch.units:
            bid = unit.block.block_id
            if bid in batch_invalid_blocks:
                block_failed.add(bid)
                batch_fully_accounted = False
            elif bid in batch_grounded_blocks or bid in batch_context_blocks:
                block_completed_chunks[bid] += 1
            else:
                block_failed.add(bid)
                batch_fully_accounted = False
                incomplete_reasons.append(
                    f"Блок {bid} ({batch.batch_id}) пропущен при извлечении требований."
                )

        if batch_fully_accounted:
            completed_batches.append(batch.batch_id)

    # Sort deduplicated grounded requirements in strict source order and assign stable IDs.
    ordered_records = sorted(
        grounded_by_anchor.values(),
        key=lambda rec: (block_order[rec[3].block_id], rec[3].start, rec[3].end, rec[2]),
    )
    requirements = [
        Requirement(
            req_id=f"R{seq:03d}",
            original_id=orig_id,
            category=category,
            summary=summary,
            source=ref,
        )
        for seq, (orig_id, category, summary, ref) in enumerate(ordered_records, start=1)
    ]

    cited_blocks = {req.source.block_id for req in requirements}
    completed_blocks: list[str] = []
    final_context_blocks: dict[str, str] = {}
    for bid in planned_blocks:
        if bid in block_failed:
            continue
        if block_completed_chunks.get(bid, 0) < block_planned_chunks.get(bid, 1):
            incomplete_reasons.append(f"Блок {bid} обработан не полностью.")
            continue
        if bid in cited_blocks:
            completed_blocks.append(bid)
        elif bid in context_reasons:
            completed_blocks.append(bid)
            final_context_blocks[bid] = context_reasons[bid]
        else:
            incomplete_reasons.append(f"Блок {bid} не имеет ни требований, ни обоснования контекста.")

    # Deduplicate reasons while preserving order.
    dedup_reasons = list(dict.fromkeys(incomplete_reasons))
    dedup_uncertainties = list(dict.fromkeys(uncertainties))

    return ExtractionResult(
        requirements=requirements,
        extraction_blocks=WorkSet(planned=planned_blocks, completed=completed_blocks),
        context_blocks=final_context_blocks,
        batches=WorkSet(planned=planned_batches, completed=completed_batches),
        incomplete_reasons=dedup_reasons,
        uncertainties=dedup_uncertainties,
        attempts=total_attempts,
        input_tokens=known_in if has_known_usage else None,
        output_tokens=known_out if has_known_usage else None,
    )
