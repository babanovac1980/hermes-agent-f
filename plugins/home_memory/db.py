import os
import random
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, TypeVar

T = TypeVar("T")

_DEFAULT_DB_PATH = "/opt/data/home-memory.db"

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);

CREATE TABLE IF NOT EXISTS category (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    name             TEXT    NOT NULL,
    short_name       TEXT,
    is_area_category INTEGER NOT NULL DEFAULT 0,
    parent_id        INTEGER REFERENCES category(id),
    description      TEXT,
    created_at       TEXT    NOT NULL,
    updated_at       TEXT,
    created_by       TEXT    NOT NULL DEFAULT 'HomeMemory',
    updated_by       TEXT,
    lock_field       INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_category_parent ON category(parent_id);
CREATE UNIQUE INDEX IF NOT EXISTS ux_category_name_parent
    ON category(lower(name), ifnull(parent_id, -1));
CREATE UNIQUE INDEX IF NOT EXISTS ux_category_shortname_parent
    ON category(lower(short_name), ifnull(parent_id, -1)) WHERE short_name IS NOT NULL;

CREATE TABLE IF NOT EXISTS status (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL,
    status_type INTEGER NOT NULL,
    note        TEXT,
    lock_field  INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_status_name ON status(lower(name));

CREATE TABLE IF NOT EXISTS element (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT    NOT NULL,
    short_name   TEXT,
    parent_id    INTEGER REFERENCES element(id),
    position     TEXT,
    sort_index   INTEGER NOT NULL DEFAULT 0,
    category_id  INTEGER NOT NULL REFERENCES category(id),
    status_id    INTEGER REFERENCES status(id),
    purpose      TEXT,
    note         TEXT,
    description  TEXT,
    user_manual  TEXT,
    created_at   TEXT    NOT NULL,
    updated_at   TEXT,
    created_by   TEXT    NOT NULL DEFAULT 'HomeMemory',
    updated_by   TEXT,
    lock_field   INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_element_parent   ON element(parent_id);
CREATE INDEX IF NOT EXISTS idx_element_category ON element(category_id);
CREATE INDEX IF NOT EXISTS idx_element_status   ON element(status_id);

CREATE TABLE IF NOT EXISTS connection (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT    NOT NULL,
    source_id       INTEGER NOT NULL REFERENCES element(id),
    destination_id  INTEGER NOT NULL REFERENCES element(id),
    category_id     INTEGER NOT NULL REFERENCES category(id),
    route           TEXT,
    length          REAL,
    purpose         TEXT,
    note            TEXT,
    description     TEXT,
    created_at      TEXT    NOT NULL,
    updated_at      TEXT,
    created_by      TEXT    NOT NULL DEFAULT 'HomeMemory',
    updated_by      TEXT,
    lock_field      INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_connection_source ON connection(source_id);
CREATE INDEX IF NOT EXISTS idx_connection_dest   ON connection(destination_id);
CREATE INDEX IF NOT EXISTS idx_connection_cat    ON connection(category_id);
CREATE UNIQUE INDEX IF NOT EXISTS ux_connection_combo
    ON connection(lower(name), category_id, source_id, destination_id);
"""

_WRITE_MAX_RETRIES = 15
_WRITE_RETRY_MIN_S = 0.020
_WRITE_RETRY_MAX_S = 0.150

_conn: sqlite3.Connection | None = None
_lock = threading.Lock()
_initialized = False


def _get_db_path() -> Path:
    return Path(os.environ.get("HOME_MEMORY_DB_PATH", _DEFAULT_DB_PATH))


def _init():
    global _conn, _initialized
    if _initialized:
        return
    db_path = _get_db_path()
    if str(db_path) != ":memory:":
        db_path.parent.mkdir(parents=True, exist_ok=True)
    _conn = sqlite3.connect(
        str(db_path),
        check_same_thread=False,
        timeout=1.0,
        isolation_level=None,
    )
    _conn.row_factory = sqlite3.Row
    _conn.execute("PRAGMA journal_mode=WAL")
    _conn.execute("PRAGMA foreign_keys=ON")
    _conn.execute("PRAGMA busy_timeout=5000")
    _init_schema()
    _seed_if_empty()
    _initialized = True


def get_connection() -> sqlite3.Connection:
    global _initialized
    if not _initialized:
        with _lock:
            if not _initialized:
                _init()
    return _conn


def _init_schema():
    _conn.executescript(SCHEMA_SQL)
    row = _conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()
    if row[0] == 0:
        _conn.execute("INSERT INTO schema_version (version) VALUES (1)")
        _conn.commit()


def _seed_if_empty():
    row = _conn.execute("SELECT COUNT(*) FROM category").fetchone()
    if row[0] > 0:
        return
    from .seed.statuses   import STATUSES
    from .seed.categories import CATEGORIES
    from .seed.elements   import ELEMENTS

    now = _utcnow()

    # Insert statuses
    for name, stype in STATUSES:
        _conn.execute(
            "INSERT INTO status (name, status_type) VALUES (?, ?)",
            (name, stype),
        )

    # Insert categories recursively
    def insert_category(cat: dict, parent_id: int | None):
        cur = _conn.execute(
            """INSERT INTO category (name, short_name, is_area_category, parent_id, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (cat["name"], cat.get("short_name"), 1 if cat["is_area_category"] else 0, parent_id, now),
        )
        new_id = cur.lastrowid
        for child in cat.get("children", []):
            insert_category(child, new_id)

    for cat in CATEGORIES:
        insert_category(cat, None)

    # Build fullname → category_id map for element seed
    cat_by_name: dict[str, int] = {}
    for row in _conn.execute("SELECT id, name FROM category"):
        cat_by_name[row["name"].lower()] = row["id"]

    # Build a fullname→id map as we insert elements (elements.json uses long-name paths)
    fullname_to_id: dict[str, int] = {}

    for elem in ELEMENTS:
        parent_path = elem["parent"]
        parent_id: int | None = None
        if parent_path:
            parent_id = fullname_to_id.get(parent_path)
            if parent_id is None:
                raise ValueError(f"Seed: parent '{parent_path}' not found for element '{elem['name']}'")

        cat_name = elem["category"].lower()
        cat_id = cat_by_name.get(cat_name)
        if cat_id is None:
            raise ValueError(f"Seed: category '{elem['category']}' not found")

        cur = _conn.execute(
            """INSERT INTO element (name, short_name, parent_id, sort_index, category_id, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (elem["name"], elem.get("short_name"), parent_id, elem.get("sort_index", 0), cat_id, now),
        )
        new_id = cur.lastrowid

        # Record under both long-name path and short-name path
        long_path = (parent_path + "/" + elem["name"]) if parent_path else elem["name"]
        sn = elem.get("short_name")
        short_seg = sn if sn else elem["name"]
        short_path = (fullname_to_id.get("__shortpath__" + str(parent_id), "") + "/" + short_seg).lstrip("/") \
            if parent_id else short_seg
        # Simpler approach: just use long name for lookup during seed
        fullname_to_id[long_path] = new_id
        # Also register short-name path for child lookups
        if sn:
            short_path_key = (parent_path + "/" + sn) if parent_path else sn
            fullname_to_id[short_path_key] = new_id

    _conn.commit()


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def execute_write(fn: Callable[[sqlite3.Connection], T]) -> T:
    """Execute fn inside a BEGIN IMMEDIATE transaction with jitter retry on lock."""
    conn = get_connection()
    last_err: Exception | None = None
    for attempt in range(_WRITE_MAX_RETRIES):
        try:
            with _lock:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    result = fn(conn)
                    conn.execute("COMMIT")
                except BaseException:
                    try:
                        conn.execute("ROLLBACK")
                    except Exception:
                        pass
                    raise
            return result
        except sqlite3.OperationalError as exc:
            err_msg = str(exc).lower()
            if "locked" in err_msg or "busy" in err_msg:
                last_err = exc
                if attempt < _WRITE_MAX_RETRIES - 1:
                    time.sleep(random.uniform(_WRITE_RETRY_MIN_S, _WRITE_RETRY_MAX_S))
                    continue
            raise
    raise last_err or sqlite3.OperationalError("database is locked after max retries")


def reset_for_testing():
    """Reset module state so tests can inject a fresh :memory: connection."""
    global _conn, _initialized
    if _conn is not None:
        try:
            _conn.close()
        except Exception:
            pass
    _conn = None
    _initialized = False
