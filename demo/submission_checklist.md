# Комплект сдачи и фактические результаты проверки

- **Репозиторий:** https://github.com/meiirzhan04/ai-requirements-validator
- **Официальное задание:** https://github.com/BCC-HUB/ai-requirements-validator-task (SHA-256 `docs/requirements.md`: `e7dae55e5453a250d93e335f5ff9c7f775f4457ccddd333f4a67e47b20d74381`)
- **Проверенная интегрированная база:** коммит `4337c57` (объединение `#1`–`#15`), модель `gemini-3.5-flash-lite`, Python 3.12.3.

## Команды воспроизведения из чистой установки

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe scripts/check_gemini.py
.\.venv\Scripts\python.exe main.py --input docs/requirements.md --output output/report.md
.\.venv\Scripts\python.exe -m streamlit run ui/app.py
```

## Фактические результаты живых прогонов (`gemini-3.5-flash-lite`)

| Кейс | Артефакт | `run_id` | Статус | Готовность | Grounding | Покрытие блоков / групп | Попытки | Длительность, с |
|---|---|---|---|---|---|---|---|---|
| `eval_samples/defective.md` | `demo/replay_defective.{md,json}` | `run-99d074504f99` | `partial` (exit `2`) | `no_go` | `2/2` (100%) | `6/6`, `1/1` (`2/3` инд.) | `5` | `26.156` |
| `eval_samples/clean.md` | `demo/replay_clean.{md,json}` | `run-c409da6fd9df` | `complete` (exit `0`) | `go` | `0/0` (`null`) | `3/3`, `1/1`, `2/2` инд. | `3` | `9.000` |
| `eval_samples/untrusted.md` | `demo/replay_untrusted.{md,json}` | `run-7592e5a01cd5` | `complete` (exit `0`) | `undetermined` | `1/1` (100%) | `4/4`, `1/1`, `1/1` инд. | `5` | `12.531` |
| Официальный `docs/requirements.md` | локальный `output/official_report.{md,json}` (вне Git) | `run-520befaba92e` | `partial` (exit `2`) | `no_go` | `50/50` (100%) | `72/72`, `7/7`, `58/59` инд. | `28` | `186.375` |

Полная машиночитаемая сводка по публичным кейсам: `demo/benchmark_summary.json`.

## Состав комплекта сдачи

1. Исходный вход организатора: локальный `docs/requirements.md` (хранится вне runtime-кода и не дублируется в промптах).
2. Репозиторий со всеми модулями (`src/`, `ui/`, `main.py`, `eval_samples/`, `tests/`, `demo/`).
3. Актуальный русский отчёт агента (Markdown + канонический JSON): генерируется командой CLI в `output/official_report.{md,json}`; публичные резервные replay-отчёты с паспортом реального запуска — в `demo/replay_*.{md,json}`.
4. Демонстрация по сценарию `demo/demo_script.md`.

## Явные ограничения

- Семантические метрики `requirement_recall`, `finding_precision`, `finding_recall`, `critical_recall`, `false_positive_rate` и `question_usefulness` в `demo/benchmark_summary.json` оставлены равными `null`, поскольку по протоколу `eval_samples/README.md` требуют согласованной ручной разметки двух независимых рецензентов (`--review <file>.json`).
- В прогонах `defective.md` и `docs/requirements.md` зафиксирован честный статус `partial` из-за строгого инварианта: модель в индивидуальном аудите вернула 1-цитатное противоречие для одного требования (`R003` и `R057`), которое система отклонила без фабрикации второй цитаты, сохранив при этом подтверждённый критический блокер (`no_go`) и полную диагностику покрытия.
- Живой PDF-прогон на `official.pdf` (подготавливается `python -m eval_samples.prepare --out output/eval --official docs/requirements.md`) выполняется дополнительно перед финальной фиксацией релизного тега на `main`.
