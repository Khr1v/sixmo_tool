# Sixmo Form Agent

Автоматизация прохождения формы на `https://sixmo.ru/` с поддержкой:
- многошагового сценария;
- рандомных задержек загрузки;
- перемешивания порядка вопросов;
- загрузки файла на шаге с `file`-полем;
- управления через чат-агента (LangChain + OpenAI API).

## Что умеет

- Запускает форму от старта до финального экрана.
- Возвращает итоговый идентификатор прохождения (`finalIdentifier`).
- Логирует и возвращает, какие вопросы были получены и какие ответы отправлены.
- Работает через tool-раннер (`Playwright`) и может вызываться агентом по команде вроде `выполни форму`.

## Архитектура

- `skills/sixmo-form-autofill/scripts/run_sixmo_form.py`  
  Низкоуровневый раннер формы (UI bootstrap + API шаги + multipart submit + файл).
- `agent/run_form_agent.py`  
  Чат-агент на LangChain, который вызывает tool и формирует ответ пользователю(демонстрация, что решение решение можно вызвать через `tool/skill`)
- `run_agent.py`  
  Короткая точка входа в интерактивный режим.

## Структура репозитория

```text
.
├── run_agent.py
├── agent/
│   ├── run_form_agent.py
│   └── requirements.txt
└── skills/
    └── sixmo-form-autofill/
        ├── SKILL.md
        ├── agents/openai.yaml
        └── scripts/
            ├── run_sixmo_form.py
            ├── input.example.json
            └── requirements.txt
```

## Требования

- Python 3.11+
- Google Chrome
- OpenAI API key

## Быстрый старт

1. Клонируйте репозиторий и перейдите в папку проекта.
2. Установите зависимости:

```bash
python -m pip install -r skills/sixmo-form-autofill/scripts/requirements.txt
python -m pip install -r agent/requirements.txt
python -m playwright install
```

3. Создайте `.env` в корне:

```env
OPENAI_API_KEY=ваш_openai_api_key
```

4. Подготовьте данные для формы:
- откройте `skills/sixmo-form-autofill/scripts/input.example.json`;
- заполните `answers`/`step_answers`;
- укажите корректный `file_path` (файл `.txt`, `.md` или `.json`, до 50 КБ).

5. Запустите чат-агента:

```bash
python run_agent.py
```

## Использование

Пример диалога:
- `привет`
- `выполни форму`

После выполнения агент возвращает:
- `Форма пройдена, идентификационный номер: <ID>`;
- список `вопрос -> отправленный ответ`.

## Прямой запуск tool без агента

```bash
python skills/sixmo-form-autofill/scripts/run_sixmo_form.py --input skills/sixmo-form-autofill/scripts/input.example.json --verbose
```

## Формат результата

`run_sixmo_form.py` возвращает JSON с полями:
- `ok`
- `flowId`
- `finalIdentifier`
- `completedAt`
- `submittedAnswers` (массив вопросов и отправленных ответов)
- `raw` (фрагменты API-ответов)

