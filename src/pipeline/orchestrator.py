"""End-to-end synchronous AI validation pipeline orchestrating all review stages."""
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from datetime import datetime, timezone
from functools import lru_cache
import logging
import subprocess
import threading
import time
from typing import Any
from uuid import uuid4

from src.contracts import (
    Coverage,
    CriterionEvaluation,
    EvidenceCandidate,
    ExecutionStatus,
    Finding,
    GroundingResult,
    ParsedDocument,
    PipelineStage,
    ProgressEvent,
    ProviderSettings,
    Question,
    Requirement,
    RunMetadata,
    SemanticStatus,
    Severity,
    StageResult,
    ValidationReport,
    Verdict,
    WorkSet,
)
from src.evidence import ground
from src.pipeline.extraction import ExtractionResult, extract_requirements
from src.pipeline.holistic import HolisticAuditResult, audit_holistic_set, deduplicate_findings
from src.pipeline.individual import IndividualAuditResult, audit_individual_requirements
from src.pipeline.questions import QuestionGenerationResult, generate_questions
from src.pipeline.verification import VerificationResult, reconcile_evaluations_with_findings, verify_findings
from src.prompts.extraction import EXTRACTION_PROMPT_VERSION
from src.prompts.holistic import HOLISTIC_PROMPT_VERSION
from src.prompts.individual import INDIVIDUAL_PROMPT_VERSION
from src.prompts.questions import QUESTIONS_PROMPT_VERSION
from src.prompts.verification import VERIFICATION_PROMPT_VERSION
from src.provider import FailureAction, FailureReason, ProviderError, QuotaBudget, generate, provider_session
from src.readiness import decide

try:
    from src.ingest.structure import reconcile_scope as _reconcile_scope
    from src.ingest.structure import scan_structure as _scan_structure
except ImportError:  # pragma: no cover - fallback only when #4 is not yet merged
    _reconcile_scope = None
    _scan_structure = None

logger = logging.getLogger(__name__)

PIPELINE_PROMPT_VERSION = "pipeline-v2"
STAGE_PROMPT_VERSIONS = (
    EXTRACTION_PROMPT_VERSION,
    INDIVIDUAL_PROMPT_VERSION,
    HOLISTIC_PROMPT_VERSION,
    VERIFICATION_PROMPT_VERSION,
    QUESTIONS_PROMPT_VERSION,
)

GenerateFn = Callable[..., StageResult[Any]]
GroundFn = Callable[[EvidenceCandidate, ParsedDocument], GroundingResult]
DecideFn = Callable[[Coverage, list[Finding], list[str]], Verdict]
ProgressCallback = Callable[[ProgressEvent], None]


@lru_cache(maxsize=1)
def _detect_agent_revision() -> str:
    try:
        output = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=3,
        ).strip()
        if output:
            return output
    except (OSError, subprocess.SubprocessError):
        pass
    return "local-dev"


def _emit(
    on_progress: ProgressCallback | None,
    stage: PipelineStage,
    state: str,
    message: str,
) -> None:
    if on_progress is None:
        return
    on_progress(ProgressEvent(stage=stage, state=state, message=message))


def _dedup_non_empty(items: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for raw in items:
        cleaned = raw.strip() if isinstance(raw, str) else ""
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            result.append(cleaned)
    return result


def _sum_optional_tokens(*values: int | None) -> int | None:
    present = [v for v in values if v is not None]
    return sum(present) if present else None


def _reconcile_extraction_scope(
    document: ParsedDocument,
    extraction: ExtractionResult,
) -> tuple[WorkSet, dict[str, str], list[str]]:
    if _reconcile_scope is not None:
        scope = _reconcile_scope(
            document,
            extraction.extraction_blocks,
            extraction.requirements,
            extraction.context_blocks,
        )
        return scope.extraction_blocks, dict(scope.context_blocks), list(scope.incomplete_reasons)

    planned = [b.block_id for b in document.version.blocks]
    cited = {r.source.block_id for r in extraction.requirements}
    justified = {
        bid: reason.strip()
        for bid, reason in extraction.context_blocks.items()
        if bid not in cited and reason.strip()
    }
    accepted = set(extraction.extraction_blocks.completed) & (cited | justified.keys())
    completed = [bid for bid in planned if bid in accepted]
    outstanding = [bid for bid in planned if bid not in accepted]
    reasons = list(document.diagnostics)
    if document.unread_pages:
        pages = ", ".join(map(str, document.unread_pages))
        reasons.append(f"Непрочитанные полностью или частично страницы: {pages}.")
    if outstanding:
        reasons.append("Не завершена обработка блоков: " + ", ".join(outstanding) + ".")
    if not planned:
        reasons.append("Нет доступных для проверки исходных блоков.")
    return (
        WorkSet(planned=planned, completed=completed),
        {bid: justified[bid] for bid in completed if bid in justified},
        reasons,
    )


def _prefix_stage_outputs(
    prefix: str,
    findings: list[Finding],
    evaluations: list[CriterionEvaluation],
) -> tuple[list[Finding], list[CriterionEvaluation]]:
    id_map = {f.finding_id: f"{prefix}-{f.finding_id}" for f in findings}
    prefixed_findings = [
        f.model_copy(update={"finding_id": id_map[f.finding_id]})
        for f in findings
    ]
    prefixed_evals = [
        ev.model_copy(
            update={
                "finding_ids": [id_map.get(fid, fid) for fid in ev.finding_ids]
            }
        )
        if ev.finding_ids
        else ev
        for ev in evaluations
    ]
    return prefixed_findings, prefixed_evals


def _remap_evaluations(
    evaluations: list[CriterionEvaluation],
    id_map: dict[str, str],
) -> list[CriterionEvaluation]:
    remapped: list[CriterionEvaluation] = []
    for ev in evaluations:
        if not ev.finding_ids:
            remapped.append(ev)
            continue
        new_ids = list(dict.fromkeys(id_map.get(fid, fid) for fid in ev.finding_ids))
        remapped.append(ev.model_copy(update={"finding_ids": new_ids}))
    return remapped


def _collect_material_uncertainties(
    findings: list[Finding],
    indiv: IndividualAuditResult | None,
    holistic: HolisticAuditResult | None,
    clarification: QuestionGenerationResult | None,
) -> list[str]:
    uncertainties: list[str] = []
    if clarification is not None:
        uncertainties.extend(clarification.blocking_uncertainties)

    recorded_reasons: set[str] = set()
    for finding in findings:
        if finding.severity in (Severity.CRITICAL, Severity.MAJOR) and finding.semantic_status in (
            SemanticStatus.UNCHECKED,
            SemanticStatus.UNCERTAIN,
        ):
            recorded_reasons.add(finding.verification_reason.strip())
            uncertainties.append(
                f"Открытая гипотеза {finding.finding_id} ({finding.severity.value}, {finding.semantic_status.value}): "
                f"{finding.verification_reason}"
            )

    if indiv is not None:
        for note in indiv.uncertainties:
            if "одноцитатное противоречие" in note:
                uncertainties.append(note)

    if holistic is not None:
        for note in holistic.uncertainties:
            cleaned = note.strip()
            if cleaned in recorded_reasons:
                continue
            if "непроверенные фрагменты" in cleaned or "две независимые подтверждённые цитаты" in cleaned:
                uncertainties.append(cleaned)

    return _dedup_non_empty(uncertainties)


def _build_report(
    *,
    document: ParsedDocument,
    settings: ProviderSettings,
    run_id: str,
    started_at: datetime,
    started_mono: float,
    agent_revision: str,
    requirements: list[Requirement],
    evaluations: list[CriterionEvaluation],
    findings: list[Finding],
    questions: list[Question],
    extraction_blocks: WorkSet,
    context_blocks: dict[str, str],
    individual_requirements: WorkSet,
    audit_batches: WorkSet,
    cross_section_groups: WorkSet,
    incomplete_reasons: list[str],
    material_uncertainties: list[str],
    request_attempts: int,
    input_tokens: int | None,
    output_tokens: int | None,
    decide_fn: DecideFn,
) -> ValidationReport:
    reasons = _dedup_non_empty(incomplete_reasons)
    uncertainties = _dedup_non_empty(material_uncertainties)

    has_usable_audits = bool(
        individual_requirements.completed
        or cross_section_groups.completed
        or any(f.confirmed for f in findings)
    )
    all_work_complete = (
        bool(extraction_blocks.planned)
        and extraction_blocks.complete
        and bool(individual_requirements.planned)
        and individual_requirements.complete
        and bool(audit_batches.planned)
        and audit_batches.complete
        and bool(cross_section_groups.planned)
        and cross_section_groups.complete
        and not reasons
        and not document.unread_pages
        and not document.diagnostics
        and request_attempts > 0
    )

    if all_work_complete:
        execution = ExecutionStatus.COMPLETE
    elif has_usable_audits:
        execution = ExecutionStatus.PARTIAL
        if not reasons:
            reasons.append("Часть обязательных этапов анализа не завершена.")
    else:
        execution = ExecutionStatus.FAILED
        if not reasons:
            reasons.append("Обязательные этапы анализа не дали пригодной оценки.")

    coverage = Coverage(
        execution=execution,
        extraction_blocks=extraction_blocks,
        context_blocks=context_blocks,
        individual_requirements=individual_requirements,
        audit_batches=audit_batches,
        cross_section_groups=cross_section_groups,
        incomplete_reasons=reasons,
        uncertainties=uncertainties,
    )
    verdict = decide_fn(coverage, findings, uncertainties)
    duration = max(0.0, round(time.monotonic() - started_mono, 3))
    metadata = RunMetadata(
        run_id=run_id,
        document_version=document.version.sha256,
        started_at=started_at,
        model=settings.model,
        agent_revision=agent_revision,
        prompt_version=PIPELINE_PROMPT_VERSION,
        origin="live",
        duration_seconds=duration,
        request_attempts=request_attempts,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )
    return ValidationReport(
        metadata=metadata,
        document=document,
        requirements=requirements,
        evaluations=evaluations,
        findings=findings,
        coverage=coverage,
        questions=questions,
        verdict=verdict,
    )


def execute_pipeline(
    document: ParsedDocument,
    settings: ProviderSettings,
    on_progress: ProgressCallback | None = None,
    *,
    generate_fn: GenerateFn = generate,
    ground_fn: GroundFn = ground,
    decide_fn: DecideFn = decide,
    budget: QuotaBudget | None = None,
    run_id: str | None = None,
    agent_revision: str | None = None,
    max_input_tokens: int = 24_000,
    max_output_tokens: int = 12_000,
    max_requirements_per_batch: int = 10,
    max_requirements_per_group: int = 80,
    verification_batch_size: int = 16,
    question_batch_size: int = 16,
    max_concurrency: int | None = None,
) -> ValidationReport:
    """Orchestrate the full validation pipeline with bounded parallelism and strict coverage."""
    if not isinstance(document, ParsedDocument):
        raise ValueError("document must be a valid ParsedDocument")
    if not isinstance(settings, ProviderSettings):
        raise ValueError("settings must be a valid ProviderSettings")

    active_run_id = run_id or f"run-{uuid4().hex[:12]}"
    started_at = datetime.now(timezone.utc)
    started_mono = time.monotonic()
    revision = agent_revision or _detect_agent_revision()
    active_budget = budget or QuotaBudget()
    effective_concurrency = (
        max(1, max_concurrency)
        if max_concurrency is not None
        else (3 if generate_fn is generate else 1)
    )
    emit_lock = threading.Lock()

    def _safe_emit(stage: PipelineStage, state: str, message: str) -> None:
        if on_progress is None:
            return
        with emit_lock:
            on_progress(ProgressEvent(stage=stage, state=state, message=message))

    blocks = document.version.blocks
    block_order = {b.block_id: idx for idx, b in enumerate(blocks)}

    # Stage 1: Structural pre-scan and input inventory check.
    _safe_emit(
        PipelineStage.INGEST,
        "started",
        f"Проверка состава документа «{document.version.name}» ({len(blocks)} блоков).",
    )
    if not blocks:
        if not document.unread_pages and not document.blank_pages and not document.diagnostics:
            _safe_emit(
                PipelineStage.INGEST,
                "failed",
                "Документ не содержит текстовых блоков или диагностик страниц.",
            )
            raise ValueError("Документ не содержит текстовых блоков для проверки.")
        reasons = list(document.diagnostics)
        if document.unread_pages:
            reasons.append(
                f"Непрочитанные полностью или частично страницы: {', '.join(map(str, document.unread_pages))}."
            )
        if document.blank_pages:
            reasons.append(
                f"Пустые страницы без текста: {', '.join(map(str, document.blank_pages))}."
            )
        reasons.append("Нет доступных для проверки исходных блоков.")
        _safe_emit(
            PipelineStage.INGEST,
            "failed",
            "Документ не содержит читаемых текстовых блоков; анализ завершён без обращения к модели.",
        )
        _safe_emit(
            PipelineStage.REPORT,
            "started",
            "Формирование отчёта о нечитаемом документе.",
        )
        report = _build_report(
            document=document,
            settings=settings,
            run_id=active_run_id,
            started_at=started_at,
            started_mono=started_mono,
            agent_revision=revision,
            requirements=[],
            evaluations=[],
            findings=[],
            questions=[],
            extraction_blocks=WorkSet(planned=[], completed=[]),
            context_blocks={},
            individual_requirements=WorkSet(planned=[], completed=[]),
            audit_batches=WorkSet(planned=[], completed=[]),
            cross_section_groups=WorkSet(planned=[], completed=[]),
            incomplete_reasons=reasons,
            material_uncertainties=[],
            request_attempts=0,
            input_tokens=None,
            output_tokens=None,
            decide_fn=decide_fn,
        )
        _safe_emit(
            PipelineStage.REPORT,
            "completed",
            f"Отчёт сформирован: готовность={report.verdict.readiness.value}.",
        )
        return report

    structural_signals = _scan_structure(document) if _scan_structure is not None else []
    _safe_emit(
        PipelineStage.INGEST,
        "completed",
        f"Проверено блоков: {len(blocks)}, структурных сигналов: {len(structural_signals)}.",
    )

    # Fail fast with explicit sanitized ProviderError when live credentials are missing.
    if generate_fn is generate and not settings.api_key:
        _safe_emit(
            PipelineStage.EXTRACT,
            "failed",
            "Отсутствует API-ключ провайдера Gemini.",
        )
        raise ProviderError(
            "API key required",
            reason=FailureReason.AUTH_ERROR,
            action=FailureAction.STOP,
        )

    logger.info(
        "run=%s stage=start blocks=%d signals=%d concurrency=%d",
        active_run_id,
        len(blocks),
        len(structural_signals),
        effective_concurrency,
    )

    session_ctx = provider_session(settings) if generate_fn is generate else nullcontext()
    with session_ctx:
        # Stage 2: Requirement extraction & grounding.
        t_stage = time.monotonic()
        _safe_emit(
            PipelineStage.EXTRACT,
            "started",
            f"Извлечение атомарных требований из {len(blocks)} блоков.",
        )
        extraction = extract_requirements(
            document,
            settings,
            budget=active_budget,
            generate_fn=generate_fn,
            ground_fn=ground_fn,
            max_input_tokens=max_input_tokens,
            max_output_tokens=max_output_tokens,
            max_concurrency=effective_concurrency,
            retry_ungrounded_blocks=(generate_fn is generate),
        )
        extraction_blocks, context_blocks, scope_reasons = _reconcile_extraction_scope(document, extraction)

        incomplete_reasons: list[str] = [*scope_reasons, *extraction.incomplete_reasons]
        if not extraction.batches.complete:
            incomplete_reasons.append(
                f"Завершено пакетов извлечения: {len(extraction.batches.completed)} из {len(extraction.batches.planned)}."
            )

        logger.info(
            "run=%s stage=extract dt=%.2fs reqs=%d blocks=%d/%d attempts=%d",
            active_run_id,
            time.monotonic() - t_stage,
            len(extraction.requirements),
            len(extraction_blocks.completed),
            len(extraction_blocks.planned),
            extraction.attempts,
        )
        if extraction_blocks.completed:
            _safe_emit(
                PipelineStage.EXTRACT,
                "completed",
                f"Извлечено требований: {len(extraction.requirements)} "
                f"(обработано блоков {len(extraction_blocks.completed)}/{len(extraction_blocks.planned)}).",
            )
        else:
            _safe_emit(
                PipelineStage.EXTRACT,
                "failed",
                "Этап извлечения не завершил ни одного блока.",
            )

        requirements = extraction.requirements
        completed_block_set = set(extraction_blocks.completed)

        indiv: IndividualAuditResult | None = None
        holistic: HolisticAuditResult | None = None
        verification: VerificationResult | None = None
        clarification: QuestionGenerationResult | None = None

        final_findings: list[Finding] = []
        final_evaluations: list[CriterionEvaluation] = []
        final_questions: list[Question] = []

        if not requirements:
            incomplete_reasons.append("Не извлечено ни одного требования для проведения аудита.")
            individual_requirements = WorkSet(planned=[], completed=[])
            audit_batches = extraction.batches
            cross_section_groups = WorkSet(planned=[], completed=[])
        else:
            def _run_individual() -> IndividualAuditResult:
                t0 = time.monotonic()
                _safe_emit(
                    PipelineStage.INDIVIDUAL,
                    "started",
                    f"Индивидуальный аудит {len(requirements)} требований по 9 критериям ISO 29148.",
                )
                res = audit_individual_requirements(
                    requirements,
                    document,
                    settings,
                    inspected_blocks=completed_block_set,
                    budget=active_budget,
                    generate_fn=generate_fn,
                    ground_fn=ground_fn,
                    max_input_tokens=max_input_tokens,
                    max_output_tokens=max_output_tokens,
                    max_requirements_per_batch=max_requirements_per_batch,
                    max_concurrency=max(1, effective_concurrency - 1) if effective_concurrency > 1 else 1,
                )
                logger.info(
                    "run=%s stage=individual dt=%.2fs reqs=%d/%d findings=%d attempts=%d",
                    active_run_id,
                    time.monotonic() - t0,
                    len(res.individual_requirements.completed),
                    len(res.individual_requirements.planned),
                    len(res.findings),
                    res.attempts,
                )
                _safe_emit(
                    PipelineStage.INDIVIDUAL,
                    "completed" if res.individual_requirements.completed else "failed",
                    f"Индивидуальный аудит: {len(res.individual_requirements.completed)}/"
                    f"{len(res.individual_requirements.planned)} требований, кандидатов находок: {len(res.findings)}.",
                )
                return res

            def _run_holistic() -> HolisticAuditResult:
                t0 = time.monotonic()
                _safe_emit(
                    PipelineStage.HOLISTIC,
                    "started",
                    f"Целостный аудит набора из {len(requirements)} требований (ISO 29148 / IEEE 830).",
                )
                res = audit_holistic_set(
                    requirements,
                    document,
                    settings,
                    inspected_blocks=completed_block_set,
                    structural_signals=structural_signals,
                    budget=active_budget,
                    generate_fn=generate_fn,
                    ground_fn=ground_fn,
                    max_requirements_per_group=max_requirements_per_group,
                    max_concurrency=1,
                )
                logger.info(
                    "run=%s stage=holistic dt=%.2fs groups=%d/%d findings=%d attempts=%d",
                    active_run_id,
                    time.monotonic() - t0,
                    len(res.cross_section_groups.completed),
                    len(res.cross_section_groups.planned),
                    len(res.findings),
                    res.attempts,
                )
                _safe_emit(
                    PipelineStage.HOLISTIC,
                    "completed" if res.cross_section_groups.completed else "failed",
                    f"Целостный аудит: групп {len(res.cross_section_groups.completed)}/"
                    f"{len(res.cross_section_groups.planned)}, кандидатов находок: {len(res.findings)}.",
                )
                return res

            # Stages 3 & 4 have zero data dependency on each other once requirements are extracted.
            if effective_concurrency > 1:
                with ThreadPoolExecutor(max_workers=2) as stage_pool:
                    indiv_fut = stage_pool.submit(_run_individual)
                    holistic_fut = stage_pool.submit(_run_holistic)
                    indiv = indiv_fut.result()
                    holistic = holistic_fut.result()
            else:
                indiv = _run_individual()
                holistic = _run_holistic()

            incomplete_reasons.extend(indiv.incomplete_reasons)
            if not indiv.individual_requirements.complete:
                incomplete_reasons.append(
                    f"Индивидуальный аудит завершён для {len(indiv.individual_requirements.completed)} "
                    f"из {len(indiv.individual_requirements.planned)} требований."
                )
            if not indiv.audit_batches.complete:
                incomplete_reasons.append(
                    f"Завершено пакетов индивидуального аудита: {len(indiv.audit_batches.completed)} "
                    f"из {len(indiv.audit_batches.planned)}."
                )
            incomplete_reasons.extend(holistic.incomplete_reasons)
            if not holistic.cross_section_groups.complete:
                incomplete_reasons.append(
                    f"Завершено групп целостного аудита: {len(holistic.cross_section_groups.completed)} "
                    f"из {len(holistic.cross_section_groups.planned)}."
                )

            # Combine and deduplicate findings across individual + holistic stages before verification.
            ind_findings, ind_evals = _prefix_stage_outputs("IND", indiv.findings, indiv.evaluations)
            hol_findings, hol_evals = _prefix_stage_outputs("HOL", holistic.findings, holistic.evaluations)
            dedup_findings, id_map = deduplicate_findings(
                [*ind_findings, *hol_findings],
                finding_id_start=1,
                block_order=block_order,
            )
            combined_evals = _remap_evaluations([*ind_evals, *hol_evals], id_map)

            # Stage 5: Independent semantic verification of MAJOR/CRITICAL candidates.
            t_verify = time.monotonic()
            to_verify_count = sum(
                1
                for f in dedup_findings
                if f.severity in (Severity.CRITICAL, Severity.MAJOR)
                and f.semantic_status == SemanticStatus.UNCHECKED
            )
            _safe_emit(
                PipelineStage.VERIFY,
                "started",
                f"Семантическая проверка серьёзных кандидатов: {to_verify_count}.",
            )
            verification = verify_findings(
                dedup_findings,
                document,
                settings,
                evaluations=combined_evals,
                inspected_blocks=completed_block_set,
                budget=active_budget,
                generate_fn=generate_fn,
                batch_size=verification_batch_size,
                max_concurrency=effective_concurrency,
            )
            incomplete_reasons.extend(verification.incomplete_reasons)
            final_findings = verification.findings
            final_evaluations = reconcile_evaluations_with_findings(
                verification.evaluations,
                final_findings,
            )
            logger.info(
                "run=%s stage=verify dt=%.2fs checked=%d confirmed=%d attempts=%d",
                active_run_id,
                time.monotonic() - t_verify,
                to_verify_count,
                sum(f.confirmed for f in final_findings),
                verification.attempts,
            )
            _safe_emit(
                PipelineStage.VERIFY,
                "completed" if not verification.incomplete_reasons else "failed",
                f"Подтверждено находок: {sum(f.confirmed for f in final_findings)} из {len(final_findings)}.",
            )

            # Stage 6: Stakeholder clarification question engine.
            t_clarify = time.monotonic()
            _safe_emit(
                PipelineStage.CLARIFY,
                "started",
                "Формирование адресных вопросов для уточнения требований.",
            )
            clarification = generate_questions(
                final_findings,
                document,
                settings,
                budget=active_budget,
                generate_fn=generate_fn,
                batch_size=question_batch_size,
                max_concurrency=effective_concurrency,
            )
            incomplete_reasons.extend(clarification.incomplete_reasons)
            final_questions = clarification.questions
            logger.info(
                "run=%s stage=clarify dt=%.2fs questions=%d attempts=%d",
                active_run_id,
                time.monotonic() - t_clarify,
                len(final_questions),
                clarification.attempts,
            )
            _safe_emit(
                PipelineStage.CLARIFY,
                "completed" if not clarification.incomplete_reasons else "failed",
                f"Сформировано уточняющих вопросов: {len(final_questions)}.",
            )

            individual_requirements = indiv.individual_requirements
            audit_batches = WorkSet(
                planned=[*extraction.batches.planned, *indiv.audit_batches.planned],
                completed=[*extraction.batches.completed, *indiv.audit_batches.completed],
            )
            cross_section_groups = holistic.cross_section_groups

    # Stage 7: Coverage reconciliation, deterministic readiness decision, and canonical report.
    _safe_emit(
        PipelineStage.REPORT,
        "started",
        "Сверка покрытия, расчёт вердикта готовности и сборка канонического отчёта.",
    )
    total_attempts = (
        extraction.attempts
        + (indiv.attempts if indiv else 0)
        + (holistic.attempts if holistic else 0)
        + (verification.attempts if verification else 0)
        + (clarification.attempts if clarification else 0)
    )
    total_in = _sum_optional_tokens(
        extraction.input_tokens,
        indiv.input_tokens if indiv else None,
        holistic.input_tokens if holistic else None,
        verification.input_tokens if verification else None,
        clarification.input_tokens if clarification else None,
    )
    total_out = _sum_optional_tokens(
        extraction.output_tokens,
        indiv.output_tokens if indiv else None,
        holistic.output_tokens if holistic else None,
        verification.output_tokens if verification else None,
        clarification.output_tokens if clarification else None,
    )
    material_uncertainties = _collect_material_uncertainties(
        final_findings,
        indiv,
        holistic,
        clarification,
    )
    report = _build_report(
        document=document,
        settings=settings,
        run_id=active_run_id,
        started_at=started_at,
        started_mono=started_mono,
        agent_revision=revision,
        requirements=requirements,
        evaluations=final_evaluations,
        findings=final_findings,
        questions=final_questions,
        extraction_blocks=extraction_blocks,
        context_blocks=context_blocks,
        individual_requirements=individual_requirements,
        audit_batches=audit_batches,
        cross_section_groups=cross_section_groups,
        incomplete_reasons=incomplete_reasons,
        material_uncertainties=material_uncertainties,
        request_attempts=total_attempts,
        input_tokens=total_in,
        output_tokens=total_out,
        decide_fn=decide_fn,
    )
    _safe_emit(
        PipelineStage.REPORT,
        "completed",
        f"Отчёт готов: статус={report.coverage.execution.value}, "
        f"готовность={report.verdict.readiness.value}, "
        f"нарушений={report.statistics.violations}, вопросов={report.statistics.questions}.",
    )
    logger.info(
        "run=%s stage=done execution=%s readiness=%s attempts=%d duration=%.3fs rate_wait=%.2fs retry_wait=%.2fs",
        active_run_id,
        report.coverage.execution.value,
        report.verdict.readiness.value,
        report.metadata.request_attempts,
        report.metadata.duration_seconds,
        active_budget.rate_limit_wait_seconds,
        active_budget.retry_wait_seconds,
    )
    return report
