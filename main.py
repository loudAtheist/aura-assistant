import os, json, re, logging
from pathlib import Path
from dotenv import load_dotenv
from telegram import Update, ReplyKeyboardMarkup, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ApplicationBuilder, MessageHandler, CallbackQueryHandler, ContextTypes, filters
import speech_recognition as sr
from pydub import AudioSegment
from openai import OpenAI
from db import (
    rename_list, normalize_text, init_db, get_conn, get_all_lists, get_list_tasks, add_task, delete_list,
    mark_task_done, mark_task_done_fuzzy, delete_task, restore_task, find_list, fetch_task, fetch_list_by_task,
    delete_task_fuzzy, delete_task_by_index, create_list, move_entity, get_all_tasks, update_user_profile,
    get_user_profile, get_completed_tasks, search_tasks, update_task, update_task_by_index, restore_task_fuzzy
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

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("AURA_DATA_DIR") or BASE_DIR).expanduser().resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)
TEMP_DIR = Path(os.getenv("TEMP_DIR") or (DATA_DIR / "tmp")).expanduser().resolve()
TEMP_DIR.mkdir(parents=True, exist_ok=True)
LOG_PATH = Path(os.getenv("AURA_LOG_PATH") or (DATA_DIR / "aura.log")).expanduser().resolve()
LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
OPENAI_LOG_PATH = Path(os.getenv("OPENAI_LOG_PATH") or (DATA_DIR / "openai_raw.log")).expanduser().resolve()
OPENAI_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)

# ========= LOG =========
logging.basicConfig(
    filename=str(LOG_PATH),
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)

client: OpenAI | None = None

# ========= DIALOG CONTEXT (per-user) =========
SESSION: dict[int, dict] = {}  # { user_id: {"last_action": str, "last_list": str, "history": [str], "pending_delete": str} }

def set_ctx(user_id: int, **kw):
    sess = SESSION.get(user_id, {"history": [], "last_list": None, "last_action": None, "pending_delete": None})
    sess.update({k:v for k,v in kw.items() if v is not None})
    if "history" in kw and isinstance(kw["history"], list):
        seen = set()
        sess["history"] = [x for x in kw["history"][-10:] if not (x in seen or seen.add(x))]
    SESSION[user_id] = sess
    logging.info(f"Updated context for user {user_id}: {sess}")

def get_ctx(user_id: int, key: str, default=None):
    return SESSION.get(user_id, {"history": [], "last_list": None, "last_action": None, "pending_delete": None}).get(key, default)

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
- Если в запросе несколько задач (например, «добавь постирать ковер помыть машину»), используй ключ tasks для множественного добавления.
- Если в запросе несколько задач для завершения (например, «лук молоко хлеб куплены»), используй ключ tasks для множественного mark_done.
- Поиск задач (например, «найди задачи с договор») должен быть регистронезависимым и искать по частичному совпадению.
- Удаление списка требует подтверждения («да»/«нет»), после «да» список удаляется, контекст очищается.
- Восстановление задачи (например, «верни задачу») поддерживает fuzzy-поиск по частичному совпадению.
- Изменение задачи (например, «измени четвёртый пункт») поддерживает указание по индексу (meta.by_index).
- Перенос задачи (например, «перенеси задачу») поддерживает fuzzy-поиск по частичному совпадению (meta.fuzzy: true).
- Решение: create/add_task/show_lists/show_tasks/show_all_tasks/mark_done/delete_task/delete_list/move_entity/search_entity/rename_list/update_profile/restore_task/show_completed_tasks/update_task/unknown.
- Если социальная реплика (привет, благодарность, «как дела?») — action: say.
- Если запрос неясен — action: clarify с вопросом.
- Нормализуй вход (регистры, пробелы, ошибки речи), но сохраняй смысл.
- Для удаления списка всегда используй clarify сначала: {{ "action": "clarify", "meta": {{ "question": "Уверен, что хочешь удалить список {pending_delete}? Скажи 'да' или 'нет'.", "pending": "{pending_delete}" }} }}
- Если команда «да» и есть pending_delete в контексте, возвращай: {{ "action": "delete_list", "entity_type": "list", "list": "{pending_delete}" }}
- Никогда не обрезай JSON. Всегда полный объект.

Формат ответа (строго JSON; без текста вне JSON):
- Для действий над базой:
{{ "action": "create|add_task|show_lists|show_tasks|show_all_tasks|mark_done|delete_task|delete_list|move_entity|search_entity|rename_list|update_profile|restore_task|show_completed_tasks|update_task|unknown",
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
- «В список Домашние дела добавь постирать ковер помыть машину купить маленький нож» → {{ "action": "add_task", "entity_type": "task", "list": "Домашние дела", "tasks": ["Постирать ковер", "Помыть машину", "Купить маленький нож"] }}
- «Лук молоко хлеб куплены» → {{ "action": "mark_done", "entity_type": "task", "list": "Домашние дела", "tasks": ["Купить лук", "Купить молоко", "Купить хлеб"], "meta": {{ "fuzzy": true }} }}
- «Переименуй список Покупки в Шопинг» → {{ "action": "rename_list", "entity_type": "list", "list": "Покупки", "title": "Шопинг" }}
- «Из списка Работа пункт Сделать уборку в гараже Перенеси в Домашние дела» → {{ "action": "move_entity", "entity_type": "task", "title": "Сделать уборку в гараже", "list": "Работа", "to_list": "Домашние дела", "meta": {{ "fuzzy": true }} }}
- «Сходить к нотариусу выполнен-конец» → {{ "action": "mark_done", "entity_type": "task", "list": "<последний список>", "title": "Сходить к нотариусу" }}
- «Покажи Домашние дела» → {{ "action": "show_tasks", "entity_type": "task", "list": "Домашние дела" }}
- «Покажи все мои дела» → {{ "action": "show_all_tasks", "entity_type": "task" }}
- «Найди задачи с договор» → {{ "action": "search_entity", "entity_type": "task", "meta": {{ "pattern": "договор" }} }}
- «Покажи выполненные задачи» → {{ "action": "show_completed_tasks", "entity_type": "task" }}
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
            logging.info(f"Extracted JSON list: {data[0]}")
            return [data[0]]  # Take first JSON to avoid duplicates
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
    return out[:1]  # Limit to one action to avoid duplicates

def wants_expand(text: str) -> bool:
    return bool(re.search(r'\b(разверну|подробн)\w*', (text or "").lower()))

def text_mentions_list_and_name(text: str):
    m = re.search(r'(?:список|лист)\s+([^\n\r]+)$', (text or "").strip(), re.IGNORECASE)
    if m:
        name = m.group(1).strip(" .!?:;«»'\"").strip()
        return name
    return None

async def send_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [["Показать списки", "Создать список"], ["Добавить задачу", "Помощь"]]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True, selective=True)
    await update.message.reply_text("Выбери действие или напиши/скажи:", reply_markup=reply_markup)

async def expand_all_lists(update: Update, conn, user_id: int, context: ContextTypes.DEFAULT_TYPE):
    lists = get_all_lists(conn, user_id)
    if not lists:
        await update.message.reply_text("Пока нет списков 🕊")
        return
    txt = "🗂 Твои списки:\n"
    for n in lists:
        txt += f"📋 *{n}*:\n"
        items = get_list_tasks(conn, user_id, n)
        if items:
            txt += "\n".join([f"{i}. {t}" for i, t in items])
        else:
            txt += "— пусто —"
        txt += "\n"
    await update.message.reply_text(txt, parse_mode="Markdown")
    set_ctx(user_id, last_action="show_lists")

async def route_actions(update: Update, context: ContextTypes.DEFAULT_TYPE, actions: list, user_id: int, original_text: str):
    conn = get_conn()
    logging.info(f"Processing actions: {json.dumps(actions)}")
    pending_delete = get_ctx(user_id, "pending_delete")
    if original_text.lower() in ["да", "yes"] and pending_delete:
        try:
            logging.info(f"Deleting list: {pending_delete}")
            deleted = delete_list(conn, user_id, pending_delete)
            if deleted:
                await update.message.reply_text(f"🗑 Список *{pending_delete}* удалён.", parse_mode="Markdown")
                set_ctx(user_id, pending_delete=None, last_list=None)
                logging.info(f"Confirmed delete_list: {pending_delete}")
            else:
                await update.message.reply_text(f"⚠️ Список *{pending_delete}* не найден.")
                set_ctx(user_id, pending_delete=None)
            return
        except Exception as e:
            logging.exception(f"Delete error: {e}")
            await update.message.reply_text("⚠️ Ошибка удаления.")
            set_ctx(user_id, pending_delete=None)
            return
    elif original_text.lower() in ["нет", "no"] and pending_delete:
        await update.message.reply_text("Удаление отменено.")
        set_ctx(user_id, pending_delete=None)
        return
    for obj in actions:
        action = obj.get("action", "unknown")
        entity_type = obj.get("entity_type", "task")
        list_name = obj.get("list") or get_ctx(user_id, "last_list")
        title = obj.get("title") or obj.get("task")
        meta = obj.get("meta", {})
        logging.info(f"Action: {action}, Entity: {entity_type}, List: {list_name}, Title: {title}")
        if action not in ["delete_list", "clarify"] and get_ctx(user_id, "pending_delete"):
            set_ctx(user_id, pending_delete=None)
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
                    if added_tasks:
                        await update.message.reply_text(f"🆕 Создан список *{obj['list']}* с задачами: {', '.join(added_tasks)}", parse_mode="Markdown")
                    else:
                        await update.message.reply_text(f"🆕 Создан список *{obj['list']}*, но задачи уже существуют.", parse_mode="Markdown")
                else:
                    await update.message.reply_text(f"🆕 Создан список *{obj['list']}*", parse_mode="Markdown")
                set_ctx(user_id, last_action="create_list", last_list=obj["list"])
            except Exception as e:
                logging.exception(f"Create list error: {e}")
                await update.message.reply_text("⚠️ Не удалось создать список. Проверь логи.")
        elif action == "add_task" and list_name:
            try:
                logging.info(f"Adding tasks to list: {list_name}")
                added_tasks = []
                if obj.get("tasks"):
                    for t in obj["tasks"]:
                        task_id = add_task(conn, user_id, list_name, t)
                        if task_id:
                            added_tasks.append(t)
                    if added_tasks:
                        await update.message.reply_text(f"✅ Добавлены задачи в *{list_name}*: {', '.join(added_tasks)}", parse_mode="Markdown")
                    else:
                        await update.message.reply_text(f"⚠️ Все указанные задачи уже есть в списке *{list_name}*.")
                elif title:
                    task_id = add_task(conn, user_id, list_name, title)
                    if task_id:
                        await update.message.reply_text(f"✅ Добавлено: *{title}* в список *{list_name}*", parse_mode="Markdown")
                    else:
                        await update.message.reply_text(f"⚠️ Задача *{title}* уже есть в списке *{list_name}*.")
                set_ctx(user_id, last_action="add_task", last_list=list_name)
            except Exception as e:
                logging.exception(f"Add task error: {e}")
                await update.message.reply_text("⚠️ Не удалось добавить задачу. Проверь логи.")
        elif action == "show_lists":
            try:
                logging.info("Showing all lists with tasks")
                lists = get_all_lists(conn, user_id)
                if not lists:
                    await update.message.reply_text("Пока нет списков 🕊")
                    set_ctx(user_id, last_action="show_lists")
                    continue
                txt = "🗂 Твои списки:\n"
                for n in lists:
                    txt += f"📋 *{n}*:\n"
                    items = get_list_tasks(conn, user_id, n)
                    if items:
                        txt += "\n".join([f"{i}. {t}" for i, t in items])
                    else:
                        txt += "— пусто —"
                    txt += "\n"
                await update.message.reply_text(txt, parse_mode="Markdown")
                set_ctx(user_id, last_action="show_lists")
            except Exception as e:
                logging.exception(f"Show lists error: {e}")
                await update.message.reply_text("⚠️ Не удалось получить списки. Проверь логи.")
        elif action == "show_tasks" and list_name:
            try:
                logging.info(f"Showing tasks for list: {list_name}")
                items = get_list_tasks(conn, user_id, list_name)
                if items:
                    txt = "\n".join([f"{i}. {t}" for i, t in items])
                    await update.message.reply_text(f"📋 *{list_name}:*\n{txt}", parse_mode="Markdown")
                else:
                    await update.message.reply_text(f"Список *{list_name}* пуст.", parse_mode="Markdown")
                set_ctx(user_id, last_action="show_tasks", last_list=list_name)
            except Exception as e:
                logging.exception(f"Show tasks error: {e}")
                await update.message.reply_text("⚠️ Не удалось получить задачи. Проверь логи.")
        elif action == "show_all_tasks":
            try:
                logging.info("Showing all tasks")
                lists = get_all_lists(conn, user_id)
                if not lists:
                    await update.message.reply_text("Пока нет дел 🕊")
                    set_ctx(user_id, last_action="show_all_tasks")
                    continue
                txt = "🗂 Все твои дела:\n"
                for n in lists:
                    txt += f"📋 *{n}*:\n"
                    items = get_list_tasks(conn, user_id, n)
                    if items:
                        txt += "\n".join([f"{i}. {t}" for i, t in items])
                    else:
                        txt += "— пусто —"
                    txt += "\n"
                await update.message.reply_text(txt, parse_mode="Markdown")
                set_ctx(user_id, last_action="show_all_tasks")
            except Exception as e:
                logging.exception(f"Show all tasks error: {e}")
                await update.message.reply_text("⚠️ Не удалось получить дела. Проверь логи.")
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
                    await update.message.reply_text(f"🗑 Удалено: *{matched}* из *{ln}*", parse_mode="Markdown")
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
                        await update.message.reply_text(f"🗑 Список *{list_name}* удалён.", parse_mode="Markdown")
                        set_ctx(user_id, last_action="delete_list", last_list=None, pending_delete=None)
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
                if obj.get("tasks"):
                    completed_tasks = []
                    for t in obj["tasks"]:
                        logging.info(f"Marking task done: {t} in list: {list_name}")
                        deleted, matched = mark_task_done_fuzzy(conn, user_id, list_name, t)
                        if deleted:
                            completed_tasks.append(matched)
                    if completed_tasks:
                        await update.message.reply_text(f"✔️ Готово: {', '.join(completed_tasks)}.", parse_mode="Markdown")
                    else:
                        await update.message.reply_text("⚠️ Не нашёл указанные задачи.")
                elif title:
                    logging.info(f"Marking task done: {title} in list: {list_name}")
                    deleted, matched = mark_task_done_fuzzy(conn, user_id, list_name, title)
                    if deleted:
                        await update.message.reply_text(f"✔️ Готово: *{matched}*.", parse_mode="Markdown")
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
                            await update.message.reply_text(f"🔄 Перемещено: *{matched}* в *{obj['to_list']}*.", parse_mode="Markdown")
                            set_ctx(user_id, last_action="move_entity", last_list=obj["to_list"])
                        else:
                            await update.message.reply_text(f"⚠️ Не удалось переместить *{matched}*. Проверь, есть ли такая задача.")
                    else:
                        await update.message.reply_text(f"⚠️ Задача *{title}* не найдена в *{obj['list']}*.")
                else:
                    updated = move_entity(conn, user_id, entity_type, title, obj["to_list"])
                    if updated:
                        await update.message.reply_text(f"🔄 Перемещено: *{title}* в *{obj['to_list']}*.", parse_mode="Markdown")
                        set_ctx(user_id, last_action="move_entity", last_list=obj["to_list"])
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
                        await update.message.reply_text(f"🔄 Задача *{old_title}* изменена на *{meta['new_title']}* в списке *{list_name}*.", parse_mode="Markdown")
                    else:
                        await update.message.reply_text(f"⚠️ Не удалось изменить задачу по индексу {meta['by_index']} в списке *{list_name}*.")
                elif title and meta.get("new_title"):
                    logging.info(f"Updating task: {title} to {meta['new_title']} in list: {list_name}")
                    updated = update_task(conn, user_id, list_name, title, meta["new_title"])
                    if updated:
                        await update.message.reply_text(f"🔄 Задача *{title}* изменена на *{meta['new_title']}* в списке *{list_name}*.", parse_mode="Markdown")
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
                    await update.message.reply_text(f"🔄 Задача *{matched}* восстановлена в списке *{list_name}*.", parse_mode="Markdown")
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
                logging.info(f"Clarify: {meta['question']}")
                keyboard = [[InlineKeyboardButton("Да", callback_data=f"clarify_yes:{meta.get('pending')}"), InlineKeyboardButton("Нет", callback_data="clarify_no")]]
                reply_markup = InlineKeyboardMarkup(keyboard)
                await update.message.reply_text("🤔 " + meta.get("question"), parse_mode="Markdown", reply_markup=reply_markup)
                set_ctx(user_id, pending_delete=meta.get("pending"))
                await send_menu(update, context)
            except Exception as e:
                logging.exception(f"Clarify error: {e}")
                await update.message.reply_text("⚠️ Не удалось уточнить. Проверь логи.")
        else:
            name_from_text = text_mentions_list_and_name(original_text)
            if name_from_text:
                logging.info(f"Showing tasks for list from text: {name_from_text}")
                items = get_list_tasks(conn, user_id, name_from_text)
                if items:
                    txt = "\n".join([f"{i}. {t}" for i, t in items])
                    await update.message.reply_text(f"📋 *{name_from_text}:*\n{txt}", parse_mode="Markdown")
                    set_ctx(user_id, last_action="show_tasks", last_list=name_from_text)
                    continue
                await update.message.reply_text(f"Список *{name_from_text}* пуст или не существует.")
            logging.info("Unknown command, no context match")
            await update.message.reply_text("🤔 Не понял, что нужно сделать.")
            await send_menu(update, context)
        logging.info(f"User {user_id}: {original_text} -> Action: {action}")

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE, input_text: str | None = None):
    user_id = update.effective_user.id
    text = (input_text or update.message.text or "").strip()
    logging.info(f"📩 Text from {user_id}: {text}")
    try:
        if client is None:
            logging.error("OpenAI client is not configured")
            await update.message.reply_text("⚠️ Конфигурация OpenAI не настроена.")
            await send_menu(update, context)
            return
        conn = get_conn()
        db_state = {
            "lists": {n: [t for _, t in get_list_tasks(conn, user_id, n)] for n in get_all_lists(conn, user_id)},
            "last_list": get_ctx(user_id, "last_list"),
            "pending_delete": get_ctx(user_id, "pending_delete")
        }
        history = get_ctx(user_id, "history", [])
        user_profile = get_user_profile(conn, user_id)
        prompt = SEMANTIC_PROMPT.format(history=json.dumps(history, ensure_ascii=False),
                                       db_state=json.dumps(db_state, ensure_ascii=False),
                                       user_profile=json.dumps(user_profile, ensure_ascii=False),
                                       pending_delete=get_ctx(user_id, "pending_delete", ""))
        logging.info(f"Sending to OpenAI: {text}")
        resp = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": text}
            ],
        )
        raw = resp.choices[0].message.content.strip()
        logging.info(f"🤖 RAW: {raw}")
        try:
            with OPENAI_LOG_PATH.open("a", encoding="utf-8") as f:
                f.write(f"\n=== RAW ({user_id}) ===\n{text}\n{raw}\n")
        except Exception:
            logging.warning("Failed to write to openai_raw.log")
        actions = extract_json_blocks(raw)
        if not actions:
            if wants_expand(text) and get_ctx(user_id, "last_action") == "show_lists":
                logging.info("No actions, but expanding lists due to context")
                await expand_all_lists(update, conn, user_id, context)
                return
            logging.warning("No valid JSON actions from OpenAI")
            await update.message.reply_text("⚠️ Модель ответила не в JSON-формате.")
            await send_menu(update, context)
            return
        await route_actions(update, context, actions, user_id, text)
        set_ctx(user_id, history=history + [text])
    except Exception as e:
        logging.exception(f"❌ handle_text error: {e}")
        await update.message.reply_text("Произошла ошибка при обработке. Проверь логи.")
        await send_menu(update, context)

async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    logging.info(f"🎙 Voice from {user_id}")
    try:
        vf = await update.message.voice.get_file()
        ogg = TEMP_DIR / f"{user_id}_voice.ogg"
        wav = TEMP_DIR / f"{user_id}_voice.wav"
        await vf.download_to_drive(str(ogg))
        AudioSegment.from_ogg(str(ogg)).export(str(wav), format="wav")
        r = sr.Recognizer()
        with sr.AudioFile(str(wav)) as src:
            audio = r.record(src)
            text = r.recognize_google(audio, language="ru-RU")
            text = normalize_text(text)
        logging.info(f"🗣 ASR: {text}")
        await update.message.reply_text(f"🗣 {text}")
        await handle_text(update, context, input_text=text)
        try:
            if ogg.exists():
                ogg.unlink()
            if wav.exists():
                wav.unlink()
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
        logging.exception(f"Callback error: {e}")
        await query.edit_message_text("⚠️ Ошибка обработки. Проверь логи.")

def main():
    if not TELEGRAM_TOKEN:
        raise RuntimeError("TELEGRAM_TOKEN is not set. Укажи токен бота в переменной окружения TELEGRAM_TOKEN.")
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY is not set. Укажи ключ OpenAI в переменной окружения OPENAI_API_KEY.")
    global client
    client = OpenAI(api_key=OPENAI_API_KEY)
    init_db()
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_handler(CallbackQueryHandler(handle_callback))
    logging.info("🚀 Aura v5.2 started.")
    app.run_polling()

if __name__ == "__main__":
    main()
