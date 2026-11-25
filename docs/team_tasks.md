# Phase 2 handoff

Start from the same locally validated main commit. Each owner checks out the corresponding branch in a separate clone or worktree. One writer per worktree. Only the lead integrates shared contracts and requirements.txt.

| Branch / owner | Modules and tests | First integration checkpoint | Definition of Done |
|---|---|---|---|
| feat/ai-core · Meirzhan | contracts, provider, prompts, pipeline, dependencies and engineering config. Own contract/provider tests and mocked orchestration. | Run the Gemini probe with a team key, exercise extraction DTOs, then produce a real report model from Dulat's parsed input. | All required blocks and audits accounted for, failures visible, semantic verification for major/critical findings, safe request accounting and actual progress. |
| feat/ingest-verification · Dulat | ingest, evidence, readiness and main.py. Own parser, grounding, decision table and CLI tests. | Supply one real Markdown and equivalent PDF parse with addressable rows. Implement decide against the executable policy. | One-command CLI, no silent page loss, original quotes reproducible, safe partial/failure decisions and tests for lost pages, negation, numbers and units. |
| feat/ui-clarification · Parasat | report, clarification, ui, eval_samples and demo. Own rendering/UI, edit workflow and evaluation tests. | Load tests/fixtures/sample_report.json through ValidationReport, display two source passages, render Russian output. | CLI and UI show matching counts, no duplicate calls on rerun, questions lead to preview and revalidation, completed demo clearly labels provenance. |

## Order and integration

1. After the foundation is shared, the three branches can proceed concurrently. If a contract must change, coordinate on main and update tests and fixture in the same change. Keep behavior tests in owner-specific files so the three branches can merge independently.
2. First working slice on 25 November: Markdown and PDF input → grounded requirement → live audit → saved Russian report. Parasat can build against the fixture before this checkpoint.
3. On 26 November run the full set audit, anti-hallucination checks, admission of partial runs and one clarification cycle. Evaluate the official Markdown/PDF pair, an independent defective document, a clean document, embedded untrusted instructions and invalid inputs. Keep manual ground truth outside runtime.
4. On 27 November run from a fresh clone, tag the tested commit, generate the submission report and record the demo. Team target 20:00 Astana time. Published submission deadline 23:59. [Official timeline](https://bcchubagents.mnu.kz/#timeline)

## Priority

- P0 covers PDF/Markdown, one live Gemini provider, individual and holistic audits, evidence, coverage, conservative verdict, clarification questions, Russian report, reproducible CLI and submission.
- P1 covers Streamlit inspection, real progress, clarification with custom answers, editable previews, full revalidation and saved report export.
- P2 includes TXT, DOCX, OCR, extra providers and optimizations after the working MVP.

## Working locally

Use `git switch feat/ai-core` (or your assigned branch) in your own clone. For concurrent work on one machine, create a distinct worktree such as `git worktree add ../hackathon-ingest feat/ingest-verification`. Each worktree has its own ignored .env and virtual environment. Run `python -m pytest -q` before merging. Publishing and sharing the repository is the lead's separate step.
