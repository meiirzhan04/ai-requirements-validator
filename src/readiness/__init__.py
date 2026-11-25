from src.contracts import Coverage, Finding, Verdict


def decide(coverage: Coverage, findings: list[Finding], uncertainties: list[str]) -> Verdict:
    """Use contracts.readiness_policy and add a Russian rationale and first actions."""
    raise NotImplementedError("Readiness explanation and prioritization pending (Dulat)")
