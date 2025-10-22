#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="${SCRIPT_DIR}"
LOG_FILE="${PROJECT_DIR}/aura.log"
PYTHON_BIN="${PYTHON_BIN:-python3}"
VENV_ACTIVATE="${PROJECT_DIR}/venv/bin/activate"

cd "${PROJECT_DIR}" || exit 1

if [[ -f "${VENV_ACTIVATE}" ]]; then
    # shellcheck disable=SC1090
    source "${VENV_ACTIVATE}"
fi

existing_pids=$(pgrep -f "python[0-9.]* .*${PROJECT_DIR}/main.py" || true)
if [[ -n "${existing_pids}" ]]; then
    echo "⚠️ Aura Assistant уже запущен (PID: ${existing_pids})"
    exit 0
fi

nohup "${PYTHON_BIN}" main.py >> "${LOG_FILE}" 2>&1 &
pid=$!
echo "🚀 Aura Assistant запущен (PID: ${pid})"
