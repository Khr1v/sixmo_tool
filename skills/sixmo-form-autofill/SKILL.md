
# Sixmo Form Autofill

Используйте этот тул, чтобы пройти весь сценарий на `https://sixmo.ru/` от старта до результата.

## Что делает

- Запускает новый flow через `/api/start.php`
- По умолчанию стартует через реальный Chrome UI (`Начать задание`) для прохождения проверки браузерной среды
- Ждет готовность каждого шага через polling `pending/ready` и `retryAfterMs`
- Сопоставляет ответы по `field.name` или по тексту вопроса (`label`), независимо от порядка полей
- Отправляет текстовые/select-ответы и загружает файл (`.txt/.md/.json`) на шаге 2
- Получает итоговый идентификатор из `/api/result.php`

## Запуск

Из директории скилла:

```bash
python -m pip install -r scripts/requirements.txt
```

Затем запустите:

```bash
python scripts/run_sixmo_form.py --input scripts/input.example.json --verbose
```

Если антибот отклоняет API-only старт, оставьте режим по умолчанию `--bootstrap-mode ui` с локальным Chrome.
Принудительно включить API-only режим можно так:

```bash
python scripts/run_sixmo_form.py --input /abs/path/input.json --bootstrap-mode api --verbose
```

Или с выводом в файл:

```bash
python scripts/run_sixmo_form.py --input /abs/path/input.json --output /abs/path/result.json --verbose
```

## Формат входных данных

Используйте JSON-объект со следующими ключами:

- `answers`: общий словарь ответов, ключи — `field.name` или полный текст вопроса
- `step_answers`: опциональные переопределения ответов по шагам (`"1"`, `"2"`)
- `file_path`: обязателен для file-поля, если путь не передан через answers для этого поля
- `start_payload`: опциональная подмена payload для `/api/start.php`
- `telemetry`: опциональная подмена telemetry для submit-запросов

Шаблон: `scripts/input.example.json`.

## Формат результата

JSON содержит:

- `ok`
- `flowId`
- `finalIdentifier`
- `completedAt`
- `submittedAnswers` (список вопрос/ответ по отправленным полям)
- `raw` (фрагменты API-ответов)

## Примечания

- Размер файла должен быть <= 50 КБ.
- Для select-полей ответ может быть как `label`, так и `value` опции.
- Если формулировки вопросов меняются, лучше использовать `step_answers` с точными текущими `label`.
