# Release acceptance

Targets are engineering thresholds. Record numerator, denominator, model, commit, document version, elapsed time and actual API attempts for each run. Record unavailable measurements explicitly. Two reviewers agree on independent ground truth before evaluating output.

| Measure | Acceptance evidence |
|---|---|
| Coverage | Map each processed block to requirements or justified context. Report skipped/unread work. Aim for ≥90% of manually annotated requirements per substantive document. |
| Findings | Target ≥85% recall on annotated critical defects and ≥80% precision on confirmed findings. Count each real issue once. Report critical misses individually. |
| Grounding | Every confirmed reference reproduces an original span. Contradictions have two independent spans and a common scenario. Refute changed negation, units or status codes. |
| Readiness | Zero false GO cases for lost pages, missing batches or mandatory failed checks. Complete clean input has no false critical blocker. Target ≤10% false alarms on annotated valid scenarios. |
| Clarification | Two reviewers check whether each question identifies a decision, owner, material trade-off and testable edit. Target ≥80% meeting all four. An answer alone never closes a finding. |
| Product | CLI and UI export the same canonical result. Rendering/filtering creates zero extra provider calls. A small official run targets five minutes when quota permits. |
| Submission | Follow the clean-install README, generate the required Russian report at the final revision, check report passport and deliver input, repository, report and demo. |

Evaluate the official Markdown and its PDF export as one semantic case in two formats, a separate defective specification, a clean specification, a document containing untrusted instructions, and invalid/empty/unsupported inputs. Include a mixed readable/unreadable PDF and provider failure in offline regression tests. Keep evaluation data and answer keys outside production rules. See [judging evidence](judging_matrix.md).
