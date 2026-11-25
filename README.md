# Агент проверки требований BCC-HUB × MNU

Фаза 1 содержит контракты, синтетический отчёт, проверку подключения к Gemini и интерфейсы трёх командных модулей. Полный прогон оценки появится после интеграции этих модулей.

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

```powershell
.\.venv\Scripts\python.exe main.py --help
.\.venv\Scripts\python.exe main.py --input docs/requirements.md --output output/report.md
```

Вторая команда задаёт интерфейс будущего прогона. В фазе 1 она сообщает о незавершённом модуле и завершается с ошибкой. Официальный вход храните отдельно в `docs/requirements.md`, приватные результаты — в `output/`. Для внешней модели используйте только публичные или явно разрешённые к передаче документы. Не передавайте ключ в аргументах командной строки. [Задание организаторов](https://github.com/BCC-HUB/ai-requirements-validator-task)

Интерфейсы и таблица решений описаны в [архитектуре](docs/architecture.md). Владельцы и очередность интеграции приведены в [задачах команды](docs/team_tasks.md).
