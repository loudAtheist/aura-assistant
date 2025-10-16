#!/bin/bash
# === Старт Aura Assistant (фоновый режим + логирование) ===

cd /opt/aura-assistant || exit 1

# Проверка существующих процессов
if pidof python > /dev/null; then
    echo "❌ Бот уже запущен! Завершаем существующие процессы..."
    pidof python | xargs kill -9
    sleep 5  # Увеличенная задержка
fi

# Активируем виртуальное окружение
source venv/bin/activate

# Запускаем бота
python main.py >> /opt/aura-assistant/aura.log 2>&1 &
echo "🚀 Aura Assistant запущен (PID: $!)"
