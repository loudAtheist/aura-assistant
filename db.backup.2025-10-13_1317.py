import sqlite3, json, os, datetime

DB_PATH = "/opt/aura-assistant/db.sqlite3"
LOG_PATH = "/opt/aura-assistant/db_debug.log"

def db_log(msg):
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(f"[{ts}] {msg}\n")

ENTITIES_DDL = """
CREATE TABLE IF NOT EXISTS entities (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  type TEXT NOT NULL,
  title TEXT,
  content TEXT,
  parent_id INTEGER,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  meta TEXT,
  UNIQUE(user_id, type, title, parent_id)
);
"""

def get_conn():
    conn = sqlite3.connect(DB_PATH, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_conn()
    conn.execute(ENTITIES_DDL)
    conn.close()
    db_log("✅ init_db() проверила/создала таблицу entities")

# ---------- Entity Layer ----------
def _get_or_create_list(conn, user_id, list_name):
    db_log(f"➡️ _get_or_create_list user={user_id} list='{list_name}'")
    cur = conn.cursor()
    cur.execute("""SELECT id FROM entities WHERE user_id=? AND type='list' AND title=? LIMIT 1""",
                (user_id, list_name))
    row = cur.fetchone()
    if row:
        db_log(f"ℹ️ список '{list_name}' уже существует (id={row[0]})")
        return row[0]
    cur.execute("""INSERT INTO entities (user_id, type, title) VALUES (?, 'list', ?)""",
                (user_id, list_name))
    db_log(f"🆕 создан новый список '{list_name}' для user={user_id}")
    return cur.lastrowid

def create_list(conn, user_id, list_name):
    _get_or_create_list(conn, user_id, list_name)
    db_log(f"✅ create_list завершён user={user_id} list='{list_name}'")

def get_all_lists(conn, user_id):
    cur = conn.cursor()
    cur.execute("""SELECT title FROM entities WHERE user_id=? AND type='list' ORDER BY created_at ASC""",
                (user_id,))
    rows = cur.fetchall()
    return [r["title"] for r in rows]

def add_task(conn, user_id, list_name, task_title):
    db_log(f"➡️ add_task user={user_id} list='{list_name}' task='{task_title}'")
    list_id = _get_or_create_list(conn, user_id, list_name)
    cur = conn.cursor()
    cur.execute("""INSERT OR IGNORE INTO entities
                   (user_id, type, title, parent_id, meta)
                   VALUES (?, 'task', ?, ?, ?)""",
                (user_id, task_title, list_id, json.dumps({"status": "open"}, ensure_ascii=False)))
    db_log(f"✅ добавлена задача '{task_title}' → список '{list_name}' (list_id={list_id})")
    return cur.lastrowid

def get_list_tasks(conn, user_id, list_name):
    cur = conn.cursor()
    cur.execute("""SELECT id FROM entities WHERE user_id=? AND type='list' AND title=? LIMIT 1""",
                (user_id, list_name))
    row = cur.fetchone()
    if not row:
        db_log(f"⚠️ список '{list_name}' не найден")
        return []
    list_id = row["id"]
    cur.execute("""SELECT title FROM entities WHERE user_id=? AND type='task' AND parent_id=? ORDER BY created_at ASC""",
                (user_id, list_id))
    tasks = [r["title"] for r in cur.fetchall()]
    db_log(f"📋 найдено {len(tasks)} задач в списке '{list_name}'")
    return tasks

def mark_task_done(conn, user_id, list_name, task_title):
    db_log(f"➡️ mark_task_done user={user_id} list='{list_name}' task='{task_title}'")
    cur = conn.cursor()
    cur.execute("""SELECT id FROM entities WHERE user_id=? AND type='list' AND title=? LIMIT 1""",
                (user_id, list_name))
    row = cur.fetchone()
    if not row:
        db_log("⚠️ список не найден")
        return 0
    list_id = row["id"]
    cur.execute("""SELECT id, meta FROM entities
                   WHERE user_id=? AND type='task' AND parent_id=? AND title=? LIMIT 1""",
                (user_id, list_id, task_title))
    task = cur.fetchone()
    if not task:
        db_log("⚠️ задача не найдена")
        return 0
    meta = {}
    if task["meta"]:
        try:
            meta = json.loads(task["meta"])
        except:
            meta = {}
    meta["status"] = "done"
    cur.execute("""UPDATE entities SET meta=? WHERE id=?""",
                (json.dumps(meta, ensure_ascii=False), task["id"]))
    db_log(f"✔️ отмечена как выполненная '{task_title}'")
    return cur.rowcount

# --- Совместимость (заглушки) ---
def delete_list(conn, user_id, list_name):
    db_log(f"🗑 delete_list(user={user_id}, list='{list_name}') вызвана (заглушка)")
    return 0

def delete_task(conn, user_id, list_name, task_title):
    db_log(f"🗑 delete_task(user={user_id}, list='{list_name}', task='{task_title}') вызвана (заглушка)")
    return 0

def restore_task(conn, user_id, list_name, task_title):
    db_log(f"♻️ restore_task(user={user_id}, list='{list_name}', task='{task_title}') вызвана (заглушка)")
    return 0

def fetch_task(conn, user_id, list_name, task_title):
    cur = conn.cursor()
    cur.execute("""SELECT e.id, e.title, e.meta FROM entities e
                   JOIN entities l ON l.id = e.parent_id
                   WHERE e.user_id=? AND e.type='task' AND l.title=? AND e.title=? LIMIT 1""",
                (user_id, list_name, task_title))
    return cur.fetchone()

def fetch_list_by_task(conn, user_id, task_title):
    cur = conn.cursor()
    cur.execute("""SELECT l.title AS list_title, e.title AS task_title
                   FROM entities e
                   JOIN entities l ON l.id = e.parent_id
                   WHERE e.user_id=? AND e.type='task' AND e.title=? LIMIT 1""",
                (user_id, task_title))
    return cur.fetchone()

# --- временная заглушка для совместимости ---
def find_list(conn, user_id, list_name):
    """
    Совместимость со старой версией main.py.
    Возвращает запись списка (id, title) по названию.
    """
    cur = conn.cursor()
    cur.execute("""SELECT id, title FROM entities
                   WHERE user_id=? AND type='list' AND title=? LIMIT 1""",
                (user_id, list_name))
    return cur.fetchone()
