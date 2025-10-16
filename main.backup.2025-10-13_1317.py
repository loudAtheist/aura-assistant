import os, json, re, logging
from pathlib import Path
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, CallbackQueryHandler, ContextTypes, filters
import speech_recognition as sr
from pydub import AudioSegment
from openai import OpenAI

from db import (
    init_db, get_conn, get_all_lists, get_list_tasks, add_task, delete_list,
    mark_task_done, delete_task, restore_task, find_list, fetch_task, fetch_list_by_task
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

# ========= PROMPT (Semantic Core) =========
SEMANTIC_PROMPT = """
Ты — Aura, интеллектуальный ассистент и интерфейс к базе данных личных сущностей пользователя (Entity System).

Твоя задача — понимать смысл естественных фраз и преобразовывать намерения человека в чёткие действия над сущностями: списками, задачами, заметками, напоминаниями, идеями.

Aura воспринимает язык как выражение намерения:
- выясни, что человек хочет сделать или узнать;
- определи, к какой сущности это относится;
- сформируй одно или несколько действий, описанных в структурированном JSON.

Ты не анализируешь слова, ты улавливаешь смысл и контекст.  
Если пользователь говорит «всё», «развёрнуто», «подробно» — значит, он хочет получить полное представление (например, все списки со всеми задачами).  
Если говорит «надо», «хочу», «напомни», «убери», «добавь», «покажи», — определяй тип действия (создать, добавить, показать, удалить, отметить выполненным, напомнить).

Aura помнит, что каждая сущность хранится в таблице entities, где каждая запись имеет тип (list, task, note, reminder) и связи parent_id.  
Действия в JSON описывают изменение или получение этих данных.

---
Формат вывода:
Aura всегда отвечает только JSON-объектами, отражающими понятые действия.  
Каждый объект описывает одно намерение пользователя:
{
  "action": "create|add_task|show_lists|show_tasks|delete_task|mark_done|update_task|move_entity|unknown",
  "entity_type": "list|task|note|reminder",
  "list": "<название списка>",
  "task": "<текст задачи>",
  "title": "<заголовок сущности>",
  "meta": { ... }
}

Если смысл не требует действий — верни { "action": "unknown" }.

---
Aura — не чат и не бот, а интерфейс понимания.  Её ответы должны быть точными действиями, а не разговорами.
"""

# ========= Helpers =========
def extract_json_blocks(s: str):
    try:
        data = json.loads(s)
        if isinstance(data, list):
            return data
        elif isinstance(data, dict):
            return [data]
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

# ========= FIXED user_id logic =========
async def route_actions(update: Update, actions: list, user_id: int | None = None):
    conn = get_conn()
    if user_id is None:
        try:
            user_id = update.effective_user.id if update and update.effective_user else 0
        except Exception:
            user_id = 0

    for obj in actions:
        action = obj.get("action", "unknown")
        entity_type = obj.get("entity_type")
        list_name = obj.get("list")
        task = obj.get("task") or obj.get("title")

        if action == "create" and entity_type == "list" and list_name:
            try:
                from db import create_list
                create_list(conn, user_id, list_name)
                await update.message.reply_text(f"🆕 Создан список *{list_name}*", parse_mode="Markdown")
            except Exception as e:
                logging.exception(f"Create list error: {e}")
                await update.message.reply_text("⚠️ Не удалось создать список.")

        elif action == "add_task" and list_name and task:
            try:
                add_task(conn, user_id, list_name, task)
                await update.message.reply_text(f"✅ Добавлено: *{task}* в список *{list_name}*", parse_mode="Markdown")
            except Exception as e:
                logging.exception(f"Add task error: {e}")
                await update.message.reply_text("⚠️ Не удалось добавить задачу.")

        elif action == "show_lists":
            try:
                lists = get_all_lists(conn, user_id)
                if lists:
                    txt = "\n".join([f"📋 {n}" for n in lists])
                    await update.message.reply_text(f"🗂 Твои списки:\n{txt}")
                else:
                    await update.message.reply_text("Пока нет списков 🕊")
            except Exception as e:
                logging.exception(f"Show lists error: {e}")
                await update.message.reply_text("⚠️ Не удалось получить списки.")

        elif action == "show_tasks" and list_name:
            try:
                items = get_list_tasks(conn, user_id, list_name)
                if items:
                    txt = "\n".join([f"• {t}" for t in items])
                    await update.message.reply_text(f"📋 *{list_name}:*\n{txt}", parse_mode="Markdown")
                else:
                    await update.message.reply_text(f"Список *{list_name}* пуст.", parse_mode="Markdown")
            except Exception as e:
                logging.exception(f"Show tasks error: {e}")
                await update.message.reply_text("⚠️ Не удалось получить задачи.")

        elif action == "delete_task" and list_name and task:
            try:
                n = delete_task(conn, user_id, list_name, task)
                await update.message.reply_text("🗑 Удалено." if n else "Нечего удалять.")
            except Exception as e:
                logging.exception(f"Delete task error: {e}")
                await update.message.reply_text("⚠️ Не удалось удалить задачу.")

        elif action == "mark_done" and list_name and task:
            try:
                n = mark_task_done(conn, user_id, list_name, task)
                await update.message.reply_text("✔️ Готово." if n else "Не нашёл такую задачу.")
            except Exception as e:
                logging.exception(f"Mark done error: {e}")
                await update.message.reply_text("⚠️ Не удалось отметить задачу.")

        else:
            await update.message.reply_text("🤔 Не понял, что нужно сделать.")

# ========= Handlers =========
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE, input_text: str | None = None):
    user_id = update.effective_user.id if update and update.effective_user else 0
    text = (input_text or update.message.text or "").strip()
    logging.info(f"📩 Text from {user_id}: {text}")

    try:
        resp = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system", "content": SEMANTIC_PROMPT},
                {"role": "user", "content": text}
            ],
        )
        raw = resp.choices[0].message.content.strip()
        logging.info(f"🤖 RAW: {raw}")
        with open("/opt/aura-assistant/openai_raw.log", "a", encoding="utf-8") as f:
            f.write(f"\n=== RAW ({user_id}) ===\n{raw}\n")

        actions = extract_json_blocks(raw)
        if not actions:
            await update.message.reply_text("⚠️ Модель ответила не в JSON-формате.")
            return
        await route_actions(update, actions, user_id)

    except Exception as e:
        logging.exception(f"❌ handle_text error: {e}")
        await update.message.reply_text("Произошла ошибка при обработке. Проверь логи.")

async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id if update and update.effective_user else 0
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

        logging.info(f"🗣 ASR: {text}")
        await update.message.reply_text(f"🗣 {text}")

        await handle_text(update, context, input_text=text)

        try:
            os.remove(ogg)
            os.remove(wav)
        except Exception:
            pass

    except Exception as e:
        logging.exception(f"❌ voice error: {e}")
        await update.message.reply_text("⚠️ Не удалось обработать голос. Проверь логи.")

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
    logging.info("🚀 Aura v5.0.1 started.")
    app.run_polling()

if __name__ == "__main__":
    main()
