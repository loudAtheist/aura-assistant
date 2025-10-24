#!/bin/bash
<<<<<<< HEAD
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="${SCRIPT_DIR}"

pids=$(pgrep -f "python[0-9.]* .*${PROJECT_DIR}/main.py" || true)
if [[ -z "${pids}" ]]; then
    echo "ℹ️ Aura Assistant не запущен"
    exit 0
fi

echo "🛑 Останавливаем Aura Assistant (PID: ${pids})"
kill -- ${pids}
for pid in ${pids}; do
    wait "${pid}" 2>/dev/null || true
done
echo "✅ Aura Assistant остановлен"
=======
# Остановка Aura Assistant
pkill -f "/opt/aura-assistant/main.py"
echo "🛑 Aura Assistant остановлен"
>>>>>>> 874f674 (Первый коммит проекта Aura Assistant)
