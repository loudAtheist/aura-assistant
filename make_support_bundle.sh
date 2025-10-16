#!/usr/bin/env bash
set -euo pipefail
TS=$(date +"%Y%m%d_%H%M%S")
OUT="/opt/aura-assistant/aura_support_$TS"
mkdir -p "$OUT"

# Ключевые файлы проекта (копируем, если существуют)
for f in main.py db.py requirements.txt start.sh stop.sh docker-compose.yml pyproject.toml poetry.lock; do
  [ -f "$f" ] && cp -v "$f" "$OUT/" || true
done

# Логи (берём хвосты, чтобы не тянуть гигабайты)
for f in aura.log aura.run.log openai_raw.log; do
  [ -f "$f" ] && tail -n 400 "$f" > "$OUT/${f}.tail.txt" || true
done

# systemd unit (если есть)
if command -v systemctl >/dev/null 2>&1; then
  systemctl list-unit-files 2>/dev/null | grep -q '^aura-assistant\.service' && \
    systemctl cat aura-assistant.service > "$OUT/aura-assistant.service.txt" || true
fi

# .env (с маскировкой значений)
if [ -f .env ]; then
  sed -E 's/^([A-Za-z0-9_]+)=.*/\1=***REDACTED*** /' .env > "$OUT/.env.redacted"
fi

# Версии инструментов/зависимостей
python -V > "$OUT/python_version.txt" 2>&1 || true
pip freeze > "$OUT/pip_freeze.txt" 2>/dev/null || true
ffmpeg -version > "$OUT/ffmpeg_version.txt" 2>/dev/null || true

# Быстрый снимок окружения
cat > "$OUT/runtime_check.txt" <<RUNTIME
WORKDIR: $(pwd)
USER: $(id -u -n) ($(id -u))
VENV: ${VIRTUAL_ENV:-none}
TELEGRAM_TOKEN_SET: $( [ -n "${TELEGRAM_TOKEN:-}" ] && echo yes || echo no )
OPENAI_API_KEY_SET: $( [ -n "${OPENAI_API_KEY:-}" ] && echo yes || echo no )
OPENAI_MODEL: ${OPENAI_MODEL:-unset}
TEMP_DIR: ${TEMP_DIR:-unset}
RUNTIME

# Полезные grep по main.py (для быстрой навигации)
[ -f main.py ] && grep -nE "ApplicationBuilder|MessageHandler|filters\.VOICE|run_polling" main.py > "$OUT/main_handlers_grep.txt" || true

# Упаковка
tar -czf "$OUT.tgz" -C "$(dirname "$OUT")" "$(basename "$OUT")"
echo "Bundle created: $OUT.tgz"
ls -lh "$OUT.tgz"
