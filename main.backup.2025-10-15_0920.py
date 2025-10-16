import os, json, re, logging
from pathlib import Path
from dotenv import load_dotenv
from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import ApplicationBuilder, MessageHandler, CallbackQueryHandler, ContextTypes, filters
import speech_recognition as sr
from pydub import AudioSegment
from openai import OpenAI
from datetime import datetime, timedelta

from db import (
    rename_list, normalize_text, init_db, get_conn, get_all_lists, get_list_tasks, add_task, delete_list,
    mark_task_done, mark_task_done_fuzzy, delete_task, restore_task, find_list, fetch_task, fetch_list_by_task,
    delete_task_fuzzy, delete_task_by_index, convert_entity, create_list, move_entity, get_all_tasks, update_user_profile, get_user_profile
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
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
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
SESSION: dict[int, dict] = {}   # { user_id: {"last_action": str, "last_list": str, "history": [str]} }

def set_ctx(user_id: int, **kw):
    sess = SESSION.get(user_id, {"history": [], "last_list": None, "last_action": None})
    sess.update({k:v for k,v in kw.items() if v is not None})
    if "history" in kw and isinstance(kw["history"], list):
        sess["history"] = kw["history"][-10:]  # Limit to 10 messages
    SESSION[user_id] = sess
    logging.info(f"Updated context for user {user_id}: {sess}")

def get_ctx(user_id: int, key: str, default=None):
    return SESSION.get(user_id, {"history": [], "last_list": None, "last_action": None}).get(key, default)

# ========= PROMPT (Semantic Core) =========
SEMANTIC_PROMPT = """
Ты — Aura, дружелюбный и остроумный ассистент, который понимает смысл человеческих фраз и управляет локальной Entity System (списки, задачи). Ты ведёшь себя как живой помощник: приветствуешь, поддерживаешь, шутишь к месту, переспрашиваешь, если нужно, и всегда действуешь осмысленно.

🧠 Как ты думаешь
• Сначала понимаешь намерение пользователя, учитывая последние сообщения (контекст: {history}) и состояние базы (db_state: {db_state}).
• Если пользователь говорит «туда», «в него», «этот список» — это относится к последнему упомянутому или созданному списку (db_state.last_list или из истории).
• Команда «Покажи список <название>» означает показать задачи конкретного списка (action: show_tasks, list: <название>).
• Команда «Переименуй список <старое_название> в <новое_название>» означает переименование списка (action: rename_list, entity_type: list, list: <старое_название>, task: <новое_название>).
• Решение принимаешь сама: создать/добавить/показать/изменить/удалить/отметить/перенести/найти/уточнить/сказать/переименовать.
• Если это социальная реплика (приветствие, small-talk, благодарность, шутка, «как дела?») — отвечай естественно и тепло.
• Если запрос неясен — вежливо уточни.
• Нормализуй вход (регистры, лишние пробелы, частые огрехи распознавания речи), но бережно к смыслу.
• Учитывай профиль пользователя (город, профессия): {user_profile}.

🧩 Формат ответа (строго JSON; без текста вне JSON)
— Для действий над базой (интенты):
{{"action": "create|add_task|show_lists|show_tasks|show_all_tasks|mark_done|delete_task|delete_list|move_entity|convert_entity|search_entity|rename_list|update_profile|unknown",
"entity_type": "list|task|user_profile",
"list": "имя списка из контекста",
"task": "имя задачи",
"to_list": "целевой список для переноса",
"tasks": ["список задач для множественного добавления"],
"meta": {{"context_used": true, "by_index": 1, "question": "уточняющий вопрос", "reason": "причина действия", "city": "город", "profession": "профессия"}}}}
— Для человеческого ответа (персоны/small-talk):
{{"action": "say",
"text": "короткий дружелюбный ответ как у живого помощника (можно слегка эмодзи)",
"meta": {{"tone": "friendly", "context_used": true}}}}
— Для уточнения:
{{"action": "clarify",
"meta": {{"question": "вежливый уточняющий вопрос", "context_used": true}}}}

🎛️ Правила поведения
• Смысл важнее слов: ты распознаёшь намерение без списков триггеров.
• Контекст: «туда/там/в него/этот» — используй последний упомянутый список из истории или db_state.last_list.
• Позиции: «первую/вторую/последнюю» — это обращение по индексу (1…; -1 = последняя) в meta.by_index, если уместно.
• Маркеры завершённости («я сделал», «готово», «закончил») трактуй как изменение состояния задач (mark_done) с fuzzy-поиском.
• Если сообщение чисто социальное — используй action:say.
• Всегда только JSON. Никаких пояснений вне JSON.

🌐 Семантическое восприятие
Aura должна воспринимать запросы **по смыслу**, а не по словам. Слова пользователя могут быть с ошибками, сокращёнными или заменёнными синонимами.
Примеры:
• «Создай список Работа внеси задачи исправить договор сходить к нотариусу написать заявление купить запчасти» → {{"action": "create", "entity_type": "list", "list": "Работа", "tasks": ["Исправить договор", "Сходить к нотариусу", "Написать заявление", "Купить запчасти"]}}
• «Перенеси задачу купить запчасти в новый список Домашние дела» → {{"action": "move_entity", "entity_type": "task", "title": "Купить запчасти", "list": "<последний список>", "to_list": "Домашние дела"}}
• «Сходить к нотариусу выполнено» → {{"action": "mark_done", "entity_type": "task", "list": "<последний список>", "task": "Сходить к нотариусу"}}
• «Внеси сдать ковер в чистку в Домашние дела» → {{"action": "add_task", "entity_type": "task", "list": "Домашние дела", "task": "Сдать ковер в чистку"}}
• «Покажи все мои дела» → {{"action": "show_all_tasks", "entity_type": "task"}}
• «Я живу в Алматы, работаю в продажах» → {{"action": "update_profile", "entity_type": "user_profile", "meta": {{"city": "Алматы", "profession": "продажи"}}}}
"""

# ========= Helpers =========
def extract_json_blocks(s: str):
    try:
        data = json.loads(s)
        if isinstance(data, list): return data
        if isinstance(data, dict): return [data]
    except Exception:
        pass
    blocks = re.findall(r'\{[^{}]*\{[^{}]*\}[^{}]*\}|\{[^{}]+\}', s, re.DOTALL)
    if not blocks:
        blocks = re.findall(r'\{[^{}]+\}', s, re.DOTALL)
    out = []
    for b in blocks:
        try:
            out.append(json.loads(b))
        except Exception:
            logging.warning(f"Skip invalid JSON block: {b[:120]}")
    return out

def wants_expand(text: str) -> bool:
    return bool(re.search(r'\b(все|всё|разверну|подробн)\w*', (text or "").lower()))

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

async def expand_all_lists(update: Update, conn, user_id: int):
    lists = get_all_lists(conn, user_id)
    if not lists:
        await update.message.reply_text("Пока нет списков 🕊")
        return
    await update.message.reply_text("🗂 Твои списки:\n" + "\n".join([f"📋 {n}" for n in lists]))
    for n in lists:
        items = get_list_tasks(conn, user_id, n)
        if items:
            txt = "\n".join([f"• {t}" for t in items])
        else:
            txt = "— пусто —"
        await update.message.reply_text(f"📋 {n}:\n{txt}")
    set_ctx(user_id, last_action="show_lists")

async def route_actions(update: Update, actions: list, user_id: int, original_text: str):
    conn = get_conn()
    logging.info(f"Processing actions: {json.dumps(actions)}")
    for obj in actions:
        action = obj.get("action", "unknown")
        entity_type = obj.get("entity_type")
        list_name = obj.get("list") or get_ctx(user_id, "last_list")
        task = obj.get("task") or obj.get("title")
        meta = obj.get("meta", {})
        logging.info(f"Action: {action}, Entity: {entity_type}, List: {list_name}, Task: {task}")

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
                await expand_all_lists(update, conn, user_id)
                continue
            name_from_text = text_mentions_list_and_name(original_text)
            if name_from_text:
                list_name = name_from_text
                action = "show_tasks"
                logging.info(f"Fallback to show_tasks for list: {list_name}")

        if action == "create" and entity_type == "list" and obj.get("list"):
            try:
                logging.info(f"Creating list: {obj['list']}")
                create_list(conn, user_id, obj["list"])
                if obj.get("tasks"):
                    for t in obj["tasks"]:
                        add_task(conn, user_id, obj["list"], t)
                    await update.message.reply_text(f"🆕 Создан список *{obj['list']}* с задачами: {', '.join(obj['tasks'])}", parse_mode="Markdown")
                else:
                    await update.message.reply_text(f"🆕 Создан список *{obj['list']}*", parse_mode="Markdown")
                set_ctx(user_id, last_action="create_list", last_list=obj["list"])
            except Exception as e:
                logging.exception(f"Create list error: {e}")
                await update.message.reply_text("⚠️ Не удалось создать список. Проверь логи.")

        elif action == "add_task" and list_name and task:
            try:
                tasks = task if isinstance(task, list) else [task]
                for t in tasks:
                    logging.info(f"Adding task: {t} to list: {list_name}")
                    task_id = add_task(conn, user_id, list_name, t)
                    if task_id:
                        await update.message.reply_text(f"✅ Добавлено: *{t}* в список *{list_name}*", parse_mode="Markdown")
                    else:
                        await update.message.reply_text(f"⚠️ Задача *{t}* уже есть в списке *{list_name}*.")
                set_ctx(user_id, last_action="add_task", last_list=list_name)
            except Exception as e:
                logging.exception(f"Add task error: {e}")
                await update.message.reply_text("⚠️ Не удалось добавить задачу. Проверь логи.")

        elif action == "show_lists":
            try:
                logging.info("Showing all lists")
                if wants_expand(original_text) or meta.get("expand"):
                    await expand_all_lists(update, conn, user_id)
                else:
                    lists = get_all_lists(conn, user_id)
                    if lists:
                        txt = "\n".join([f"📋 {n}" for n in lists])
                        await update.message.reply_text(f"🗂 Твои списки:\n{txt}")
                    else:
                        await update.message.reply_text("Пока нет списков 🕊")
                set_ctx(user_id, last_action="show_lists")
            except Exception as e:
                logging.exception(f"Show lists error: {e}")
                await update.message.reply_text("⚠️ Не удалось получить списки. Проверь логи.")

        elif action == "show_tasks" and list_name:
            try:
                logging.info(f"Showing tasks for list: {list_name}")
                items = get_list_tasks(conn, user_id, list_name)
                if items:
                    txt = "\n".join([f"• {t}" for t in items])
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
                tasks = get_all_tasks(conn, user_id)
                if tasks:
                    txt = "\n".join([f"📋 {list_title}: {task_title}" for list_title, task_title in tasks])
                    await update.message.reply_text(f"🗂 Все твои дела:\n{txt}", parse_mode="Markdown")
                else:
                    await update.message.reply_text("Пока нет дел 🕊")
                set_ctx(user_id, last_action="show_all_tasks")
            except Exception as e:
                logging.exception(f"Show all tasks error: {e}")
                await update.message.reply_text("⚠️ Не удалось получить дела. Проверь логи.")

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
                    q = task[0] if isinstance(task, list) and task else task or original_text
                    logging.info(f"Deleting task fuzzy: {q} in list: {ln}")
                    deleted, matched = delete_task_fuzzy(conn, user_id, ln, q)
                if deleted:
                    await update.message.reply_text(f"🗑 Удалено: *{matched}* из *{ln}*", parse_mode="Markdown")
                else:
                    await update.message.reply_text("Нечего удалять.")
                set_ctx(user_id, last_action="delete_task", last_list=ln)
            except Exception as e:
                logging.exception(f"Delete task error: {e}")
                await update.message.reply_text("⚠️ Не удалось удалить задачу. Проверь логи.")

        elif action == "delete_list" and entity_type == "list" and list_name:
            try:
                logging.info(f"Deleting list: {list_name}")
                deleted = delete_list(conn, user_id, list_name)
                if deleted:
                    await update.message.reply_text(f"🗑 Список *{list_name}* удалён.", parse_mode="Markdown")
                    set_ctx(user_id, last_action="delete_list", last_list=None)
                else:
                    await update.message.reply_text(f"⚠️ Список *{list_name}* не найден.")
            except Exception as e:
                logging.exception(f"Delete list error: {e}")
                await update.message.reply_text("⚠️ Не удалось удалить список. Проверь логи.")

        elif action == "mark_done" and list_name and task:
            try:
                tasks = task if isinstance(task, list) else [task]
                for t in tasks:
                    logging.info(f"Marking task done: {t} in list: {list_name}")
                    deleted, matched = mark_task_done_fuzzy(conn, user_id, list_name, t)
                    if deleted:
                        await update.message.reply_text(f"✔️ Готово: *{matched}*.", parse_mode="Markdown")
                    else:
                        await update.message.reply_text("⚠️ Не нашёл такую задачу.")
                set_ctx(user_id, last_action="mark_done", last_list=list_name)
            except Exception as e:
                logging.exception(f"Mark done error: {e}")
                await update.message.reply_text("⚠️ Не удалось отметить задачу. Проверь логи.")

        elif action == "rename_list" and entity_type == "list" and list_name and task:
            try:
                logging.info(f"Renaming list: {list_name} to {task}")
                renamed = rename_list(conn, user_id, list_name, task)
                if renamed:
                    await update.message.reply_text(f"🆕 Список *{list_name}* переименован в *{task}*.", parse_mode="Markdown")
                    set_ctx(user_id, last_action="rename_list", last_list=task)
                else:
                    await update.message.reply_text(f"⚠️ Список *{list_name}* не найден или *{task}* уже существует.")
            except Exception as e:
                logging.exception(f"Rename list error: {e}")
                await update.message.reply_text("⚠️ Не удалось переименовать список. Проверь логи.")

        elif action == "move_entity" and entity_type and obj.get("title") and obj.get("list") and obj.get("to_list"):
            try:
                logging.info(f"Moving {entity_type} '{obj['title']}' from {obj['list']} to {obj['to_list']}")
                updated = move_entity(conn, user_id, entity_type, obj["title"], obj["list"], obj["to_list"])
                if updated:
                    await update.message.reply_text(f"🔄 Перемещено: *{obj['title']}* в *{obj['to_list']}*.", parse_mode="Markdown")
                    set_ctx(user_id, last_action="move_entity", last_list=obj["to_list"])
                else:
                    await update.message.reply_text(f"⚠️ Не удалось переместить *{obj['title']}*.")
            except Exception as e:
                logging.exception(f"Move entity error: {e}")
                await update.message.reply_text("⚠️ Не удалось переместить задачу. Проверь логи.")

        elif action == "convert_entity" and list_name and task and meta.get("new_type"):
            try:
                tasks = task if isinstance(task, list) else [task]
                for t in tasks:
                    logging.info(f"Converting task: {t} to {meta['new_type']} in list: {list_name}")
                    n = convert_entity(conn, user_id, list_name, t, meta["new_type"])
                    await update.message.reply_text(f"🔄 Преобразовано: *{t}* в *{meta['new_type']}*.", parse_mode="Markdown")
                set_ctx(user_id, last_action="convert_entity", last_list=list_name)
            except Exception as e:
                logging.exception(f"Convert entity error: {e}")
                await update.message.reply_text("⚠️ Не удалось преобразовать задачу. Проверь логи.")

        elif action == "update_profile" and entity_type == "user_profile" and meta:
            try:
                logging.info(f"Updating user profile for user {user_id}: {meta}")
                update_user_profile(conn, user_id, meta.get("city"), meta.get("profession"))
                await update.message.reply_text("🆙 Профиль обновлён!", parse_mode="Markdown")
            except Exception as e:
                logging.exception(f"Update profile error: {e}")
                await update.message.reply_text("⚠️ Не удалось обновить профиль. Проверь логи.")

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
                await update.message.reply_text("🤔 " + meta.get("question"))
                await send_menu(update, context)
            except Exception as e:
                logging.exception(f"Clarify error: {e}")
                await update.message.reply_text("⚠️ Не удалось уточнить. Проверь логи.")

        else:
            if wants_expand(original_text) and get_ctx(user_id, "last_action") == "show_lists":
                logging.info("Expanding all lists due to context")
                await expand_all_lists(update, conn, user_id)
            else:
                name_from_text = text_mentions_list_and_name(original_text)
                if name_from_text:
                    logging.info(f"Showing tasks for list from text: {name_from_text}")
                    items = get_list_tasks(conn, user_id, name_from_text)
                    if items:
                        txt = "\n".join([f"• {t}" for t in items])
                        await update.message.reply_text(f"📋 *{name_from_text}:*\n{txt}", parse_mode="Markdown")
                        set_ctx(user_id, last_action="show_tasks", last_list=name_from_text)
                        continue
                logging.info("Unknown command, no context match")
                await update.message.reply_text("🤔 Не понял, что нужно сделать.")
                await send_menu(update, context)
        logging.info(f"User {user_id}: {original_text} -> Action: {action}")

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE, input_text: str | None = None):
    user_id = update.effective_user.id
    text = (input_text or update.message.text or "").strip()
    logging.info(f"📩 Text from {user_id}: {text}")

    try:
        conn = get_conn()
        db_state = {
            "lists": {n: get_list_tasks(conn, user_id, n) for n in get_all_lists(conn, user_id)},
            "last_list": get_ctx(user_id, "last_list")
        }
        history = get_ctx(user_id, "history", [])
        user_profile = get_user_profile(conn, user_id)
        prompt = SEMANTIC_PROMPT.format(history=json.dumps(history[-10:], ensure_ascii=False), 
                                       db_state=json.dumps(db_state, ensure_ascii=False),
                                       user_profile=json.dumps(user_profile, ensure_ascii=False))
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
            with open("/opt/aura-assistant/openai_raw.log", "a", encoding="utf-8") as f:
                f.write(f"\n=== RAW ({user_id}) ===\n{text}\n{raw}\n")
        except Exception:
            logging.warning("Failed to write to openai_raw.log")

        actions = extract_json_blocks(raw)
        if not actions:
            if wants_expand(text) and get_ctx(user_id, "last_action") == "show_lists":
                logging.info("No actions, but expanding lists due to context")
                await expand_all_lists(update, conn, user_id)
                return
            logging.warning("No valid JSON actions from OpenAI")
            await update.message.reply_text("⚠️ Модель ответила не в JSON-формате.")
            await send_menu(update, context)
            return

        await route_actions(update, actions, user_id, text)
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
    q = update.callback_query
    await q.answer()
    await q.edit_message_text("🌀 Обработка...")

def main():
    init_db()
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_handler(CallbackQueryHandler(handle_callback))
    logging.info("🚀 Aura v5.1 started.")
    app.run_polling()

if __name__ == "__main__":
    main()
