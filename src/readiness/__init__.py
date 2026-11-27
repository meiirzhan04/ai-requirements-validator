from src.contracts import (
    Coverage, ExecutionStatus, Finding, Readiness, SemanticStatus, Severity, Verdict, readiness_policy,
)


_RATIONALE = {
    Readiness.NO_GO: "Подтверждён критический дефект. Начало разработки и тестирования следует отложить.",
    Readiness.CONDITIONAL: "Обязательные проверки завершены. Для допуска нужны исправления существенных дефектов.",
    Readiness.GO: "Обязательные проверки завершены. Требования готовы к разработке и тестированию.",
    Readiness.UNDETERMINED: "Имеющихся результатов недостаточно для решения о готовности.",
}
_ORDER = {Severity.CRITICAL: 0, Severity.MAJOR: 1, Severity.MINOR: 2}


def decide(coverage: Coverage, findings: list[Finding], uncertainties: list[str]) -> Verdict:
    """Explain the shared policy and retain material questions on the report's coverage."""
    material = list(dict.fromkeys([*coverage.uncertainties, *uncertainties]))
    if any(not isinstance(question, str) or not question.strip() for question in material):
        raise ValueError("Material uncertainties must contain a non-empty explanation")
    readiness = readiness_policy(coverage, findings, material)
    rationale = [_RATIONALE[readiness]]
    actions = []

    if coverage.execution == ExecutionStatus.FAILED:
        rationale.append("Анализ завершился с ошибкой, пригодная оценка отсутствует.")
    elif coverage.execution == ExecutionStatus.PARTIAL:
        rationale.append("Анализ выполнен частично.")
    if coverage.incomplete_reasons:
        rationale.append("Пробелы покрытия: " + " · ".join(coverage.incomplete_reasons))

    confirmed = sorted((f for f in findings if f.confirmed), key=lambda f: _ORDER[f.severity])
    blockers = [f for f in findings if f.severity == Severity.CRITICAL and
                f.semantic_status in (SemanticStatus.UNCHECKED, SemanticStatus.UNCERTAIN)]
    for finding in confirmed:
        if finding.severity == Severity.CRITICAL:
            actions.append(f"{finding.finding_id} — {finding.recommendation}")
    if coverage.incomplete_reasons:
        actions.append("Восстановить пропущенный объём и повторить анализ.")
    for finding in blockers:
        rationale.append(f"Открытая критическая гипотеза {finding.finding_id}: {finding.verification_reason}")
        actions.append(f"Проверить гипотезу {finding.finding_id} по исходным условиям и исключениям.")
    for question in material:
        rationale.append(f"Требуется уточнение: {question}")
        actions.append(f"Согласовать с ответственным: {question}")
    for finding in confirmed:
        if finding.severity != Severity.CRITICAL:
            actions.append(f"{finding.finding_id} — {finding.recommendation}")
    if readiness == Readiness.GO:
        actions.append("Передать проверенную версию требований в разработку и тестирование.")
    verdict = Verdict(readiness=readiness, rationale=" ".join(rationale), next_actions=actions)
    coverage.uncertainties[:] = material
    return verdict
