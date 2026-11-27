"""Russian presentation of one canonical, validated report snapshot."""
import html
from collections.abc import Iterable

from src.contracts import EvidenceRef, ValidationReport


READINESS = {"go": "Готово", "conditional_go": "Готово при выполнении условий",
             "no_go": "Не готово", "undetermined": "Недостаточно данных"}
EXECUTION = {"complete": "Полный", "partial": "Частичный", "failed": "Сбой"}
SEVERITY = {"critical": "Критическая", "major": "Существенная", "minor": "Незначительная"}
SEMANTIC = {"supported": "Подтверждено", "uncertain": "Неопределённо",
            "refuted": "Опровергнуто", "unchecked": "Проверка ожидается"}
ROLE = {"product_owner": "Владелец продукта", "analyst": "Аналитик", "architect": "Архитектор",
        "engineer": "Разработчик", "qa": "Тестировщик", "security": "Специалист по безопасности"}
CRITERION_STATUS = {"pass": "Выполнен", "fail": "Нарушен", "unknown": "Неизвестно",
                    "not_applicable": "Неприменим"}
CATEGORY = {"business": "Бизнес", "functional": "Функция", "quality": "Качество",
            "data": "Данные", "interface": "Интерфейс", "acceptance": "Приёмка",
            "governance": "Управление"}
GROUNDING = {"exact": "Точное совпадение", "normalized": "Нормализованное совпадение",
             "fuzzy_candidate": "Приблизительный кандидат", "unverified": "Не проверено"}


def _text(value: object) -> str:
    # Escape untrusted Markdown as character references, including GFM table delimiters.
    escaped = html.escape(str(value), quote=False)
    punctuation = r'\|`*_[]()#!~'
    safe = escaped.translate({ord(c): f"&#{ord(c)};" for c in punctuation})
    return "<br>".join(safe.splitlines())


def _table(headers: tuple[str, ...], rows: Iterable[Iterable[object]]) -> list[str]:
    result = ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
    for row in rows:
        result.append("| " + " | ".join(_text(value) for value in row) + " |")
    return result


def load_json(data: str | bytes) -> ValidationReport:
    return ValidationReport.model_validate_json(data)


def render_json(report: ValidationReport) -> str:
    data = report.model_dump_json(indent=2)
    load_json(data)
    return data


def render_markdown(report: ValidationReport) -> str:
    """Render a validated snapshot without contacting the provider."""
    version, meta, stats = report.document.version, report.metadata, report.statistics
    coverage = report.coverage
    references = {}

    def source(ref: EvidenceRef) -> str:
        key = (*ref.anchor, ref.grounding, ref.quote)
        if key not in references:
            references[key] = (f"E{len(references) + 1:03}", ref)
        return references[key][0]

    lines = ["# Отчёт о проверке требований", "", "## Паспорт прогона", ""]
    lines += _table(("Поле", "Значение"), [
        ("Документ", version.name), ("Дата запуска", meta.started_at.isoformat()),
        ("Происхождение", "Синтетический пример" if meta.origin == "synthetic" else "Запуск модели"),
        ("Модель", meta.model), ("Git-ревизия", meta.agent_revision), ("Версия правил", meta.prompt_version),
        ("SHA-256 документа", version.sha256), ("Прогон", meta.run_id), ("Длительность, с", meta.duration_seconds),
        ("Попытки запросов", meta.request_attempts),
        ("Входные токены", meta.input_tokens if meta.input_tokens is not None else "Нет данных"),
        ("Выходные токены", meta.output_tokens if meta.output_tokens is not None else "Нет данных"),
    ])
    lines += ["", "## Готовность к разработке и тестированию", "",
              f"**{READINESS[report.verdict.readiness]}**", "", _text(report.verdict.rationale),
              "", "## Сводка", ""]
    lines += _table(("Показатель", "Количество"), [
        ("Требования", stats.requirements), ("Подтверждённые нарушения", stats.violations),
        ("Критические", stats.critical), ("Существенные", stats.major), ("Незначительные", stats.minor),
        ("Открытые гипотезы", stats.open_hypotheses), ("Вопросы", stats.questions),
    ])
    lines += ["", "## Реестр требований", ""]
    lines += _table(("ID", "Исходный ID", "Категория", "Требование", "Источник"), (
        (r.req_id, r.original_id or "—", CATEGORY[r.category], r.summary, source(r.source))
        for r in report.requirements
    ))
    for holistic, title in ((False, "Индивидуальный аудит"), (True, "Аудит документа в целом")):
        lines += ["", f"## {title}", ""]
        lines += _table(("Объект", "Критерий", "Результат", "Обоснование", "Находки"), (
            (e.requirement_id or "Документ", e.criterion, CRITERION_STATUS[e.status],
             e.rationale, ", ".join(e.finding_ids) or "—") for e in report.evaluations
            if (e.requirement_id is None) == holistic
        ))
    critical = [f.finding_id for f in report.findings if f.confirmed and f.severity == "critical"]
    lines += ["", "## Критические дефекты", "", _text(", ".join(critical)) or "Подтверждённых критических дефектов нет.",
              "", "## Результаты проверки находок", ""]
    if not report.findings:
        lines.append("Находок нет.")
    for f in report.findings:
        lines += [f"### {_text(f.finding_id)} · {SEVERITY[f.severity]}", "", _text(f.claim), "",
                  f"**{SEMANTIC[f.semantic_status]}**. {_text(f.verification_reason)}", "",
                  f"Требования — {_text(', '.join(f.requirement_ids)) or 'весь документ'}. "
                  f"Критерии — {_text(', '.join(f.criteria))}.",
                  "", "Источники — " + (", ".join(source(ref) for ref in f.evidence) or "см. контекст пропуска") + "."]
        if f.gap:
            lines += ["", f"Ожидаемые сведения — {_text(f.gap.expected_information)}. "
                      f"Контекст — {source(f.gap.context)}. Проверены блоки {_text(', '.join(f.gap.inspected_blocks))}."]
        if f.confirmed:
            lines += ["", f"Исправление — {_text(f.recommendation)}"]
        lines.append("")
    lines += ["## Покрытие", "", f"Статус анализа — **{EXECUTION[coverage.execution]}**.", ""]
    scopes = [("Блоки", coverage.extraction_blocks), ("Требования", coverage.individual_requirements),
              ("Пакеты", coverage.audit_batches), ("Межсекционные группы", coverage.cross_section_groups)]
    coverage_rows = []
    for name, work in scopes:
        completed = set(work.completed)
        pending = ", ".join(x for x in work.planned if x not in completed)
        coverage_rows.append((name, len(completed), len(work.planned), pending or "—"))
    lines += _table(("Область", "Завершено", "Запланировано", "Не обработано"), coverage_rows)
    lines += ["", *[f"- Контекст {_text(block)} — {_text(reason)}" for block, reason in coverage.context_blocks.items()],
              *[f"- Непрочитанная страница {page}" for page in report.document.unread_pages],
              *[f"- Пустая страница {page}" for page in report.document.blank_pages],
              *[f"- Ограничение — {_text(reason)}" for reason in dict.fromkeys(report.document.diagnostics + coverage.incomplete_reasons)],
              *[f"- Неопределённость — {_text(reason)}" for reason in coverage.uncertainties],
              "", "## Вопросы для уточнения", ""]
    if not report.questions:
        lines.append("Вопросов нет.")
    for q in sorted(report.questions, key=lambda question: question.priority):
        lines += [f"### {_text(q.question_id)} · {q.priority} · {ROLE[q.stakeholder]}", "",
                  _text(q.question), "", _text(q.uncertainty), "", f"Влияние — {_text(q.business_impact)}",
                  "", f"Находки — {_text(', '.join(q.finding_ids))}. Источники — {', '.join(source(r) for r in q.evidence)}.", ""]
        lines.extend(f"- {_text(o.label)} — {_text(o.consequence)}" for o in q.options)
        lines.append("")
    lines += ["", "## Первоочередные действия", ""]
    lines.extend(f"{i}. {_text(action)}" for i, action in enumerate(report.verdict.next_actions, 1))
    if not report.verdict.next_actions:
        lines.append("Действия не указаны.")
    lines += ["", "## Исходные цитаты", ""]
    blocks = {b.block_id: b for b in version.blocks}
    rows = []
    for label, ref in references.values():
        loc = blocks[ref.block_id].location
        position = f"стр. {loc.page}" if loc.page else f"строки {loc.line_start}–{loc.line_end}"
        if loc.bbox:
            position += f", bbox {loc.bbox}"
        rows.append((label, ref.block_id, loc.section, position, f"[{ref.start}, {ref.end})", GROUNDING[ref.grounding], ref.quote))
    lines += _table(("Ссылка", "Блок", "Раздел", "Положение блока", "Смещение", "Совпадение", "Цитата"), rows)
    return "\n".join(lines).rstrip() + "\n"
