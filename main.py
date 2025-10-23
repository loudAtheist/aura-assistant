import json
import logging
import os
import random
import re
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from openai import (
    APIConnectionError,
    APIError,
    APITimeoutError,
    AuthenticationError,
    OpenAI,
    OpenAIError,
    RateLimitError,
)
from pydub import AudioSegment
import speech_recognition as sr
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from db import (
    add_task,
    create_list,
    delete_list,
    delete_task,
    delete_task_by_index,
    delete_task_fuzzy,
    find_list,
    fetch_list_by_task,
    fetch_task,
    get_all_lists,
    get_all_tasks,
    get_completed_tasks,
    get_conn,
    get_deleted_tasks,
    get_list_tasks,
    get_user_profile,
    init_db,
    mark_task_done,
    mark_task_done_fuzzy,
    move_entity,
    normalize_text,
    rename_list,
    restore_task,
    restore_task_fuzzy,
    search_tasks,
    update_task,
    update_task_by_index,
    update_user_profile,
    set_embedding_provider,
)
dotenv_path = Path(__file__).resolve().parent / ".env"
if dotenv_path.exists():
    load_dotenv(dotenv_path)
    _dotenv_message = ("info", f".env loaded from {dotenv_path}")
else:
    _dotenv_message = ("warning", f".env not found at {dotenv_path}")

LOG_DIR = Path(os.getenv("LOG_DIR", "/opt/aura-assistant"))
LOG_DIR.mkdir(parents=True, exist_ok=True)
RUN_LOG_FILE = LOG_DIR / "aura_run.log"
ERROR_LOG_FILE = LOG_DIR / "codex_errors.log"
RAW_LOG_FILE = LOG_DIR / "openai_raw.log"

LOG_FORMAT = "%(asctime)s | %(levelname)s | %(name)s | %(message)s"
logger = logging.getLogger("aura")
logger.setLevel(logging.DEBUG)
logger.propagate = False

for handler in list(logger.handlers):
    logger.removeHandler(handler)

console_handler = logging.StreamHandler()
console_handler.setLevel(logging.DEBUG)
console_handler.setFormatter(logging.Formatter(LOG_FORMAT))

file_handler = logging.FileHandler(RUN_LOG_FILE, encoding="utf-8")
file_handler.setLevel(logging.DEBUG)
file_handler.setFormatter(logging.Formatter(LOG_FORMAT))

error_handler = logging.FileHandler(ERROR_LOG_FILE, encoding="utf-8")
error_handler.setLevel(logging.ERROR)
error_handler.setFormatter(logging.Formatter(LOG_FORMAT))

logger.addHandler(console_handler)
logger.addHandler(file_handler)
logger.addHandler(error_handler)

logging.captureWarnings(True)
logger.debug("Logging configured: console + %s, %s", RUN_LOG_FILE, ERROR_LOG_FILE)

getattr(logger, _dotenv_message[0])(_dotenv_message[1])

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-3.5-turbo")
TEMP_DIR = os.getenv("TEMP_DIR", "/opt/aura-assistant/tmp")
os.makedirs(TEMP_DIR, exist_ok=True)
if not TELEGRAM_TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN не установлен")
if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY не установлен")
client = OpenAI(api_key=OPENAI_API_KEY)
logger.debug("Temporary directory ready at %s", TEMP_DIR)
logger.info("OpenAI client initialized for model %s", OPENAI_MODEL)

_EMBEDDING_MODEL = "text-embedding-3-small"
_EMBEDDING_CACHE: dict[str, list[float]] = {}


def _normalize_embedding_text(text: str) -> str:
    normalized = (text or "").strip()
    normalized = re.sub(r"\s+", " ", normalized)
    return normalized.lower()


def _get_text_embedding(text: str) -> list[float] | None:
    normalized = _normalize_embedding_text(text)
    if not normalized:
        return None
    if normalized in _EMBEDDING_CACHE:
        return _EMBEDDING_CACHE[normalized]
    try:
        response = client.embeddings.create(
            model=_EMBEDDING_MODEL,
            input=normalized,
        )
    except (
        APIConnectionError,
        APIError,
        APITimeoutError,
        AuthenticationError,
        OpenAIError,
        RateLimitError,
    ) as exc:
        logger.error("Failed to compute embedding for '%s': %s", normalized, exc)
        return None
    embedding = response.data[0].embedding
    _EMBEDDING_CACHE[normalized] = embedding
    return embedding


set_embedding_provider(_get_text_embedding)
# ========= DIALOG CONTEXT (per-user) =========
SESSION: dict[int, dict] = {} # { user_id: {"last_action": str, "last_list": str, "history": [str], "pending_delete": str, "pending_confirmation": dict} }
SIGNIFICANT_ACTIONS = {"create", "add_task", "move_entity", "mark_done", "restore_task", "delete_task", "delete_list"}
HISTORY_SKIP_ACTIONS = {"show_lists", "show_completed_tasks", "clarify", "confirm"}
STYLE = os.getenv("AURA_STYLE", "minimal").strip().lower()
if STYLE not in {"minimal", "vibrant"}:
    STYLE = "minimal"

YES_ANSWERS = {"да", "yes"}
NO_ANSWERS = {"нет", "no"}

TASK_EMOJI_MAP = {
    "купить": "🛒",
    "хлеб": "🥖",
    "молоко": "🥛",
    "сыр": "🧀",
    "почистить": "🧽",
    "мусор": "🗑",
    "оплатить": "💰",
    "лампочка": "💡",
    "интернет": "🌐",
    "отчет": "📄",
    "проект": "📄",
    "пациент": "🧍",
    "встреча": "📞",
}
DEFAULT_TASK_EMOJI = "🧩"
VIBRANT_ACCENTS = ["✨", "🔥", "⚡", "🌟"]
STYLE_CONFIG = {
    "minimal": {
        "list_icon": "📘",
        "section_icon": "📋",
        "all_lists_icon": "🗂",
        "action_icons": {
            "add_task": "🟢",
            "create": "📘",
            "delete_list": "🗑",
            "delete_task": "🗑",
            "mark_done": "✔️",
            "move_entity": "🔄",
            "rename_list": "✏️",
            "restore_task": "🌱",
            "update_task": "✏️",
            "update_profile": "🆙",
        },
    },
    "vibrant": {
        "list_icon": "🌈",
        "section_icon": "🌈",
        "all_lists_icon": "🌈",
        "action_icons": {
            "add_task": "🟢✨",
            "create": "🪄",
            "delete_list": "🗑️",
            "delete_task": "🗑️",
            "mark_done": "🎯",
            "move_entity": "🚚",
            "rename_list": "✏️",
            "restore_task": "🌱",
            "update_task": "✏️",
            "update_profile": "🆙",
        },
    },
}

def _get_style_config() -> dict:
    return STYLE_CONFIG.get(STYLE, STYLE_CONFIG["minimal"])


LIST_ICON = _get_style_config()["list_icon"]
SECTION_ICON = _get_style_config()["section_icon"]
ALL_LISTS_ICON = _get_style_config()["all_lists_icon"]


def get_action_icon(action: str) -> str:
    config = _get_style_config()["action_icons"]
    return config.get(action, "✨")


def get_emoji_for_task(title: str | None) -> str:
    if not title:
        return DEFAULT_TASK_EMOJI
    lowered = title.lower()
    for keyword, emoji in TASK_EMOJI_MAP.items():
        if keyword in lowered:
            return emoji
    return DEFAULT_TASK_EMOJI


def _task_suffix(title: str) -> str:
    emoji = get_emoji_for_task(title)
    return f" {emoji}" if emoji else ""


def format_task_line(index: int, title: str, style: str = STYLE) -> str:
    suffix = _task_suffix(title)
    if style == "vibrant":
        accent = random.choice(VIBRANT_ACCENTS)
        if suffix:
            return f"{index}️⃣ {title}{suffix}{accent}"
        return f"{index}️⃣ {title} {accent}"
    return f"{index}. {title}{suffix}"


def format_task_bullet(icon: str, title: str) -> str:
    return f"{icon} {title}{_task_suffix(title)}"


def format_section_title(title: str) -> str:
    return f"{SECTION_ICON} {title}:"
def format_list_output(conn, user_id: int, list_name: str, heading_label: str | None = None) -> str:
    heading = heading_label or format_section_title(list_name)
    tasks = get_list_tasks(conn, user_id, list_name)
    if tasks:
        lines = [format_task_line(idx, title) for idx, title in tasks]
    else:
        lines = ["_— пусто —_"]
    return f"{heading}\n" + "\n".join(lines)


def show_all_lists(conn, user_id: int, heading_label: str | None = None) -> str:
    lists = get_all_lists(conn, user_id)
    if not lists:
        empty_message = f"{ALL_LISTS_ICON} Пока нет списков."
        return f"{heading_label}\n_— пусто —_" if heading_label else empty_message
    blocks = [
        format_list_output(conn, user_id, name, heading_label=format_section_title(name))
        for name in lists
    ]
    combined = "\n\n".join(blocks)
    if heading_label:
        return f"{heading_label}\n\n{combined}"
    return combined
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
    logger.info("Updated context for user %s: %s", user_id, sess)
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
class _PromptValues(dict):
    """Helper for safe string formatting of SEMANTIC_PROMPT."""

    def __missing__(self, key: str) -> str:
        logger.warning("Missing placeholder '%s' while rendering prompt", key)
        return ""


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
- Если в одной команде несколько задач (например, «добавь постирать ковер, помыть машину, купить нож» или «добавь постирать ковер помыть машину»), всегда возвращай одно действие add_task с массивом tasks, содержащим все задачи. Запятые, пробелы или союз «и» обозначают отдельные задачи. Не генерируй clarify для задач в одной команде.
- Если список запрошен, но отсутствует в db_state.lists — верни clarify с вопросом «Списка *<имя>* нет. Создать?» и meta.pending = «<имя>».
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
- «В список Домашние дела добавь постирать ковер, помыть машину, купить маленький нож» → {{ "action": "add_task", "entity_type": "task", "list": "Домашние дела", "tasks": ["Постирать ковер", "Помыть машину", "Купить маленький нож"] }}
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
            logger.info(f"Extracted JSON list: {data}")
            return data
        if isinstance(data, dict):
            logger.info(f"Extracted JSON dict: {data}")
            return [data]
    except Exception:
        logger.exception("Failed to parse JSON directly: %s", s[:120])
    blocks = re.findall(r'\{[^{}]*\{[^{}]*\}[^{}]*\}|\{[^{}]+\}', s, re.DOTALL)
    if not blocks:
        blocks = re.findall(r'\{[^{}]+\}', s, re.DOTALL)
    out = []
    for b in blocks:
        try:
            parsed = json.loads(b)
            logger.info(f"Extracted JSON block: {parsed}")
            out.append(parsed)
        except Exception:
            logger.exception("Skip invalid JSON block: %s", b[:120])
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
VERB_BOUNDARY_SUFFIXES = (
    "ться",
    "тса",
    "ись",
    "йся",
    "ть",
    "ти",
    "йте",
    "айте",
    "яйте",
    "ите",
    "ете",
    "ай",
    "яй",
    "ей",
    "уй",
    "ри",
    "ни",
)
SHORT_VERB_BOUNDARY_SUFFIXES = {"ай", "яй", "ей", "уй", "ри", "ни"}
STOPWORD_TOKENS = {
    "и",
    "а",
    "но",
    "или",
    "на",
    "к",
    "в",
    "во",
    "по",
    "с",
    "со",
    "у",
    "за",
    "до",
    "от",
    "из",
    "для",
    "при",
    "о",
    "об",
    "обо",
    "же",
    "то",
}
def _token_clean(token: str) -> str:
    return re.sub(r"\s+", " ", (token or "")).strip(" .!?:;«»'\"")
def looks_like_verb_token(token: str) -> bool:
    cleaned = re.sub(r"[^а-яё-]", "", (token or "").lower())
    if not cleaned or len(cleaned) < 3:
        return False
    for suffix in VERB_BOUNDARY_SUFFIXES:
        if cleaned.endswith(suffix):
            if suffix in SHORT_VERB_BOUNDARY_SUFFIXES and len(cleaned) < 4:
                continue
            return True
    return False
def guess_enumerated_chunks(segment: str, base_title: str | None = None) -> list[str]:
    if not segment:
        return []
    normalized = re.sub(r"[\r\n]+", " ", segment)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    if not normalized:
        return []
    words = [w for w in re.split(r"\s+", normalized) if w]
    if len(words) <= 1:
        return []
    chunks: list[list[str]] = []
    current: list[str] = []
    for idx, word in enumerate(words):
        cleaned = _token_clean(word)
        if not cleaned:
            continue
        boundary = idx != 0 and looks_like_verb_token(cleaned)
        if boundary and current:
            chunks.append(current)
            current = []
        current.append(cleaned)
    if current:
        chunks.append(current)
    phrases = [
        _token_clean(" ".join(chunk))
        for chunk in chunks
        if _token_clean(" ".join(chunk))
    ]
    if len(phrases) > 1:
        unique_phrases: list[str] = []
        seen: set[str] = set()
        for phrase in phrases:
            key = phrase.lower()
            if key not in seen:
                seen.add(key)
                unique_phrases.append(phrase)
        if len(unique_phrases) > 1:
            return unique_phrases
    filtered_words = [
        _token_clean(word)
        for word in words
        if _token_clean(word) and _token_clean(word).lower() not in STOPWORD_TOKENS
    ]
    if base_title:
        if (
            len(filtered_words) > 1
            and all(" " not in token for token in filtered_words)
            and sum(1 for token in filtered_words if not looks_like_verb_token(token)) >= 2
        ):
            unique_simple: list[str] = []
            seen_simple: set[str] = set()
            for token in filtered_words:
                key = token.lower()
                if key not in seen_simple:
                    seen_simple.add(key)
                    unique_simple.append(token)
            if len(unique_simple) > 1:
                return unique_simple
    else:
        if (
            len(filtered_words) > 2
            and all(" " not in token for token in filtered_words)
            and sum(1 for token in filtered_words if not looks_like_verb_token(token)) >= 2
        ):
            unique_simple: list[str] = []
            seen_simple: set[str] = set()
            for token in filtered_words:
                key = token.lower()
                if key not in seen_simple:
                    seen_simple.add(key)
                    unique_simple.append(token)
            if len(unique_simple) > 1:
                return unique_simple
    return []
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
            seen = buffer.setdefault("_seen", set())
            for raw in current_tasks or []:
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
            continue
        flush_buffer()
        buffer = {
            "action": "mark_done",
            "entity_type": entity_type,
            "list": list_name,
            "tasks": [],
            "_seen": set(),
        }
        seen = buffer["_seen"]
        for raw in current_tasks or []:
            cleaned = re.sub(r"\s+", " ", (raw or "").strip())
            if not cleaned:
                continue
            key = cleaned.lower()
            if key in seen:
                continue
            seen.add(key)
            buffer["tasks"].append(cleaned)
        if meta:
            buffer["meta"] = meta
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
    raw_items = re.split(r"(?:[,;]|\bи\b)", segment, flags=re.IGNORECASE)
    tasks = [item.strip(" .!?:;«»'\"") for item in raw_items if item.strip(" .!?:;«»'\"")]
    unique = []
    seen = set()
    for task in tasks:
        if not task:
            continue
        key = task.lower()
        if key not in seen:
            seen.add(key)
            unique.append(task)
    return unique
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
            logger.exception("Failed to fetch tasks for list %s while building semantic state", name)
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


def process_task_additions(
    conn,
    user_id: int,
    list_name: str,
    tasks: list[str] | None,
    *,
    force_first: bool = False,
) -> dict[str, Any]:
    results: dict[str, Any] = {
        "added": [],
        "auto_used": [],
        "duplicate": None,
        "skipped": [],
    }
    if not tasks:
        return results
    for idx, raw_task in enumerate(tasks):
        if not raw_task:
            continue
        force = force_first and idx == 0
        add_result = add_task(conn, user_id, list_name, raw_task, force=force)
        title_to_use = add_result.get("title") or raw_task
        if add_result.get("duplicate_detected"):
            similarity = add_result.get("similarity") or 0.0
            logger.info(
                'Duplicate detected: "%s" ≈ "%s" (similarity=%.2f)',
                raw_task,
                title_to_use,
                similarity,
            )
        if add_result.get("duplicate_detected") and not force:
            results["duplicate"] = {
                "list": list_name,
                "requested": raw_task,
                "existing": title_to_use,
                "similarity": add_result.get("similarity"),
                "remaining": tasks[idx + 1 :],
            }
            break
        if add_result.get("created") or add_result.get("restored"):
            results["added"].append(title_to_use)
        elif add_result.get("duplicate_detected"):
            results["auto_used"].append(
                {
                    "requested": raw_task,
                    "existing": title_to_use,
                    "similarity": add_result.get("similarity"),
                }
            )
        else:
            results["skipped"].append(raw_task)
    return results


def compose_task_feedback(list_name: str, task_results: dict[str, Any]) -> list[str]:
    messages: list[str] = []
    added = task_results.get("added") or []
    if added:
        icon = get_action_icon("add_task")
        details = "\n".join(format_task_bullet(icon, task) for task in added)
        if STYLE == "vibrant":
            header = f"{icon} Добавлено в {list_name}:"
        else:
            header = f"{icon} Добавлены задачи в {LIST_ICON} {list_name}:"
        messages.append(f"{header}\n{details}")
    auto_used = task_results.get("auto_used") or []
    if auto_used:
        used_titles = [f"“{item['existing']}”" for item in auto_used]
        messages.append(
            "ℹ️ Эти задачи уже есть: "
            + ", ".join(used_titles)
            + ". Использую существующие."
        )
    if (
        not added
        and not auto_used
        and not task_results.get("duplicate")
        and task_results.get("skipped")
    ):
        messages.append(f"⚠️ Все указанные задачи уже есть в {LIST_ICON} {list_name}.")
    return messages


def build_task_duplicate_question(list_name: str, duplicate_info: dict[str, Any]) -> str:
    existing = duplicate_info.get("existing") or ""
    requested = duplicate_info.get("requested") or ""
    if list_name:
        return (
            f"🤔 Похоже, уже есть похожая задача в списке “{list_name}”: “{existing}”."
            f" Всё равно добавить “{requested}”? (да / нет)"
        )
    return (
        f"🤔 Похоже, уже есть похожая задача: “{existing}”."
        f" Всё равно добавить “{requested}”? (да / нет)"
    )


def build_list_duplicate_question(requested: str, existing: str) -> str:
    existing_clean = existing or ""
    requested_clean = requested or ""
    return (
        f"🤔 Похоже, уже есть похожий список: “{existing_clean}”."
        f" Всё равно создать “{requested_clean}”? (да / нет)"
    )
async def perform_create_list(
    target: Any,
    conn,
    user_id: int,
    list_name: str,
    tasks: list[str] | None = None,
    *,
    force: bool = False,
) -> bool:
    try:
        logger.info(f"Creating list: {list_name}")
        result = create_list(conn, user_id, list_name, force=force)
        message_obj = getattr(target, "message", None)
        if message_obj is None:
            message_obj = target
        if result.get("duplicate_detected"):
            existing_title = result.get("duplicate_title") or list_name
            similarity = result.get("similarity") or 0.0
            logger.info(
                'Duplicate detected: "%s" ≈ "%s" (similarity=%.2f)',
                list_name,
                existing_title,
                similarity,
            )
            if force:
                message_parts = [
                    f"⚠️ Список “{existing_title}” уже существует. Использую его."
                ]
                task_results = process_task_additions(conn, user_id, existing_title, tasks)
                message_parts.extend(compose_task_feedback(existing_title, task_results))
                list_block = format_list_output(
                    conn,
                    user_id,
                    existing_title,
                    heading_label=format_section_title("Актуальный список"),
                )
                message_parts.append(list_block)
                await message_obj.reply_text("\n\n".join(message_parts), parse_mode="Markdown")
                set_ctx(
                    user_id,
                    last_action="create_list",
                    last_list=existing_title,
                    pending_confirmation=None,
                )
                duplicate_info = task_results.get("duplicate")
                if duplicate_info:
                    question = build_task_duplicate_question(existing_title, duplicate_info)
                    await message_obj.reply_text(question)
                    set_ctx(
                        user_id,
                        pending_confirmation={
                            "action": "add_task",
                            "entity_type": "task",
                            "list": existing_title,
                            "title": duplicate_info.get("requested"),
                            "similar_to": duplicate_info.get("existing"),
                            "similarity": duplicate_info.get("similarity"),
                            "remaining_tasks": duplicate_info.get("remaining") or [],
                        },
                    )
                return True
            question = build_list_duplicate_question(list_name, existing_title)
            await message_obj.reply_text(question)
            set_ctx(
                user_id,
                pending_confirmation={
                    "action": "add_list",
                    "entity_type": "list",
                    "title": list_name,
                    "similar_to": existing_title,
                    "similarity": similarity,
                    "tasks": tasks or [],
                },
            )
            return False
        action_icon = get_action_icon("create")
        if STYLE == "minimal":
            header = f"{action_icon} Создан новый список {LIST_ICON} {list_name} ✨"
        else:
            header = f"{action_icon} Создан новый список: {list_name} ✨"
        task_results = process_task_additions(conn, user_id, list_name, tasks)
        message_parts = [header]
        message_parts.extend(compose_task_feedback(list_name, task_results))
        list_block = format_list_output(
            conn,
            user_id,
            list_name,
            heading_label=format_section_title("Актуальный список"),
        )
        message_parts.append(list_block)
        await message_obj.reply_text("\n\n".join(message_parts), parse_mode="Markdown")
        set_ctx(
            user_id,
            last_action="create_list",
            last_list=list_name,
            pending_confirmation=None,
        )
        duplicate_info = task_results.get("duplicate")
        if duplicate_info:
            question = build_task_duplicate_question(list_name, duplicate_info)
            await message_obj.reply_text(question)
            set_ctx(
                user_id,
                pending_confirmation={
                    "action": "add_task",
                    "entity_type": "task",
                    "list": list_name,
                    "title": duplicate_info.get("requested"),
                    "similar_to": duplicate_info.get("existing"),
                    "similarity": duplicate_info.get("similarity"),
                    "remaining_tasks": duplicate_info.get("remaining") or [],
                },
            )
        return True
    except Exception as e:
        logger.exception(f"Create list error: {e}")
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
async def handle_pending_confirmation(
    message,
    context: ContextTypes.DEFAULT_TYPE,
    conn,
    user_id: int,
    pending_confirmation: dict,
    response: str,
) -> str | None:
    if not pending_confirmation:
        return None
    normalized = response.strip().lower()
    is_yes = normalized in YES_ANSWERS
    is_no = normalized in NO_ANSWERS
    action = pending_confirmation.get("action")
    entity_type = pending_confirmation.get("entity_type")
    conf_type = pending_confirmation.get("type")
    if not action and conf_type == "duplicate_task":
        pending_confirmation = {
            "action": "add_task",
            "entity_type": "task",
            "list": pending_confirmation.get("list"),
            "title": pending_confirmation.get("requested_title"),
            "similar_to": pending_confirmation.get("existing_title"),
            "similarity": pending_confirmation.get("similarity"),
            "remaining_tasks": pending_confirmation.get("remaining_tasks"),
        }
        action = pending_confirmation.get("action")
        entity_type = pending_confirmation.get("entity_type")
    if action == "add_task" and entity_type == "task":
        list_name = pending_confirmation.get("list") or get_ctx(user_id, "last_list")
        if not list_name:
            await message.reply_text("⚠️ Не понимаю, в какой список добавить задачу.")
            set_ctx(user_id, pending_confirmation=None)
            return None
        if not is_yes:
            await message.reply_text("Хорошо, не добавляю задачу.")
            set_ctx(user_id, pending_confirmation=None)
            return "cancel_duplicate_task"
        requested = pending_confirmation.get("title")
        remaining = pending_confirmation.get("remaining_tasks") or []
        tasks_to_process: list[str] = []
        if requested:
            tasks_to_process.append(requested)
        tasks_to_process.extend(remaining)
        task_results = process_task_additions(
            conn,
            user_id,
            list_name,
            tasks_to_process,
            force_first=True,
        )
        message_parts = compose_task_feedback(list_name, task_results)
        list_block = format_list_output(
            conn,
            user_id,
            list_name,
            heading_label=format_section_title("Актуальный список"),
        )
        if message_parts:
            message_parts.append(list_block)
            await message.reply_text("\n\n".join(message_parts), parse_mode="Markdown")
        else:
            await message.reply_text(list_block, parse_mode="Markdown")
        set_ctx(
            user_id,
            pending_confirmation=None,
            last_list=list_name,
            last_action="add_task",
        )
        duplicate_info = task_results.get("duplicate")
        if duplicate_info:
            question = build_task_duplicate_question(list_name, duplicate_info)
            await message.reply_text(question)
            set_ctx(
                user_id,
                pending_confirmation={
                    "action": "add_task",
                    "entity_type": "task",
                    "list": list_name,
                    "title": duplicate_info.get("requested"),
                    "similar_to": duplicate_info.get("existing"),
                    "similarity": duplicate_info.get("similarity"),
                    "remaining_tasks": duplicate_info.get("remaining") or [],
                },
            )
        return "add_task"
    if action == "add_list" and entity_type == "list":
        list_to_create = pending_confirmation.get("title")
        if not list_to_create:
            await message.reply_text("⚠️ Не понимаю, какой список создать.")
            set_ctx(user_id, pending_confirmation=None)
            return None
        if not is_yes:
            await message.reply_text("Хорошо, не создаю список.")
            set_ctx(user_id, pending_confirmation=None)
            return "cancel_create"
        tasks = pending_confirmation.get("tasks") or []
        set_ctx(user_id, pending_confirmation=None)
        handled = await perform_create_list(
            message,
            conn,
            user_id,
            list_to_create,
            tasks,
            force=True,
        )
        return "create" if handled else None
    conf_type = pending_confirmation.get("type")
    if conf_type == "delete_tasks":
        if not is_yes:
            await message.reply_text("Удаление отменено.")
            set_ctx(user_id, pending_confirmation=None)
            return "cancel_delete"
        tasks = pending_confirmation.get("tasks") or []
        if not tasks:
            await message.reply_text("⚠️ Нет задач для удаления.")
            set_ctx(user_id, pending_confirmation=None)
            return None
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
        return "delete_task" if deleted_entries else None
    if conf_type == "create_list":
        list_to_create = pending_confirmation.get("list")
        if not list_to_create:
            await message.reply_text("⚠️ Не понимаю, какой список создать.")
            set_ctx(user_id, pending_confirmation=None)
            return None
        if not is_yes:
            await message.reply_text("Ок, не создаю список.")
            set_ctx(user_id, pending_confirmation=None)
            return "cancel_create"
        existing = find_list(conn, user_id, list_to_create)
        if existing:
            await message.reply_text(
                f"⚠️ Список *{list_to_create}* уже существует.",
                parse_mode="Markdown",
            )
            set_ctx(user_id, pending_confirmation=None, last_list=list_to_create)
            return None
        handled = await perform_create_list(message, conn, user_id, list_to_create)
        set_ctx(user_id, pending_confirmation=None)
        return "create" if handled else None
    if conf_type == "use_existing_list":
        existing_title = pending_confirmation.get("existing_title")
        if not existing_title:
            set_ctx(user_id, pending_confirmation=None)
            await message.reply_text("⚠️ Не нашёл подходящий список.")
            return None
        if not is_yes:
            await message.reply_text("Хорошо, скажи другое название списка.")
            set_ctx(user_id, pending_confirmation=None)
            return "cancel_use_existing_list"
        tasks = pending_confirmation.get("tasks") or []
        task_results = process_task_additions(conn, user_id, existing_title, tasks)
        message_parts = [f"⚠️ Использую существующий список “{existing_title}”."]
        message_parts.extend(compose_task_feedback(existing_title, task_results))
        list_block = format_list_output(
            conn,
            user_id,
            existing_title,
            heading_label=format_section_title("Актуальный список"),
        )
        message_parts.append(list_block)
        await message.reply_text("\n\n".join(message_parts), parse_mode="Markdown")
        set_ctx(
            user_id,
            pending_confirmation=None,
            last_list=existing_title,
            last_action="create_list",
        )
        duplicate_info = task_results.get("duplicate")
        if duplicate_info:
            question = build_task_duplicate_question(existing_title, duplicate_info)
            await message.reply_text(question)
            set_ctx(
                user_id,
                pending_confirmation={
                    "action": "add_task",
                    "entity_type": "task",
                    "list": existing_title,
                    "title": duplicate_info.get("requested"),
                    "similar_to": duplicate_info.get("existing"),
                    "similarity": duplicate_info.get("similarity"),
                    "remaining_tasks": duplicate_info.get("remaining") or [],
                },
            )
        return "use_existing_list"
    if conf_type == "duplicate_task":
        list_name = pending_confirmation.get("list") or get_ctx(user_id, "last_list")
        if not list_name:
            await message.reply_text("⚠️ Не понимаю, в какой список добавить задачу.")
            set_ctx(user_id, pending_confirmation=None)
            return None
        if not is_yes:
            await message.reply_text("Ок, не добавляю похожую задачу.")
            set_ctx(user_id, pending_confirmation=None)
            return "cancel_duplicate_task"
        requested = pending_confirmation.get("requested_title")
        remaining = pending_confirmation.get("remaining_tasks") or []
        tasks_to_process = []
        if requested:
            tasks_to_process.append(requested)
        tasks_to_process.extend(remaining)
        task_results = process_task_additions(
            conn,
            user_id,
            list_name,
            tasks_to_process,
            force_first=True,
        )
        message_parts = compose_task_feedback(list_name, task_results)
        list_block = format_list_output(
            conn,
            user_id,
            list_name,
            heading_label=format_section_title("Актуальный список"),
        )
        if message_parts:
            message_parts.append(list_block)
            await message.reply_text("\n\n".join(message_parts), parse_mode="Markdown")
        else:
            await message.reply_text(list_block, parse_mode="Markdown")
        set_ctx(
            user_id,
            pending_confirmation=None,
            last_list=list_name,
            last_action="add_task",
        )
        duplicate_info = task_results.get("duplicate")
        if duplicate_info:
            question = build_task_duplicate_question(list_name, duplicate_info)
            await message.reply_text(question)
            set_ctx(
                user_id,
                pending_confirmation={
                    "action": "add_task",
                    "entity_type": "task",
                    "list": list_name,
                    "title": duplicate_info.get("requested"),
                    "similar_to": duplicate_info.get("existing"),
                    "similarity": duplicate_info.get("similarity"),
                    "remaining_tasks": duplicate_info.get("remaining") or [],
                },
            )
        return "add_task"
    await message.reply_text(
        "⚠️ Не удалось обработать подтверждение. Попробуй сформулировать команду заново."
    )
    set_ctx(user_id, pending_confirmation=None)
    return None
async def send_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [["Показать списки", "Создать список"], ["Добавить задачу", "Помощь"]]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True, selective=True)
    await update.message.reply_text("Выбери действие или напиши/скажи:", reply_markup=reply_markup)
async def expand_all_lists(update: Update, conn, user_id: int, context: ContextTypes.DEFAULT_TYPE):
    lists = get_all_lists(conn, user_id)
    if not lists:
        await update.message.reply_text(
            f"{ALL_LISTS_ICON} Пока нет списков.",
            parse_mode="Markdown",
        )
        return
    message = show_all_lists(conn, user_id)
    await update.message.reply_text(message, parse_mode="Markdown")
    set_ctx(user_id, last_action="show_lists")
async def route_actions(update: Update, context: ContextTypes.DEFAULT_TYPE, actions: list, user_id: int, original_text: str) -> list[str]:
    conn = get_conn()
    logger.info(f"Processing actions: {json.dumps(actions)}")
    normalized_actions = normalize_action_payloads(actions)
    normalized_actions = collapse_mark_done_actions(normalized_actions)
    executed_actions: list[str] = []
    pending_delete = get_ctx(user_id, "pending_delete")
    pending_confirmation = get_ctx(user_id, "pending_confirmation")
    normalized_reply = original_text.strip().lower()
    if original_text.lower() in ["да", "yes"] and pending_delete:
        try:
            logger.info(f"Deleting list: {pending_delete}")
            deleted = delete_list(conn, user_id, pending_delete)
            if deleted:
                await update.message.reply_text(f"🗑 Список *{pending_delete}* удалён.", parse_mode="Markdown")
                set_ctx(user_id, pending_delete=None, last_list=None)
                logger.info(f"Confirmed delete_list: {pending_delete}")
                executed_actions.append("delete_list")
            else:
                await update.message.reply_text(f"⚠️ Список *{pending_delete}* не найден.")
                set_ctx(user_id, pending_delete=None)
            return executed_actions
        except Exception as e:
            logger.exception(f"Delete error: {e}")
            await update.message.reply_text("⚠️ Ошибка удаления.")
            set_ctx(user_id, pending_delete=None)
            return executed_actions
    elif original_text.lower() in ["нет", "no"] and pending_delete:
        await update.message.reply_text("Удаление отменено.")
        set_ctx(user_id, pending_delete=None)
        return executed_actions
    if pending_confirmation and normalized_reply in YES_ANSWERS.union(NO_ANSWERS):
        handled = await handle_pending_confirmation(
            update.message,
            context,
            conn,
            user_id,
            pending_confirmation,
            normalized_reply,
        )
        if handled:
            executed_actions.append(handled)
        return executed_actions
    for obj in normalized_actions:
        action = obj.get("action", "unknown")
        entity_type = obj.get("entity_type", "task")
        list_name = obj.get("list") or get_ctx(user_id, "last_list")
        title = obj.get("title") or obj.get("task")
        meta = obj.get("meta", {})
        logger.info(f"Action: {action}, Entity: {entity_type}, List: {list_name}, Title: {title}")
        if action not in ["delete_list", "clarify"] and get_ctx(user_id, "pending_delete"):
            set_ctx(user_id, pending_delete=None)
        if list_name == "<последний список>":
            list_name = get_ctx(user_id, "last_list")
            logger.info(f"Resolved placeholder to last_list: {list_name}")
            if not list_name:
                logger.warning("No last_list in context, asking for clarification")
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
                logger.info(f"Fallback to show_tasks for list: {list_name}")
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
                logger.info(f"Adding tasks to list: {list_name}")
                tasks = obj.get("tasks", []) or ([title] if title else [])
                if not tasks:
                    tasks = extract_task_list_from_command(original_text, list_name)
                task_results = process_task_additions(conn, user_id, list_name, tasks)
                message_parts = compose_task_feedback(list_name, task_results)
                list_block = format_list_output(
                    conn,
                    user_id,
                    list_name,
                    heading_label=format_section_title("Актуальный список"),
                )
                if message_parts:
                    message_parts.append(list_block)
                    await update.message.reply_text(
                        "\n\n".join(message_parts),
                        parse_mode="Markdown",
                    )
                else:
                    await update.message.reply_text(list_block, parse_mode="Markdown")
                duplicate_info = task_results.get("duplicate")
                if duplicate_info:
                    question = build_task_duplicate_question(list_name, duplicate_info)
                    await update.message.reply_text(question)
                    set_ctx(
                        user_id,
                        pending_confirmation={
                            "action": "add_task",
                            "entity_type": "task",
                            "list": list_name,
                            "title": duplicate_info.get("requested"),
                            "similar_to": duplicate_info.get("existing"),
                            "similarity": duplicate_info.get("similarity"),
                            "remaining_tasks": duplicate_info.get("remaining") or [],
                        },
                    )
                    logger.info(
                        "Pending confirmation for duplicate task '%s' ≈ '%s' (%.2f) in list '%s'",
                        duplicate_info.get("requested"),
                        duplicate_info.get("existing"),
                        duplicate_info.get("similarity"),
                        list_name,
                    )
                    return executed_actions
                set_ctx(user_id, last_action="add_task", last_list=list_name)
                if task_results.get("added") or task_results.get("auto_used"):
                    executed_actions.append("add_task")
            except Exception as e:
                logger.exception(f"Add task error: {e}")
                await update.message.reply_text("⚠️ Не удалось добавить задачу. Проверь логи.")
        elif action == "show_lists":
            try:
                logger.info("Showing all lists with tasks")
                await expand_all_lists(update, conn, user_id, context)
            except Exception as e:
                logger.exception(f"Show lists error: {e}")
                await update.message.reply_text("⚠️ Не удалось получить списки. Проверь логи.")
        elif action == "show_tasks" and list_name:
            try:
                logger.info(f"Showing tasks for list: {list_name}")
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
                message = format_list_output(conn, user_id, list_name, heading_label=format_section_title(list_name))
                await update.message.reply_text(message, parse_mode="Markdown")
                set_ctx(user_id, last_action="show_tasks", last_list=list_name)
            except Exception as e:
                logger.exception(f"Show tasks error: {e}")
                await update.message.reply_text("⚠️ Не удалось получить задачи. Проверь логи.")
        elif action == "show_all_tasks":
            try:
                logger.info("Showing all tasks")
                lists = get_all_lists(conn, user_id)
                if not lists:
                    await update.message.reply_text(
                        f"{ALL_LISTS_ICON} Все задачи:\n_— пусто —_",
                        parse_mode="Markdown",
                    )
                    set_ctx(user_id, last_action="show_all_tasks")
                    continue
                blocks = [
                    format_list_output(conn, user_id, n, heading_label=format_section_title(n))
                    for n in lists
                ]
                message = f"{ALL_LISTS_ICON} Все задачи:\n\n" + "\n\n".join(blocks)
                await update.message.reply_text(message, parse_mode="Markdown")
                set_ctx(user_id, last_action="show_all_tasks")
            except Exception as e:
                logger.exception(f"Show all tasks error: {e}")
                await update.message.reply_text("⚠️ Не удалось получить дела. Проверь логи.")
        elif action == "show_completed_tasks":
            try:
                logger.info("Showing completed tasks")
                tasks = get_completed_tasks(conn, user_id, limit=15)
                if tasks:
                    lines = []
                    for list_title, task_title in tasks:
                        list_display = list_title or "Архив"
                        lines.append(f"{list_display} — {task_title}{_task_suffix(task_title)}")
                    header = f"{get_action_icon('mark_done')} Завершённые задачи (последние 15):\n"
                    await update.message.reply_text(header + "\n".join(lines), parse_mode="Markdown")
                else:
                    await update.message.reply_text(f"{get_action_icon('mark_done')} Пока нет выполненных задач.")
                set_ctx(user_id, last_action="show_completed_tasks")
            except Exception as e:
                logger.exception(f"Show completed tasks error: {e}")
                await update.message.reply_text("⚠️ Не удалось получить выполненные задачи. Проверь логи.")
        elif action == "show_deleted_tasks":
            try:
                logger.info("Showing deleted tasks")
                tasks = get_deleted_tasks(conn, user_id, limit=15)
                if tasks:
                    lines = []
                    for list_title, task_title in tasks:
                        list_display = list_title or "Без списка"
                        lines.append(f"{list_display} — {task_title}{_task_suffix(task_title)}")
                    header = f"{get_action_icon('delete_task')} Удалённые задачи (последние 15):\n"
                    await update.message.reply_text(header + "\n".join(lines), parse_mode="Markdown")
                else:
                    await update.message.reply_text(f"{get_action_icon('delete_task')} Пока нет удалённых задач.")
                set_ctx(user_id, last_action="show_deleted_tasks")
            except Exception as e:
                logger.exception(f"Show deleted tasks error: {e}")
                await update.message.reply_text("⚠️ Не удалось получить удалённые задачи. Проверь логи.")
        elif action == "search_entity" and meta.get("pattern"):
            try:
                logger.info(f"Searching tasks with pattern: {meta['pattern']}")
                tasks = search_tasks(conn, user_id, meta["pattern"])
                if tasks:
                    grouped: dict[str, list[str]] = {}
                    for list_title, task_title in tasks:
                        list_display = list_title or "Без списка"
                        grouped.setdefault(list_display, []).append(task_title)
                    blocks = []
                    for list_display, titles in grouped.items():
                        heading = format_section_title(list_display)
                        lines = [format_task_line(i, t) for i, t in enumerate(titles, start=1)]
                        blocks.append(f"{heading}\n" + "\n".join(lines))
                    message = f"{ALL_LISTS_ICON} Найденные задачи:\n\n" + "\n\n".join(blocks)
                    await update.message.reply_text(message, parse_mode="Markdown")
                else:
                    await update.message.reply_text(f"Задачи с '{meta['pattern']}' не найдены.")
                set_ctx(user_id, last_action="search_entity")
            except Exception as e:
                logger.exception(f"Search tasks error: {e}")
                await update.message.reply_text("⚠️ Не удалось найти задачи. Проверь логи.")
        elif action == "delete_task":
            try:
                ln = list_name or get_ctx(user_id, "last_list")
                if not ln:
                    logger.info("No list name provided for delete_task")
                    await update.message.reply_text("🤔 Уточни, из какого списка удалить.")
                    await send_menu(update, context)
                    continue
                if meta.get("by_index"):
                    logger.info(f"Deleting task by index: {meta['by_index']} in list: {ln}")
                    deleted, matched = delete_task_by_index(conn, user_id, ln, meta["by_index"])
                else:
                    logger.info(f"Deleting task fuzzy: {title} in list: {ln}")
                    deleted, matched = delete_task_fuzzy(conn, user_id, ln, title)
                if deleted:
                    action_icon = get_action_icon("delete_task")
                    task_name = matched or title or "задача"
                    if STYLE == "vibrant":
                        header = f"{action_icon} Удалено из {ln}:"
                    else:
                        header = f"{action_icon} Удалено из {LIST_ICON} {ln}:"
                    details = format_task_bullet(action_icon, task_name)
                    list_block = format_list_output(conn, user_id, ln, heading_label=format_section_title(ln))
                    message = f"{header}\n{details}\n\n{list_block}"
                    await update.message.reply_text(message, parse_mode="Markdown")
                else:
                    await update.message.reply_text("⚠️ Задача не найдена или уже выполнена.")
                set_ctx(user_id, last_action="delete_task", last_list=ln)
            except Exception as e:
                logger.exception(f"Delete task error: {e}")
                await update.message.reply_text("⚠️ Не удалось удалить задачу. Проверь логи.")
        elif action == "delete_list" and entity_type == "list" and list_name:
            try:
                pending_delete = get_ctx(user_id, "pending_delete")
                if pending_delete == list_name and original_text.lower() in ["да", "yes"]:
                    logger.info(f"Deleting list: {list_name}")
                    deleted = delete_list(conn, user_id, list_name)
                    if deleted:
                        remaining = show_all_lists(conn, user_id, heading_label=f"{ALL_LISTS_ICON} Оставшиеся списки:")
                        message = f"{get_action_icon('delete_list')} Список {list_name} удалён.\n\n{remaining}"
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
                logger.exception(f"Delete list error: {e}")
                await update.message.reply_text("⚠️ Не удалось удалить список. Проверь логи.")
                set_ctx(user_id, pending_delete=None)
        elif action == "mark_done" and list_name:
            try:
                logger.info(f"Marking tasks done in list: {list_name}")
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
                    logger.info(f"Marking task done: {task_phrase} in list: {list_name}")
                    deleted, matched = mark_task_done_fuzzy(conn, user_id, list_name, task_phrase)
                    if deleted:
                        completed_tasks.append(matched)
                if completed_tasks:
                    action_icon = get_action_icon("mark_done")
                    details = "\n".join(format_task_bullet(action_icon, task) for task in completed_tasks)
                    if STYLE == "vibrant":
                        header = f"{action_icon} Готово в {list_name}:"
                    else:
                        header = f"{action_icon} Готово в {LIST_ICON} {list_name}:"
                    list_block = format_list_output(conn, user_id, list_name, heading_label=format_section_title(list_name))
                    message = f"{header}\n{details}\n\n{list_block}"
                    await update.message.reply_text(message, parse_mode="Markdown")
                    executed_actions.append("mark_done")
                elif tasks_to_mark:
                    await update.message.reply_text("⚠️ Не нашёл указанные задачи.")
                elif title:
                    logger.info(f"Marking task done: {title} in list: {list_name}")
                    deleted, matched = mark_task_done_fuzzy(conn, user_id, list_name, title)
                    if deleted:
                        action_icon = get_action_icon("mark_done")
                        if STYLE == "vibrant":
                            header = f"{action_icon} Готово в {list_name}:"
                        else:
                            header = f"{action_icon} Готово в {LIST_ICON} {list_name}:"
                        details = format_task_bullet(action_icon, matched)
                        list_block = format_list_output(conn, user_id, list_name, heading_label=format_section_title(list_name))
                        message = f"{header}\n{details}\n\n{list_block}"
                        await update.message.reply_text(message, parse_mode="Markdown")
                        executed_actions.append("mark_done")
                    else:
                        await update.message.reply_text("⚠️ Не нашёл такую задачу.")
                set_ctx(user_id, last_action="mark_done", last_list=list_name)
            except Exception as e:
                logger.exception(f"Mark done error: {e}")
                await update.message.reply_text("⚠️ Не удалось отметить задачу. Проверь логи.")
        elif action == "rename_list" and entity_type == "list" and list_name and title:
            try:
                logger.info(f"Renaming list: {list_name} to {title}")
                renamed = rename_list(conn, user_id, list_name, title)
                if renamed:
                    icon = get_action_icon("rename_list")
                    await update.message.reply_text(f"{icon} Список {list_name} переименован в {title}.", parse_mode="Markdown")
                    set_ctx(user_id, last_action="rename_list", last_list=title)
                else:
                    await update.message.reply_text(f"⚠️ Список {list_name} не найден или {title} уже существует.")
            except Exception as e:
                logger.exception(f"Rename list error: {e}")
                await update.message.reply_text("⚠️ Не удалось переименовать список. Проверь логи.")
        elif action == "move_entity" and entity_type and title and obj.get("list") and obj.get("to_list"):
            try:
                target_list_name = obj["to_list"]
                logger.info(f"Moving {entity_type} '{title}' from {obj['list']} to {target_list_name}")
                list_exists = find_list(conn, user_id, obj["list"])
                to_list_exists = find_list(conn, user_id, target_list_name)
                if not list_exists:
                    await update.message.reply_text(f"⚠️ Список *{obj['list']}* не найден.")
                    continue
                if not to_list_exists:
                    logger.info(f"Creating target list '{target_list_name}' for user {user_id}")
                    create_result = create_list(conn, user_id, target_list_name)
                    if create_result.get("duplicate_detected"):
                        target_list_name = create_result.get("duplicate_title") or target_list_name
                        logger.info(
                            "Using existing list '%s' for move instead of creating new",
                            target_list_name,
                        )
                if meta.get("fuzzy"):
                    logger.info(f"Moving task fuzzy: {title} from {obj['list']} to {target_list_name}")
                    tasks = get_list_tasks(conn, user_id, obj["list"])
                    matched = None
                    for _, task_title in tasks:
                        if title.lower() in task_title.lower():
                            matched = task_title
                            break
                    if matched:
                        updated = move_entity(
                            conn,
                            user_id,
                            entity_type,
                            matched,
                            obj["list"],
                            target_list_name,
                        )
                        if updated:
                            action_icon = get_action_icon("move_entity")
                            target_label = (
                                target_list_name
                                if STYLE == "vibrant"
                                else f"{LIST_ICON} {target_list_name}"
                            )
                            header = (
                                f"{action_icon} Перемещено: {matched} → в {target_label}{_task_suffix(matched)}"
                            )
                            list_block = format_list_output(
                                conn,
                                user_id,
                                target_list_name,
                                heading_label=format_section_title(target_list_name),
                            )
                            message = f"{header}\n\n{list_block}"
                            await update.message.reply_text(message, parse_mode="Markdown")
                            set_ctx(user_id, last_action="move_entity", last_list=target_list_name)
                            executed_actions.append("move_entity")
                        else:
                            await update.message.reply_text(f"⚠️ Не удалось переместить *{matched}*. Проверь, есть ли такая задача.")
                    else:
                        await update.message.reply_text(f"⚠️ Задача *{title}* не найдена в *{obj['list']}*.")
                else:
                    updated = move_entity(
                        conn,
                        user_id,
                        entity_type,
                        title,
                        obj["list"],
                        target_list_name,
                    )
                    if updated:
                        action_icon = get_action_icon("move_entity")
                        target_label = (
                            target_list_name
                            if STYLE == "vibrant"
                            else f"{LIST_ICON} {target_list_name}"
                        )
                        header = (
                            f"{action_icon} Перемещено: {title} → в {target_label}{_task_suffix(title)}"
                        )
                        list_block = format_list_output(
                            conn,
                            user_id,
                            target_list_name,
                            heading_label=format_section_title(target_list_name),
                        )
                        message = f"{header}\n\n{list_block}"
                        await update.message.reply_text(message, parse_mode="Markdown")
                        set_ctx(user_id, last_action="move_entity", last_list=target_list_name)
                        executed_actions.append("move_entity")
                    else:
                        await update.message.reply_text(f"⚠️ Не удалось переместить *{title}*. Проверь, есть ли такая задача.")
            except Exception as e:
                logger.exception(f"Move entity error: {e}")
                await update.message.reply_text("⚠️ Не удалось переместить задачу. Проверь логи.")
        elif action == "update_task" and entity_type == "task" and list_name:
            try:
                logger.info(f"Updating task in list: {list_name}")
                if meta.get("by_index") and meta.get("new_title"):
                    logger.info(f"Updating task by index: {meta['by_index']} to '{meta['new_title']}' in list: {list_name}")
                    updated, old_title = update_task_by_index(conn, user_id, list_name, meta["by_index"], meta["new_title"])
                    if updated:
                        action_icon = get_action_icon("update_task")
                        if STYLE == "vibrant":
                            header = f"{action_icon} Обновлено в {list_name}:"
                        else:
                            header = f"{action_icon} Обновлено в {LIST_ICON} {list_name}:"
                        details = f"{action_icon} {old_title} → {meta['new_title']}{_task_suffix(meta['new_title'])}"
                        list_block = format_list_output(conn, user_id, list_name, heading_label=format_section_title(list_name))
                        message = f"{header}\n{details}\n\n{list_block}"
                        await update.message.reply_text(message, parse_mode="Markdown")
                    else:
                        await update.message.reply_text(f"⚠️ Не удалось изменить задачу по индексу {meta['by_index']} в списке *{list_name}*.")
                elif title and meta.get("new_title"):
                    logger.info(f"Updating task: {title} to {meta['new_title']} in list: {list_name}")
                    updated = update_task(conn, user_id, list_name, title, meta["new_title"])
                    if updated:
                        action_icon = get_action_icon("update_task")
                        if STYLE == "vibrant":
                            header = f"{action_icon} Обновлено в {list_name}:"
                        else:
                            header = f"{action_icon} Обновлено в {LIST_ICON} {list_name}:"
                        details = f"{action_icon} {title} → {meta['new_title']}{_task_suffix(meta['new_title'])}"
                        list_block = format_list_output(conn, user_id, list_name, heading_label=format_section_title(list_name))
                        message = f"{header}\n{details}\n\n{list_block}"
                        await update.message.reply_text(message, parse_mode="Markdown")
                    else:
                        await update.message.reply_text(f"⚠️ Не удалось изменить задачу *{title}* в списке *{list_name}*.")
                else:
                    await update.message.reply_text(f"🤔 Уточни, на что изменить задачу в списке *{list_name}*.")
                    await send_menu(update, context)
                    continue
                set_ctx(user_id, last_action="update_task", last_list=list_name)
            except Exception as e:
                logger.exception(f"Update task error: {e}")
                await update.message.reply_text("⚠️ Не удалось изменить задачу. Проверь логи.")
        elif action == "update_profile" and entity_type == "user_profile" and meta:
            try:
                logger.info(f"Updating user profile for user {user_id}: {meta}")
                update_user_profile(conn, user_id, meta.get("city"), meta.get("profession"))
                await update.message.reply_text("🆙 Профиль обновлён!", parse_mode="Markdown")
            except Exception as e:
                logger.exception(f"Update profile error: {e}")
                await update.message.reply_text("⚠️ Не удалось обновить профиль. Проверь логи.")
        elif action == "restore_task" and entity_type == "task" and list_name and title:
            try:
                logger.info(f"Restoring task: {title} in list: {list_name}")
                if meta.get("fuzzy"):
                    restored, matched, suggestion = restore_task_fuzzy(conn, user_id, list_name, title)
                else:
                    restored, matched, suggestion = restore_task(conn, user_id, list_name, title)
                if restored:
                    resolved_title = matched or title
                    icon = get_action_icon("restore_task")
                    await update.message.reply_text(
                        f"{icon} Задача {resolved_title} восстановлена в списке {list_name}.",
                        parse_mode="Markdown",
                    )
                elif suggestion:
                    await update.message.reply_text(suggestion)
                else:
                    await update.message.reply_text(f"⚠️ Не удалось восстановить *{title}*.")
                set_ctx(user_id, last_action="restore_task", last_list=list_name)
            except Exception as e:
                logger.exception(f"Restore task error: {e}")
                await update.message.reply_text("⚠️ Не удалось восстановить задачу. Проверь логи.")
        elif action == "say" and obj.get("text"):
            try:
                logger.info(f"Say: {obj['text']}")
                await update.message.reply_text(obj.get("text"))
            except Exception as e:
                logger.exception(f"Say error: {e}")
                await update.message.reply_text("⚠️ Не удалось отправить сообщение. Проверь логи.")
        elif action == "clarify" and meta.get("question"):
            try:
                logger.info(f"Clarify: {meta['question']}")
                keyboard = [[InlineKeyboardButton("Да", callback_data=f"clarify_yes:{meta.get('pending')}"), InlineKeyboardButton("Нет", callback_data="clarify_no")]]
                reply_markup = InlineKeyboardMarkup(keyboard)
                await update.message.reply_text("🤔 " + meta.get("question"), parse_mode="Markdown", reply_markup=reply_markup)
                set_ctx(user_id, pending_delete=meta.get("pending"))
                await send_menu(update, context)
            except Exception as e:
                logger.exception(f"Clarify error: {e}")
                await update.message.reply_text("⚠️ Не удалось уточнить. Проверь логи.")
        else:
            name_from_text = text_mentions_list_and_name(original_text)
            if name_from_text:
                logger.info(f"Showing tasks for list from text: {name_from_text}")
                items = get_list_tasks(conn, user_id, name_from_text)
                if items:
                    message = format_list_output(
                        conn,
                        user_id,
                        name_from_text,
                        heading_label=format_section_title(name_from_text),
                    )
                    await update.message.reply_text(message, parse_mode="Markdown")
                    set_ctx(user_id, last_action="show_tasks", last_list=name_from_text)
                    continue
                await update.message.reply_text(f"Список *{name_from_text}* пуст или не существует.")
            logger.info("Unknown command, no context match")
            await update.message.reply_text("🤔 Не понял, что нужно сделать.")
            await send_menu(update, context)
        logger.info(f"User {user_id}: {original_text} -> Action: {action}")

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE, input_text: str | None = None):
    user_id = update.effective_user.id
    text = (input_text or update.message.text or "").strip()
    logger.info("📩 Text from %s: %s", user_id, text)
    try:
        conn = get_conn()
        history = get_ctx(user_id, "history", [])
        db_state, session_state = build_semantic_state(conn, user_id, history)
        user_profile = get_user_profile(conn, user_id)
        prompt_values = _PromptValues(
            history=json.dumps(history, ensure_ascii=False),
            db_state=json.dumps(db_state, ensure_ascii=False),
            session_state=json.dumps(session_state, ensure_ascii=False),
            user_profile=json.dumps(user_profile, ensure_ascii=False),
            lexicon=SEMANTIC_LEXICON_JSON,
            pending_delete=get_ctx(user_id, "pending_delete", ""),
        )
        prompt = SEMANTIC_PROMPT.format_map(prompt_values)
        logger.info("Dispatching text to OpenAI model '%s'", OPENAI_MODEL)
        try:
            resp = client.chat.completions.create(
                model=OPENAI_MODEL,
                messages=[
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": text},
                ],
            )
        except AuthenticationError as auth_error:
            logger.error("OpenAI authentication failed: %s", auth_error)
            await update.message.reply_text(
                "⚠️ Ошибка авторизации OpenAI. Проверь API-ключ.")
            return
        except (
            APIConnectionError,
            APIError,
            APITimeoutError,
            OpenAIError,
            RateLimitError,
        ) as api_error:
            logger.error(
                "OpenAI API error while processing message for user %s: %s",
                user_id,
                api_error,
                exc_info=True,
            )
            await update.message.reply_text(
                "⚠️ OpenAI временно недоступен. Попробуй ещё раз позже.")
            await send_menu(update, context)
            return
        raw = resp.choices[0].message.content.strip()
        logger.info("🤖 RAW response: %s", raw)
        try:
            with open(RAW_LOG_FILE, "a", encoding="utf-8") as f:
                f.write(f"\n=== RAW ({user_id}) ===\n{text}\n{raw}\n")
        except Exception:
            logger.exception("Failed to write to openai_raw.log")
        actions = extract_json_blocks(raw)
        if not actions:
            if wants_expand(text) and get_ctx(user_id, "last_action") == "show_lists":
                logger.info("No actions, but expanding lists due to context")
                await expand_all_lists(update, conn, user_id, context)
                return
            logger.warning("No valid JSON actions from OpenAI")
            await update.message.reply_text("⚠️ Модель ответила не в JSON-формате.")
            await send_menu(update, context)
            return
        await route_actions(update, context, actions, user_id, text)
        set_ctx(user_id, history=history + [text])
    except Exception as e:
        logger.exception(f"❌ handle_text error: {e}")
        await update.message.reply_text("Произошла ошибка при обработке. Проверь логи.")
        await send_menu(update, context)

async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    logger.info("🎙 Voice from %s", user_id)
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
        logger.info("🗣 ASR transcript: %s", text)
        await update.message.reply_text(f"🗣 {text}")
        await handle_text(update, context, input_text=text)
        try:
            os.remove(ogg)
            os.remove(wav)
        except Exception:
            logger.warning("Failed to clean up temp voice files %s and %s", ogg, wav, exc_info=True)
    except Exception as e:
        logger.exception(f"❌ voice error: {e}")
        await update.message.reply_text("⚠️ Не удалось обработать голос. Проверь логи.")
        await send_menu(update, context)

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    data = query.data
    logger.info(f"Callback from {user_id}: {data}")
    try:
        if data.startswith("delete_list:"):
            list_name = data.split(":")[1]
            deleted = delete_list(get_conn(), user_id, list_name)
            if deleted:
                await query.edit_message_text(f"🗑 Список *{list_name}* удалён.", parse_mode="Markdown")
                set_ctx(user_id, last_action="delete_list", last_list=None, pending_delete=None)
            else:
                await query.edit_message_text(f"⚠️ Список *{list_name}* не найден.")
                set_ctx(user_id, pending_delete=None)
        elif data == "cancel_delete":
            await query.edit_message_text("Хорошо, отмена удаления.")
            set_ctx(user_id, pending_delete=None)
        elif data.startswith("clarify_yes:"):
            list_name = data.split(":")[1]
            deleted = delete_list(get_conn(), user_id, list_name)
            if deleted:
                await query.edit_message_text(f"🗑 Список *{list_name}* удалён.", parse_mode="Markdown")
                set_ctx(user_id, last_action="delete_list", last_list=None, pending_delete=None)
            else:
                await query.edit_message_text(f"⚠️ Список *{list_name}* не найден.")
                set_ctx(user_id, pending_delete=None)
        elif data == "clarify_no":
            await query.edit_message_text("Хорошо, отмена удаления.")
            set_ctx(user_id, pending_delete=None)
        else:
            await query.edit_message_text("⚠️ Неизвестная команда.")
    except Exception as e:
        logger.exception(f"Callback error: {e}")
        await query.edit_message_text("⚠️ Ошибка обработки. Проверь логи.")

def main():
    init_db()
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_handler(CallbackQueryHandler(handle_callback))
    logger.info("🚀 Aura v5.2 started.")
    app.run_polling()

if __name__ == "__main__":
    main()
