import os
import sqlite3
from datetime import datetime

DB_PATH = os.getenv("DB_PATH", "/opt/aura-assistant/db.sqlite3")

def get_conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def has_table(conn, name: str) -> bool:
    cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?;", (name,))
    return cur.fetchone() is not None

def init_db():
    conn = get_conn()
    cur = conn.cursor()
    # Основная универсальная таблица
    cur.execute("""
    CREATE TABLE IF NOT EXISTS entities (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        content TEXT NOT NULL,
        type TEXT DEFAULT 'note',     -- task | list | note | reminder | study
        tags TEXT,
        parent_id INTEGER,
        status TEXT DEFAULT 'active', -- active | done | deleted
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """)
    # Индексы для скорости
    cur.execute("CREATE INDEX IF NOT EXISTS idx_entities_user_type_status ON entities(user_id, type, status);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_entities_parent ON entities(parent_id);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_entities_user_content_type ON entities(user_id, content, type);")
    conn.commit()

    # Автомиграция со старой схемы lists -> entities
    try:
        if has_table(conn, "lists"):
            migrate_legacy_lists_to_entities(conn)
    except Exception as e:
        # Логов у нас тут нет — оставим тихо, чтобы не падать при старте
        pass
    finally:
        conn.close()

def migrate_legacy_lists_to_entities(conn: sqlite3.Connection):
    """
    Переносит данные из старой таблицы `lists(user_id, list_name, task)`
    в новую схему entities: создаёт entity-списки и entity-задачи с parent_id.
    Старую таблицу переименовывает в lists_migrated_<ts>.
    """
    cur = conn.cursor()
    # distinct списки
    cur.execute("SELECT DISTINCT user_id, list_name FROM lists;")
    rows = cur.fetchall()
    list_map = {}  # (user_id, list_name) -> list_entity_id

    for r in rows:
        user_id = r["user_id"]
        list_name = (r["list_name"] or "Без названия").strip()
        list_id = get_or_create_list_entity(conn, user_id, list_name)
        list_map[(user_id, list_name)] = list_id

    # сами задачи
    cur.execute("SELECT user_id, list_name, task FROM lists;")
    for r in cur.fetchall():
        user_id = r["user_id"]
        list_name = (r["list_name"] or "Без названия").strip()
        content = (r["task"] or "").strip()
        if not content:
            continue
        parent_id = list_map.get((user_id, list_name))
        if parent_id:
            create_entity(conn, user_id, content=content, type_="task", parent_id=parent_id)

    # Переименуем старую таблицу
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    cur.execute(f"ALTER TABLE lists RENAME TO lists_migrated_{ts};")
    conn.commit()

def create_entity(conn, user_id: int, content: str, type_: str = "task", tags: str = None, parent_id: int = None):
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO entities (user_id, content, type, tags, parent_id, status)
        VALUES (?, ?, ?, ?, ?, 'active');
    """, (user_id, content.strip(), type_.strip(), tags, parent_id))
    conn.commit()
    return cur.lastrowid

def get_or_create_list_entity(conn, user_id: int, list_name: str):
    name = list_name.strip()
    cur = conn.cursor()
    cur.execute("""
        SELECT id FROM entities WHERE user_id=? AND type='list' AND content=? AND status!='deleted' LIMIT 1;
    """, (user_id, name))
    row = cur.fetchone()
    if row:
        return row["id"]
    return create_entity(conn, user_id, content=name, type_="list")

def add_task(conn, user_id: int, list_name: str, task_content: str):
    parent_id = get_or_create_list_entity(conn, user_id, list_name)
    return create_entity(conn, user_id, content=task_content, type_="task", parent_id=parent_id)

def find_list(conn, user_id: int, list_name: str):
    cur = conn.cursor()
    cur.execute("""
        SELECT * FROM entities
        WHERE user_id=? AND type='list' AND content=? AND status!='deleted' LIMIT 1;
    """, (user_id, list_name.strip()))
    return cur.fetchone()

def get_list_tasks(conn, user_id: int, list_name: str, statuses=("active",)):
    lst = find_list(conn, user_id, list_name)
    if not lst:
        return [], None
    parent_id = lst["id"]
    qmarks = ",".join("?" * len(statuses))
    cur = conn.cursor()
    cur.execute(f"""
        SELECT * FROM entities
        WHERE user_id=? AND type='task' AND parent_id=? AND status IN ({qmarks})
        ORDER BY id ASC;
    """, (user_id, parent_id, *statuses))
    return cur.fetchall(), lst

def get_all_lists(conn, user_id: int):
    cur = conn.cursor()
    cur.execute("""
        SELECT id, content FROM entities
        WHERE user_id=? AND type='list' AND status!='deleted'
        ORDER BY content COLLATE NOCASE ASC;
    """, (user_id,))
    return cur.fetchall()

def mark_task_done(conn, user_id: int, task_id: int):
    cur = conn.cursor()
    cur.execute("""
        UPDATE entities SET status='done' WHERE id=? AND user_id=? AND type='task';
    """, (task_id, user_id))
    conn.commit()

def delete_task(conn, user_id: int, task_id: int):
    cur = conn.cursor()
    cur.execute("""
        UPDATE entities SET status='deleted' WHERE id=? AND user_id=? AND type='task';
    """, (task_id, user_id))
    conn.commit()

def restore_task(conn, user_id: int, task_id: int):
    cur = conn.cursor()
    cur.execute("""
        UPDATE entities SET status='active' WHERE id=? AND user_id=? AND type='task';
    """, (task_id, user_id))
    conn.commit()

def delete_list(conn, user_id: int, list_name: str):
    lst = find_list(conn, user_id, list_name)
    if not lst:
        return
    lid = lst["id"]
    cur = conn.cursor()
    # помечаем удалённой и сам список, и все его задачи
    cur.execute("UPDATE entities SET status='deleted' WHERE id=? AND user_id=?;", (lid, user_id))
    cur.execute("UPDATE entities SET status='deleted' WHERE parent_id=? AND user_id=?;", (lid, user_id))
    conn.commit()

def fetch_task(conn, user_id: int, task_id: int):
    cur = conn.cursor()
    cur.execute("SELECT * FROM entities WHERE id=? AND user_id=? AND type='task' LIMIT 1;", (task_id, user_id))
    return cur.fetchone()

def fetch_list_by_task(conn, task_row):
    if not task_row:
        return None
    if task_row["parent_id"] is None:
        return None
    cur = conn.cursor()
    cur.execute("SELECT * FROM entities WHERE id=? AND type='list' LIMIT 1;", (task_row["parent_id"],))
    return cur.fetchone()
