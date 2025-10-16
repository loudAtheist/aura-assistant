import sqlite3, json, os, re
from Levenshtein import distance
import logging

DB_PATH = "/opt/aura-assistant/db.sqlite3"

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
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.isolation_level = None  # autocommit
    return conn

def init_db():
    conn = get_conn()
    with conn:
        conn.execute(ENTITIES_DDL)
    conn.close()

def _get_or_create_list(conn, user_id, list_name):
    try:
        cur = conn.cursor()
        cur.execute("""SELECT id FROM entities
                       WHERE user_id=? AND type='list' AND title=? LIMIT 1""",
                    (user_id, list_name))
        row = cur.fetchone()
        if row:
            logging.info(f"List '{list_name}' already exists for user {user_id}, ID: {row[0]}")
            return row[0]
        cur.execute("""INSERT INTO entities (user_id, type, title)
                       VALUES (?, 'list', ?)""",
                    (user_id, list_name))
        list_id = cur.lastrowid
        logging.info(f"Created list '{list_name}' for user {user_id}, ID: {list_id}")
        return list_id
    except sqlite3.Error as e:
        logging.error(f"SQLite error in _get_or_create_list: {e}")
        raise

def create_list(conn, user_id, list_name):
    return _get_or_create_list(conn, user_id, list_name)

def rename_list(conn, user_id, old_name, new_name):
    try:
        cur = conn.cursor()
        cur.execute("""SELECT id FROM entities
                       WHERE user_id=? AND type='list' AND title=? LIMIT 1""",
                    (user_id, old_name))
        row = cur.fetchone()
        if not row:
            logging.info(f"No list '{old_name}' found for user {user_id}")
            return 0
        cur.execute("""SELECT id FROM entities
                       WHERE user_id=? AND type='list' AND title=? LIMIT 1""",
                    (user_id, new_name))
        if cur.fetchone():
            logging.info(f"List '{new_name}' already exists for user {user_id}")
            return 0
        cur.execute("""UPDATE entities SET title=? WHERE id=?""",
                    (new_name, row["id"]))
        logging.info(f"Renamed list '{old_name}' to '{new_name}' for user {user_id}")
        return 1
    except sqlite3.Error as e:
        logging.error(f"SQLite error in rename_list: {e}")
        return 0

def find_list(conn, user_id, list_name):
    try:
        cur = conn.cursor()
        cur.execute("""SELECT id, title FROM entities
                       WHERE user_id=? AND type='list' AND title=? LIMIT 1""",
                    (user_id, list_name))
        return cur.fetchone()
    except sqlite3.Error as e:
        logging.error(f"SQLite error in find_list: {e}")
        return None

def get_all_lists(conn, user_id):
    try:
        cur = conn.cursor()
        cur.execute("""SELECT title FROM entities
                       WHERE user_id=? AND type='list' AND (meta IS NULL OR json_extract(meta, '$.deleted') IS NOT TRUE)
                       ORDER BY created_at ASC""",
                    (user_id,))
        return [row["title"] for row in cur.fetchall()]
    except sqlite3.Error as e:
        logging.error(f"SQLite error in get_all_lists: {e}")
        return []

def add_task(conn, user_id, list_name, task_title):
    try:
        list_id = _get_or_create_list(conn, user_id, list_name)
        cur = conn.cursor()
        cur.execute("""SELECT id FROM entities
                       WHERE user_id=? AND type='task' AND parent_id=? AND title=?""",
                    (user_id, list_id, task_title))
        if cur.fetchone():
            logging.info(f"Task '{task_title}' already exists in list '{list_name}' for user {user_id}")
            return 0
        cur.execute("""INSERT OR IGNORE INTO entities
                       (user_id, type, title, parent_id, meta)
                       VALUES (?, 'task', ?, ?, ?)""",
                    (user_id, task_title, list_id, json.dumps({"status": "open"}, ensure_ascii=False)))
        task_id = cur.lastrowid
        logging.info(f"Added task '{task_title}' to list '{list_name}' for user {user_id}, ID: {task_id}")
        return task_id
    except sqlite3.Error as e:
        logging.error(f"SQLite error in add_task: {e}")
        raise

def get_list_tasks(conn, user_id, list_name):
    try:
        cur = conn.cursor()
        cur.execute("""SELECT id FROM entities
                       WHERE user_id=? AND type='list' AND title=? LIMIT 1""",
                    (user_id, list_name))
        row = cur.fetchone()
        if not row:
            logging.info(f"No list '{list_name}' found for user {user_id}")
            return []
        list_id = row["id"]
        cur.execute("""SELECT title FROM entities
                       WHERE user_id=? AND type='task' AND parent_id=? AND (meta IS NULL OR json_extract(meta, '$.deleted') IS NOT TRUE)
                       ORDER BY created_at ASC""",
                    (user_id, list_id))
        tasks = [(i+1, r["title"]) for i, r in enumerate(cur.fetchall())]
        logging.info(f"Retrieved {len(tasks)} tasks for list '{list_name}' for user {user_id}")
        return tasks
    except sqlite3.Error as e:
        logging.error(f"SQLite error in get_list_tasks: {e}")
        return []

def mark_task_done(conn, user_id, list_name, task_title):
    try:
        cur = conn.cursor()
        cur.execute("""SELECT id FROM entities
                       WHERE user_id=? AND type='list' AND title=? LIMIT 1""",
                    (user_id, list_name))
        row = cur.fetchone()
        if not row:
            logging.info(f"No list '{list_name}' found for user {user_id}")
            return 0
        list_id = row["id"]
        cur.execute("""SELECT id, meta FROM entities
                       WHERE user_id=? AND type='task' AND parent_id=? AND title=? LIMIT 1""",
                    (user_id, list_id, task_title))
        task = cur.fetchone()
        if not task:
            logging.info(f"No task '{task_title}' found in list '{list_name}' for user {user_id}")
            return 0
        meta = {}
        if task["meta"]:
            try: meta = json.loads(task["meta"])
            except: meta = {}
        meta["status"] = "done"
        cur.execute("""UPDATE entities SET meta=? WHERE id=?""",
                    (json.dumps(meta, ensure_ascii=False), task["id"]))
        logging.info(f"Marked task '{task_title}' as done in list '{list_name}' for user {user_id}")
        return cur.rowcount
    except sqlite3.Error as e:
        logging.error(f"SQLite error in mark_task_done: {e}")
        return 0

def mark_task_done_fuzzy(conn, user_id, list_name, pattern):
    try:
        if not pattern:
            logging.info(f"No pattern provided for fuzzy mark done in list '{list_name}' for user {user_id}")
            return 0, None
        q = re.sub(r'[^0-9a-zA-Zа-яА-ЯёЁ ]+', ' ', pattern).strip()
        if not q:
            logging.info(f"Invalid pattern after cleaning for fuzzy mark done in list '{list_name}' for user {user_id}")
            return 0, None
        cur = conn.cursor()
        cur.execute("""SELECT id FROM entities
                       WHERE user_id=? AND type='list' AND title=? LIMIT 1""",
                    (user_id, list_name))
        row = cur.fetchone()
        if not row:
            logging.info(f"No list '{list_name}' found for user {user_id}")
            return 0, None
        list_id = row["id"]
        cur.execute("""SELECT id, title, meta FROM entities
                       WHERE user_id=? AND type='task' AND parent_id=? AND (meta IS NULL OR json_extract(meta, '$.deleted') IS NOT TRUE)""",
                    (user_id, list_id))
        candidates = [(r["id"], r["title"], r["meta"]) for r in cur.fetchall()]
        if not candidates:
            logging.info(f"No tasks found in list '{list_name}' for user {user_id}")
            return 0, None
        candidates.sort(key=lambda x: distance(x[1].lower(), q.lower()))
        if distance(candidates[0][1].lower(), q.lower()) > len(q) // 2:
            logging.info(f"No close match for pattern '{q}' in list '{list_name}' for user {user_id}")
            return 0, None
        chosen_id, chosen_title, meta = candidates[0]
        try:
            meta = json.loads(meta) if meta else {}
        except:
            meta = {}
        meta["status"] = "done"
        cur.execute("UPDATE entities SET meta=? WHERE id=?", (json.dumps(meta, ensure_ascii=False), chosen_id))
        logging.info(f"Fuzzy marked task '{chosen_title}' as done in list '{list_name}' for user {user_id}")
        return 1, chosen_title
    except sqlite3.Error as e:
        logging.error(f"SQLite error in mark_task_done_fuzzy: {e}")
        return 0, None

def delete_list(conn, user_id, list_name):
    try:
        cur = conn.cursor()
        cur.execute("""SELECT id, meta FROM entities
                       WHERE user_id=? AND type='list' AND title=? LIMIT 1""",
                    (user_id, list_name))
        row = cur.fetchone()
        if not row:
            logging.info(f"No list '{list_name}' found for user {user_id}")
            return 0
        list_id, meta = row["id"], row["meta"]
        try: meta = json.loads(meta) if meta else {}
        except: meta = {}
        meta["deleted"] = True
        cur.execute("UPDATE entities SET meta=? WHERE id=?",
                    (json.dumps(meta, ensure_ascii=False), list_id))
        cur.execute("""SELECT id, meta FROM entities
                       WHERE user_id=? AND type='task' AND parent_id=?""",
                    (user_id, list_id))
        for r in cur.fetchall():
            m = {}
            try: m = json.loads(r["meta"]) if r["meta"] else {}
            except: m = {}
            m["deleted"] = True
            cur.execute("UPDATE entities SET meta=? WHERE id=?", (json.dumps(m, ensure_ascii=False), r["id"]))
        logging.info(f"Deleted list '{list_name}' for user {user_id}")
        return 1
    except sqlite3.Error as e:
        logging.error(f"SQLite error in delete_list: {e}")
        return 0

def delete_task(conn, user_id, list_name, task_title):
    try:
        cur = conn.cursor()
        cur.execute("""SELECT id FROM entities
                       WHERE user_id=? AND type='list' AND title=? LIMIT 1""",
                    (user_id, list_name))
        row = cur.fetchone()
        if not row:
            logging.info(f"No list '{list_name}' found for user {user_id}")
            return 0
        list_id = row["id"]
        cur.execute("""SELECT id, meta FROM entities
                       WHERE user_id=? AND type='task' AND parent_id=? AND title=? LIMIT 1""",
                    (user_id, list_id, task_title))
        t = cur.fetchone()
        if not t:
            logging.info(f"No task '{task_title}' found in list '{list_name}' for user {user_id}")
            return 0
        meta = {}
        try: meta = json.loads(t["meta"]) if t["meta"] else {}
        except: meta = {}
        meta["deleted"] = True
        cur.execute("UPDATE entities SET meta=? WHERE id=?", (json.dumps(meta, ensure_ascii=False), t["id"]))
        logging.info(f"Deleted task '{task_title}' from list '{list_name}' for user {user_id}")
        return 1
    except sqlite3.Error as e:
        logging.error(f"SQLite error in delete_task: {e}")
        return 0

def restore_task(conn, user_id, list_name, task_title):
    try:
        cur = conn.cursor()
        cur.execute("""SELECT id FROM entities
                       WHERE user_id=? AND type='list' AND title=? LIMIT 1""",
                    (user_id, list_name))
        row = cur.fetchone()
        if not row:
            logging.info(f"No list '{list_name}' found for user {user_id}")
            return 0
        list_id = row["id"]
        cur.execute("""SELECT id, meta FROM entities
                       WHERE user_id=? AND type='task' AND parent_id=? AND title=? LIMIT 1""",
                    (user_id, list_id, task_title))
        t = cur.fetchone()
        if not t:
            logging.info(f"No task '{task_title}' found in list '{list_name}' for user {user_id}")
            return 0
        meta = json.loads(t["meta"]) if t["meta"] else {}
        meta["deleted"] = False
        if "status" in meta:
            meta["status"] = "open"
        cur.execute("UPDATE entities SET meta=? WHERE id=?", (json.dumps(meta, ensure_ascii=False), t["id"]))
        logging.info(f"Restored task '{task_title}' in list '{list_name}' for user {user_id}")
        return 1
    except sqlite3.Error as e:
        logging.error(f"SQLite error in restore_task: {e}")
        return 0

def get_completed_tasks(conn, user_id):
    try:
        cur = conn.cursor()
        cur.execute("""SELECT l.title AS list_title, e.title AS task_title, e.meta FROM entities e
                       LEFT JOIN entities l ON l.id = e.parent_id
                       WHERE e.user_id=? AND e.type='task' AND (json_extract(e.meta, '$.status') = 'done' OR json_extract(e.meta, '$.deleted') = true)
                       ORDER BY e.created_at DESC LIMIT 100""",
                    (user_id,))
        tasks = [(row["list_title"], row["task_title"], json.loads(row["meta"]) if row["meta"] else {}) for row in cur.fetchall()]
        logging.info(f"Retrieved {len(tasks)} completed/deleted tasks for user {user_id}")
        return tasks
    except sqlite3.Error as e:
        logging.error(f"SQLite error in get_completed_tasks: {e}")
        return []

def fetch_task(conn, user_id, list_name, task_title):
    try:
        cur = conn.cursor()
        cur.execute("""SELECT e.id, e.title, e.meta FROM entities e
                       JOIN entities l ON l.id = e.parent_id
                       WHERE e.user_id=? AND e.type='task' AND l.type='list' AND l.title=? AND e.title=?
                       LIMIT 1""",
                    (user_id, list_name, task_title))
        task = cur.fetchone()
        logging.info(f"Fetched task '{task_title}' from list '{list_name}' for user {user_id}: {'Found' if task else 'Not found'}")
        return task
    except sqlite3.Error as e:
        logging.error(f"SQLite error in fetch_task: {e}")
        return None

def fetch_list_by_task(conn, user_id, task_title):
    try:
        cur = conn.cursor()
        cur.execute("""SELECT l.title AS list_title, e.title AS task_title
                       FROM entities e
                       JOIN entities l ON l.id = e.parent_id
                       WHERE e.user_id=? AND e.type='task' AND e.title=?
                       LIMIT 1""",
                    (user_id, task_title))
        result = cur.fetchone()
        logging.info(f"Fetched list by task '{task_title}' for user {user_id}: {'Found' if result else 'Not found'}")
        return result
    except sqlite3.Error as e:
        logging.error(f"SQLite error in fetch_list_by_task: {e}")
        return None

def convert_entity(conn, user_id, list_name, task_title, new_type):
    try:
        cur = conn.cursor()
        cur.execute("""SELECT id FROM entities
                       WHERE user_id=? AND type='list' AND title=? LIMIT 1""",
                    (user_id, list_name))
        row = cur.fetchone()
        if not row:
            logging.info(f"No list '{list_name}' found for user {user_id}")
            return 0
        list_id = row["id"]
        cur.execute("""SELECT id FROM entities
                       WHERE user_id=? AND type='task' AND parent_id=? AND title=? LIMIT 1""",
                    (user_id, list_id, task_title))
        task = cur.fetchone()
        if not task:
            logging.info(f"No task '{task_title}' found in list '{list_name}' for user {user_id}")
            return 0
        cur.execute("UPDATE entities SET type=? WHERE id=?", (new_type, task["id"]))
        logging.info(f"Converted task '{task_title}' to type '{new_type}' in list '{list_name}' for user {user_id}")
        return cur.rowcount
    except sqlite3.Error as e:
        logging.error(f"SQLite error in convert_entity: {e}")
        return 0

def move_entity(conn, user_id, entity_type, title, from_list, to_list):
    try:
        cur = conn.cursor()
        cur.execute("SELECT id FROM entities WHERE user_id=? AND type='list' AND title=? LIMIT 1", (user_id, from_list))
        from_row = cur.fetchone()
        if not from_row:
            logging.info(f"No list '{from_list}' found for user {user_id}")
            return 0
        from_list_id = from_row["id"]
        to_list_id = _get_or_create_list(conn, user_id, to_list)
        cur.execute("SELECT id FROM entities WHERE user_id=? AND type=? AND title=? AND parent_id=? LIMIT 1", 
                    (user_id, entity_type, title, from_list_id))
        entity = cur.fetchone()
        if not entity:
            logging.info(f"No {entity_type} '{title}' found in list '{from_list}' for user {user_id}")
            return 0
        cur.execute("UPDATE entities SET parent_id=? WHERE id=?", (to_list_id, entity["id"]))
        logging.info(f"Moved {entity_type} '{title}' from '{from_list}' to '{to_list}' for user {user_id}")
        return 1
    except sqlite3.Error as e:
        logging.error(f"SQLite error in move_entity: {e}")
        return 0

def get_all_tasks(conn, user_id):
    try:
        cur = conn.cursor()
        cur.execute("SELECT l.title AS list_title, e.title AS task_title FROM entities e JOIN entities l ON l.id = e.parent_id WHERE e.user_id=? AND e.type='task' AND (e.meta IS NULL OR json_extract(e.meta, '$.deleted') IS NOT TRUE) ORDER BY l.title, e.created_at", (user_id,))
        tasks = [(row["list_title"], row["task_title"]) for row in cur.fetchall()]
        logging.info(f"Retrieved {len(tasks)} tasks for user {user_id}")
        return tasks
    except sqlite3.Error as e:
        logging.error(f"SQLite error in get_all_tasks: {e}")
        return []

def update_user_profile(conn, user_id, city=None, profession=None):
    try:
        meta = {"city": city, "profession": profession}
        cur = conn.cursor()
        cur.execute("INSERT OR REPLACE INTO entities (user_id, type, title, meta) VALUES (?, 'user_profile', ?, ?)", 
                    (user_id, f"user_{user_id}", json.dumps(meta, ensure_ascii=False)))
        logging.info(f"Updated user profile for user {user_id}: {meta}")
        return 1
    except sqlite3.Error as e:
        logging.error(f"SQLite error in update_user_profile: {e}")
        return 0

def get_user_profile(conn, user_id):
    try:
        cur = conn.cursor()
        cur.execute("SELECT meta FROM entities WHERE user_id=? AND type='user_profile' AND title=? LIMIT 1", 
                    (user_id, f"user_{user_id}"))
        row = cur.fetchone()
        if row and row["meta"]:
            return json.loads(row["meta"])
        return {}
    except sqlite3.Error as e:
        logging.error(f"SQLite error in get_user_profile: {e}")
        return {}

def delete_task_fuzzy(conn, user_id, list_name, pattern: str):
    try:
        if not pattern: 
            logging.info(f"No pattern provided for fuzzy delete in list '{list_name}' for user {user_id}")
            return 0, None
        q = re.sub(r'[^0-9a-zA-Zа-яА-ЯёЁ ]+', ' ', pattern).strip()
        if not q:
            logging.info(f"Invalid pattern after cleaning for fuzzy delete in list '{list_name}' for user {user_id}")
            return 0, None
        cur = conn.cursor()
        cur.execute("""SELECT id FROM entities
                       WHERE user_id=? AND type='list' AND title=? LIMIT 1""",
                    (user_id, list_name))
        row = cur.fetchone()
        if not row:
            logging.info(f"No list '{list_name}' found for user {user_id}")
            return 0, None
        list_id = row["id"]
        cur.execute("""SELECT id, title, meta FROM entities
                       WHERE user_id=? AND type='task' AND parent_id=? AND (meta IS NULL OR json_extract(meta, '$.deleted') IS NOT TRUE)""",
                    (user_id, list_id))
        candidates = [(r["id"], r["title"], r["meta"]) for r in cur.fetchall()]
        if not candidates:
            logging.info(f"No tasks found in list '{list_name}' for user {user_id}")
            return 0, None
        candidates.sort(key=lambda x: distance(x[1].lower(), q.lower()))
        if distance(candidates[0][1].lower(), q.lower()) > len(q) // 2:
            logging.info(f"No close match for pattern '{q}' in list '{list_name}' for user {user_id}")
            return 0, None
        chosen_id, chosen_title, meta = candidates[0]
        try:
            m = json.loads(meta) if meta else {}
        except:
            m = {}
        m["deleted"] = True
        cur.execute("UPDATE entities SET meta=? WHERE id=?",
                    (json.dumps(m, ensure_ascii=False), chosen_id))
        logging.info(f"Fuzzy deleted task '{chosen_title}' from list '{list_name}' for user {user_id}")
        return 1, chosen_title
    except sqlite3.Error as e:
        logging.error(f"SQLite error in delete_task_fuzzy: {e}")
        return 0, None

def delete_task_by_index(conn, user_id, list_name: str, index: int):
    try:
        cur = conn.cursor()
        cur.execute("""SELECT id FROM entities
                       WHERE user_id=? AND type='list' AND title=? LIMIT 1""",
                    (user_id, list_name))
        row = cur.fetchone()
        if not row:
            logging.info(f"No list '{list_name}' found for user {user_id}")
            return 0, None
        list_id = row["id"]
        cur.execute("""SELECT id, title, meta FROM entities
                       WHERE user_id=? AND type='task' AND parent_id=? AND (meta IS NULL OR json_extract(meta, '$.deleted') IS NOT TRUE)
                       ORDER BY created_at ASC""",
                    (user_id, list_id))
        tasks = cur.fetchall()
        if not tasks or index < 1 or index > len(tasks):
            logging.info(f"Invalid index {index} for list '{list_name}' for user {user_id}")
            return 0, None
        chosen = tasks[index - 1]
        task_id, task_title, meta = chosen["id"], chosen["title"], chosen["meta"]
        try:
            m = json.loads(meta) if meta else {}
        except:
            m = {}
        m["deleted"] = True
        cur.execute("UPDATE entities SET meta=? WHERE id=?",
                    (json.dumps(m, ensure_ascii=False), task_id))
        logging.info(f"Deleted task '{task_title}' by index {index} from list '{list_name}' for user {user_id}")
        return 1, task_title
    except sqlite3.Error as e:
        logging.error(f"SQLite error in delete_task_by_index: {e}")
        return 0, None

def normalize_text(value: str) -> str:
    if not value:
        return value
    value = value.strip()
    value = re.sub(r'\s+', ' ', value)
    value = value[:1].upper() + value[1:]
    value = re.sub(r'\bsp[oO]2\b', 'SPO2', value, flags=re.IGNORECASE)
    return value
