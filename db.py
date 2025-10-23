from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
from collections.abc import Iterable, Sequence
from typing import Any, TypedDict

from Levenshtein import distance

DB_PATH = os.getenv("DB_PATH", "/opt/aura-assistant/db.sqlite3")
DB_DEBUG_PATH = os.getenv("DB_DEBUG_LOG", "/opt/aura-assistant/db_debug.log")

_db_debug_dir = os.path.dirname(DB_DEBUG_PATH)
if _db_debug_dir:
    os.makedirs(_db_debug_dir, exist_ok=True)

_sql_logger = logging.getLogger("aura.db.sql")
if not _sql_logger.handlers:
    _sql_logger.setLevel(logging.DEBUG)
    _sql_handler = logging.FileHandler(DB_DEBUG_PATH, encoding="utf-8")
    _sql_handler.setFormatter(logging.Formatter("%(asctime)s | %(message)s"))
    _sql_logger.addHandler(_sql_handler)
    _sql_logger.propagate = False

_semantic_logger = logging.getLogger("semantic_similarity")
if not _semantic_logger.handlers:
    _semantic_logger.setLevel(logging.DEBUG)
    _semantic_handler = logging.FileHandler(DB_DEBUG_PATH, encoding="utf-8")
    _semantic_handler.setFormatter(
        logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s")
    )
    _semantic_logger.addHandler(_semantic_handler)
    _semantic_logger.propagate = False

_app_logger = logging.getLogger("aura")


def _trace_sql(statement: str) -> None:
    _sql_logger.debug(statement)

_TOKEN_STOPWORDS = {
    "и",
    "да",
    "нет",
    "куплены",
    "куплено",
    "куплена",
    "куплен",
    "готово",
    "готовы",
    "готов",
    "выполнено",
    "выполнены",
    "выполнен",
    "сделано",
    "сделаны",
    "сделан",
}

_SEMANTIC_STOPWORDS = _TOKEN_STOPWORDS | {
    "во",
    "в",
    "на",
    "по",
    "за",
    "из",
    "у",
    "к",
    "со",
    "от",
    "до",
    "для",
    "это",
    "эта",
    "этот",
    "там",
}

_SEMANTIC_REPLACEMENTS = {
    "оплатить": "платить",
    "заплатить": "платить",
    "оплата": "платить",
    "платеж": "платить",
    "квитанция": "платить",
    "покупку": "покуп",
    "покупка": "покуп",
    "покупки": "покуп",
    "купить": "покуп",
    "электричество": "свет",
    "электричества": "свет",
    "электроэнергия": "свет",
    "электроэнергию": "свет",
    "свет": "свет",
}

_SEMANTIC_SUFFIXES = (
    "иями",
    "ями",
    "ами",
    "иях",
    "ях",
    "ев",
    "ов",
    "его",
    "ого",
    "ему",
    "ому",
    "ыми",
    "ими",
)


class CreationResult(TypedDict, total=False):
    id: int | None
    title: str | None
    created: bool
    restored: bool
    duplicate_detected: bool
    duplicate_id: int | None
    duplicate_title: str | None
    similarity: float | None
    auto_use: bool
    missing_parent: bool
def _tokenize(text: str) -> list[str]:
    parts = re.split(r"[^0-9a-zA-Zа-яА-ЯёЁ]+", (text or "").lower())
    return [p for p in parts if p and p not in _TOKEN_STOPWORDS]


def _normalize_semantic_token(token: str) -> str:
    base = _SEMANTIC_REPLACEMENTS.get(token, token)
    if len(base) > 4:
        for suffix in _SEMANTIC_SUFFIXES:
            if base.endswith(suffix) and len(base) - len(suffix) >= 4:
                base = base[: -len(suffix)]
                break
    if len(base) > 4 and base[-1] in {"и", "ы", "а", "я", "е", "ю", "ь", "й"}:
        base = base[:-1]
    return base


def _semantic_tokenize(text: str) -> list[str]:
    normalized = re.sub(r"[^0-9a-zA-Zа-яА-ЯёЁ]+", " ", (text or "").lower())
    normalized = normalized.replace("ё", "е")
    raw_tokens = [part for part in normalized.split() if part]
    tokens: list[str] = []
    for token in raw_tokens:
        if token in _SEMANTIC_STOPWORDS:
            continue
        reduced = _normalize_semantic_token(token)
        if reduced and reduced not in _SEMANTIC_STOPWORDS:
            tokens.append(reduced)
    return tokens


def _creation_result(
    *,
    entity_id: int | None = None,
    title: str | None = None,
    created: bool = False,
    restored: bool = False,
    duplicate_detected: bool = False,
    duplicate_id: int | None = None,
    duplicate_title: str | None = None,
    similarity: float | None = None,
    auto_use: bool = False,
    missing_parent: bool = False,
) -> CreationResult:
    return {
        "id": entity_id,
        "title": title,
        "created": created,
        "restored": restored,
        "duplicate_detected": duplicate_detected,
        "duplicate_id": duplicate_id,
        "duplicate_title": duplicate_title,
        "similarity": similarity,
        "auto_use": auto_use,
        "missing_parent": missing_parent,
    }


def _log_semantic_match(candidate: str, existing: str, score: float) -> None:
    message = f"🔍 Found semantically similar: {existing} ≈ {candidate} ({score:.2f})"
    _semantic_logger.info(message)
    _app_logger.info("⚠️ Duplicate detected: %s ≈ %s (%.2f)", candidate, existing, score)


def semantic_similarity(text_a: str | None, text_b: str | None) -> float:
    tokens_a = set(_semantic_tokenize(text_a or ""))
    tokens_b = set(_semantic_tokenize(text_b or ""))
    union = tokens_a | tokens_b
    if not union:
        score = 0.0
    else:
        score = len(tokens_a & tokens_b) / len(union)
    _semantic_logger.debug(
        "Tokens(A): %s Tokens(B): %s → %.2f",
        sorted(tokens_a),
        sorted(tokens_b),
        score,
    )
    return score


def _find_semantic_duplicate(
    conn: sqlite3.Connection,
    user_id: int,
    title: str,
    entity_type: str,
    *,
    parent_id: int | None = None,
    threshold: float = 0.75,
) -> tuple[int, str, float] | None:
    cleaned = (title or "").strip()
    if not cleaned:
        return None
    params: list[Any] = [user_id, entity_type]
    query = [
        "SELECT id, title FROM entities",
        "WHERE user_id = ? AND type = ?",
        "AND (meta IS NULL OR json_extract(meta, '$.deleted') IS NOT TRUE)",
    ]
    if parent_id is not None:
        query.append("AND parent_id = ?")
        params.append(parent_id)
    cur = conn.execute(" ".join(query), tuple(params))
    best_match: tuple[int, str, float] | None = None
    for row in cur.fetchall():
        candidate_id = row["id"] if isinstance(row, sqlite3.Row) else row[0]
        candidate_title = row["title"] if isinstance(row, sqlite3.Row) else row[1]
        score = semantic_similarity(cleaned, candidate_title)
        if score > threshold and (best_match is None or score > best_match[2]):
            best_match = (candidate_id, candidate_title, score)
    if best_match:
        _log_semantic_match(cleaned, best_match[1], best_match[2])
    return best_match


def _score_candidates(
    pattern_tokens: Iterable[str],
    cleaned_pattern: str,
    candidates: Iterable[tuple[int, str, str]],
) -> tuple[int, str, str] | None:
    pt_set = set(pattern_tokens)
    cleaned_lower = cleaned_pattern.lower()
    scored: list[tuple[int, int, int, tuple[int, str, str]]] = []
    for candidate in candidates:
        cand_id, cand_title, cand_meta = candidate
        title_lower = cand_title.lower()
        tokens = set(_tokenize(cand_title))
        overlap = len(pt_set & tokens) if pt_set else 0
        substring_match = False
        if pt_set:
            substring_match = any(
                len(token) > 2 and token in title_lower for token in pt_set
            )
        if cleaned_lower and cleaned_lower in title_lower:
            substring_match = True
        if not pt_set and cleaned_lower:
            substring_match = cleaned_lower in title_lower
        if pt_set and overlap == 0 and not substring_match:
            continue
        edit_distance = distance(title_lower, cleaned_lower) if cleaned_lower else 0
        scored.append((-overlap, edit_distance, len(title_lower), candidate))
    if not scored and cleaned_lower:
        for candidate in candidates:
            cand_id, cand_title, cand_meta = candidate
            title_lower = cand_title.lower()
            if cleaned_lower in title_lower or title_lower in cleaned_lower:
                return candidate
        return None
    if not scored:
        return None
    scored.sort()
    best = scored[0][3]
    return best


def _select_candidate(
    pattern: str, candidates: Sequence[tuple[int, str, str]]
) -> tuple[int, str, str] | None:
    if not pattern:
        return None
    cleaned = re.sub(r"[^0-9a-zA-Zа-яА-ЯёЁ ]+", " ", pattern).strip()
    if not cleaned:
        return None
    lowered = cleaned.lower()
    for candidate in candidates:
        _, title, _ = candidate
        if title and title.strip().lower() == lowered:
            return candidate
    return _score_candidates(_tokenize(pattern), cleaned, candidates)


def _load_meta(meta_text: str | None) -> dict[str, Any]:
    if not meta_text:
        return {}
    try:
        return json.loads(meta_text)
    except json.JSONDecodeError:
        logging.warning("Failed to decode meta payload: %s", meta_text)
        return {}


def _dump_meta(meta: dict[str, Any] | None) -> str | None:
    if not meta:
        return None
    return json.dumps(meta, ensure_ascii=False)


def _get_list_id(conn: sqlite3.Connection, user_id: int, list_name: str) -> int | None:
    cur = conn.execute(
        """
        SELECT id FROM entities
        WHERE user_id = ? AND type = 'list' AND LOWER(title) = LOWER(?)
          AND (json_extract(meta, '$.deleted') IS NULL OR json_extract(meta, '$.deleted') IS NOT TRUE)
        LIMIT 1
        """,
        (user_id, list_name),
    )
    row = cur.fetchone()
    if not row:
        logging.info("No list '%s' found for user %s", list_name, user_id)
        return None
    return row["id"] if isinstance(row, sqlite3.Row) else row[0]


def _get_task_row(
    conn: sqlite3.Connection,
    user_id: int,
    list_id: int,
    title: str,
) -> sqlite3.Row | None:
    cur = conn.execute(
        """
        SELECT id, title, meta
        FROM entities
        WHERE user_id = ? AND type = 'task' AND parent_id = ?
          AND LOWER(title) = LOWER(?)
          AND (meta IS NULL OR json_extract(meta, '$.deleted') IS NOT TRUE)
        LIMIT 1
        """,
        (user_id, list_id, title),
    )
    return cur.fetchone()


def _list_active_tasks(
    conn: sqlite3.Connection,
    user_id: int,
    list_id: int,
) -> Sequence[sqlite3.Row]:
    cur = conn.execute(
        """
        SELECT id, title, meta
        FROM entities
        WHERE user_id = ? AND type = 'task' AND parent_id = ?
          AND (meta IS NULL OR json_extract(meta, '$.deleted') IS NOT TRUE)
          AND COALESCE(json_extract(meta, '$.status'), 'open') != 'done'
        ORDER BY created_at ASC
        """,
        (user_id, list_id),
    )
    return cur.fetchall()


def _list_restorable_tasks(
    conn: sqlite3.Connection,
    user_id: int,
    list_id: int,
) -> Sequence[sqlite3.Row]:
    cur = conn.execute(
        """
        SELECT id, title, meta
        FROM entities
        WHERE user_id = ? AND type = 'task' AND parent_id = ?
          AND (
                json_extract(meta, '$.deleted') = true
             OR json_extract(meta, '$.status') = 'done'
             OR json_extract(meta, '$.archived') = true
          )
        """,
        (user_id, list_id),
    )
    return cur.fetchall()

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

def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.isolation_level = None
    conn.set_trace_callback(_trace_sql)
    return conn

def init_db() -> None:
    conn = get_conn()
    with conn:
        conn.execute(ENTITIES_DDL)
    conn.close()

def _get_or_create_list(conn: sqlite3.Connection, user_id: int, list_name: str) -> int | None:
    existing_id = _get_list_id(conn, user_id, list_name)
    if existing_id is not None:
        logging.info("List '%s' already exists for user %s, ID: %s", list_name, user_id, existing_id)
        return existing_id
    try:
        cur = conn.execute(
            """
            INSERT INTO entities (user_id, type, title)
            VALUES (?, 'list', ?)
            """,
            (user_id, list_name),
        )
        list_id = cur.lastrowid
        logging.info("Created list '%s' for user %s, ID: %s", list_name, user_id, list_id)
        return list_id
    except sqlite3.Error as exc:
        logging.error("SQLite error in _get_or_create_list: %s", exc)
        raise


def create_list(
    conn: sqlite3.Connection,
    user_id: int,
    list_name: str,
    *,
    force: bool = False,
) -> CreationResult:
    cleaned_name = (list_name or "").strip()
    if not cleaned_name:
        logging.info("Empty list name received for user %s", user_id)
        return _creation_result(title=list_name)
    try:
        if not force:
            duplicate = _find_semantic_duplicate(
                conn,
                user_id,
                cleaned_name,
                "list",
            )
            if duplicate:
                duplicate_id, duplicate_title, score = duplicate
                logging.info(
                    "Semantic duplicate detected for list '%s' -> '%s' (%.2f) for user %s",
                    cleaned_name,
                    duplicate_title,
                    score,
                    user_id,
                )
                return _creation_result(
                    entity_id=duplicate_id,
                    title=duplicate_title,
                    duplicate_detected=True,
                    duplicate_id=duplicate_id,
                    duplicate_title=duplicate_title,
                    similarity=score,
                    auto_use=score >= 0.85,
                )
        cur = conn.execute(
            """
            INSERT INTO entities (user_id, type, title)
            VALUES (?, 'list', ?)
            """,
            (user_id, cleaned_name),
        )
        list_id = cur.lastrowid
        logging.info(
            "Created list '%s' for user %s, ID: %s", cleaned_name, user_id, list_id
        )
        return _creation_result(entity_id=list_id, title=cleaned_name, created=True)
    except sqlite3.IntegrityError:
        existing_id = _get_list_id(conn, user_id, cleaned_name)
        if existing_id is not None:
            logging.info(
                "List '%s' already exists for user %s, ID: %s", cleaned_name, user_id, existing_id
            )
            score = semantic_similarity(cleaned_name, cleaned_name)
            _log_semantic_match(cleaned_name, cleaned_name, score)
            return _creation_result(
                entity_id=existing_id,
                title=cleaned_name,
                duplicate_detected=True,
                duplicate_id=existing_id,
                duplicate_title=cleaned_name,
                similarity=1.0,
                auto_use=True,
            )
        logging.error(
            "IntegrityError: Failed to create list '%s' for user %s", cleaned_name, user_id
        )
        return _creation_result(title=cleaned_name)
    except sqlite3.Error as exc:
        logging.error("SQLite error in create_list: %s", exc)
        return _creation_result(title=cleaned_name)

def rename_list(conn: sqlite3.Connection, user_id: int, old_name: str, new_name: str) -> int:
    try:
        list_id = _get_list_id(conn, user_id, old_name)
        if list_id is None:
            return 0
        if _get_list_id(conn, user_id, new_name) is not None:
            logging.info("List '%s' already exists for user %s", new_name, user_id)
            return 0
        conn.execute(
            "UPDATE entities SET title = ? WHERE id = ?",
            (new_name, list_id),
        )
        logging.info("Renamed list '%s' to '%s' for user %s", old_name, new_name, user_id)
        return 1
    except sqlite3.Error as exc:
        logging.error("SQLite error in rename_list: %s", exc)
        return 0


def find_list(conn: sqlite3.Connection, user_id: int, list_name: str) -> sqlite3.Row | None:
    try:
        cur = conn.execute(
            """
            SELECT id, title
            FROM entities
            WHERE user_id = ? AND type = 'list' AND LOWER(title) = LOWER(?)
            LIMIT 1
            """,
            (user_id, list_name),
        )
        return cur.fetchone()
    except sqlite3.Error as exc:
        logging.error("SQLite error in find_list: %s", exc)
        return None


def get_all_lists(conn: sqlite3.Connection, user_id: int) -> list[str]:
    try:
        cur = conn.execute(
            """
            SELECT title
            FROM entities
            WHERE user_id = ? AND type = 'list'
              AND (meta IS NULL OR json_extract(meta, '$.deleted') IS NOT TRUE)
            ORDER BY title ASC
            """,
            (user_id,),
        )
        return [row["title"] for row in cur.fetchall()]
    except sqlite3.Error as exc:
        logging.error("SQLite error in get_all_lists: %s", exc)
        return []

def add_task(
    conn: sqlite3.Connection,
    user_id: int,
    list_name: str,
    title: str,
    *,
    force: bool = False,
) -> CreationResult:
    list_row = find_list(conn, user_id, list_name)
    if not list_row:
        logging.info(
            "Cannot add task '%s': list '%s' not found for user %s",
            title,
            list_name,
            user_id,
        )
        return _creation_result(title=title, missing_parent=True)
    list_id = list_row["id"] if isinstance(list_row, sqlite3.Row) else list_row[0]
    existing_task = _get_task_row(conn, user_id, list_id, title)
    if existing_task:
        stored_title = existing_task["title"]
        meta = _load_meta(existing_task["meta"])
        logging.info(
            "Found existing task '%s' in list '%s' for user %s with meta: %s",
            stored_title,
            list_name,
            user_id,
            meta,
        )
        changed = False
        if meta.pop("deleted", None):
            changed = True
        if meta.get("status") == "done":
            meta.pop("status", None)
            changed = True
        if changed:
            conn.execute(
                "UPDATE entities SET meta = ? WHERE id = ?",
                (_dump_meta(meta), existing_task["id"]),
            )
            logging.info(
                "Restored task '%s' in list '%s' for user %s",
                stored_title,
                list_name,
                user_id,
            )
            return _creation_result(
                entity_id=existing_task["id"],
                title=stored_title,
                restored=True,
            )
        score = semantic_similarity(title, stored_title)
        if score < 1.0:
            score = 1.0
        _log_semantic_match(title, stored_title, score)
        logging.info(
            "Task '%s' already exists and is not done in list '%s' for user %s",
            stored_title,
            list_name,
            user_id,
        )
        return _creation_result(
            entity_id=existing_task["id"],
            title=stored_title,
            duplicate_detected=True,
            duplicate_id=existing_task["id"],
            duplicate_title=stored_title,
            similarity=1.0,
            auto_use=True,
        )
    if not force:
        duplicate = _find_semantic_duplicate(
            conn,
            user_id,
            title,
            "task",
            parent_id=list_id,
        )
        if duplicate:
            duplicate_id, duplicate_title, score = duplicate
            logging.info(
                "Semantic duplicate detected for task '%s' -> '%s' (%.2f) in list '%s' for user %s",
                title,
                duplicate_title,
                score,
                list_name,
                user_id,
            )
            return _creation_result(
                entity_id=duplicate_id,
                title=duplicate_title,
                duplicate_detected=True,
                duplicate_id=duplicate_id,
                duplicate_title=duplicate_title,
                similarity=score,
                auto_use=score >= 0.85,
            )
    try:
        cur = conn.execute(
            "INSERT INTO entities (user_id, type, title, parent_id) VALUES (?, 'task', ?, ?)",
            (user_id, title, list_id),
        )
        task_id = cur.lastrowid
        logging.info("Added new task '%s' to list '%s' for user %s", title, list_name, user_id)
        return _creation_result(entity_id=task_id, title=title, created=True)
    except sqlite3.IntegrityError as exc:
        logging.error(
            "IntegrityError: Failed to add task '%s' to list '%s' for user %s: %s",
            title,
            list_name,
            user_id,
            exc,
        )
        return _creation_result(title=title)

def update_task(conn: sqlite3.Connection, user_id: int, list_name: str, old_title: str, new_title: str) -> int:
    try:
        list_id = _get_list_id(conn, user_id, list_name)
        if list_id is None:
            return 0
        task_row = _get_task_row(conn, user_id, list_id, old_title)
        if not task_row:
            logging.info("No task '%s' found in list '%s' for user %s", old_title, list_name, user_id)
            return 0
        if _get_task_row(conn, user_id, list_id, new_title):
            logging.info("Task '%s' already exists in list '%s' for user %s", new_title, list_name, user_id)
            return 0
        conn.execute("UPDATE entities SET title = ? WHERE id = ?", (new_title, task_row["id"]))
        logging.info(
            "Updated task '%s' to '%s' in list '%s' for user %s",
            old_title,
            new_title,
            list_name,
            user_id,
        )
        return 1
    except sqlite3.Error as exc:
        logging.error("SQLite error in update_task: %s", exc)
        return 0


def update_task_by_index(
    conn: sqlite3.Connection,
    user_id: int,
    list_name: str,
    index: int,
    new_title: str,
) -> tuple[int, str | None]:
    try:
        list_id = _get_list_id(conn, user_id, list_name)
        if list_id is None:
            return 0, None
        tasks = _list_active_tasks(conn, user_id, list_id)
        if not tasks or index < 1 or index > len(tasks):
            logging.info("Invalid index %s for list '%s' for user %s", index, list_name, user_id)
            return 0, None
        chosen = tasks[index - 1]
        task_id, old_title = chosen["id"], chosen["title"]
        if _get_task_row(conn, user_id, list_id, new_title):
            logging.info("Task '%s' already exists in list '%s' for user %s", new_title, list_name, user_id)
            return 0, None
        conn.execute("UPDATE entities SET title = ? WHERE id = ?", (new_title, task_id))
        logging.info(
            "Updated task '%s' to '%s' by index %s in list '%s' for user %s",
            old_title,
            new_title,
            index,
            list_name,
            user_id,
        )
        return 1, old_title
    except sqlite3.Error as exc:
        logging.error("SQLite error in update_task_by_index: %s", exc)
        return 0, None

def get_list_tasks(conn: sqlite3.Connection, user_id: int, list_name: str) -> list[tuple[int, str]]:
    try:
        list_id = _get_list_id(conn, user_id, list_name)
        if list_id is None:
            return []
        tasks = _list_active_tasks(conn, user_id, list_id)
        results = [(idx + 1, row["title"]) for idx, row in enumerate(tasks)]
        logging.info("Retrieved %s tasks for list '%s' for user %s", len(results), list_name, user_id)
        return results
    except sqlite3.Error as exc:
        logging.error("SQLite error in get_list_tasks: %s", exc)
        return []

def mark_task_done(conn: sqlite3.Connection, user_id: int, list_name: str, task_title: str) -> int:
    try:
        list_id = _get_list_id(conn, user_id, list_name)
        if list_id is None:
            return 0
        candidate_rows = list(_list_active_tasks(conn, user_id, list_id))
        candidates = [(row["id"], row["title"], row["meta"]) for row in candidate_rows]
        if not candidates:
            logging.info("No active tasks found in list '%s' for user %s", list_name, user_id)
            return 0
        chosen = _select_candidate(task_title, candidates)
        if not chosen:
            logging.info(
                "No task matching '%s' found in list '%s' for user %s",
                task_title,
                list_name,
                user_id,
            )
            return 0
        chosen_id, chosen_title, meta_text = chosen
        meta = _load_meta(meta_text)
        meta["status"] = "done"
        meta.pop("deleted", None)
        conn.execute(
            "UPDATE entities SET meta = ? WHERE id = ?",
            (_dump_meta(meta), chosen_id),
        )
        logging.info("Marked task '%s' as done in list '%s' for user %s", chosen_title, list_name, user_id)
        return 1
    except sqlite3.Error as exc:
        logging.error("SQLite error in mark_task_done: %s", exc)
        return 0


def mark_task_done_fuzzy(
    conn: sqlite3.Connection,
    user_id: int,
    list_name: str,
    pattern: str,
) -> tuple[int, str | None]:
    try:
        if not pattern:
            logging.info("No pattern provided for fuzzy mark done in list '%s' for user %s", list_name, user_id)
            return 0, None
        cleaned = re.sub(r"[^0-9a-zA-Zа-яА-ЯёЁ ]+", " ", pattern).strip()
        if not cleaned:
            logging.info("Invalid pattern after cleaning for fuzzy mark done in list '%s' for user %s", list_name, user_id)
            return 0, None
        list_id = _get_list_id(conn, user_id, list_name)
        if list_id is None:
            return 0, None
        candidates = [
            (row["id"], row["title"], row["meta"])
            for row in _list_active_tasks(conn, user_id, list_id)
        ]
        if not candidates:
            logging.info("No tasks found in list '%s' for user %s", list_name, user_id)
            return 0, None
        chosen = _score_candidates(_tokenize(pattern), cleaned, candidates)
        if not chosen:
            logging.info("No close match for pattern '%s' in list '%s' for user %s", cleaned, list_name, user_id)
            return 0, None
        chosen_id, chosen_title, meta_text = chosen
        meta = _load_meta(meta_text)
        meta["status"] = "done"
        meta.pop("deleted", None)
        conn.execute(
            "UPDATE entities SET meta = ? WHERE id = ?",
            (_dump_meta(meta), chosen_id),
        )
        logging.info("Fuzzy marked task '%s' as done in list '%s' for user %s", chosen_title, list_name, user_id)
        return 1, chosen_title
    except sqlite3.Error as exc:
        logging.error("SQLite error in mark_task_done_fuzzy: %s", exc)
        return 0, None

def delete_list(conn: sqlite3.Connection, user_id: int, list_name: str) -> int:
    try:
        cur = conn.execute(
            """
            SELECT id, meta FROM entities
            WHERE user_id = ? AND type = 'list' AND title = ?
            LIMIT 1
            """,
            (user_id, list_name),
        )
        row = cur.fetchone()
        if not row:
            logging.info("No list '%s' found for user %s", list_name, user_id)
            return 0
        list_id = row["id"]
        list_meta = _load_meta(row["meta"])
        list_meta["deleted"] = True
        conn.execute(
            "UPDATE entities SET meta = ? WHERE id = ?",
            (_dump_meta(list_meta), list_id),
        )
        task_rows = conn.execute(
            """
            SELECT id, meta
            FROM entities
            WHERE user_id = ? AND type = 'task' AND parent_id = ?
            """,
            (user_id, list_id),
        ).fetchall()
        for task_row in task_rows:
            task_meta = _load_meta(task_row["meta"])
            task_meta.pop("deleted", None)
            task_meta["archived"] = True
            if list_name:
                task_meta["archived_from"] = list_name
            conn.execute(
                "UPDATE entities SET meta = ? WHERE id = ?",
                (_dump_meta(task_meta), task_row["id"]),
            )
        logging.info("Deleted list '%s' for user %s", list_name, user_id)
        return 1
    except sqlite3.Error as exc:
        logging.error("SQLite error in delete_list: %s", exc)
        return 0

def delete_task(conn: sqlite3.Connection, user_id: int, list_name: str, task_title: str) -> int:
    try:
        list_id = _get_list_id(conn, user_id, list_name)
        if list_id is None:
            return 0
        task_row = _get_task_row(conn, user_id, list_id, task_title)
        if not task_row:
            logging.info("No task '%s' found in list '%s' for user %s", task_title, list_name, user_id)
            return 0
        meta = _load_meta(task_row["meta"])
        if meta.get("status") == "done":
            logging.info("Task '%s' is already done in list '%s' for user %s", task_title, list_name, user_id)
            return 0
        meta["deleted"] = True
        conn.execute(
            "UPDATE entities SET meta = ? WHERE id = ?",
            (_dump_meta(meta), task_row["id"]),
        )
        logging.info("Deleted task '%s' from list '%s' for user %s", task_title, list_name, user_id)
        return 1
    except sqlite3.Error as exc:
        logging.error("SQLite error in delete_task: %s", exc)
        return 0

def _suggest_new_list_for_restore(
    conn: sqlite3.Connection, user_id: int, list_name: str, task_title: str
) -> str | None:
    try:
        cur = conn.execute(
            """
            SELECT 1
            FROM entities
            WHERE user_id = ? AND type = 'task'
              AND LOWER(title) = LOWER(?)
              AND json_extract(meta, '$.archived') = true
              AND (
                    json_extract(meta, '$.archived_from') IS NULL
                 OR LOWER(json_extract(meta, '$.archived_from')) = LOWER(?)
              )
            LIMIT 1
            """,
            (user_id, task_title, list_name),
        )
        if cur.fetchone():
            return (
                f"Список «{list_name}» удалён. Создай новый список и скажи, куда вернуть задачу «{task_title}»."
            )
        return None
    except sqlite3.Error as exc:
        logging.error("SQLite error in _suggest_new_list_for_restore: %s", exc)
        return None


def restore_task(
    conn: sqlite3.Connection, user_id: int, list_name: str, task_title: str
) -> tuple[int, str | None, str | None]:
    try:
        list_id = _get_list_id(conn, user_id, list_name)
        if list_id is None:
            suggestion = _suggest_new_list_for_restore(conn, user_id, list_name, task_title)
            if suggestion:
                logging.info(
                    "List '%s' missing for restore_task; suggesting new list for user %s",
                    list_name,
                    user_id,
                )
            else:
                logging.info("No list '%s' found for user %s", list_name, user_id)
            return 0, None, suggestion
        candidate_rows = list(_list_restorable_tasks(conn, user_id, list_id))
        candidates = [(row["id"], row["title"], row["meta"]) for row in candidate_rows]
        if not candidates:
            logging.info("No restorable tasks in list '%s' for user %s", list_name, user_id)
            return 0, None, None
        chosen = _select_candidate(task_title, candidates)
        if not chosen:
            logging.info(
                "No restorable task matching '%s' in list '%s' for user %s",
                task_title,
                list_name,
                user_id,
            )
            return 0, None, None
        chosen_id, chosen_title, meta_text = chosen
        meta = _load_meta(meta_text)
        changed = False
        if meta.pop("deleted", None):
            changed = True
        if meta.pop("archived", None):
            changed = True
        if meta.pop("archived_from", None) is not None:
            changed = True
        if meta.get("status") == "done":
            meta.pop("status", None)
            changed = True
        if not changed:
            logging.info(
                "Task '%s' already active in list '%s' for user %s",
                chosen_title,
                list_name,
                user_id,
            )
            return 0, None, None
        conn.execute(
            "UPDATE entities SET meta = ? WHERE id = ?",
            (_dump_meta(meta), chosen_id),
        )
        logging.info("Restored task '%s' in list '%s' for user %s", chosen_title, list_name, user_id)
        return 1, chosen_title, None
    except sqlite3.Error as exc:
        logging.error("SQLite error in restore_task: %s", exc)
        return 0, None, None


def restore_task_fuzzy(
    conn: sqlite3.Connection, user_id: int, list_name: str, pattern: str
) -> tuple[int, str | None, str | None]:
    try:
        if not pattern:
            logging.info("No pattern provided for fuzzy restore in list '%s' for user %s", list_name, user_id)
            return 0, None, None
        cleaned = re.sub(r"[^0-9a-zA-Zа-яА-ЯёЁ ]+", " ", pattern).strip()
        if not cleaned:
            logging.info("Invalid pattern after cleaning for fuzzy restore in list '%s' for user %s", list_name, user_id)
            return 0, None, None
        list_id = _get_list_id(conn, user_id, list_name)
        if list_id is None:
            suggestion = _suggest_new_list_for_restore(conn, user_id, list_name, pattern)
            if suggestion:
                logging.info(
                    "List '%s' missing for fuzzy restore; suggesting new list for user %s",
                    list_name,
                    user_id,
                )
            else:
                logging.info("No list '%s' found for fuzzy restore for user %s", list_name, user_id)
            return 0, None, suggestion
        candidates = [
            (row["id"], row["title"], row["meta"])
            for row in _list_restorable_tasks(conn, user_id, list_id)
        ]
        if not candidates:
            logging.info("No deleted tasks found in list '%s' for user %s", list_name, user_id)
            return 0, None, None
        logging.info(
            "Restore search candidates: %s",
            [title for _, title, _ in candidates],
        )
        chosen = _score_candidates(_tokenize(pattern), cleaned, candidates)
        if not chosen:
            logging.info("No close match for pattern '%s' in list '%s' for user %s", cleaned, list_name, user_id)
            return 0, None, None
        chosen_id, chosen_title, meta_text = chosen
        meta = _load_meta(meta_text)
        changed = False
        if meta.pop("deleted", None):
            changed = True
        if meta.get("status") == "done":
            meta.pop("status", None)
            changed = True
        if meta.pop("archived", None):
            changed = True
        if meta.pop("archived_from", None) is not None:
            changed = True
        if not changed:
            logging.info(
                "Task '%s' already active in list '%s' for user %s",
                chosen_title,
                list_name,
                user_id,
            )
            return 0, None, None
        conn.execute(
            "UPDATE entities SET meta = ? WHERE id = ?",
            (_dump_meta(meta), chosen_id),
        )
        logging.info("Fuzzy restored task '%s' in list '%s' for user %s", chosen_title, list_name, user_id)
        return 1, chosen_title, None
    except sqlite3.Error as exc:
        logging.error("SQLite error in restore_task_fuzzy: %s", exc)
        return 0, None, None

def get_completed_tasks(conn: sqlite3.Connection, user_id: int, limit: int = 15) -> list[tuple[str, str]]:
    try:
        query = """
            SELECT
                e.title AS task_title,
                CASE
                    WHEN json_extract(e.meta, '$.archived') = true
                         OR l.id IS NULL
                         OR json_extract(l.meta, '$.deleted') = true
                    THEN 1
                    ELSE 0
                END AS archived_flag,
                CASE
                    WHEN json_extract(e.meta, '$.archived') = true
                         OR l.id IS NULL
                         OR json_extract(l.meta, '$.deleted') = true
                    THEN COALESCE(json_extract(e.meta, '$.archived_from'), l.title)
                    ELSE l.title
                END AS source_title
            FROM entities e
            LEFT JOIN entities l ON l.id = e.parent_id AND l.type = 'list'
            WHERE e.user_id = ?
              AND e.type = 'task'
              AND (
                    json_extract(e.meta, '$.status') = 'done'
                 OR json_extract(e.meta, '$.done') = true
                )
              AND (json_extract(e.meta, '$.deleted') IS NULL OR json_extract(e.meta, '$.deleted') IS NOT TRUE)
            ORDER BY COALESCE(json_extract(e.meta, '$.completed_at'), e.created_at) DESC
            LIMIT ?
        """
        _trace_sql(
            "get_completed_tasks query | params=%s | %s"
            % (str((user_id, limit)), " ".join(line.strip() for line in query.strip().splitlines()))
        )
        cur = conn.execute(query, (user_id, limit))
        tasks: list[tuple[str, str]] = []
        for row in cur.fetchall():
            archived_flag = row["archived_flag"] if isinstance(row, sqlite3.Row) else row[1]
            source_title = row["source_title"] if isinstance(row, sqlite3.Row) else row[2]
            task_title = row["task_title"] if isinstance(row, sqlite3.Row) else row[0]
            if archived_flag:
                display_title = "Архив"
                if source_title:
                    display_title = f"{display_title} • {source_title}"
            else:
                display_title = source_title or "Архив"
            tasks.append((display_title, task_title))
        logging.info("Retrieved %s completed tasks for user %s", len(tasks), user_id)
        return tasks
    except sqlite3.Error as exc:
        logging.error("SQLite error in get_completed_tasks: %s", exc)
        return []


def get_deleted_tasks(conn: sqlite3.Connection, user_id: int, limit: int = 15) -> list[tuple[str, str]]:
    try:
        cur = conn.execute(
            """
            SELECT l.title AS list_title, e.title AS task_title
            FROM entities e
            LEFT JOIN entities l ON l.id = e.parent_id
            WHERE e.user_id = ?
              AND e.type = 'task'
              AND json_extract(e.meta, '$.deleted') = true
            ORDER BY e.created_at DESC
            LIMIT ?
            """,
            (user_id, limit),
        )
        tasks = [(row["list_title"], row["task_title"]) for row in cur.fetchall()]
        logging.info("Retrieved %s deleted tasks for user %s", len(tasks), user_id)
        return tasks
    except sqlite3.Error as exc:
        logging.error("SQLite error in get_deleted_tasks: %s", exc)
        return []

def search_tasks(conn: sqlite3.Connection, user_id: int, pattern: str) -> list[tuple[str, str]]:
    try:
        cleaned = re.sub(r"[^0-9a-zA-Zа-яА-ЯёЁ ]+", " ", pattern).strip()
        if not cleaned:
            logging.info("Invalid pattern for search tasks for user %s", user_id)
            return []
        cur = conn.execute(
            """
            SELECT l.title AS list_title, e.title AS task_title
            FROM entities e
            JOIN entities l ON l.id = e.parent_id
            WHERE e.user_id = ? AND e.type = 'task'
              AND LOWER(e.title) LIKE LOWER(?)
              AND (e.meta IS NULL OR json_extract(e.meta, '$.deleted') IS NOT TRUE)
              AND json_extract(e.meta, '$.status') != 'done'
            ORDER BY e.created_at ASC
            """,
            (user_id, f"%{cleaned}%"),
        )
        tasks = [(row["list_title"], row["task_title"]) for row in cur.fetchall()]
        logging.info(
            "Found %s tasks matching '%s' for user %s: %s",
            len(tasks),
            cleaned,
            user_id,
            tasks,
        )
        return tasks
    except sqlite3.Error as exc:
        logging.error("SQLite error in search_tasks: %s", exc)
        return []

def fetch_task(conn: sqlite3.Connection, user_id: int, list_name: str, task_title: str):
    try:
        cur = conn.execute(
            """
            SELECT e.id, e.title, e.meta
            FROM entities e
            JOIN entities l ON l.id = e.parent_id
            WHERE e.user_id = ? AND e.type = 'task' AND l.type = 'list'
              AND LOWER(l.title) = LOWER(?)
              AND LOWER(e.title) = LOWER(?)
              AND (e.meta IS NULL OR json_extract(e.meta, '$.deleted') IS NOT TRUE)
            LIMIT 1
            """,
            (user_id, list_name, task_title),
        )
        task = cur.fetchone()
        logging.info(
            "Fetched task '%s' from list '%s' for user %s: %s",
            task_title,
            list_name,
            user_id,
            "Found" if task else "Not found",
        )
        return task
    except sqlite3.Error as exc:
        logging.error("SQLite error in fetch_task: %s", exc)
        return None


def fetch_list_by_task(conn: sqlite3.Connection, user_id: int, task_title: str):
    try:
        cur = conn.execute(
            """
            SELECT l.title AS list_title, e.title AS task_title
            FROM entities e
            JOIN entities l ON l.id = e.parent_id
            WHERE e.user_id = ? AND e.type = 'task' AND e.title = ?
            LIMIT 1
            """,
            (user_id, task_title),
        )
        result = cur.fetchone()
        logging.info(
            "Fetched list by task '%s' for user %s: %s",
            task_title,
            user_id,
            "Found" if result else "Not found",
        )
        return result
    except sqlite3.Error as exc:
        logging.error("SQLite error in fetch_list_by_task: %s", exc)
        return None


def move_entity(
    conn: sqlite3.Connection,
    user_id: int,
    entity_type: str,
    title: str,
    from_list: str,
    to_list: str,
) -> int:
    try:
        logging.info(
            "Moving %s '%s' from '%s' to '%s' for user %s",
            entity_type,
            title,
            from_list,
            to_list,
            user_id,
        )
        from_list_id = _get_list_id(conn, user_id, from_list)
        if from_list_id is None:
            logging.info("List '%s' not found for user %s", from_list, user_id)
            return 0
        to_list_id = _get_list_id(conn, user_id, to_list)
        if to_list_id is None:
            logging.info("Target list '%s' not found for user %s", to_list, user_id)
            return 0
        task_row = _get_task_row(conn, user_id, from_list_id, title)
        if not task_row:
            logging.info(
                "No %s '%s' found in list '%s' for user %s",
                entity_type,
                title,
                from_list,
                user_id,
            )
            return 0
        conn.execute(
            "UPDATE entities SET parent_id = ? WHERE id = ?",
            (to_list_id, task_row["id"]),
        )
        logging.info(
            "✅ Task '%s' moved from '%s' to '%s'",
            task_row["title"],
            from_list,
            to_list,
        )
        return 1
    except sqlite3.Error as exc:
        logging.error("SQLite error in move_entity: %s", exc)
        return 0

def get_all_tasks(conn: sqlite3.Connection, user_id: int) -> list[tuple[str, str]]:
    try:
        cur = conn.execute(
            """
            SELECT l.title AS list_title, e.title AS task_title
            FROM entities e
            JOIN entities l ON l.id = e.parent_id
            WHERE e.user_id = ?
              AND e.type = 'task'
              AND (e.meta IS NULL OR json_extract(e.meta, '$.deleted') IS NOT TRUE)
              AND json_extract(e.meta, '$.status') != 'done'
            ORDER BY l.title, e.created_at
            """,
            (user_id,),
        )
        tasks = [(row["list_title"], row["task_title"]) for row in cur.fetchall()]
        logging.info("Retrieved %s tasks for user %s", len(tasks), user_id)
        return tasks
    except sqlite3.Error as exc:
        logging.error("SQLite error in get_all_tasks: %s", exc)
        return []

def update_user_profile(
    conn: sqlite3.Connection,
    user_id: int,
    city: str | None = None,
    profession: str | None = None,
) -> int:
    try:
        meta = {"city": city, "profession": profession}
        conn.execute(
            "INSERT OR REPLACE INTO entities (user_id, type, title, meta) VALUES (?, 'user_profile', ?, ?)",
            (user_id, f"user_{user_id}", json.dumps(meta, ensure_ascii=False)),
        )
        logging.info("Updated user profile for user %s: %s", user_id, meta)
        return 1
    except sqlite3.Error as exc:
        logging.error("SQLite error in update_user_profile: %s", exc)
        return 0


def get_user_profile(conn: sqlite3.Connection, user_id: int) -> dict[str, Any]:
    try:
        cur = conn.execute(
            "SELECT meta FROM entities WHERE user_id = ? AND type = 'user_profile' AND title = ? LIMIT 1",
            (user_id, f"user_{user_id}"),
        )
        row = cur.fetchone()
        if row and row["meta"]:
            return json.loads(row["meta"])
        return {}
    except sqlite3.Error as exc:
        logging.error("SQLite error in get_user_profile: %s", exc)
        return {}

def delete_task_fuzzy(conn, user_id, list_name, pattern: str):
    try:
        if not pattern:
            logging.info("No pattern provided for fuzzy delete in list '%s' for user %s", list_name, user_id)
            return 0, None
        cleaned = re.sub(r"[^0-9a-zA-Zа-яА-ЯёЁ ]+", " ", pattern).strip()
        if not cleaned:
            logging.info("Invalid pattern after cleaning for fuzzy delete in list '%s' for user %s", list_name, user_id)
            return 0, None
        list_id = _get_list_id(conn, user_id, list_name)
        if list_id is None:
            return 0, None
        candidates = [
            (row["id"], row["title"], row["meta"])
            for row in _list_active_tasks(conn, user_id, list_id)
        ]
        if not candidates:
            logging.info("No tasks found in list '%s' for user %s", list_name, user_id)
            return 0, None
        target = min(
            candidates,
            key=lambda item: distance(item[1].lower(), cleaned.lower()),
        )
        if distance(target[1].lower(), cleaned.lower()) > len(cleaned) // 2:
            logging.info("No close match for pattern '%s' in list '%s' for user %s", cleaned, list_name, user_id)
            return 0, None
        chosen_id, chosen_title, meta_text = target
        meta = _load_meta(meta_text)
        meta["deleted"] = True
        conn.execute(
            "UPDATE entities SET meta = ? WHERE id = ?",
            (_dump_meta(meta), chosen_id),
        )
        logging.info("Fuzzy deleted task '%s' from list '%s' for user %s", chosen_title, list_name, user_id)
        return 1, chosen_title
    except sqlite3.Error as exc:
        logging.error("SQLite error in delete_task_fuzzy: %s", exc)
        return 0, None


def delete_task_by_index(conn, user_id, list_name: str, index: int):
    try:
        list_id = _get_list_id(conn, user_id, list_name)
        if list_id is None:
            return 0, None
        tasks = _list_active_tasks(conn, user_id, list_id)
        if not tasks or index < 1 or index > len(tasks):
            logging.info("Invalid index %s for list '%s' for user %s", index, list_name, user_id)
            return 0, None
        chosen = tasks[index - 1]
        task_id, task_title = chosen["id"], chosen["title"]
        meta = _load_meta(chosen["meta"])
        meta["deleted"] = True
        conn.execute(
            "UPDATE entities SET meta = ? WHERE id = ?",
            (_dump_meta(meta), task_id),
        )
        logging.info("Deleted task '%s' by index %s from list '%s' for user %s", task_title, index, list_name, user_id)
        return 1, task_title
    except sqlite3.Error as exc:
        logging.error("SQLite error in delete_task_by_index: %s", exc)
        return 0, None

def normalize_text(value: str) -> str:
    if not value:
        return value
    value = value.strip()
    value = re.sub(r'\s+', ' ', value)
    value = value[:1].upper() + value[1:]
    value = re.sub(r'\bsp[oO]2\b', 'SPO2', value, flags=re.IGNORECASE)
    return value

