from src.contracts import EvidenceCandidate, GroundingResult, ParsedDocument


def ground(candidate: EvidenceCandidate, document: ParsedDocument) -> GroundingResult:
    """Locate a quote in its version and block. Reject stale versions with ValueError."""
    raise NotImplementedError("Evidence lookup pending (Dulat)")
