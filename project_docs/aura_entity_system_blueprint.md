# Aura Entity System — Technical Blueprint
### Updated: 2025-10-13 • Aura v5.0.6 “Entity Intelligence Update”

Этот документ — техническое приложение к проекту **Aura Assistant**. Он фиксирует архитектуру, схему данных, работу Semantic Core (ИИ-промта), голосовой конвейер и планы развития (включая Ebbinghaus Memory Engine).

---

## 1) System Overview

- **Назначение**: голосовой/текстовый ассистент с “пониманием смысла” команд и универсальной моделью данных (Entity System).
- **Интерфейс**: Telegram (текст + voice).
- **LLM**: OpenAI `gpt-3.5-turbo` (конфиг через `.env`).
- **Хранилище**: SQLite `/opt/aura-assistant/db.sqlite3` (автокоммит).
- **Ключевая идея**: любые данные — это **сущности** (`entities`), а язык — интерфейс управления ими.

---

## 2) Runtime & Host

- **OS**: Ubuntu 24.04 LTS (noble)
- **Python**: 3.12.x (venv в `/opt/aura-assistant/venv`)
- **Основные пакеты**: `python-telegram-bot 21.4`, `openai`, `pydub`, `speech_recognition`, `python-dotenv`
- **Проект**: `/opt/aura-assistant`
- **Логи**:
  - Приложение: `/opt/aura-assistant/aura.log`
  - Отладка БД: `/opt/aura-assistant/db_debug.log`
  - Сырые ответы LLM: `/opt/aura-assistant/openai_raw.log`

---

## 3) Архитектура и взаимодействие компонентов

Telegram → main.py (Handlers)
├─ voice: OGG → WAV → STT
├─ text: handle_text()
└─ Semantic Router (LLM prompt)
↓ JSON actions
db.py (Entity Layer → SQLite)

pgsql
Copy code

`main.py` управляет жизненным циклом бота,  
`db.py` — логикой работы с сущностями (Entity System),  
`migrate_to_entities.py` — миграцией старых таблиц,  
а остальные скрипты (`start.sh`, `stop.sh`) — вспомогательные.

---

## 4) Entity System (ES Core)

Таблица `entities` — универсальное хранилище всех типов информации.

```sql
CREATE TABLE entities (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  type TEXT NOT NULL,
  title TEXT,
  content TEXT,
  parent_id INTEGER,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  meta TEXT,
  UNIQUE(user_id, type, title, parent_id)
);
Типы: list, task, note, reminder, idea.
Любая сущность может быть родителем для другой.

5) Semantic Core Prompt
Промт обучает LLM понимать смысл пользовательской фразы
и отвечать строго JSON-объектами.

Пример:

json
Copy code
{ "action": "create", "entity_type": "list", "list": "Покупки" }
{ "action": "add_task", "entity_type": "task", "list": "Покупки", "task": "Молоко" }
{ "action": "update", "entity_type": "task", "list": "Работа", "task": "Презентация", "content": "для понедельника" }
main.py парсит JSON через extract_json_blocks()
и вызывает нужные функции из db.py.

6) Голосовой модуль
Telegram → .ogg → /tmp

pydub → .wav

speech_recognition (Google Speech API) → текст

Текст → Semantic Router → db.py

Ответ пользователю.

7) Ebbinghaus Memory Engine (план)
Интеллектуальная память, основанная на кривой забывания Эббингауза.
Aura будет:

отслеживать, что пользователь изучил;

сохранять meta.review:

json
Copy code
{ "interval": 3, "next_at": "2025-10-16T09:00", "confidence": 0.8 }
напоминать по интервалам 1→3→7→14→30 дней;

адаптировать интервалы под результаты.

8) Roadmap (v5.1 → v6.0)
Версия	Нововведение
v5.1	update_entity, move_entity, content-поддержка
v5.2	Semantic Context Engine
v5.3	Новые типы note, reminder, idea
v5.4	Анализ внимания
v5.5	Ebbinghaus Memory Engine
v5.6	Клиентская синхронизация
v6.0	Адаптивное самообучение

9) Принципы
Язык = интерфейс

Сущности > таблицы

Память = повторение

Прозрачность = логи и открытая архитектура

Файл создан автоматически 2025-10-13. Версия Aura v5.0.6 “Entity Intelligence Update”.
