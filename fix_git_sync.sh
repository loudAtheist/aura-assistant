set -e
echo "🧠 Fixing Git sync issues..."

cd /opt/aura-assistant

# 1️⃣ Сохраняем локальные изменения
if ! git diff --quiet || ! git diff --cached --quiet; then
  echo "💾 Сохраняю локальные изменения во временный stash..."
  git stash push -m "auto-stash-before-sync"
fi

# 2️⃣ Получаем последнюю историю
echo "🔄 Fetching origin..."
git fetch origin

# 3️⃣ Принудительно объединяем истории (если требуется)
echo "🧩 Объединяю с origin/main (allow unrelated histories)..."
git pull origin main --allow-unrelated-histories --rebase || true

# 4️⃣ Восстанавливаем stash, если был
if git stash list | grep -q "auto-stash-before-sync"; then
  echo "♻️ Восстанавливаю stash..."
  git stash pop || true
fi

# 5️⃣ Коммитим и пушим
echo "⬆️ Делаю автокоммит и пуш..."
git add -A
git commit -m "fix: auto-sync after merge $(date '+%Y-%m-%d_%H-%M-%S')" || true
git push origin HEAD:main --force-with-lease

echo "✅ Синхронизация завершена!"
