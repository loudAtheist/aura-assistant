import os
import json
import logging
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    ApplicationBuilder, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters
)
import speech_recognition as sr
from pydub import AudioSegment
from openai import OpenAI

from db import (
    init_db, get_conn, get_all_lists, get_list_tasks, add_task, delete_list,
    mark_task_done, delete_task, restore_task, find_list, fetch_task, fetch_list_by_task
)

# ================== НАСТРОЙКА СРЕДЫ ==================
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-3.5-turbo")
TEMP_DIR = os.getenv("TEMP_DIR", "/opt/aura-assistant/tmp")
os.makedirs(TEMP_DIR, exist_ok=True)

# ================== ЛОГИ ==================
logging.basicConfig(
    filename="/opt/aura-assistant/aura.log",
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)

# ================== КЛИЕНТ ==================
client = OpenAI(api_key=OPENAI_API_KEY)

# ================== ОБРАБОТКА ТЕКСТОВ ==================
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text.strip()
    logging.info(f"📩 Text from {user_id}: {text}")

    try:
        system_prompt = """
Ты — Aura Assistant, интеллектуальный менеджер задач и напоминаний.
Отвечай строго в формате JSON, без пояснений.
Пример структуры ответа:
{
  "action": "add_task",
  "list": "Работа",
  "task": "позвонить клиенту"
}
Если не можешь определить действие, верни:
{ "action": "unknown" }
        """

        response = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": text}
            ],
        )

        raw_reply = response.choices[0].message.content.strip()
        logging.info(f"🤖 RAW reply: {raw_reply}")

        # Проверка JSON-ответа
        if not raw_reply.startswith("{"):
            await update.message.reply_text("⚠️ Модель ответила не в JSON-формате.")
            return

        data = json.loads(raw_reply)
        action = data.get("action", "unknown")
        task = data.get("task")
        list_name = data.get("list")

        conn = get_conn()

        # === ДЕЙСТВИЯ ===
        if action == "add_task" and list_name and task:
            add_task(conn, user_id, list_name, task)
            await update.message.reply_text(f"✅ Добавлено: *{task}* в список *{list_name}*", parse_mode="Markdown")

        elif action == "show_lists":
            lists = get_all_lists(conn, user_id)
            if lists:
                formatted = "\n".join([f"📋 {name}" for name in lists])
                await update.message.reply_text(f"🗂 Твои списки:\n{formatted}")
            else:
                await update.message.reply_text("Пока нет списков 🕊")

        elif action == "show_tasks" and list_name:
            tasks = get_list_tasks(conn, user_id, list_name)
            if tasks:
                formatted = "\n".join([f"• {t}" for t in tasks])
                await update.message.reply_text(f"📋 *{list_name}:*\n{formatted}", parse_mode="Markdown")
            else:
                await update.message.reply_text(f"Список *{list_name}* пуст.", parse_mode="Markdown")

        elif action == "unknown":
            await update.message.reply_text("⚠️ Не распознал команду. Пример: «создай список Работа».")

        else:
            await update.message.reply_text("🤔 Не понял, что нужно сделать.")

    except Exception as e:
        logging.exception(f"❌ Ошибка обработки текста: {e}")
        await update.message.reply_text("Произошла ошибка при обработке. Проверь логи.")


# ================== ОБРАБОТКА ГОЛОСА ==================
async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    logging.info(f"🎙 Голосовое сообщение от {user_id}")

    try:
        voice_file = await update.message.voice.get_file()
        ogg_path = os.path.join(TEMP_DIR, f"{user_id}_voice.ogg")
        wav_path = os.path.join(TEMP_DIR, f"{user_id}_voice.wav")

        await voice_file.download_to_drive(ogg_path)

        # Конвертация
        AudioSegment.from_ogg(ogg_path).export(wav_path, format="wav")

        recognizer = sr.Recognizer()
        with sr.AudioFile(wav_path) as source:
            audio = recognizer.record(source)
            text = recognizer.recognize_google(audio, language="ru-RU")

        logging.info(f"🗣 Распознан текст: {text}")
        await update.message.reply_text(f"🗣 {text}")

        # Передаём текст в обработчик
        update.message.text = text
        await handle_text(update, context)

    except Exception as e:
        logging.exception(f"❌ Ошибка голосового модуля: {e}")
        await update.message.reply_text("⚠️ Не удалось обработать голос. Проверь логи.")


# ================== CALLBACK ==================
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("🌀 Обработка...")

# ================== MAIN ==================
def main():
    init_db()
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_handler(CallbackQueryHandler(handle_callback))

    logging.info("🚀 Aura Assistant запущен и готов к работе.")
    app.run_polling()


if __name__ == "__main__":
    main()
