#!/bin/bash
set -e

cd /opt/aura-assistant

echo "📁 Включаем виртуальное окружение..."
source venv/bin/activate

# === 1. Добавляем логи, если их нет ===
echo "🔍 Проверяем наличие логов в main.py..."
if ! grep -q "RAW AI RESPONSE" main.py; then
  sed -i '/raw = (resp\.choices\[0\]\.message\.content or ""\)\.strip()/a\    logging.info(f"RAW AI RESPONSE: {raw}")' main.py
  echo "✅ Добавлен лог RAW AI RESPONSE"
fi

if ! grep -q "ASK_AI CALLED" main.py; then
  sed -i '/response = ask_ai(user_text, user_id)/i\    logging.info(f"ASK_AI CALLED with text: {user_text}")' main.py
  echo "✅ Добавлен лог ASK_AI CALLED"
fi

# === 2. Перезапускаем бота ===
echo "🛑 Останавливаем старый процесс..."
pkill -f "main.py" || true
sleep 1

echo "🚀 Запускаем Aura Assistant заново..."
nohup python -u main.py > /opt/aura-assistant/aura.run.log 2>&1 &

sleep 5
echo "📜 Проверяем последние строки лога:"
tail -n 15 /opt/aura-assistant/aura.run.log

echo
echo "✅ Готово. Теперь скажи в Telegram голосом, например:"
echo "   «создай список работа»"
echo "   затем выполни:"
echo "   grep \"ASK_AI CALLED\" /opt/aura-assistant/aura.run.log | tail -n 3"
echo "   grep \"RAW AI RESPONSE\" /opt/aura-assistant/aura.run.log | tail -n 3"
