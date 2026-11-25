# Foundation contract, version 1

One synchronous Python core serves both entry points. Python owns identifiers, source anchors, coverage, validation, statistics and the readiness policy. Gemini handles semantic extraction, audit and clarification. Source text and answers are untrusted. A generated report is a snapshot of one document revision.

## Pipeline

1. Ingest PDF or Markdown into ordered blocks. Preserve table headings, row boundaries, code and original text. Apply 10 MiB and 100-page input limits before contacting the provider. Read in memory or clean up private temporary files. Record each unread page and explicitly identified blank page. A structural pre-scan produces signals to inspect.
2. Extract requirements per block with original identifiers where present. Each completed block has a requirement or a documented context-only disposition. Leave ambiguous or unprocessed blocks outstanding. Original identifiers can repeat. Python assigns report IDs in source order.
3. Audit each requirement against the nine individual ISO criteria. Audit the set against five set-level ISO criteria and eight IEEE criteria. Each completed scope contains exactly one evaluation per criterion. Explain unknown and inapplicable results. The catalog is defined once in src/contracts/enums.py.
4. Size batches for both input and estimated structured output, reserving 20% headroom. Repeat table headers across splits. For long documents index entities, conditions and expected outcomes, then retrieve original passages for cross-section comparisons. Track all planned batches and groups. Four or five calls is a small-document optimization target.
5. Ground quotes inside the named block. Use zero-based half-open Unicode offsets, one-based source lines/pages and optional PDF rectangles. Block IDs are stable in source order within a document digest and parser version. Normalization may fold spaces, NBSP and line endings only with an original-offset map. Store the original substring. A fuzzy candidate remains unconfirmed. An ambiguous occurrence requires a unique address.
6. A contradiction needs independent grounded spans and a scenario with incompatible outcomes. A gap records the expectation, a context anchor and inspected blocks. For major/critical claims, a separate verification pass evaluates context and exceptions and returns supported, refuted or uncertain with a reason. Share one canonical finding across overlapping criteria. Deduplicate using claim, conditions and evidence before assigning finding IDs.
7. Build owner-specific questions with finding links, evidence, business impact, options and consequences. Determine readiness using verified findings and coverage. Serialize one validated snapshot for Russian Markdown, JSON and Streamlit.

The organizer report needs a run passport (document, date, model, commit/tag), requirement inventory with original IDs or quotes, individual and set audits, counts, critical findings, readiness for development/testing and first fixes. Include citation coordinates, uncertainties and uncovered scope. Live, saved and synthetic results have distinct presentation labels. The fixture has synthetic origin and zero provider requests.

## Interfaces

| Owner | Import and call | Responsibility and error contract |
|---|---|---|
| Meirzhan | `src.pipeline.run_pipeline(document: ParsedDocument, settings: ProviderSettings, on_progress: Callable[[ProgressEvent], None] or None = None) -> ValidationReport` | Orchestrate a shareable revision. Emit events on real transitions. Encode usable partial results with coverage diagnostics. |
| Meirzhan | `src.provider.generate(stage: PipelineStage, payload: BaseModel, output_type: type[T], *, settings: ProviderSettings or None = None) -> StageResult[T]` | One structured Gemini request. Raise sanitized ProviderError on failure. Stage output uses provider DTOs. |
| Dulat | `src.ingest.parse(name: str, data: bytes) -> ParsedDocument` | Reject invalid inputs with ValueError. Keep page-level failures visible. |
| Dulat | `src.evidence.ground(candidate: EvidenceCandidate, document: ParsedDocument) -> GroundingResult` | Reject stale versions with ValueError. Return explicit status/reason for a missing or ambiguous quote. |
| Dulat | `src.readiness.decide(coverage: Coverage, findings: list[Finding], uncertainties: list[str]) -> Verdict` | Use the shared pure readiness_policy and attach Russian rationale and next actions. Persist any supplied material uncertainties in report.coverage.uncertainties. |
| Parasat | `src.report.render_markdown(report: ValidationReport) -> str` | Render one validated snapshot without network calls. Validate external JSON at its boundary. |
| Parasat | `src.clarification.propose_change(answer: Answer, source: ValidationReport) -> ProposedChange` | Resolve question, option, relevant evidence and version in the report. Reject stale/invalid references with ValueError. Default to an editable draft. Optional model assistance uses the shared generate function and a CLARIFY-stage DTO. |
| Parasat | `src.clarification.apply_change(version: DocumentVersion, change: ProposedChange) -> DocumentVersion` | Check old source text and version. Return a new revision with parent SHA. Stale or ambiguous edits raise ValueError. |

Phase 1 implements contracts, the executable decision policy and single-request provider transport. Remaining interface functions raise NotImplementedError. The integration tests check the shared call signatures. Each owner adds implementation tests within their track.

## Coverage and readiness

Apply the first matching row. Complete means all mandatory enumerated work finished, valid individual and holistic assessments exist and the input inventory was accounted for. Partial retains usable results. Failed means there is no usable assessment. An unknown criterion can reflect unavailable external information even when execution completed. Material uncertainty is recorded separately.

| Condition | Readiness |
|---|---|
| Failed execution | Undetermined |
| Confirmed critical defect, including on a partial run | No go |
| Partial execution without a confirmed blocker | Undetermined |
| Material uncertainty or an unchecked/uncertain critical hypothesis | Undetermined |
| Confirmed major defect | Conditional go |
| Otherwise, complete | Go |

tests/unit/test_readiness.py is the executable table. Report validation rejects inconsistent imported statistics and verdicts. Counts are derived from unique confirmed findings. An absence claim only covers its inspected scope.

## Provider and product boundaries

The smoke path uses one attempt and a 60-second timeout. In the pipeline, budget RPM, TPM, RPD and elapsed time using actual project limits. Count every request including retries and repairs. Allow at most two extra attempts for transient failures, with jitter and server-directed delay. Stop immediately for permanent configuration errors or exhausted daily quota. Split truncated batches, validate expected IDs, and allow at most one structured repair for a finished malformed response. Record unknown usage after transport failure. Keep SDK retries disabled so attempts remain measurable.

CLI and UI must establish an explicit cloud-data boundary before sending a real document. No shell, file or URL tools are exposed to generated content. Logs carry stage, run ID, attempt count, duration and safe error class only. A synthetic fixture is never a live fallback.

For clarification, retain the answer and proposed edit against the original digest. Apply explicitly to a working copy, then run the full pipeline. Confirm resolution against the corrected rule and its related scenarios. Keep the old report and unresolved uncertainty visible. Session-local state keys include revision, model and rules version. Filters, downloads and tab changes must not generate requests.
