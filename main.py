import os, json, re, logging, asyncio
from pathlib import Path
from dotenv import load_dotenv
from telegram import Update, ReplyKeyboardMarkup, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ApplicationBuilder, MessageHandler, CallbackQueryHandler, ContextTypes, filters
import speech_recognition as sr
from pydub import AudioSegment
from openai import OpenAI
from datetime import datetime, timedelta
from typing import Any
from db import (
    rename_list, normalize_text, init_db, get_conn, get_all_lists, get_list_tasks, add_task, delete_list,
    mark_task_done, mark_task_done_fuzzy, delete_task, restore_task, find_list, fetch_task, fetch_list_by_task,
    delete_task_fuzzy, delete_task_by_index, create_list, move_entity, get_all_tasks, update_user_profile,
    get_user_profile, get_completed_tasks, get_deleted_tasks, search_tasks, update_task, update_task_by_index, restore_task_fuzzy
)

# ========= ENV =========
dotenv_path = Path(__file__).resolve().parent / ".env"
if dotenv_path.exists():
    load_dotenv(dotenv_path)
    print(f"[INFO] .env loaded from {dotenv_path}")
else:
    print(f"[WARNING] .env not found at {dotenv_path}")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-3.5-turbo")
TEMP_DIR = os.getenv("TEMP_DIR", "/opt/aura-assistant/tmp")
os.makedirs(TEMP_DIR, exist_ok=True)

if not TELEGRAM_TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN не установлен")
if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY не установлен")

LOG_DIR = Path(os.getenv("LOG_DIR", "/opt/aura-assistant"))
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_DIR / "aura.log"
RAW_LOG_FILE = LOG_DIR / "openai_raw.log"

# ========= LOG =========
logging.basicConfig(
    filename=str(LOG_FILE),
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)

client = OpenAI(api_key=OPENAI_API_KEY)

# ========= DIALOG CONTEXT (per-user) =========
SESSION: dict[int, dict] = {}  # { user_id: {"last_action": str, "last_list": str, "history": [str], "pending_delete": str, "pending_confirmation": dict} }
SIGNIFICANT_ACTIONS = {"create", "add_task", "move_entity", "mark_done", "restore_task", "delete_task", "delete_list"}
HISTORY_SKIP_ACTIONS = {"show_lists", "show_completed_tasks", "clarify", "confirm"}

LIST_ICON = "📘"
SECTION_ICON = "📋"
ALL_LISTS_ICON = "🗂"

ACTION_ICONS = {
    "add_task": "🟢",
    "create": "📘",
    "delete_list": "🗑",
    "delete_task": "🗑",
    "mark_done": "✔️",
    "move_entity": "🔄",
    "rename_list": "🆕",
    "restore_task": "♻️",
    "update_task": "🔄",
    "update_profile": "🆙",
}


def get_action_icon(action: str) -> str:
    return ACTION_ICONS.get(action, "✨")


def format_list_output(conn, user_id: int, list_name: str, heading_label: str | None = None) -> str:
    heading = heading_label or f"{SECTION_ICON} *{list_name}:*"
    tasks = get_list_tasks(conn, user_id, list_name)
    if tasks:
        lines = [f"{idx}. {title}" for idx, title in tasks]
    else:
        lines = ["_— пусто —_"]
    return f"{heading}  \n" + "\n".join(lines)


def show_all_lists(conn, user_id: int, heading_label: str | None = None) -> str:
    heading = heading_label or f"{ALL_LISTS_ICON} *Твои списки:*"
    lists = get_all_lists(conn, user_id)
    if not lists:
        return f"{heading}  \n_— пусто —_"
    body = "\n".join(f"{SECTION_ICON} {name}" for name in lists)
    return f"{heading}  \n{body}"

def set_ctx(user_id: int, **kw):
    sess = SESSION.get(
        user_id,
        {
            "history": [],
            "last_list": None,
            "last_action": None,
            "pending_delete": None,
            "pending_confirmation": None,
        },
    )
    for key, value in kw.items():
        if key == "history" and isinstance(value, list):
            seen = set()
            sess["history"] = [x for x in value[-10:] if not (x in seen or seen.add(x))]
        else:
            sess[key] = value
    SESSION[user_id] = sess
    logging.info(f"Updated context for user {user_id}: {sess}")

def get_ctx(user_id: int, key: str, default=None):
    return SESSION.get(
        user_id,
        {
            "history": [],
            "last_list": None,
            "last_action": None,
            "pending_delete": None,
            "pending_confirmation": None,
        },
    ).get(key, default)

# ========= PROMPT (Semantic Core) =========
SEMANTIC_LEXICON = {
    "task_synonyms": [
        "дело",
        "дела",
        "задача",
        "задачи",
        "пункт",
        "пункты",
        "заметка",
        "заметки",
        "напоминание",
        "напоминания",
        "TODO",
        "to-do",
    ],
    "list_synonyms": [
        "список",
        "списки",
        "лист",
        "папка",
        "категория",
        "проект",
        "трекер",
        "блокнот",
    ],
    "reminder_verbs": [
        "напомни",
        "напомнить",
        "напоминал",
        "запомни",
        "запомнить",
    ],
}

SEMANTIC_LEXICON_JSON = json.dumps(SEMANTIC_LEXICON, ensure_ascii=False)

SEMANTIC_PROMPT = """
Ты — Aura, дружелюбный и остроумный ассистент, который понимает смысл человеческих фраз и управляет локальной Entity System (списки, задачи, заметки, напоминания). Ты ведёшь себя как живой помощник: приветствуешь, поддерживаешь, шутишь к месту, переспрашиваешь, если нужно, и всегда действуешь осмысленно.

Как ты думаешь:
- Сначала подумай шаг за шагом: 1) Какое намерение? 2) Какой контекст (последний список, история)? 3) Какое действие выбрать?
- Учитывай последние сообщения (контекст: {history}), состояние базы (db_state: {db_state}) и состояние сеанса (session_state: {session_state}).
- Учитывай профиль пользователя (город, профессия): {user_profile}.
- Пользуйся семантическим словарём (синонимы сущностей и маркеры намерений): {lexicon}.
- Если пользователь говорит «туда», «в него», «этот список» — это последний упомянутый список (db_state.last_list или история).
- Приоритет точного имени списка над контекстом (например, «Домашние дела» важнее last_list).
- Слова из task_synonyms и reminder_verbs обозначают задачи/заметки/напоминания → entity_type всегда "task" и действие add_task/mark_done/delete_task/... в зависимости от намерения.
- Слова из list_synonyms обозначают списки. Если пользователь говорит «в этот блокнот» или «в этот проект», используй актуальный список (last_list или уточнённый).
- Если просит сохранить заметку/напоминание и нет явного списка, используй last_list. Если он отсутствует — уточни, следует ли создать список (например, «Напоминания»).
- Команда «Покажи список <название>» или «покажи <название>» → показать задачи (action: show_tasks, entity_type: task, list: <название>).
- Если в одной команде несколько раз встречается «список <имя>» для создания — верни отдельные действия create для каждого списка в одном JSON-массиве.
- Если список запрошен, но отсутствует в db_state.lists — верни clarify с вопросом «Списка *<имя>* нет. Создать?» и meta.pending = «<имя>».
- Если в запросе несколько задач (например, «добавь постирать ковер помыть машину»), используй ключ tasks для множественного добавления.
- Если после глагола «добавь» перечислены элементы через запятые или союз «и» (например, «добавь хлеб, молоко и сыр» или «в покупки добавь хлеб, молоко, сыр»), трактуй каждое перечисление как отдельную задачу одного действия add_task. Если список не назван явно, опирайся на db_state.last_list. Не переходи к clarify, когда намерение очевидно.
- Если в запросе несколько задач для завершения (например, «лук, морковь куплены, машина помыта»), верни JSON-ответ с ключом actions, где каждое действие — отдельный mark_done по задаче, и добавь ui_text с кратким резюме выполненного.
- Если пользователь вводит усечённое слово, но намерение однозначно читается ("спис", "удал", "добав"), интерпретируй его по контексту без дополнительного уточнения.
- Поиск задач (например, «найди задачи с договор») должен быть регистронезависимым и искать по частичному совпадению.
- Команда «Покажи удалённые задачи» → action: show_deleted_tasks, entity_type: task.
- Удаление списка требует подтверждения («да»/«нет»), после «да» список удаляется, контекст очищается.
- Восстановление задачи (например, «верни задачу») поддерживает fuzzy-поиск по частичному совпадению.
- Изменение задачи (например, «измени четвёртый пункт») поддерживает указание по индексу (meta.by_index).
- Перенос задачи (например, «перенеси задачу») поддерживает fuzzy-поиск по частичному совпадению (meta.fuzzy: true).
- Решение: create/add_task/show_lists/show_tasks/show_all_tasks/mark_done/delete_task/delete_list/move_entity/search_entity/rename_list/update_profile/restore_task/show_completed_tasks/show_deleted_tasks/update_task/unknown.
- Если социальная реплика (привет, благодарность, «как дела?») — action: say.
- Если запрос неясен — action: clarify с вопросом.
- Нормализуй вход (регистры, пробелы, ошибки речи), но сохраняй смысл.
- Для удаления списка всегда используй clarify сначала: {{ "action": "clarify", "meta": {{ "question": "Уверен, что хочешь удалить список {pending_delete}? Скажи 'да' или 'нет'.", "pending": "{pending_delete}" }} }}
- Если команда «да» и есть pending_delete в контексте, возвращай: {{ "action": "delete_list", "entity_type": "list", "list": "{pending_delete}" }}
- Никогда не обрезай JSON. Всегда полный объект.

Формат ответа (строго JSON; без текста вне JSON):
- Для действий над базой:
{{ "action": "create|add_task|show_lists|show_tasks|show_all_tasks|mark_done|delete_task|delete_list|move_entity|search_entity|rename_list|update_profile|restore_task|show_completed_tasks|show_deleted_tasks|update_task|unknown",
  "entity_type": "list|task|user_profile",
  "list": "имя списка",
  "title": "имя задачи или заметки",
  "to_list": "целевой список для переноса",
  "tasks": ["список задач для множественного добавления или завершения"],
  "meta": {{ "context_used": true, "by_index": 1, "question": "уточняющий вопрос", "reason": "причина действия", "city": "город", "profession": "профессия", "pattern": "поисковый запрос", "new_title": "новое название задачи", "fuzzy": true }} }}
- Для человеческого ответа:
{{ "action": "say", "text": "короткий дружелюбный ответ", "meta": {{ "tone": "friendly", "context_used": true }} }}
- Для уточнения:
{{ "action": "clarify", "meta": {{ "question": "вежливый уточняющий вопрос", "context_used": true }} }}

Правила поведения:
- Смысл важнее слов: распознавай намерение без триггеров.
- Контекст: «туда/там/в него» — последний список из истории или db_state.last_list.
- Позиции: «первую/вторую» — meta.by_index (1…; -1 = последняя).
- Маркеры завершения («выполнено», «сделано», «куплено») — для каждой найденной задачи формируй отдельное действие mark_done (в массиве actions, если их несколько) и используй fuzzy-поиск.
- Удаление списка требует подтверждения («да»/«нет»), после «да» список удаляется, контекст очищается.
- Социальные реплики — action: say.
- Только JSON.

Примеры:
- «Создай список Работа внеси задачи исправить договор сходить к нотариусу» → {{ "action": "create", "entity_type": "list", "list": "Работа", "tasks": ["Исправить договор", "Сходить к нотариусу"] }}
- «Создай список Работа и список Домашние дела» → [{{ "action": "create", "entity_type": "list", "list": "Работа" }}, {{ "action": "create", "entity_type": "list", "list": "Домашние дела" }}]
- «В список Домашние дела добавь постирать ковер помыть машину купить маленький нож» → {{ "action": "add_task", "entity_type": "task", "list": "Домашние дела", "tasks": ["Постирать ковер", "Помыть машину", "Купить маленький нож"] }}
- «Лук, морковь куплены, машина помыта» → {{ "actions": [ {{ "action": "mark_done", "entity_type": "task", "list": "Домашние дела", "title": "Купить лук" }}, {{ "action": "mark_done", "entity_type": "task", "list": "Домашние дела", "title": "Купить морковь" }}, {{ "action": "mark_done", "entity_type": "task", "list": "Домашние дела", "title": "Помыть машину" }} ], "ui_text": "Отмечаю: лук, морковь и машина — выполнено." }}
- «Переименуй список Покупки в Шопинг» → {{ "action": "rename_list", "entity_type": "list", "list": "Покупки", "title": "Шопинг" }}
- «Из списка Работа пункт Сделать уборку в гараже Перенеси в Домашние дела» → {{ "action": "move_entity", "entity_type": "task", "title": "Сделать уборку в гараже", "list": "Работа", "to_list": "Домашние дела", "meta": {{ "fuzzy": true }} }}
- «Сходить к нотариусу выполнен-конец» → {{ "action": "mark_done", "entity_type": "task", "list": "<последний список>", "title": "Сходить к нотариусу" }}
- «Покажи Домашние дела» → {{ "action": "show_tasks", "entity_type": "task", "list": "Домашние дела" }}
- «Покажи Домашние дела» (списка ещё нет) → {{ "action": "clarify", "meta": {{ "question": "Списка *Домашние дела* нет. Создать?", "pending": "Домашние дела" }} }}
- «Покажи все мои дела» → {{ "action": "show_all_tasks", "entity_type": "task" }}
- «Найди задачи с договор» → {{ "action": "search_entity", "entity_type": "task", "meta": {{ "pattern": "договор" }} }}
- «Покажи выполненные задачи» → {{ "action": "show_completed_tasks", "entity_type": "task" }}
- «Покажи удалённые задачи» → {{ "action": "show_deleted_tasks", "entity_type": "task" }}
- «Я живу в Алматы, работаю в продажах» → {{ "action": "update_profile", "entity_type": "user_profile", "meta": {{ "city": "Алматы", "profession": "продажи" }} }}
- «Восстанови задачу Позвонить клиенту в список Работа» → {{ "action": "restore_task", "entity_type": "task", "list": "Работа", "title": "Позвонить клиенту", "meta": {{ "fuzzy": true }} }}
- «Удали список Шопинг» → {{ "action": "clarify", "meta": {{ "question": "Уверен, что хочешь удалить список Шопинг? Скажи 'да' или 'нет'.", "pending": "Шопинг" }} }}
- «Да» (после удаления списка) → {{ "action": "delete_list", "entity_type": "list", "list": "{pending_delete}" }}
- «Измени четвёртый пункт в списке Работа на Проверить баги» → {{ "action": "update_task", "entity_type": "task", "list": "Работа", "meta": {{ "by_index": 4, "new_title": "Проверить баги" }} }}
"""

# ========= Helpers =========
def extract_json_blocks(s: str):
    try:
        data = json.loads(s)
        if isinstance(data, list):
            logging.info(f"Extracted JSON list: {data}")
            return data
        if isinstance(data, dict):
            logging.info(f"Extracted JSON dict: {data}")
            return [data]
    except Exception:
        logging.warning(f"Failed to parse JSON directly: {s[:120]}")
    blocks = re.findall(r'\{[^{}]*\{[^{}]*\}[^{}]*\}|\{[^{}]+\}', s, re.DOTALL)
    if not blocks:
        blocks = re.findall(r'\{[^{}]+\}', s, re.DOTALL)
    out = []
    for b in blocks:
        try:
            parsed = json.loads(b)
            logging.info(f"Extracted JSON block: {parsed}")
            out.append(parsed)
        except Exception:
            logging.warning(f"Skip invalid JSON block: {b[:120]}")
    return out

def wants_expand(text: str) -> bool:
    return bool(re.search(r'\b(разверну|подробн)\w*', (text or "").lower()))

def text_mentions_list_and_name(text: str):
    m = re.search(r'(?:список|лист)\s+([^\n\r]+)$', (text or "").strip(), re.IGNORECASE)
    if m:
        name = m.group(1).strip(" .!?:;«»'\"").strip()
        return name
    return None

def extract_tasks_from_question(question: str) -> list[str]:
    if not question:
        return []
    return [m.strip() for m in re.findall(r"'([^']+)'", question)]


COMPLETION_BASES: dict[str, list[str]] = {
    "куплен": ["", "а", "о", "ы"],
    "помыт": ["", "а", "о", "ы", "ый", "ая", "ое", "ые"],
    "готов": ["", "а", "о", "ы", "ый", "ая", "ое", "ые"],
    "сделан": ["", "а", "о", "ы", "ный", "ная", "ное", "ные"],
    "выполнен": ["", "а", "о", "ы", "ный", "ная", "ное", "ные"],
    "завершен": ["", "а", "о", "ы", "ный", "ная", "ное", "ные"],
    "завершён": ["", "а", "о", "ы", "ный", "ная", "ное", "ные"],
    "законч": ["ен", "ена", "ено", "ены"],
    "приготовлен": ["", "а", "о", "ы"],
    "сварен": ["", "а", "о", "ы"],
    "постиран": ["", "а", "о", "ы"],
    "уложен": ["", "а", "о", "ы"],
}

COMPLETION_WORDS = sorted({
    base + suffix
    for base, suffixes in COMPLETION_BASES.items()
    for suffix in suffixes
}, key=len, reverse=True)

COMPLETION_WORD_PATTERN = r"\b(?:" + "|".join(re.escape(word) for word in COMPLETION_WORDS) + r")\b"
COMPLETION_WORD_REGEX = re.compile(COMPLETION_WORD_PATTERN, re.IGNORECASE)
COMPLETION_SPLIT_PATTERN = re.compile(r"(?:[,;]|\bи\b|" + COMPLETION_WORD_PATTERN + r")", re.IGNORECASE)

TASK_ENTITY_SYNONYMS = {"task", "tasks", "todo", "todos", "note", "notes", "reminder", "reminders", "entry", "item"}
ACTION_SYNONYM_MAP: dict[str, tuple[str, str | None]] = {
    "add_note": ("add_task", "task"),
    "add_notes": ("add_task", "task"),
    "add_reminder": ("add_task", "task"),
    "add_reminders": ("add_task", "task"),
    "create_note": ("add_task", "task"),
    "create_notes": ("add_task", "task"),
    "create_reminder": ("add_task", "task"),
    "create_reminders": ("add_task", "task"),
    "complete_note": ("mark_done", "task"),
    "complete_notes": ("mark_done", "task"),
    "complete_reminder": ("mark_done", "task"),
    "complete_reminders": ("mark_done", "task"),
    "finish_note": ("mark_done", "task"),
    "finish_reminder": ("mark_done", "task"),
    "delete_note": ("delete_task", "task"),
    "delete_notes": ("delete_task", "task"),
    "delete_reminder": ("delete_task", "task"),
    "delete_reminders": ("delete_task", "task"),
    "remove_note": ("delete_task", "task"),
    "remove_reminder": ("delete_task", "task"),
    "restore_note": ("restore_task", "task"),
    "restore_notes": ("restore_task", "task"),
    "restore_reminder": ("restore_task", "task"),
    "restore_reminders": ("restore_task", "task"),
    "update_note": ("update_task", "task"),
    "update_reminder": ("update_task", "task"),
    "move_note": ("move_entity", "task"),
    "move_reminder": ("move_entity", "task"),
    "show_notes": ("show_tasks", "task"),
    "show_reminders": ("show_tasks", "task"),
    "list_notes": ("show_tasks", "task"),
    "list_reminders": ("show_tasks", "task"),
}


def canonicalize_action_dict(obj: dict) -> dict:
    canonical = dict(obj)
    action_name = canonical.get("action")
    if isinstance(action_name, str):
        lowered = action_name.lower()
        if lowered in ACTION_SYNONYM_MAP:
            mapped_action, default_entity = ACTION_SYNONYM_MAP[lowered]
            canonical["action"] = mapped_action
            if default_entity and not canonical.get("entity_type"):
                canonical["entity_type"] = default_entity
        else:
            canonical["action"] = lowered
    entity_type = canonical.get("entity_type")
    if isinstance(entity_type, str) and entity_type.lower() in TASK_ENTITY_SYNONYMS:
        canonical["entity_type"] = "task"
    return canonical


def extract_tasks_from_phrase(phrase: str) -> list[str]:
    if not phrase:
        return []
    raw_parts = [
        p.strip()
        for p in COMPLETION_SPLIT_PATTERN.split(phrase)
        if p and p.strip() and not COMPLETION_WORD_REGEX.fullmatch(p.strip())
    ]
    parts: list[str] = []
    for part in raw_parts:
        cleaned = COMPLETION_WORD_REGEX.sub(" ", part)
        cleaned = re.sub(r"\s+", " ", cleaned)
        cleaned = cleaned.strip(" .!?:;«»'\"")
        if cleaned:
            parts.append(cleaned)
    unique_parts: list[str] = []
    seen = set()
    for part in parts:
        lower = part.lower()
        if lower not in seen:
            seen.add(lower)
            unique_parts.append(part)
    return unique_parts if len(unique_parts) > 1 else []


def parse_completed_task_titles(text: str) -> list[str]:
    if not text or not COMPLETION_WORD_REGEX.search(text):
        return []
    potential_titles: list[str] = []
    for segment in re.split(r"[.!?]+", text):
        segment = segment.strip()
        if not segment or not COMPLETION_WORD_REGEX.search(segment):
            continue
        extracted = extract_tasks_from_phrase(segment)
        if extracted:
            potential_titles.extend(extracted)
            continue
        cleaned = COMPLETION_WORD_REGEX.sub(" ", segment)
        cleaned = re.sub(r"\b(?:и|а|но|что|же|то|уж)\b", " ", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s+", " ", cleaned)
        cleaned = cleaned.strip(" .!?:;«»'\"")
        if cleaned:
            potential_titles.append(cleaned)
    unique: list[str] = []
    seen: set[str] = set()
    for title in potential_titles:
        normalized = title.strip()
        if not normalized:
            continue
        key = normalized.lower()
        if key not in seen:
            seen.add(key)
            unique.append(normalized)
    return unique if len(unique) > 1 else []


def format_completion_summary(titles: list[str]) -> str:
    if not titles:
        return ""
    if len(titles) == 1:
        return f"Отмечаю: {titles[0]} — выполнено."
    if len(titles) == 2:
        return f"Отмечаю: {titles[0]} и {titles[1]} — выполнено."
    return f"Отмечаю: {', '.join(titles[:-1])} и {titles[-1]} — выполнено."


def normalize_action_payloads(payloads: list) -> list[dict]:
    if not payloads:
        return []
    normalized: list[dict] = []
    for obj in payloads:
        if isinstance(obj, dict) and "actions" in obj and isinstance(obj["actions"], list):
            for inner in obj["actions"]:
                if isinstance(inner, dict):
                    normalized.append(canonicalize_action_dict(inner))
            ui_text = obj.get("ui_text")
            if isinstance(ui_text, str) and ui_text.strip():
                normalized.append({
                    "action": "say",
                    "text": ui_text.strip(),
                    "meta": {"tone": "friendly", "source": "semantic_summary"},
                })
            continue
        if isinstance(obj, dict):
            normalized.append(canonicalize_action_dict(obj))
    return normalized


def _merge_mark_done_buffer(buffer: dict, tasks: list[str], meta: dict | None = None):
    seen: set[str] = buffer.setdefault("_seen", set())
    for raw in tasks or []:
        cleaned = re.sub(r"\s+", " ", (raw or "").strip())
        if not cleaned:
            continue
        key = cleaned.lower()
        if key in seen:
            continue
        seen.add(key)
        buffer.setdefault("tasks", []).append(cleaned)
    if meta:
        buffer_meta = buffer.setdefault("meta", {})
        for m_key, m_value in meta.items():
            if m_key == "fuzzy":
                buffer_meta["fuzzy"] = bool(buffer_meta.get("fuzzy") or m_value)
            elif m_key not in buffer_meta:
                buffer_meta[m_key] = m_value


def collapse_mark_done_actions(actions: list[dict]) -> list[dict]:
    if not actions:
        return []

    collapsed: list[dict] = []
    buffer: dict | None = None

    def flush_buffer():
        nonlocal buffer
        if not buffer:
            return
        if not buffer.get("tasks"):
            buffer.pop("_seen", None)
            collapsed.append(buffer)
            buffer = None
            return
        buffer.pop("_seen", None)
        if not buffer.get("meta"):
            buffer.pop("meta", None)
        collapsed.append(buffer)
        buffer = None

    for obj in actions:
        if obj.get("action") != "mark_done":
            flush_buffer()
            collapsed.append(obj)
            continue

        current_tasks: list[str] = []
        raw_tasks = obj.get("tasks") if isinstance(obj.get("tasks"), list) else []
        for t in raw_tasks:
            if isinstance(t, str):
                current_tasks.append(t)
        title_value = obj.get("title") or obj.get("task")
        if isinstance(title_value, str) and title_value.strip():
            extracted = extract_tasks_from_phrase(title_value)
            if extracted:
                current_tasks.extend(extracted)
            else:
                current_tasks.append(title_value)

        if not current_tasks:
            flush_buffer()
            collapsed.append(obj)
            continue

        list_name = obj.get("list")
        entity_type = obj.get("entity_type", "task")
        meta = obj.get("meta") if isinstance(obj.get("meta"), dict) else {}

        if buffer and buffer.get("list") == list_name and buffer.get("entity_type") == entity_type:
            _merge_mark_done_buffer(buffer, current_tasks, meta)
            continue

        flush_buffer()
        buffer = {
            "action": "mark_done",
            "entity_type": entity_type,
            "list": list_name,
            "tasks": [],
        }
        _merge_mark_done_buffer(buffer, current_tasks, meta)

    flush_buffer()
    return collapsed


def extract_task_list_from_command(command: str, list_name: str | None = None, base_title: str | None = None) -> list[str]:
    if not command:
        return []
    match = re.search(r"\bдобав[а-яё]*\b", command, flags=re.IGNORECASE)
    if not match:
        return []
    segment = command[match.end():].strip()
    if not segment:
        return []
    if list_name:
        pattern = re.compile(rf"^(?:в|во)\s+(?:список|лист)?\s*{re.escape(list_name)}\b", flags=re.IGNORECASE)
        segment = pattern.sub("", segment, count=1).strip()
    segment = re.sub(r"^(?:в|во)\s+(?:список|лист)\s+[\w\s]+", "", segment, count=1, flags=re.IGNORECASE).strip()
    segment = segment.strip(" .!?:;«»'\"")
    if not segment:
        return []
    raw_items = extract_tasks_from_phrase(segment)
    if not raw_items:
        return []
    base_clean = (base_title or "").strip()
    prefix = ""
    suffix = ""
    if base_clean:
        first_raw = raw_items[0].strip()
        if first_raw:
            idx = base_clean.lower().rfind(first_raw.lower())
            if idx != -1:
                prefix = base_clean[:idx]
                suffix = base_clean[idx + len(first_raw):]

    def apply_template(raw_value: str) -> str:
        candidate = raw_value.strip()
        if prefix or suffix:
            candidate = f"{prefix}{candidate}{suffix}"
        candidate = re.sub(r"\s+", " ", candidate).strip()
        return candidate

    tasks: list[str] = []
    for idx, raw_value in enumerate(raw_items):
        if idx == 0 and base_clean:
            candidate = base_clean
        else:
            candidate = apply_template(raw_value)
        if candidate:
            tasks.append(candidate)

    unique: list[str] = []
    seen: set[str] = set()
    for item in tasks:
        cleaned = item.strip()
        if not cleaned:
            continue
        key = cleaned.lower()
        if key not in seen:
            seen.add(key)
            unique.append(cleaned)
    return unique if len(unique) > 1 else []

def split_user_commands(text: str) -> list[str]:
    if not text:
        return []
    normalized = text.replace("\r", "\n")
    raw_parts = re.split(r'(?:[.,;]+|\n+)', normalized, flags=re.IGNORECASE)
    parts = [p.strip() for p in raw_parts if p and p.strip()]
    commands: list[str] = []
    last_create_verb: str | None = None

    for part in parts:
        lower_part = part.lower()

        create_match = re.search(r"\b(созда[ййтеь]*)\b", lower_part)
        if create_match and re.search(r"\bсписок\b", lower_part):
            last_create_verb = create_match.group(1)
            commands.append(part)
            continue

        if last_create_verb and re.match(r"^(?:список|лист)\b", lower_part):
            prefix = "создай"
            if last_create_verb:
                prefix = last_create_verb
            commands.append(f"{prefix} {part}")
            continue

        last_create_verb = None
        commands.append(part)

    expanded_commands: list[str] = []
    for command in commands:
        create_match = re.search(r"\b(созда[ййтеь]*)\b", command, flags=re.IGNORECASE)
        list_occurrences = list(re.finditer(r"(?:список|лист)\s+", command, flags=re.IGNORECASE))
        if create_match and len(list_occurrences) > 1:
            prefix = create_match.group(0)
            for idx, match in enumerate(list_occurrences):
                start = match.start()
                end = list_occurrences[idx + 1].start() if idx + 1 < len(list_occurrences) else len(command)
                fragment = command[start:end].strip()
                fragment = re.sub(r"^[,\s]+", "", fragment)
                fragment = re.sub(r"\s*(?:и|,)+\s*$", "", fragment, flags=re.IGNORECASE)
                fragment = fragment.strip(" .!?:;«»'\"")
                if fragment:
                    expanded_commands.append(f"{prefix} {fragment}".strip())
            continue
        expanded_commands.append(command.strip())

    return expanded_commands


def parse_multi_list_creation(text: str) -> list[str]:
    if not text:
        return []
    pattern = re.compile(r"\bсозда[ййтеь]*\s+списк\w*\b", re.IGNORECASE)
    match = pattern.search(text)
    if not match:
        return []
    remainder = text[match.end():].strip()
    if not remainder:
        return []
    parts = re.split(r"(?:[,;]|\bи\b)", remainder, flags=re.IGNORECASE)
    cleaned: list[str] = []
    for part in parts:
        chunk = part.strip(" .!?:;«»'\"")
        if not chunk:
            continue
        chunk = re.sub(r"^(?:список|лист)\s+", "", chunk, flags=re.IGNORECASE)
        chunk = chunk.strip(" .!?:;«»'\"")
        if chunk:
            cleaned.append(chunk)
    seen = set()
    unique: list[str] = []
    for item in cleaned:
        lowered = item.lower()
        if lowered not in seen:
            seen.add(lowered)
            unique.append(item)
    return unique if len(unique) > 1 else []


def build_semantic_state(conn, user_id: int, history: list[str] | None = None) -> tuple[dict, dict]:
    lists = get_all_lists(conn, user_id)
    list_tasks: dict[str, list[str]] = {}
    for name in lists:
        try:
            tasks = get_list_tasks(conn, user_id, name)
        except Exception:
            tasks = []
        list_tasks[name] = [title for _, title in tasks[:10]]

    last_list = get_ctx(user_id, "last_list")
    last_action = get_ctx(user_id, "last_action")
    pending_delete = get_ctx(user_id, "pending_delete")
    pending_confirmation = get_ctx(user_id, "pending_confirmation")

    db_state = {
        "lists": list_tasks,
        "last_list": last_list,
        "pending_delete": pending_delete,
        "total_lists": len(lists),
        "total_tasks": sum(len(items) for items in list_tasks.values()),
    }

    session_state: dict[str, Any] = {
        "last_action": last_action,
    }
    if pending_confirmation:
        session_state["pending_confirmation"] = pending_confirmation
    if history:
        session_state["recent_history"] = history[-5:]
    if last_list:
        session_state["focus_list"] = {
            "name": last_list,
            "tasks": list_tasks.get(last_list, []),
        }

    recent_tasks = get_all_tasks(conn, user_id)
    if recent_tasks:
        session_state["recent_tasks"] = [
            {"list": list_name, "title": title}
            for list_name, title in recent_tasks[:10]
        ]

    return db_state, session_state


async def perform_create_list(target: Any, conn, user_id: int, list_name: str, tasks: list[str] | None = None) -> bool:
    try:
        logging.info(f"Creating list: {list_name}")
        create_list(conn, user_id, list_name)
        action_icon = get_action_icon("create")
        header = f"{action_icon} Создан новый список {LIST_ICON} *{list_name}*."
        details = None
        if tasks:
            added_tasks: list[str] = []
            for task in tasks:
                task_id = add_task(conn, user_id, list_name, task)
                if task_id:
                    added_tasks.append(task)
            if added_tasks:
                add_icon = get_action_icon("add_task")
                details = "\n".join(f"{add_icon} {task}" for task in added_tasks)
            else:
                details = f"⚠️ Задачи уже были в {LIST_ICON} *{list_name}*."
        list_block = format_list_output(conn, user_id, list_name, heading_label=f"{SECTION_ICON} *Актуальный список:*")
        message_obj = getattr(target, "message", None)
        if message_obj is None:
            message_obj = target
        if details:
            message = f"{header}  \n{details}\n\n{list_block}"
        else:
            message = f"{header}  \n\n{list_block}"
        await message_obj.reply_text(message, parse_mode="Markdown")
        set_ctx(user_id, last_action="create_list", last_list=list_name)
        return True
    except Exception as e:
        logging.exception(f"Create list error: {e}")
        message_obj = getattr(target, "message", None)
        if message_obj is None:
            message_obj = target
        await message_obj.reply_text("⚠️ Не удалось создать список. Проверь логи.")
        return False

def map_tasks_to_lists(conn, user_id: int, task_titles: list[str]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    if not task_titles:
        return mapping
    lowered_targets = {title.lower(): title for title in task_titles}
    for list_name in get_all_lists(conn, user_id):
        items = [t.lower() for _, t in get_list_tasks(conn, user_id, list_name)]
        for raw_lower, original in lowered_targets.items():
            if raw_lower in items and original not in mapping:
                mapping[original] = list_name
    return mapping

async def handle_pending_confirmation(message, context: ContextTypes.DEFAULT_TYPE, conn, user_id: int, pending_confirmation: dict) -> bool:
    if not pending_confirmation:
        return False
    conf_type = pending_confirmation.get("type")
    if conf_type == "delete_tasks":
        tasks = pending_confirmation.get("tasks") or []
        if not tasks:
            await message.reply_text("⚠️ Нет задач для удаления.")
            set_ctx(user_id, pending_confirmation=None)
            return False
        base_list = pending_confirmation.get("list") or get_ctx(user_id, "last_list")
        task_to_list = {task: base_list for task in tasks if base_list}
        if not base_list:
            task_to_list.update(map_tasks_to_lists(conn, user_id, tasks))
        deleted_entries = []
        failed_entries = []
        for task in tasks:
            target_list = task_to_list.get(task)
            if not target_list:
                failed_entries.append((None, task))
                continue
            deleted, matched = delete_task_fuzzy(conn, user_id, target_list, task)
            if deleted:
                deleted_entries.append((target_list, matched or task))
            else:
                failed_entries.append((target_list, task))
        messages = []
        if deleted_entries:
            grouped: dict[str, list[str]] = {}
            for list_name, title in deleted_entries:
                grouped.setdefault(list_name, []).append(title)
            parts = [f"*{ln}*: {', '.join(titles)}" for ln, titles in grouped.items()]
            messages.append("🗑 Удалено: " + "; ".join(parts))
            last_list_value = deleted_entries[-1][0]
            set_ctx(user_id, last_action="delete_task", last_list=last_list_value)
        if failed_entries:
            parts = []
            for list_name, title in failed_entries:
                if list_name:
                    parts.append(f"*{title}* в *{list_name}*")
                else:
                    parts.append(f"*{title}*")
            messages.append("⚠️ Не удалось удалить: " + ", ".join(parts))
        if messages:
            await message.reply_text("\n".join(messages), parse_mode="Markdown")
        else:
            await message.reply_text("⚠️ Не удалось обработать удаление задач.")
        set_ctx(user_id, pending_confirmation=None)
        return bool(deleted_entries)
    elif conf_type == "create_list":
        list_to_create = pending_confirmation.get("list")
        if not list_to_create:
            await message.reply_text("⚠️ Не понимаю, какой список создать.")
            set_ctx(user_id, pending_confirmation=None)
            return False
        existing = find_list(conn, user_id, list_to_create)
        if existing:
            await message.reply_text(f"⚠️ Список *{list_to_create}* уже существует.", parse_mode="Markdown")
            set_ctx(user_id, pending_confirmation=None, last_list=list_to_create)
            return False
        handled = await perform_create_list(message, conn, user_id, list_to_create)
        set_ctx(user_id, pending_confirmation=None)
        return handled
    else:
        await message.reply_text("⚠️ Не удалось обработать подтверждение. Попробуй сформулировать команду заново.")
        set_ctx(user_id, pending_confirmation=None)
        return False

async def send_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [["Показать списки", "Создать список"], ["Добавить задачу", "Помощь"]]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True, selective=True)
    await update.message.reply_text("Выбери действие или напиши/скажи:", reply_markup=reply_markup)

async def expand_all_lists(update: Update, conn, user_id: int, context: ContextTypes.DEFAULT_TYPE):
    lists = get_all_lists(conn, user_id)
    if not lists:
        await update.message.reply_text(
            f"{ALL_LISTS_ICON} *Твои списки:*  \n_— пусто —_",
            parse_mode="Markdown",
        )
        return
    overview = "\n".join(f"{SECTION_ICON} {name}" for name in lists)
    detailed_blocks = [
        format_list_output(conn, user_id, name, heading_label=f"{SECTION_ICON} *{name}:*")
        for name in lists
    ]
    message = f"{ALL_LISTS_ICON} *Твои списки:*  \n{overview}\n\n" + "\n\n".join(detailed_blocks)
    await update.message.reply_text(message, parse_mode="Markdown")
    set_ctx(user_id, last_action="show_lists")


def merge_add_task_with_clarify(actions: list, user_id: int, original_text: str) -> list:
    if not actions:
        return actions
    add_indices = [idx for idx, obj in enumerate(actions) if obj.get("action") == "add_task"]
    if len(add_indices) != 1:
        return actions
    add_index = add_indices[0]
    clarifies_after = [idx for idx in range(add_index + 1, len(actions)) if actions[idx].get("action") == "clarify"]
    if not clarifies_after:
        return actions
    if any(actions[idx].get("action") != "clarify" for idx in range(add_index + 1, len(actions))):
        return actions
    base_action = dict(actions[add_index])
    tasks_field = base_action.get("tasks") if isinstance(base_action.get("tasks"), list) else None
    base_tasks: list[str] = []
    if tasks_field:
        base_tasks = [t for t in tasks_field if isinstance(t, str) and t.strip()]
    title_field = base_action.get("title")
    if isinstance(title_field, str) and title_field.strip():
        if not base_tasks:
            base_tasks = [title_field.strip()]
        elif title_field.strip().lower() not in {t.lower() for t in base_tasks}:
            base_tasks.insert(0, title_field.strip())
    if not base_tasks:
        return actions
    list_name = base_action.get("list") or get_ctx(user_id, "last_list")
    extracted = extract_task_list_from_command(original_text, list_name, base_tasks[0])
    if not extracted or len(extracted) <= len(base_tasks):
        return actions
    combined: list[str] = []
    seen: set[str] = set()
    for task in extracted:
        cleaned = task.strip()
        if not cleaned:
            continue
        key = cleaned.lower()
        if key not in seen:
            seen.add(key)
            combined.append(cleaned)
    if len(combined) <= len(base_tasks):
        return actions
    base_action["list"] = list_name or base_action.get("list")
    base_action.pop("title", None)
    base_action["tasks"] = combined
    new_actions: list = []
    for idx, obj in enumerate(actions):
        if idx == add_index:
            new_actions.append(base_action)
        elif idx in clarifies_after:
            continue
        else:
            new_actions.append(obj)
    logging.info(
        "Merged add_task with clarifications: %s", json.dumps(base_action, ensure_ascii=False)
    )
    return new_actions


async def route_actions(update: Update, context: ContextTypes.DEFAULT_TYPE, actions: list, user_id: int, original_text: str) -> list[str]:
    conn = get_conn()
    logging.info(f"Processing actions: {json.dumps(actions)}")
    normalized_actions = normalize_action_payloads(actions)
    normalized_actions = collapse_mark_done_actions(normalized_actions)
    actions = merge_add_task_with_clarify(normalized_actions, user_id, original_text)
    executed_actions: list[str] = []
    pending_delete = get_ctx(user_id, "pending_delete")
    if original_text.lower() in ["да", "yes"] and pending_delete:
        try:
            logging.info(f"Deleting list: {pending_delete}")
            deleted = delete_list(conn, user_id, pending_delete)
            if deleted:
                await update.message.reply_text(f"🗑 Список *{pending_delete}* удалён.", parse_mode="Markdown")
                set_ctx(user_id, pending_delete=None, last_list=None)
                logging.info(f"Confirmed delete_list: {pending_delete}")
                executed_actions.append("delete_list")
            else:
                await update.message.reply_text(f"⚠️ Список *{pending_delete}* не найден.")
                set_ctx(user_id, pending_delete=None)
            return executed_actions
        except Exception as e:
            logging.exception(f"Delete error: {e}")
            await update.message.reply_text("⚠️ Ошибка удаления.")
            set_ctx(user_id, pending_delete=None)
            return executed_actions
    elif original_text.lower() in ["нет", "no"] and pending_delete:
        await update.message.reply_text("Удаление отменено.")
        set_ctx(user_id, pending_delete=None)
        return executed_actions
    for obj in actions:
        action = obj.get("action", "unknown")
        entity_type = obj.get("entity_type", "task")
        list_name = obj.get("list") or get_ctx(user_id, "last_list")
        title = obj.get("title") or obj.get("task")
        meta = obj.get("meta", {})
        logging.info(f"Action: {action}, Entity: {entity_type}, List: {list_name}, Title: {title}")
        if action not in ["delete_list", "clarify"] and get_ctx(user_id, "pending_delete"):
            set_ctx(user_id, pending_delete=None)
        if action != "clarify" and get_ctx(user_id, "pending_confirmation"):
            set_ctx(user_id, pending_confirmation=None)
        if list_name == "<последний список>":
            list_name = get_ctx(user_id, "last_list")
            logging.info(f"Resolved placeholder to last_list: {list_name}")
            if not list_name:
                logging.warning("No last_list in context, asking for clarification")
                await update.message.reply_text("🤔 Уточни, в какой список добавить задачу.")
                await send_menu(update, context)
                continue
        if action in ("unknown", None):
            if wants_expand(original_text) and get_ctx(user_id, "last_action") == "show_lists":
                await expand_all_lists(update, conn, user_id, context)
                continue
            name_from_text = text_mentions_list_and_name(original_text)
            if name_from_text:
                list_name = name_from_text
                action = "show_tasks"
                entity_type = "task"
                logging.info(f"Fallback to show_tasks for list: {list_name}")
        if action == "create" and entity_type == "list" and obj.get("list"):
            handled = await perform_create_list(update, conn, user_id, obj["list"], obj.get("tasks"))
            if handled:
                executed_actions.append("create")
        elif action == "create_multiple" and entity_type == "list":
            created_any = False
            for list_title in obj.get("lists", []) or []:
                list_clean = (list_title or "").strip()
                if not list_clean:
                    continue
                handled = await perform_create_list(update, conn, user_id, list_clean)
                if handled:
                    created_any = True
            if created_any:
                executed_actions.append("create")
        elif action == "add_task" and list_name:
            try:
                logging.info(f"Adding tasks to list: {list_name}")
                action_icon = get_action_icon("add_task")
                message_parts: list[str] = []
                if obj.get("tasks"):
                    added_tasks: list[str] = []
                    for t in obj["tasks"]:
                        task_id = add_task(conn, user_id, list_name, t)
                        if task_id:
                            added_tasks.append(t)
                    if added_tasks:
                        details = "\n".join(f"{action_icon} {task}" for task in added_tasks)
                        message_parts.append(f"{action_icon} Добавлены задачи в {LIST_ICON} *{list_name}:*  \n{details}")
                    else:
                        message_parts.append(f"⚠️ Все указанные задачи уже есть в {LIST_ICON} *{list_name}*.")
                elif title:
                    task_id = add_task(conn, user_id, list_name, title)
                    if task_id:
                        message_parts.append(f"{action_icon} Добавлено в {LIST_ICON} *{list_name}:*  \n{action_icon} {title}")
                    else:
                        message_parts.append(f"⚠️ Задача *{title}* уже есть в {LIST_ICON} *{list_name}*.")
                if message_parts:
                    list_block = format_list_output(conn, user_id, list_name, heading_label=f"{SECTION_ICON} *Актуальный список:*")
                    message_parts.append(list_block)
                    await update.message.reply_text("\n\n".join(message_parts), parse_mode="Markdown")
                set_ctx(user_id, last_action="add_task", last_list=list_name)
                executed_actions.append("add_task")
            except Exception as e:
                logging.exception(f"Add task error: {e}")
                await update.message.reply_text("⚠️ Не удалось добавить задачу. Проверь логи.")
        elif action == "show_lists":
            try:
                logging.info("Showing all lists with tasks")
                await expand_all_lists(update, conn, user_id, context)
            except Exception as e:
                logging.exception(f"Show lists error: {e}")
                await update.message.reply_text("⚠️ Не удалось получить списки. Проверь логи.")
        elif action == "show_tasks" and list_name:
            try:
                logging.info(f"Showing tasks for list: {list_name}")
                if not find_list(conn, user_id, list_name):
                    question = f"⚠️ Списка *{list_name}* нет. Создать?"
                    keyboard = [[
                        InlineKeyboardButton("Да", callback_data=f"create_list_yes:{list_name}"),
                        InlineKeyboardButton("Нет", callback_data="create_list_no"),
                    ]]
                    reply_markup = InlineKeyboardMarkup(keyboard)
                    await update.message.reply_text(question, parse_mode="Markdown", reply_markup=reply_markup)
                    set_ctx(
                        user_id,
                        pending_confirmation={
                            "type": "create_list",
                            "list": list_name,
                            "question": question,
                        },
                        pending_delete=None,
                    )
                    continue
                items = get_list_tasks(conn, user_id, list_name)
                message = format_list_output(conn, user_id, list_name, heading_label=f"{SECTION_ICON} *{list_name}:*")
                await update.message.reply_text(message, parse_mode="Markdown")
                set_ctx(user_id, last_action="show_tasks", last_list=list_name)
            except Exception as e:
                logging.exception(f"Show tasks error: {e}")
                await update.message.reply_text("⚠️ Не удалось получить задачи. Проверь логи.")
        elif action == "show_all_tasks":
            try:
                logging.info("Showing all tasks")
                lists = get_all_lists(conn, user_id)
                if not lists:
                    await update.message.reply_text(
                        f"{ALL_LISTS_ICON} *Все твои дела:*  \n_— пусто —_",
                        parse_mode="Markdown",
                    )
                    set_ctx(user_id, last_action="show_all_tasks")
                    continue
                blocks = [
                    format_list_output(conn, user_id, n, heading_label=f"{SECTION_ICON} *{n}:*")
                    for n in lists
                ]
                message = f"{ALL_LISTS_ICON} *Все твои дела:*\n\n" + "\n\n".join(blocks)
                await update.message.reply_text(message, parse_mode="Markdown")
                set_ctx(user_id, last_action="show_all_tasks")
            except Exception as e:
                logging.exception(f"Show all tasks error: {e}")
                await update.message.reply_text("⚠️ Не удалось получить дела. Проверь логи.")
        elif action == "show_completed_tasks":
            try:
                logging.info("Showing completed tasks")
                tasks = get_completed_tasks(conn, user_id, limit=15)
                if tasks:
                    lines = []
                    for list_title, task_title in tasks:
                        list_display = list_title or "Без списка"
                        lines.append(f"✅ *{list_display}*: {task_title}")
                    header = "✅ Выполненные задачи (последние 15):\n"
                    await update.message.reply_text(header + "\n".join(lines), parse_mode="Markdown")
                else:
                    await update.message.reply_text("Пока нет выполненных задач 💤")
                set_ctx(user_id, last_action="show_completed_tasks")
            except Exception as e:
                logging.exception(f"Show completed tasks error: {e}")
                await update.message.reply_text("⚠️ Не удалось получить выполненные задачи. Проверь логи.")
        elif action == "show_deleted_tasks":
            try:
                logging.info("Showing deleted tasks")
                tasks = get_deleted_tasks(conn, user_id, limit=15)
                if tasks:
                    lines = []
                    for list_title, task_title in tasks:
                        list_display = list_title or "Без списка"
                        lines.append(f"🗑 *{list_display}*: {task_title}")
                    header = "🗑 Удалённые задачи (последние 15):\n"
                    await update.message.reply_text(header + "\n".join(lines), parse_mode="Markdown")
                else:
                    await update.message.reply_text("Пока нет удалённых задач ✨")
                set_ctx(user_id, last_action="show_deleted_tasks")
            except Exception as e:
                logging.exception(f"Show deleted tasks error: {e}")
                await update.message.reply_text("⚠️ Не удалось получить удалённые задачи. Проверь логи.")
        elif action == "search_entity" and meta.get("pattern"):
            try:
                logging.info(f"Searching tasks with pattern: {meta['pattern']}")
                tasks = search_tasks(conn, user_id, meta["pattern"])
                if tasks:
                    txt = "🗂 Найденные задачи:\n"
                    for list_title, task_title in tasks:
                        txt += f"📋 *{list_title}*: {task_title}\n"
                    await update.message.reply_text(txt, parse_mode="Markdown")
                else:
                    await update.message.reply_text(f"Задачи с '{meta['pattern']}' не найдены.")
                set_ctx(user_id, last_action="search_entity")
            except Exception as e:
                logging.exception(f"Search tasks error: {e}")
                await update.message.reply_text("⚠️ Не удалось найти задачи. Проверь логи.")
        elif action == "delete_task":
            try:
                ln = list_name or get_ctx(user_id, "last_list")
                if not ln:
                    logging.info("No list name provided for delete_task")
                    await update.message.reply_text("🤔 Уточни, из какого списка удалить.")
                    await send_menu(update, context)
                    continue
                if meta.get("by_index"):
                    logging.info(f"Deleting task by index: {meta['by_index']} in list: {ln}")
                    deleted, matched = delete_task_by_index(conn, user_id, ln, meta["by_index"])
                else:
                    logging.info(f"Deleting task fuzzy: {title} in list: {ln}")
                    deleted, matched = delete_task_fuzzy(conn, user_id, ln, title)
                if deleted:
                    action_icon = get_action_icon("delete_task")
                    task_name = matched or title or "задача"
                    header = f"{action_icon} Удалено из {LIST_ICON} *{ln}:*"
                    details = f"{action_icon} {task_name}"
                    list_block = format_list_output(conn, user_id, ln, heading_label=f"{SECTION_ICON} *{ln}:*")
                    message = f"{header}  \n{details}\n\n{list_block}"
                    await update.message.reply_text(message, parse_mode="Markdown")
                else:
                    await update.message.reply_text("⚠️ Задача не найдена или уже выполнена.")
                set_ctx(user_id, last_action="delete_task", last_list=ln)
            except Exception as e:
                logging.exception(f"Delete task error: {e}")
                await update.message.reply_text("⚠️ Не удалось удалить задачу. Проверь логи.")
        elif action == "delete_list" and entity_type == "list" and list_name:
            try:
                pending_delete = get_ctx(user_id, "pending_delete")
                if pending_delete == list_name and original_text.lower() in ["да", "yes"]:
                    logging.info(f"Deleting list: {list_name}")
                    deleted = delete_list(conn, user_id, list_name)
                    if deleted:
                        remaining = show_all_lists(conn, user_id, heading_label=f"{ALL_LISTS_ICON} *Оставшиеся списки:*")
                        message = f"{get_action_icon('delete_list')} Список *{list_name}* удалён.  \n\n{remaining}"
                        await update.message.reply_text(message, parse_mode="Markdown")
                        set_ctx(user_id, last_action="delete_list", last_list=None, pending_delete=None)
                        executed_actions.append("delete_list")
                    else:
                        await update.message.reply_text(f"⚠️ Список *{list_name}* не найден.")
                        set_ctx(user_id, pending_delete=None)
                elif pending_delete == list_name and original_text.lower() in ["нет", "no"]:
                    await update.message.reply_text("Хорошо, отмена удаления.")
                    set_ctx(user_id, pending_delete=None)
                else:
                    keyboard = [[InlineKeyboardButton("Да", callback_data=f"delete_list:{list_name}"), InlineKeyboardButton("Нет", callback_data="cancel_delete")]]
                    reply_markup = InlineKeyboardMarkup(keyboard)
                    await update.message.reply_text(f"🤔 Уверен, что хочешь удалить список *{list_name}*?", parse_mode="Markdown", reply_markup=reply_markup)
                    set_ctx(user_id, pending_delete=list_name)
            except Exception as e:
                logging.exception(f"Delete list error: {e}")
                await update.message.reply_text("⚠️ Не удалось удалить список. Проверь логи.")
                set_ctx(user_id, pending_delete=None)
        elif action == "mark_done" and list_name:
            try:
                logging.info(f"Marking tasks done in list: {list_name}")
                tasks_to_mark: list[str] = []
                if obj.get("tasks"):
                    tasks_to_mark = list(obj["tasks"])
                elif title:
                    multi = extract_tasks_from_phrase(title)
                    if multi:
                        tasks_to_mark = multi
                    else:
                        tasks_to_mark = [title]
                completed_tasks: list[str] = []
                for task_phrase in tasks_to_mark:
                    logging.info(f"Marking task done: {task_phrase} in list: {list_name}")
                    deleted, matched = mark_task_done_fuzzy(conn, user_id, list_name, task_phrase)
                    if deleted:
                        completed_tasks.append(matched)
                if completed_tasks:
                    action_icon = get_action_icon("mark_done")
                    details = "\n".join(f"{action_icon} {task}" for task in completed_tasks)
                    header = f"{action_icon} Готово в {LIST_ICON} *{list_name}:*"
                    list_block = format_list_output(conn, user_id, list_name, heading_label=f"{SECTION_ICON} *{list_name}:*")
                    message = f"{header}  \n{details}\n\n{list_block}"
                    await update.message.reply_text(message, parse_mode="Markdown")
                    executed_actions.append("mark_done")
                elif tasks_to_mark:
                    await update.message.reply_text("⚠️ Не нашёл указанные задачи.")
                elif title:
                    logging.info(f"Marking task done: {title} in list: {list_name}")
                    deleted, matched = mark_task_done_fuzzy(conn, user_id, list_name, title)
                    if deleted:
                        action_icon = get_action_icon("mark_done")
                        header = f"{action_icon} Готово в {LIST_ICON} *{list_name}:*"
                        details = f"{action_icon} {matched}"
                        list_block = format_list_output(conn, user_id, list_name, heading_label=f"{SECTION_ICON} *{list_name}:*")
                        message = f"{header}  \n{details}\n\n{list_block}"
                        await update.message.reply_text(message, parse_mode="Markdown")
                        executed_actions.append("mark_done")
                    else:
                        await update.message.reply_text("⚠️ Не нашёл такую задачу.")
                set_ctx(user_id, last_action="mark_done", last_list=list_name)
            except Exception as e:
                logging.exception(f"Mark done error: {e}")
                await update.message.reply_text("⚠️ Не удалось отметить задачу. Проверь логи.")
        elif action == "rename_list" and entity_type == "list" and list_name and title:
            try:
                logging.info(f"Renaming list: {list_name} to {title}")
                renamed = rename_list(conn, user_id, list_name, title)
                if renamed:
                    await update.message.reply_text(f"🆕 Список *{list_name}* переименован в *{title}*.", parse_mode="Markdown")
                    set_ctx(user_id, last_action="rename_list", last_list=title)
                else:
                    await update.message.reply_text(f"⚠️ Список *{list_name}* не найден или *{title}* уже существует.")
            except Exception as e:
                logging.exception(f"Rename list error: {e}")
                await update.message.reply_text("⚠️ Не удалось переименовать список. Проверь логи.")
        elif action == "move_entity" and entity_type and title and obj.get("list") and obj.get("to_list"):
            try:
                logging.info(f"Moving {entity_type} '{title}' from {obj['list']} to {obj['to_list']}")
                list_exists = find_list(conn, user_id, obj["list"])
                to_list_exists = find_list(conn, user_id, obj["to_list"])
                if not list_exists:
                    await update.message.reply_text(f"⚠️ Список *{obj['list']}* не найден.")
                    continue
                if not to_list_exists:
                    logging.info(f"Creating target list '{obj['to_list']}' for user {user_id}")
                    create_list(conn, user_id, obj["to_list"])
                if meta.get("fuzzy"):
                    logging.info(f"Moving task fuzzy: {title} from {obj['list']} to {obj['to_list']}")
                    tasks = get_list_tasks(conn, user_id, obj["list"])
                    matched = None
                    for _, task_title in tasks:
                        if title.lower() in task_title.lower():
                            matched = task_title
                            break
                    if matched:
                        updated = move_entity(conn, user_id, entity_type, matched, obj["to_list"])
                        if updated:
                            action_icon = get_action_icon("move_entity")
                            header = f"{action_icon} Перемещено: *{matched}* → в {LIST_ICON} *{obj['to_list']}*"
                            list_block = format_list_output(
                                conn,
                                user_id,
                                obj["to_list"],
                                heading_label=f"{SECTION_ICON} *{obj['to_list']}:*",
                            )
                            message = f"{header}  \n\n{list_block}"
                            await update.message.reply_text(message, parse_mode="Markdown")
                            set_ctx(user_id, last_action="move_entity", last_list=obj["to_list"])
                            executed_actions.append("move_entity")
                        else:
                            await update.message.reply_text(f"⚠️ Не удалось переместить *{matched}*. Проверь, есть ли такая задача.")
                    else:
                        await update.message.reply_text(f"⚠️ Задача *{title}* не найдена в *{obj['list']}*.")
                else:
                    updated = move_entity(conn, user_id, entity_type, title, obj["to_list"])
                    if updated:
                        action_icon = get_action_icon("move_entity")
                        header = f"{action_icon} Перемещено: *{title}* → в {LIST_ICON} *{obj['to_list']}*"
                        list_block = format_list_output(
                            conn,
                            user_id,
                            obj["to_list"],
                            heading_label=f"{SECTION_ICON} *{obj['to_list']}:*",
                        )
                        message = f"{header}  \n\n{list_block}"
                        await update.message.reply_text(message, parse_mode="Markdown")
                        set_ctx(user_id, last_action="move_entity", last_list=obj["to_list"])
                        executed_actions.append("move_entity")
                    else:
                        await update.message.reply_text(f"⚠️ Не удалось переместить *{title}*. Проверь, есть ли такая задача.")
            except Exception as e:
                logging.exception(f"Move entity error: {e}")
                await update.message.reply_text("⚠️ Не удалось переместить задачу. Проверь логи.")
        elif action == "update_task" and entity_type == "task" and list_name:
            try:
                logging.info(f"Updating task in list: {list_name}")
                if meta.get("by_index") and meta.get("new_title"):
                    logging.info(f"Updating task by index: {meta['by_index']} to '{meta['new_title']}' in list: {list_name}")
                    updated, old_title = update_task_by_index(conn, user_id, list_name, meta["by_index"], meta["new_title"])
                    if updated:
                        action_icon = get_action_icon("update_task")
                        header = f"{action_icon} Обновлено в {LIST_ICON} *{list_name}:*"
                        details = f"{action_icon} {old_title} → {meta['new_title']}"
                        list_block = format_list_output(conn, user_id, list_name, heading_label=f"{SECTION_ICON} *{list_name}:*")
                        message = f"{header}  \n{details}\n\n{list_block}"
                        await update.message.reply_text(message, parse_mode="Markdown")
                    else:
                        await update.message.reply_text(f"⚠️ Не удалось изменить задачу по индексу {meta['by_index']} в списке *{list_name}*.")
                elif title and meta.get("new_title"):
                    logging.info(f"Updating task: {title} to {meta['new_title']} in list: {list_name}")
                    updated = update_task(conn, user_id, list_name, title, meta["new_title"])
                    if updated:
                        action_icon = get_action_icon("update_task")
                        header = f"{action_icon} Обновлено в {LIST_ICON} *{list_name}:*"
                        details = f"{action_icon} {title} → {meta['new_title']}"
                        list_block = format_list_output(conn, user_id, list_name, heading_label=f"{SECTION_ICON} *{list_name}:*")
                        message = f"{header}  \n{details}\n\n{list_block}"
                        await update.message.reply_text(message, parse_mode="Markdown")
                    else:
                        await update.message.reply_text(f"⚠️ Не удалось изменить задачу *{title}* в списке *{list_name}*.")
                else:
                    await update.message.reply_text(f"🤔 Уточни, на что изменить задачу в списке *{list_name}*.")
                    await send_menu(update, context)
                    continue
                set_ctx(user_id, last_action="update_task", last_list=list_name)
            except Exception as e:
                logging.exception(f"Update task error: {e}")
                await update.message.reply_text("⚠️ Не удалось изменить задачу. Проверь логи.")
        elif action == "update_profile" and entity_type == "user_profile" and meta:
            try:
                logging.info(f"Updating user profile for user {user_id}: {meta}")
                update_user_profile(conn, user_id, meta.get("city"), meta.get("profession"))
                await update.message.reply_text("🆙 Профиль обновлён!", parse_mode="Markdown")
            except Exception as e:
                logging.exception(f"Update profile error: {e}")
                await update.message.reply_text("⚠️ Не удалось обновить профиль. Проверь логи.")
        elif action == "restore_task" and entity_type == "task" and list_name and title:
            try:
                logging.info(f"Restoring task: {title} in list: {list_name}")
                if meta.get("fuzzy"):
                    restored, matched = restore_task_fuzzy(conn, user_id, list_name, title)
                else:
                    restored = restore_task(conn, user_id, list_name, title)
                    matched = title if restored else None
                if restored:
                    action_icon = get_action_icon("restore_task")
                    task_name = matched or title
                    header = f"{action_icon} Восстановлено в {LIST_ICON} *{list_name}:*"
                    details = f"{action_icon} {task_name}"
                    list_block = format_list_output(conn, user_id, list_name, heading_label=f"{SECTION_ICON} *{list_name}:*")
                    message = f"{header}  \n{details}\n\n{list_block}"
                    await update.message.reply_text(message, parse_mode="Markdown")
                    executed_actions.append("restore_task")
                else:
                    await update.message.reply_text(f"⚠️ Не удалось восстановить *{title}*.")
                set_ctx(user_id, last_action="restore_task", last_list=list_name)
            except Exception as e:
                logging.exception(f"Restore task error: {e}")
                await update.message.reply_text("⚠️ Не удалось восстановить задачу. Проверь логи.")
        elif action == "say" and obj.get("text"):
            try:
                logging.info(f"Say: {obj['text']}")
                await update.message.reply_text(obj.get("text"))
            except Exception as e:
                logging.exception(f"Say error: {e}")
                await update.message.reply_text("⚠️ Не удалось отправить сообщение. Проверь логи.")
        elif action == "clarify" and meta.get("question"):
            try:
                question_text_raw = meta.get("question") or ""
                logging.info(f"Clarify: {question_text_raw}")
                if meta.get("confirmed"):
                    set_ctx(user_id, pending_confirmation=None)
                pending = meta.get("pending")
                if pending:
                    question_lower = question_text_raw.lower()
                    if "удал" in question_lower:
                        keyboard = [[
                            InlineKeyboardButton("Да", callback_data=f"clarify_yes:{pending}"),
                            InlineKeyboardButton("Нет", callback_data="clarify_no"),
                        ]]
                        reply_markup = InlineKeyboardMarkup(keyboard)
                        await update.message.reply_text("🤔 " + question_text_raw, parse_mode="Markdown", reply_markup=reply_markup)
                        set_ctx(user_id, pending_delete=pending, pending_confirmation=None)
                    elif "созда" in question_lower:
                        keyboard = [[
                            InlineKeyboardButton("Да", callback_data=f"create_list_yes:{pending}"),
                            InlineKeyboardButton("Нет", callback_data="create_list_no"),
                        ]]
                        reply_markup = InlineKeyboardMarkup(keyboard)
                        await update.message.reply_text("🤔 " + question_text_raw, parse_mode="Markdown", reply_markup=reply_markup)
                        set_ctx(
                            user_id,
                            pending_confirmation={
                                "type": "create_list",
                                "list": pending,
                                "question": question_text_raw,
                            },
                        )
                    else:
                        keyboard = [[
                            InlineKeyboardButton("Да", callback_data="clarify_generic_yes"),
                            InlineKeyboardButton("Нет", callback_data="clarify_generic_no"),
                        ]]
                        reply_markup = InlineKeyboardMarkup(keyboard)
                        await update.message.reply_text("🤔 " + question_text_raw, parse_mode="Markdown", reply_markup=reply_markup)
                        confirmation_payload = {
                            "question": question_text_raw,
                            "entity_type": entity_type,
                            "list": list_name,
                            "original_text": original_text,
                            "pending": pending,
                            "type": "generic",
                        }
                        set_ctx(user_id, pending_confirmation=confirmation_payload)
                else:
                    keyboard = [[
                        InlineKeyboardButton("Да", callback_data="clarify_generic_yes"),
                        InlineKeyboardButton("Нет", callback_data="clarify_generic_no"),
                    ]]
                    reply_markup = InlineKeyboardMarkup(keyboard)
                    await update.message.reply_text("🤔 " + question_text_raw, parse_mode="Markdown", reply_markup=reply_markup)
                    confirmation_payload = {
                        "question": question_text_raw,
                        "entity_type": entity_type,
                        "list": list_name,
                        "original_text": original_text,
                        "type": "generic",
                    }
                    question_lower = question_text_raw.lower()
                    if entity_type == "task" and "удал" in question_lower:
                        tasks_to_handle = extract_tasks_from_question(question_text_raw)
                        confirmation_payload.update(
                            {
                                "type": "delete_tasks",
                                "tasks": tasks_to_handle,
                            }
                        )
                    set_ctx(user_id, pending_confirmation=confirmation_payload)
            except Exception as e:
                logging.exception(f"Clarify error: {e}")
                await update.message.reply_text("⚠️ Не удалось уточнить. Проверь логи.")
        else:
            name_from_text = text_mentions_list_and_name(original_text)
            if name_from_text:
                logging.info(f"Showing tasks for list from text: {name_from_text}")
                if not find_list(conn, user_id, name_from_text):
                    question = f"⚠️ Списка *{name_from_text}* нет. Создать?"
                    keyboard = [[
                        InlineKeyboardButton("Да", callback_data=f"create_list_yes:{name_from_text}"),
                        InlineKeyboardButton("Нет", callback_data="create_list_no"),
                    ]]
                    reply_markup = InlineKeyboardMarkup(keyboard)
                    await update.message.reply_text(question, parse_mode="Markdown", reply_markup=reply_markup)
                    set_ctx(
                        user_id,
                        pending_confirmation={
                            "type": "create_list",
                            "list": name_from_text,
                            "question": question,
                        },
                    )
                    continue
                items = get_list_tasks(conn, user_id, name_from_text)
                if items:
                    txt = "\n".join([f"{i}. {t}" for i, t in items])
                    await update.message.reply_text(f"📋 *{name_from_text}:*\n{txt}", parse_mode="Markdown")
                else:
                    await update.message.reply_text(f"📋 *{name_from_text}:*\n— пусто —", parse_mode="Markdown")
                set_ctx(user_id, last_action="show_tasks", last_list=name_from_text)
                continue
            logging.info("Unknown command, no context match")
            await update.message.reply_text("🤔 Не понял, что нужно сделать.")
            await send_menu(update, context)
        logging.info(f"User {user_id}: {original_text} -> Action: {action}")
    return executed_actions

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE, input_text: str | None = None):
    user_id = update.effective_user.id
    text = (input_text or update.message.text or "").strip()
    logging.info(f"📩 Text from {user_id}: {text}")
    try:
        conn = get_conn()
        commands = split_user_commands(text)
        if not commands:
            commands = [text]
        for part in commands:
            command_text = part.strip()
            if not command_text:
                continue
            history = get_ctx(user_id, "history", [])
            lower_command = command_text.lower()
            pending_delete = get_ctx(user_id, "pending_delete")
            pending_confirmation = get_ctx(user_id, "pending_confirmation")
            if lower_command in ["да", "yes", "нет", "no"] and (pending_delete or pending_confirmation):
                if lower_command in ["да", "yes"]:
                    if pending_delete:
                        try:
                            logging.info(f"Deleting list via pending_delete: {pending_delete}")
                            deleted = delete_list(conn, user_id, pending_delete)
                            if deleted:
                                remaining = show_all_lists(conn, user_id, heading_label=f"{ALL_LISTS_ICON} *Оставшиеся списки:*")
                                message = f"{get_action_icon('delete_list')} Список *{pending_delete}* удалён.  \n\n{remaining}"
                                await update.message.reply_text(message, parse_mode="Markdown")
                                set_ctx(user_id, pending_delete=None, pending_confirmation=None, last_list=None)
                            else:
                                await update.message.reply_text(f"⚠️ Список *{pending_delete}* не найден.")
                                set_ctx(user_id, pending_delete=None)
                        except Exception as e:
                            logging.exception(f"Delete list error during confirmation: {e}")
                            await update.message.reply_text("⚠️ Ошибка удаления.")
                            set_ctx(user_id, pending_delete=None)
                    elif pending_confirmation:
                        await handle_pending_confirmation(update.message, context, conn, user_id, pending_confirmation)
                else:
                    if pending_delete:
                        await update.message.reply_text("❎ Отмена удаления.")
                        set_ctx(user_id, pending_delete=None)
                    if pending_confirmation:
                        await update.message.reply_text("❎ Отмена.")
                        set_ctx(user_id, pending_confirmation=None)
                continue
            multi_lists = parse_multi_list_creation(command_text)
            if multi_lists:
                actions = [{"action": "create", "entity_type": "list", "list": name} for name in multi_lists]
                executed_actions = await route_actions(update, context, actions, user_id, command_text) or []
                if any(action in SIGNIFICANT_ACTIONS and action not in HISTORY_SKIP_ACTIONS for action in executed_actions):
                    history = get_ctx(user_id, "history", [])
                    set_ctx(user_id, history=history + [command_text])
                continue
            completed_titles = parse_completed_task_titles(command_text)
            if completed_titles:
                payload = {
                    "actions": [
                        {"action": "mark_done", "entity_type": "task", "title": title}
                        for title in completed_titles
                    ]
                }
                summary_text = format_completion_summary(completed_titles)
                if summary_text:
                    payload["ui_text"] = summary_text
                executed_actions = await route_actions(update, context, [payload], user_id, command_text) or []
                if any(action in SIGNIFICANT_ACTIONS and action not in HISTORY_SKIP_ACTIONS for action in executed_actions):
                    history = get_ctx(user_id, "history", [])
                    set_ctx(user_id, history=history + [command_text])
                continue
            db_state, session_state = build_semantic_state(conn, user_id, history)
            user_profile = get_user_profile(conn, user_id)
            prompt = SEMANTIC_PROMPT.format(history=json.dumps(history, ensure_ascii=False),
                                           db_state=json.dumps(db_state, ensure_ascii=False),
                                           session_state=json.dumps(session_state, ensure_ascii=False),
                                           user_profile=json.dumps(user_profile, ensure_ascii=False),
                                           lexicon=SEMANTIC_LEXICON_JSON,
                                           pending_delete=get_ctx(user_id, "pending_delete", ""))
            logging.info(f"Sending to OpenAI: {command_text}")
            resp = await asyncio.to_thread(
                lambda: client.chat.completions.create(
                    model=OPENAI_MODEL,
                    messages=[
                        {"role": "system", "content": prompt},
                        {"role": "user", "content": command_text}
                    ],
                )
            )
            raw = resp.choices[0].message.content.strip()
            logging.info(f"🤖 RAW: {raw}")
            try:
                with open(RAW_LOG_FILE, "a", encoding="utf-8") as f:
                    f.write(f"\n=== RAW ({user_id}) ===\n{command_text}\n{raw}\n")
            except Exception:
                logging.warning("Failed to write to openai_raw.log")
            actions = extract_json_blocks(raw)
            if not actions:
                if wants_expand(command_text) and get_ctx(user_id, "last_action") == "show_lists":
                    logging.info("No actions, but expanding lists due to context")
                    await expand_all_lists(update, conn, user_id, context)
                    continue
                logging.warning("No valid JSON actions from OpenAI")
                await update.message.reply_text("⚠️ Модель ответила не в JSON-формате.")
                await send_menu(update, context)
                continue
            executed_actions = await route_actions(update, context, actions, user_id, command_text) or []
            if any(action in SIGNIFICANT_ACTIONS and action not in HISTORY_SKIP_ACTIONS for action in executed_actions):
                history = get_ctx(user_id, "history", [])
                set_ctx(user_id, history=history + [command_text])
    except Exception as e:
        logging.exception(f"❌ handle_text error: {e}")
        await update.message.reply_text("Произошла ошибка при обработке. Проверь логи.")
        await send_menu(update, context)

async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    logging.info(f"🎙 Voice from {user_id}")
    try:
        vf = await update.message.voice.get_file()
        ogg = os.path.join(TEMP_DIR, f"{user_id}_voice.ogg")
        wav = os.path.join(TEMP_DIR, f"{user_id}_voice.wav")
        await vf.download_to_drive(ogg)
        AudioSegment.from_ogg(ogg).export(wav, format="wav")
        r = sr.Recognizer()
        with sr.AudioFile(wav) as src:
            audio = r.record(src)
            text = r.recognize_google(audio, language="ru-RU")
            text = normalize_text(text)
        logging.info(f"🗣 ASR: {text}")
        await update.message.reply_text(f"🗣 {text}")
        await handle_text(update, context, input_text=text)
        try:
            os.remove(ogg); os.remove(wav)
        except Exception:
            pass
    except Exception as e:
        logging.exception(f"❌ voice error: {e}")
        await update.message.reply_text("⚠️ Не удалось обработать голос. Проверь логи.")
        await send_menu(update, context)

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    data = query.data
    logging.info(f"Callback from {user_id}: {data}")
    conn = get_conn()
    try:
        if data.startswith("delete_list:"):
            list_name = data.split(":")[1]
            deleted = delete_list(conn, user_id, list_name)
            if deleted:
                remaining = show_all_lists(conn, user_id, heading_label=f"{ALL_LISTS_ICON} *Оставшиеся списки:*")
                message = f"{get_action_icon('delete_list')} Список *{list_name}* удалён.  \n\n{remaining}"
                await query.edit_message_text(message, parse_mode="Markdown")
                set_ctx(user_id, last_action="delete_list", last_list=None, pending_delete=None)
            else:
                await query.edit_message_text(f"⚠️ Список *{list_name}* не найден.")
                set_ctx(user_id, pending_delete=None)
        elif data == "cancel_delete":
            await query.edit_message_text("❎ Отмена удаления.")
            set_ctx(user_id, pending_delete=None)
        elif data.startswith("clarify_yes:"):
            list_name = data.split(":")[1]
            deleted = delete_list(conn, user_id, list_name)
            if deleted:
                remaining = show_all_lists(conn, user_id, heading_label=f"{ALL_LISTS_ICON} *Оставшиеся списки:*")
                message = f"{get_action_icon('delete_list')} Список *{list_name}* удалён.  \n\n{remaining}"
                await query.edit_message_text(message, parse_mode="Markdown")
                set_ctx(user_id, last_action="delete_list", last_list=None, pending_delete=None)
            else:
                await query.edit_message_text(f"⚠️ Список *{list_name}* не найден.")
                set_ctx(user_id, pending_delete=None)
        elif data == "clarify_no":
            await query.edit_message_text("❎ Отмена удаления.")
            set_ctx(user_id, pending_delete=None)
        elif data == "clarify_generic_yes":
            pending_conf = get_ctx(user_id, "pending_confirmation")
            if pending_conf:
                handled = await handle_pending_confirmation(query.message, context, conn, user_id, pending_conf)
                set_ctx(user_id, pending_confirmation=None)
                if handled:
                    await query.edit_message_text("✅ Подтверждение получено.")
                else:
                    await query.edit_message_text("⚠️ Не удалось обработать подтверждение.")
            else:
                await query.edit_message_text("⚠️ Нет действий для подтверждения.")
        elif data == "clarify_generic_no":
            await query.edit_message_text("Хорошо, отмена.")
            set_ctx(user_id, pending_confirmation=None)
        elif data.startswith("create_list_yes:"):
            list_name = data.split(":", 1)[1]
            conn = get_conn()
            existing = find_list(conn, user_id, list_name)
            if existing:
                await query.edit_message_text(f"⚠️ Список *{list_name}* уже существует.", parse_mode="Markdown")
                set_ctx(user_id, pending_confirmation=None, last_list=list_name)
            else:
                try:
                    create_list(conn, user_id, list_name)
                    await query.edit_message_text(f"🆕 Создан список *{list_name}*", parse_mode="Markdown")
                    set_ctx(user_id, pending_confirmation=None, last_action="create_list", last_list=list_name)
                except Exception as e:
                    logging.exception(f"Create list via callback error: {e}")
                    await query.edit_message_text("⚠️ Не удалось создать список. Проверь логи.")
                    set_ctx(user_id, pending_confirmation=None)
        elif data == "create_list_no":
            await query.edit_message_text("Хорошо, не создаю.")
            set_ctx(user_id, pending_confirmation=None)
        else:
            await query.edit_message_text("⚠️ Неизвестная команда.")
    except Exception as e:
        logging.exception(f"Callback error: {e}")
        await query.edit_message_text("⚠️ Ошибка обработки. Проверь логи.")

def main():
    init_db()
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_handler(CallbackQueryHandler(handle_callback))
    logging.info("🚀 Aura v5.2 started.")
    app.run_polling()

if __name__ == "__main__":
    main()
