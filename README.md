# Агент проверки требований BCC-HUB × MNU

Агент статической предразработочной проверки финтех-ТЗ (Markdown и PDF) с единым Python-ядром для CLI и Streamlit, строгой привязкой цитат к исходному тексту, аудитом по ISO/IEC/IEEE 29148 и IEEE 830, независимой семантической проверкой и адресными вопросами стейкхолдерам.

## Установка на Windows

Из корня репозитория в PowerShell с Python 3.12 выполните

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe -m pytest -q
```

Версии прямых зависимостей задаёт `requirements.txt`. Все офлайн-тесты работают без ключа. Для просмотра схемы и фикстуры выполните

```powershell
.\.venv\Scripts\python.exe -c "from pathlib import Path; from src.contracts import ValidationReport; r=ValidationReport.model_validate_json(Path('tests/fixtures/sample_report.json').read_bytes()); print(r.metadata.origin, r.statistics, r.verdict.readiness)"
```

## Проверка Gemini

Получите ключ своего проекта в [Google AI Studio](https://aistudio.google.com/api-keys). Создайте локальный файл командой `Copy-Item .env.example .env` при первом запуске. В редакторе укажите ключ в поле `GEMINI_API_KEY`. Можно использовать переменную среды с тем же именем. Переменные процесса имеют приоритет. Начальное значение `GEMINI_MODEL` — `gemini-3.5-flash-lite`. Доступность зависит от проекта. [Карточка модели](https://aistudio.google.com/docs/models/gemini-3.5-flash-lite)

```powershell
.\.venv\Scripts\python.exe scripts/check_gemini.py
```

Проверка отправляет один короткий синтетический запрос. PASS означает валидный структурированный ответ со значением 7, FAIL даёт ненулевой код выхода, SKIPPED указывает на отсутствие ключа и допускает офлайн-разработку. Для квот используйте показатели своего проекта. [Google Gen AI SDK](https://googleapis.github.io/python-genai/) · [Лимиты](https://ai.google.dev/gemini-api/docs/rate-limits)

## Точка входа

### CLI

```powershell
.\.venv\Scripts\python.exe main.py --help
.\.venv\Scripts\python.exe main.py --input docs/requirements.md --output output/report.md
.\.venv\Scripts\python.exe main.py --input docs/requirements.md --output output/report.md --json-output output/report.json
```

Команда формирует согласованную пару отчётов: русский Markdown (`--output`) и канонический JSON (`--json-output`, по умолчанию рядом с `--output` с расширением `.json`), не изменяя исходный документ. Коды завершения: `0` — полный анализ (`complete`), `2` — частичный пригодный отчёт (`partial`), `3` — отчёт о неуспешном анализе (`failed`), `1` — ошибка входа, конфигурации, ключа или записи. Официальный вход храните отдельно в `docs/requirements.md`, приватные результаты — в `output/`. Для внешней модели используйте только публичные или явно разрешённые к передаче документы. Не передавайте ключ в аргументах командной строки. [Задание организаторов](https://github.com/BCC-HUB/ai-requirements-validator-task)

### Streamlit UI

```powershell
.\.venv\Scripts\python.exe -m streamlit run ui/app.py
```

Интерфейсы и таблица решений описаны в [архитектуре](docs/architecture.md). Владельцы и очередность интеграции приведены в [задачах команды](docs/team_tasks.md).
