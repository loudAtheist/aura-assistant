import os, json, re, logging
from pathlib import Path
from dotenv import load_dotenv
from telegram import Update, ReplyKeyboardMarkup, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ApplicationBuilder, MessageHandler, CallbackQueryHandler, ContextTypes, filters
import speech_recognition as sr
from pydub import AudioSegment
from openai import OpenAI
from datetime import datetime, timedelta
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

# ========= LOG =========
logging.basicConfig(
    filename="/opt/aura-assistant/aura.log",
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)

client = OpenAI(api_key=OPENAI_API_KEY)

# ========= DIALOG CONTEXT (per-user) =========
SESSION: dict[int, dict] = {}  # { user_id: {"last_action": str, "last_list": str, "history": [str], "pending_delete": str, "pending_confirmation": dict} }
SIGNIFICANT_ACTIONS = {"create", "add_task", "move_entity", "mark_done", "restore_task", "delete_task", "delete_list"}

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
    sess.update({k:v for k,v in kw.items() if v is not None})
    if "history" in kw and isinstance(kw["history"], list):
        seen = set()
        sess["history"] = [x for x in kw["history"][-10:] if not (x in seen or seen.add(x))]
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
SEMANTIC_PROMPT = """
Ты — Aura, дружелюбный и остроумный ассистент, который понимает смысл человеческих фраз и управляет локальной Entity System (списки, задачи). Ты ведёшь себя как живой помощник: приветствуешь, поддерживаешь, шутишь к месту, переспрашиваешь, если нужно, и всегда действуешь осмысленно.

Как ты думаешь:
- Сначала подумай шаг за шагом: 1) Какое намерение? 2) Какой контекст (последний список, история)? 3) Какое действие выбрать?
- Учитывай последние сообщения (контекст: {history}) и состояние базы (db_state: {db_state}).
- Учитывай профиль пользователя (город, профессия): {user_profile}.
- Если пользователь говорит «туда», «в него», «этот список» — это последний упомянутый список (db_state.last_list или история).
- Приоритет точного имени списка над контекстом (например, «Домашние дела» важнее last_list).
- Команда «Покажи список <название>» или «покажи <название>» → показать задачи (action: show_tasks, entity_type: task, list: <название>).
- Если в одной команде несколько раз встречается «список <имя>» для создания — верни отдельные действия create для каждого списка в одном JSON-массиве.
- Если список запрошен, но отсутствует в db_state.lists — верни clarify с вопросом «Списка *<имя>* нет. Создать?» и meta.pending = «<имя>».
- Если в запросе несколько задач (например, «добавь постирать ковер помыть машину»), используй ключ tasks для множественного добавления.
- Если в запросе несколько задач для завершения (например, «лук молоко хлеб куплены»), используй ключ tasks для множественного mark_done.
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
- Маркеры завершения («выполнено», «сделано», «куплено») — mark_done с fuzzy-поиском для каждой задачи в tasks.
- Удаление списка требует подтверждения («да»/«нет»), после «да» список удаляется, контекст очищается.
- Социальные реплики — action: say.
- Только JSON.

Примеры:
- «Создай список Работа внеси задачи исправить договор сходить к нотариусу» → {{ "action": "create", "entity_type": "list", "list": "Работа", "tasks": ["Исправить договор", "Сходить к нотариусу"] }}
- «Создай список Работа и список Домашние дела» → [{{ "action": "create", "entity_type": "list", "list": "Работа" }}, {{ "action": "create", "entity_type": "list", "list": "Домашние дела" }}]
- «В список Домашние дела добавь постирать ковер помыть машину купить маленький нож» → {{ "action": "add_task", "entity_type": "task", "list": "Домашние дела", "tasks": ["Постирать ковер", "Помыть машину", "Купить маленький нож"] }}
- «Лук молоко хлеб куплены» → {{ "action": "mark_done", "entity_type": "task", "list": "Домашние дела", "tasks": ["Купить лук", "Купить молоко", "Купить хлеб"], "meta": {{ "fuzzy": true }} }}
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


def extract_tasks_from_phrase(phrase: str) -> list[str]:
    if not phrase:
        return []
    split_pattern = r"(?:[,;]|\bи\b|\bкуплен[аоы]?\b|\bкуплены\b|\bготов[аоы]?\b|\bвыполнен[аоы]?\b|\bсделан[аоы]?\b)"
    raw_parts = [
        p.strip()
        for p in re.split(split_pattern, phrase, flags=re.IGNORECASE)
        if p and p.strip()
    ]
    parts: list[str] = []
    for part in raw_parts:
        cleaned = re.sub(r"\b(куплен[аоы]?|куплены|готов[аоы]?|выполнен[аоы]?|сделан[аоы]?)\b", " ", part, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
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

def split_user_commands(text: str) -> list[str]:
    if not text:
        return []
    normalized = text.replace("\r", "\n")
    raw_parts = re.split(r'(?:[.,;]+|\n+|\bи\b)', normalized, flags=re.IGNORECASE)
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

async def handle_pending_confirmation(message, context: ContextTypes.DEFAULT_TYPE, conn, user_id: int, pending_confirmation: dict):
    if not pending_confirmation:
        return
    conf_type = pending_confirmation.get("type")
    if conf_type == "delete_tasks":
        tasks = pending_confirmation.get("tasks") or []
        if not tasks:
            await message.reply_text("⚠️ Нет задач для удаления.")
            set_ctx(user_id, pending_confirmation=None)
            return
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
    elif conf_type == "create_list":
        list_to_create = pending_confirmation.get("list")
        if not list_to_create:
            await message.reply_text("⚠️ Не понимаю, какой список создать.")
            set_ctx(user_id, pending_confirmation=None)
            return
        existing = find_list(conn, user_id, list_to_create)
        if existing:
            await message.reply_text(f"⚠️ Список *{list_to_create}* уже существует.", parse_mode="Markdown")
            set_ctx(user_id, pending_confirmation=None, last_list=list_to_create)
            return
        try:
            create_list(conn, user_id, list_to_create)
            action_icon = get_action_icon("create")
            header = f"{action_icon} Создан новый список {LIST_ICON} *{list_to_create}*."
            list_block = format_list_output(conn, user_id, list_to_create, heading_label=f"{SECTION_ICON} *Актуальный список:*")
            await message.reply_text(f"{header}  \n\n{list_block}", parse_mode="Markdown")
            set_ctx(user_id, pending_confirmation=None, last_action="create_list", last_list=list_to_create)
        except Exception as e:
            logging.exception(f"Create list via confirmation error: {e}")
            await message.reply_text("⚠️ Не удалось создать список. Проверь логи.")
            set_ctx(user_id, pending_confirmation=None)
    else:
        await message.reply_text("⚠️ Не удалось обработать подтверждение. Попробуй сформулировать команду заново.")
        set_ctx(user_id, pending_confirmation=None)

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

async def route_actions(update: Update, context: ContextTypes.DEFAULT_TYPE, actions: list, user_id: int, original_text: str) -> list[str]:
    conn = get_conn()
    logging.info(f"Processing actions: {json.dumps(actions)}")
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
            try:
                logging.info(f"Creating list: {obj['list']}")
                create_list(conn, user_id, obj["list"])
                if obj.get("tasks"):
                    added_tasks = []
                    for t in obj["tasks"]:
                        task_id = add_task(conn, user_id, obj["list"], t)
                        if task_id:
                            added_tasks.append(t)
                    action_icon = get_action_icon("create")
                    add_icon = get_action_icon("add_task")
                    header = f"{action_icon} Создан новый список {LIST_ICON} *{obj['list']}*."
                    if added_tasks:
                        details = "\n".join(f"{add_icon} {task}" for task in added_tasks)
                    else:
                        details = f"⚠️ Задачи уже были в {LIST_ICON} *{obj['list']}*."
                    list_block = format_list_output(conn, user_id, obj["list"], heading_label=f"{SECTION_ICON} *Актуальный список:*")
                    message = f"{header}  \n{details}\n\n{list_block}"
                    await update.message.reply_text(message, parse_mode="Markdown")
                else:
                    action_icon = get_action_icon("create")
                    header = f"{action_icon} Создан новый список {LIST_ICON} *{obj['list']}*."
                    list_block = format_list_output(conn, user_id, obj["list"], heading_label=f"{SECTION_ICON} *Актуальный список:*")
                    await update.message.reply_text(f"{header}  \n\n{list_block}", parse_mode="Markdown")
                set_ctx(user_id, last_action="create_list", last_list=obj["list"])
                executed_actions.append("create")
            except Exception as e:
                logging.exception(f"Create list error: {e}")
                await update.message.reply_text("⚠️ Не удалось создать список. Проверь логи.")
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
            db_state = {
                "lists": {n: [t for _, t in get_list_tasks(conn, user_id, n)] for n in get_all_lists(conn, user_id)},
                "last_list": get_ctx(user_id, "last_list"),
                "pending_delete": get_ctx(user_id, "pending_delete")
            }
            user_profile = get_user_profile(conn, user_id)
            prompt = SEMANTIC_PROMPT.format(history=json.dumps(history, ensure_ascii=False),
                                           db_state=json.dumps(db_state, ensure_ascii=False),
                                           user_profile=json.dumps(user_profile, ensure_ascii=False),
                                           pending_delete=get_ctx(user_id, "pending_delete", ""))
            logging.info(f"Sending to OpenAI: {command_text}")
            resp = client.chat.completions.create(
                model=OPENAI_MODEL,
                messages=[
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": command_text}
                ],
            )
            raw = resp.choices[0].message.content.strip()
            logging.info(f"🤖 RAW: {raw}")
            try:
                with open("/opt/aura-assistant/openai_raw.log", "a", encoding="utf-8") as f:
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
            if any(action in SIGNIFICANT_ACTIONS for action in executed_actions):
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
                await handle_pending_confirmation(query.message, context, conn, user_id, pending_conf)
                set_ctx(user_id, pending_confirmation=None)
                await query.edit_message_text("✅ Подтверждение получено.")
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
