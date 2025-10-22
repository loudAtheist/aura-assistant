# Aura Assistant — Отчёт о работе над проектом (Бета-тест, версия 5.0.6)

## Введение
**Aura Assistant** — автономный голосовой и текстовый ИИ-ассистент, развёрнутый на локальном сервере без зависимости от облачных сервисов. Его ключевая идея — управление универсальной базой данных через понимание смысла пользовательских фраз, а не через фиксированный набор команд. 【F:project_clean_dump.md†L7-L45】

Текущая цель проекта — завершение бета-тестирования ядра **Entity System** и **Semantic Core**, которые обеспечивают гибкое хранение сущностей и семантическую маршрутизацию действий. 【F:project_clean_dump.md†L17-L86】

## Состояние проекта (Aura Assistant Beta v5.0.6)
### Реализованный функционал
- Создание списков и задач с универсальной таблицей `entities` и автологированием операций. 【F:project_clean_dump.md†L49-L108】【F:project_clean_dump.md†L126-L165】
- Голосовой конвейер (OGG → WAV → распознавание речи → семантическая маршрутизация). 【F:project_clean_dump.md†L87-L118】
- Многопользовательская поддержка в `db.py` и Telegram-интерфейс в `main.py`. 【F:project_clean_dump.md†L33-L86】【F:aura_project_snapshot.txt†L1-L179】
- Автоматическое восстановление таблицы `entities` при запуске и подробное логирование базы (`db_debug.log`). 【F:project_clean_dump.md†L49-L108】【F:db_debug.log†L1-L38】

### Текущие тесты в рамках бета-этапа
- Валидация корректности JSON-ответов Semantic Core (проверка извлечения и маршрутизации действий в `main.py`). 【F:aura_project_snapshot.txt†L65-L179】
- Тестирование устойчивости к сбоям SQLite-базы и API Telegram (наблюдения в `db_debug.log` и `logs/bot.log`). 【F:db_debug.log†L1-L38】【F:logs/bot.log†L1-L120】
- Проверка точности распознавания речи в голосовом конвейере. 【F:project_clean_dump.md†L87-L118】

### Известные проблемы и наблюдения
- Конфликты Telegram polling при параллельном запуске нескольких экземпляров бота (`telegram.error.Conflict`). 【F:logs/bot.log†L1-L120】
- Сеансовый контекст хранится в памяти процесса; необходимо учитывать его сброс при рестарте и корректность загрузки переменных окружения (`session_state`, OpenAI API key). 【F:aura_project_snapshot.txt†L1-L179】
- Необработанные случаи некорректного названия списка приводят к предупреждениям (пример: «⚠️ список 'Дом' не найден»). 【F:db_debug.log†L25-L38】

## Архитектура и окружение сервера
### Инфраструктура
- **Хостинг:** Hetzner (Германия)
- **Проект:** `/opt/aura-assistant/`
- **ОС:** Ubuntu 24.04.3 LTS (ядро 6.8.0-85-generic)
- **Python:** 3.12.3 (виртуальное окружение `/opt/aura-assistant/venv/`)
- **IP:** 168.119.191.62
- **Аптайм:** ~5 дней (на момент снимка)
- **Доступные ресурсы:** 7.6 GiB RAM, 75 GiB SSD, swap отсутствует. 【F:project_clean_dump.md†L33-L86】【F:server_info.txt†L1-L75】

### Структура проекта и ключевые файлы
- `main.py` — Telegram-бот, обработчики текста и голоса, интеграция с OpenAI, маршрутизация действий. 【F:project_clean_dump.md†L17-L86】【F:aura_project_snapshot.txt†L1-L179】
- `db.py` — уровень данных для Entity System (создание/получение/обновление сущностей). 【F:project_clean_dump.md†L17-L108】
- `migrate_to_entities.py` — миграция старой схемы в универсальную таблицу `entities`. 【F:project_clean_dump.md†L21-L86】
- `start.sh` / `stop.sh` — сервисные скрипты запуска и остановки. 【F:project_clean_dump.md†L17-L86】
- `logs/` — журналы выполнения (`bot.log`, `db_debug.log`, `aura.run.log`). 【F:project_clean_dump.md†L21-L86】【F:server_info.txt†L39-L120】
- `tmp/` — временные аудиофайлы для распознавания речи. 【F:project_clean_dump.md†L17-L118】

### Конфигурация и секреты
- Настройки и ключи хранятся в `.env` (Telegram bot token, OpenAI API key, модель `gpt-4o-mini` по умолчанию, директория временных файлов). 【F:aura_project_snapshot.txt†L17-L76】
- Логирование: `aura.log` (основной журнал), `db_debug.log` (диагностика БД), `openai_raw.log` (сырые ответы модели). 【F:project_clean_dump.md†L21-L86】【F:server_info.txt†L39-L120】
- Поддерживаемые зависимости (основные): `python-telegram-bot`, `openai`, `speech_recognition`, `pydub`, `python-dotenv`, `ffmpeg-python`, `httpx`. 【F:project_clean_dump.md†L33-L86】【F:server_info.txt†L13-L42】

### Схема данных
Единая таблица `entities` хранит списки, задачи, заметки и другие типы. Поля: `id`, `user_id`, `type`, `title`, `content`, `parent_id`, `meta`, `created_at`; действует уникальный индекс по сочетанию (`user_id`, `type`, `title`, `parent_id`). 【F:project_clean_dump.md†L49-L108】【F:project_docs/aura_entity_system_blueprint.md†L29-L73】

### Semantic Core
LLM обрабатывает любую фразу пользователя, возвращая строго JSON-объект с описанием действия (`create`, `add_task`, `show_tasks`, `delete_list`, `mark_done`, `move_entity`, `update_task`, `clarify`, `say`, др.). `main.py` извлекает JSON через `extract_json_blocks()` и передаёт команды в Entity System. 【F:project_clean_dump.md†L87-L118】【F:project_docs/aura_entity_system_blueprint.md†L9-L61】【F:aura_project_snapshot.txt†L65-L179】

## Реализованные возможности и ограничения
### Возможности
- Создание и управление списками/задачами по голосу и тексту, включая массовое добавление задач. 【F:project_clean_dump.md†L87-L165】【F:aura_project_snapshot.txt†L100-L179】
- Многопользовательский режим с хранением данных на уровне Telegram-ID. 【F:project_clean_dump.md†L49-L108】【F:db_debug.log†L1-L38】
- Автологирование всех действий и восстановление таблицы `entities` при каждом запуске. 【F:project_clean_dump.md†L21-L108】【F:db_debug.log†L1-L14】
- Контекстуальное общение: учёт истории, последнего списка, профиля пользователя. 【F:aura_project_snapshot.txt†L65-L179】

### Ограничения
- Обновление и перенос задач реализованы частично и готовятся к выпуску в v5.1 (заглушки в `db.py`/`main.py`). 【F:project_clean_dump.md†L151-L165】【F:aura_project_snapshot.txt†L1-L179】
- Типы `note`, `reminder`, `idea` пока не задействованы в пользовательском интерфейсе; поддержка намечена на v5.3. 【F:project_clean_dump.md†L132-L152】
- Ошибки Telegram API (конфликты polling) требуют ручного перезапуска и внедрения очереди/вебхука. 【F:logs/bot.log†L1-L120】
- Развёртывание зависит от внешнего сервиса OpenAI; при ошибках авторизации ассистент частично деградирует. 【F:project_docs/aura_entity_system_blueprint.md†L9-L25】【F:aura_project_snapshot.txt†L17-L76】

## Планы развития (v5.1 – v6.0)
| Версия | Ключевые задачи |
|--------|-----------------|
| **v5.1** | Полноценные операции `update_entity`, `move_entity`, улучшение ошибок контекста. |
| **v5.2** | Semantic Context Engine — глубокое удержание диалогового контекста и разрешение омонимов. |
| **v5.3** | Внедрение типов `note`, `reminder`, `idea` в интерфейс и сценарии. |
| **v5.4** | Аналитика внимания и активности пользователей. |
| **v5.5** | Реализация Ebbinghaus Memory Engine (интервальные повторения). |
| **v5.6** | Клиентская синхронизация (Android, Linux-клиент). |
| **v6.0** | Полностью автономный ассистент с самообучением и адаптацией. |【F:project_clean_dump.md†L167-L189】【F:project_docs/aura_entity_system_blueprint.md†L73-L120】

## Контекст бета-теста
В бета-тесте 5.0.6 проверяются стабильность Entity System, корректность семантической маршрутизации и надёжность голосового ввода. Команда отслеживает целостность JSON-ответов, стабильность SQLite и API Telegram, а также устойчивость к пользовательским ошибкам (неизвестные списки, повторы, неточные формулировки). Акцент сделан на подтверждении концепции «понимание смысла, а не команд» перед расширением функциональности в ветке 5.1+. 【F:project_clean_dump.md†L7-L165】【F:project_docs/aura_entity_system_blueprint.md†L9-L90】【F:db_debug.log†L1-L38】【F:logs/bot.log†L1-L120】

