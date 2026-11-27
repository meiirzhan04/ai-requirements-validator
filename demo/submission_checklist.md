# Комплект сдачи и фактические результаты проверки

- **Репозиторий:** https://github.com/meiirzhan04/ai-requirements-validator
- **Официальное задание:** https://github.com/BCC-HUB/ai-requirements-validator-task (SHA-256 `docs/requirements.md`: `e7dae55e5453a250d93e335f5ff9c7f775f4457ccddd333f4a67e47b20d74381`)
- **Проверенная оптимизированная ревизия:** ветка `feat/final-product-optimization`, коммиты `7b9ec33` / `c8c52da` / `1bcc38d`, модель `gemini-3.5-flash-lite`, Python 3.12.3, `188` офлайн-тестов (`pytest -q`).

## Команды воспроизведения из чистой установки

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe scripts/check_gemini.py
.\.venv\Scripts\python.exe main.py --input docs/requirements.md --output output/official_report.md
.\.venv\Scripts\python.exe -m streamlit run ui/app.py
```

## Фактические результаты живых CLI-прогонов (`gemini-3.5-flash-lite`)

| Кейс | Артефакт | Ревизия / `run_id` | Статус | Готовность | Grounding | Покрытие блоков / инд. / групп | Попытки | Токены (вх / вых) | Длительность, с |
|---|---|---|---|---|---|---|---|---|---|
| `eval_samples/defective.md` | `demo/replay_defective.{md,json}` | `7b9ec33` / `run-ad41a97167f1` | `complete` (exit `0`) | `no_go` | `2/2` (100%) | `6/6`, `3/3`, `1/1` | `7` | `5 104 / 5 288` | `15.703` (база: `26.156`) |
| `eval_samples/clean.md` | `demo/replay_clean.{md,json}` | `7b9ec33` / `run-f72385bab960` | `complete` (exit `0`) | `go` | `0/0` (`null`) | `3/3`, `2/2`, `1/1` | `3` | `3 124 / 3 207` | `6.656` (база: `9.000`) |
| `eval_samples/untrusted.md` | `demo/replay_untrusted.{md,json}` | `c8c52da` / `run-d8c4a53d355b` | `complete` (exit `0`) | `go` | `1/1` (100%) | `4/4`, `1/1`, `1/1` | `4` | `3 013 / 3 680` | `8.765` (база: `12.531`) |
| Официальный `docs/requirements.md` (без 503) | локальный бенчмарк (`pipeline-v2`) | `run-61723c283adf` | `complete` (exit `0`) | `no_go` | `11/11` (100%) | `72/72`, `53/53`, `1/1` | `14` | `37 851 / 42 746` | `42.109` (база: `186.375`) |
| Официальный `docs/requirements.md` (финал CLI) | локальный `output/official_report.{md,json}` (вне Git) | `1bcc38d` / `run-c80c1eb0fcd1` | `partial` (exit `2`, 503 на `indiv-3`) | `no_go` | `20/20` (100%) | `72/72`, `33/45`, `2/2` | `20` | `34 990 / 37 343` | `134.500` (база: `186.375`) |
| Официальный `output/eval/official.pdf` (финал CLI) | локальный `output/official_pdf_report.{md,json}` (вне Git) | `1bcc38d` / `run-80e3a1a61508` | `partial` (exit `2`, 503 на `whole-spec`) | `undetermined` | `7/7` (100%) | `49/49`, `43/43`, `1/2` | `13` | `26 849 / 34 444` | `73.204` |

Негативные CLI-проверки (`empty.md`, `corrupt.pdf`, `unsupported.txt`) завершаются кодом `1` с диагностикой на русском языке до обращения к Gemini API. Полная машиночитаемая сводка публичных кейсов: `demo/benchmark_summary.json`.

## Состав комплекта сдачи

1. Исходный вход организатора: локальный `docs/requirements.md` (хранится вне runtime-кода и не дублируется в промптах).
2. Репозиторий со всеми модулями (`src/`, `ui/`, `.streamlit/config.toml`, `main.py`, `eval_samples/`, `tests/`, `demo/`).
3. Актуальный русский отчёт агента (Markdown + канонический JSON): генерируется командой CLI в `output/official_report.{md,json}` и `output/official_pdf_report.{md,json}`; публичные резервные replay-отчёты с паспортом реального запуска — в `demo/replay_*.{md,json}`.
4. Демонстрация по сценарию `demo/demo_script.md`.

## Явные ограничения

- Семантические метрики `requirement_recall`, `finding_precision`, `finding_recall`, `critical_recall`, `false_positive_rate` и `question_usefulness` в `demo/benchmark_summary.json` оставлены равными `null`, поскольку по протоколу `eval_samples/README.md` требуют согласованной ручной разметки двух независимых рецензентов (`--review <file>.json`).
- Время выполнения и полнота живых прогонов зависят от текущей стабильности внешнего API Google Gemini (`gemini-3.5-flash-lite`): при стабильной работе API полный прогон официального ТЗ занимает **42.1 с** (`complete`), при всплесках `HTTP 503 Service Unavailable` со стороны Google система честно отрабатывает ограниченные ретраи, фиксирует неполноту и не допускает ложного вердикта `GO` (`partial/no_go` или `partial/undetermined`).
- Для слияния `feat/final-product-optimization` в `main`, закрытия Issue #15 (после ревью), фиксации релизного тега и записи итогового демонстрационного видео требуется участие команды.
