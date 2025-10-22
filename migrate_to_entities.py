from __future__ import annotations

import json
import os
import sqlite3
import sys
from collections.abc import Iterable

DB_PATH = os.getenv("DB_PATH", "/opt/aura-assistant/db.sqlite3")

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

def table_exists(conn: sqlite3.Connection, name: str) -> bool:
    cur = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (name,),
    )
    return cur.fetchone() is not None


def _quote_identifier(name: str) -> str:
    escaped = name.replace('"', '""')
    return f'"{escaped}"'


def table_columns(conn: sqlite3.Connection, name: str) -> set[str]:
    cur = conn.execute(f"PRAGMA table_info({_quote_identifier(name)})")
    return {row[1] for row in cur.fetchall()}


def ensure_list_entity(conn: sqlite3.Connection, user_id: int, name: str) -> int | None:
    conn.execute(
        "INSERT OR IGNORE INTO entities (user_id, type, title) VALUES (?, 'list', ?)",
        (user_id, name),
    )
    row = conn.execute(
        "SELECT id FROM entities WHERE user_id=? AND type='list' AND title=? LIMIT 1",
        (user_id, name),
    ).fetchone()
    return row[0] if row else None


def migrate_lists_tasks(conn: sqlite3.Connection) -> None:
    if not table_exists(conn, "lists") and not table_exists(conn, "tasks"):
        return

    list_id_by_key: dict[tuple[int, str], int] = {}

    if table_exists(conn, "lists"):
        cols = table_columns(conn, "lists")
        if {"user_id", "name"}.issubset(cols):
            for user_id, name in conn.execute("SELECT DISTINCT user_id, name FROM lists"):
                list_id = ensure_list_entity(conn, user_id, name)
                if list_id is not None:
                    list_id_by_key[(user_id, name)] = list_id

    if table_exists(conn, "tasks"):
        cols = table_columns(conn, "tasks")
        if {"user_id", "list_name", "task"}.issubset(cols):
            tasks: Iterable[tuple[int, str, str]] = conn.execute(
                "SELECT user_id, list_name, task FROM tasks"
            )
            for user_id, list_name, task in tasks:
                list_id = list_id_by_key.get((user_id, list_name))
                if list_id is None:
                    list_id = ensure_list_entity(conn, user_id, list_name)
                    if list_id is not None:
                        list_id_by_key[(user_id, list_name)] = list_id
                if list_id is None:
                    continue
                conn.execute(
                    """
                    INSERT OR IGNORE INTO entities (user_id, type, title, parent_id, meta)
                    VALUES (?, 'task', ?, ?, ?)
                    """,
                    (
                        user_id,
                        task,
                        list_id,
                        json.dumps({"status": "open"}, ensure_ascii=False),
                    ),
                )

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
