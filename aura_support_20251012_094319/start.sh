cat <<'EOF' > /opt/aura-assistant/start.sh
#!/bin/bash
# === Старт Aura Assistant (фоновый режим + логирование) ===

cd /opt/aura-assistant || exit 1

# Активируем виртуальное окружение
source venv/bin/activate
