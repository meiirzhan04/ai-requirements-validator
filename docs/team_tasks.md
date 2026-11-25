# Team ownership and integration

| Human owner | Files and tests | Delivery checkpoint |
|---|---|---|
| Meiirzhan · @meiirzhan04 | src/contracts, src/provider, src/prompts, src/pipeline, dependencies, CI and shared configuration. Own contract/provider and pipeline tests. | Real structured response, auditable coverage, semantic verification and integrated run. |
| Dulat · @rakhyymzhan | src/ingest, src/evidence, src/readiness, main.py and corresponding parser/grounding/CLI tests. | MD/PDF with addressable rows, safe readiness explanation and a reproducible CLI report. |
| Parasat · @parasattt | src/report, src/clarification, ui, eval_samples, demo and associated tests. | Fixture-backed renderer, evidence views, stakeholder answer with revalidation, benchmark and demo. |

CODEOWNERS requests reviews from eligible collaborators. The lead coordinates product reviews until Parasat accepts write access. Each issue defines its allowed files, prerequisites, assistant and exact branch. Concurrent tasks with overlapping files must coordinate before either edits those files.

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

The three local developer branches point to the foundation commit 1032e25 and have zero unique commits at the collaboration handoff. They are eligible for retirement after each developer confirms there is no separate work. They remain local. Use the [one-issue workflow](../CONTRIBUTING.md) for current development.
