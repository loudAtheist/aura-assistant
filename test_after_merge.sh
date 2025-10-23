#!/bin/bash
# =========================================
# Aura Assistant — Тест после мерджа
# =========================================

APP_DIR="/opt/aura-assistant"
VENV_DIR="$APP_DIR/venv"
LOG_MAIN="$APP_DIR/aura.log"
LOG_CODEX="$APP_DIR/codex_errors.log"
LOG_DB="$APP_DIR/db_debug.log"
DB_FILE="$APP_DIR/db.sqlite3"

echo "🧩 Aura Assistant — тест после мерджа"
cd "$APP_DIR" || exit 1

# 1️⃣ Подтягиваем последние изменения
echo "🔄 Получаю последнюю версию из GitHub..."
git fetch origin
git pull --rebase origin main || git pull origin main

# 2️⃣ Фиксируем и пушим локальные изменения
echo "⬆️ Делаю коммит и пуш..."
git add .
git commit -m "Post-merge auto-commit — $(date +%Y-%m-%d_%H-%M-%S)" || echo "ℹ️ Нет изменений для коммита."
git push origin main || echo "⚠️ Пуш не выполнен (возможно нет новых изменений)."

# 3️⃣ Проверяем и убиваем старые процессы
PIDS=$(pgrep -f "main.py")
if [ -n "$PIDS" ]; then
  echo "⛔ Найдены старые процессы: $PIDS"
  kill -9 $PIDS
  echo "✅ Старые процессы завершены."
else
  echo "ℹ️ Старых процессов не найдено."
fi

# 4️⃣ Очищаем базу и логи
echo "🧹 Очищаю базу и логи..."
rm -f "$DB_FILE" "$LOG_MAIN" "$LOG_CODEX" "$LOG_DB"

# 5️⃣ Активируем виртуальное окружение
echo "🧬 Активирую виртуальное окружение..."
source "$VENV_DIR/bin/activate"

# 6️⃣ Инициализируем новую базу
echo "🪄 Создаю новую базу данных..."
python3 db.py --init

# 7️⃣ Запускаем бота в фоне
echo "🚀 Запускаю Aura Assistant для теста..."
nohup python3 main.py >> "$LOG_MAIN" 2>&1 &

sleep 3
echo "✨ Бот запущен для теста!"
echo "📜 Основной лог: tail -f $LOG_MAIN"
