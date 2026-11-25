from src.contracts import Answer, DocumentVersion, ProposedChange, ValidationReport


def propose_change(answer: Answer, source: ValidationReport) -> ProposedChange:
    """Use the question, options and evidence in source. Reject stale answers with ValueError."""
    raise NotImplementedError("Clarification draft pending (Parasat)")


def apply_change(version: DocumentVersion, change: ProposedChange) -> DocumentVersion:
    """Validate version, range and original quote. Return a new working revision."""
    raise NotImplementedError("Working-copy revision pending (Parasat)")
