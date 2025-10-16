import sqlite3, json, os, sys

DB_PATH = "/opt/aura-assistant/db.sqlite3"

DDL = """
CREATE TABLE IF NOT EXISTS entities (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  type TEXT NOT NULL,             -- 'list', 'task', 'note', 'reminder', ...
  title TEXT,                     -- человекочитаемое имя/название
  content TEXT,                   -- свободный текст/описание
  parent_id INTEGER,              -- ссылка на родителя (например, task->list)
  created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  meta TEXT,                      -- JSON-поле для гибких свойств
  UNIQUE(user_id, type, title, parent_id)
);
"""

def table_exists(conn, name):
    cur = conn.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,))
    return cur.fetchone() is not None

def migrate_lists_tasks(conn):
    cur = conn.cursor()

    # Если старых таблиц нет — просто выходим
    if not table_exists(conn, "lists") and not table_exists(conn, "tasks"):
        return

    # Подгружаем ВСЕ списки (если таблица есть)
    lists = []
    if table_exists(conn, "lists"):
        cur.execute("PRAGMA table_info(lists)")
        cols = [c[1] for c in cur.fetchall()]
        # ожидаем минимум: user_id, name
        if set(["user_id","name"]).issubset(set(cols)):
            cur.execute("SELECT DISTINCT user_id, name FROM lists")
            lists = cur.fetchall()

    # Подготовим индекс имён списков -> id entity
    list_id_by_key = {}  # (user_id, name) -> entity_id

    # Создадим записи-списки в entities (idempotent)
    for user_id, name in lists:
        try:
            cur.execute("""
              INSERT OR IGNORE INTO entities (user_id, type, title)
              VALUES (?, 'list', ?)
            """, (user_id, name))
            conn.commit()
        except Exception:
            pass
        cur.execute("""
          SELECT id FROM entities WHERE user_id=? AND type='list' AND title=? LIMIT 1
        """, (user_id, name))
        row = cur.fetchone()
        if row:
            list_id_by_key[(user_id, name)] = row[0]

    # Теперь задачи
    if table_exists(conn, "tasks"):
        cur.execute("PRAGMA table_info(tasks)")
        cols = [c[1] for c in cur.fetchall()]
        # ожидаем минимум: user_id, list_name, task
        if set(["user_id","list_name","task"]).issubset(set(cols)):
            cur.execute("SELECT user_id, list_name, task FROM tasks")
            for user_id, list_name, task in cur.fetchall():
                # убедимся, что есть list entity
                if (user_id, list_name) not in list_id_by_key:
                    # создадим список, если вдруг не было
                    cur.execute("""
                      INSERT OR IGNORE INTO entities (user_id, type, title)
                      VALUES (?, 'list', ?)
                    """, (user_id, list_name))
                    conn.commit()
                    cur.execute("""
                      SELECT id FROM entities WHERE user_id=? AND type='list' AND title=? LIMIT 1
                    """, (user_id, list_name))
                    r2 = cur.fetchone()
                    if r2: list_id_by_key[(user_id, list_name)] = r2[0]

                parent_id = list_id_by_key.get((user_id, list_name))
                # добавим task
                cur.execute("""
                  INSERT OR IGNORE INTO entities (user_id, type, title, parent_id, meta)
                  VALUES (?, 'task', ?, ?, ?)
                """, (user_id, task, parent_id, json.dumps({"status": "open"})))
            conn.commit()

def main():
    if not os.path.exists(DB_PATH):
        print(f"DB not found: {DB_PATH}", file=sys.stderr)
        sys.exit(1)
    conn = sqlite3.connect(DB_PATH)
    try:
        with conn:
            conn.execute(DDL)
            migrate_lists_tasks(conn)
        print("Migration to entities: OK")
    finally:
        conn.close()

if __name__ == "__main__":
    main()
