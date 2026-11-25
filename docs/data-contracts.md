# Canonical contract index

Import shared types from [src.contracts](../src/contracts/__init__.py). The implementation defines fields, defaults, validators and serialization. Use this index to locate the relevant boundary.

| Boundary | Source of truth | Consumer responsibility |
|---|---|---|
| Version, parsed blocks, source ranges and grounding | [source.py](../src/contracts/source.py) | Carry the digest and original Unicode offsets. Preserve PDF page accounting and Markdown line references. |
| Requirements, evaluations, findings, coverage, clarification and verdict | [audit.py](../src/contracts/audit.py) | Retain uncertainty and incomplete work. Build the explanation around the common readiness policy. |
| Quality standards, statuses and stage IDs | [enums.py](../src/contracts/enums.py) | Use the individual/set criterion catalogs and scoped statuses. |
| Provider settings, run passport, progress and typed stage output | [runtime.py](../src/contracts/runtime.py) | Emit observations from actual execution. Secrets are excluded from serialized settings. |
| Aggregate, integrity and derived statistics | [report.py](../src/contracts/report.py) | Validate imported JSON with ValidationReport.model_validate_json before displaying or exporting. |
| Model-facing structured outputs | [dto.py](../src/provider/dto.py) | Validate the provider response and assemble canonical objects in Python. |

Use the [synthetic report](../tests/fixtures/sample_report.json) for local UI and renderer work. Changes to shared fields require the lead's coordination, updated fixture, owner tests and the [aggregate contract suite](../tests/unit/test_contracts.py).
