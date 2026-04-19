"""
Port of HomeMemoryMCP/Tools/QueryHelpers.cs.
All functions receive a sqlite3.Connection.
"""
from __future__ import annotations

import sqlite3
from typing import Optional

ETREE_CTE = """
WITH RECURSIVE etree AS (
    SELECT id, name, short_name, parent_id, position, sort_index, category_id, status_id,
           CAST(coalesce(nullif(trim(short_name),''), name) AS TEXT) AS fullname,
           CAST(name AS TEXT) AS longname,
           CAST(printf('%010d', coalesce(sort_index, 2147483647))
                || coalesce(nullif(trim(short_name),''), name) AS TEXT) AS sortpath,
           0 AS depth
    FROM element WHERE parent_id IS NULL
  UNION ALL
    SELECT e.id, e.name, e.short_name, e.parent_id, e.position, e.sort_index, e.category_id, e.status_id,
           p.fullname || '/' || coalesce(nullif(trim(e.short_name),''), e.name),
           p.longname || '/' || e.name,
           p.sortpath || '/' || printf('%010d', coalesce(e.sort_index, 2147483647))
                             || coalesce(nullif(trim(e.short_name),''), e.name),
           p.depth + 1
    FROM element e JOIN etree p ON e.parent_id = p.id
)
"""

CAT_TREE_CTE = """
WITH RECURSIVE cat_tree AS (
    SELECT id, name, short_name, is_area_category, parent_id,
           CAST(coalesce(nullif(trim(short_name),''), name) AS TEXT) AS cat_fullname,
           0 AS cat_depth
    FROM category WHERE parent_id IS NULL
  UNION ALL
    SELECT c.id, c.name, c.short_name, c.is_area_category, c.parent_id,
           p.cat_fullname || '/' || coalesce(nullif(trim(c.short_name),''), c.name),
           p.cat_depth + 1
    FROM category c JOIN cat_tree p ON c.parent_id = p.id
)
"""


# ── Element helpers ──────────────────────────────────────────────────────────

def load_etree(conn: sqlite3.Connection) -> tuple[list[sqlite3.Row], dict[str, sqlite3.Row]]:
    """
    Returns (all_rows, by_fullname).
    by_fullname is keyed by short-name fullname; long-name fallbacks added
    only where the key is not yet present (short-name wins on collision).
    """
    rows = conn.execute(
        f"{ETREE_CTE} SELECT id, name, short_name, fullname, longname, depth FROM etree"
    ).fetchall()
    by_fullname: dict[str, sqlite3.Row] = {}
    for r in rows:
        key = r["fullname"].lower()
        if key not in by_fullname:
            by_fullname[key] = r
    for r in rows:
        key = r["longname"].lower()
        if key not in by_fullname:
            by_fullname[key] = r
    return rows, by_fullname


def resolve_element_fullname(conn: sqlite3.Connection, path: str) -> str | None:
    """Returns canonical short-name fullname, or None if not found."""
    path = path.strip().rstrip("/")
    if not path:
        return None
    row = conn.execute(
        f"{ETREE_CTE} SELECT fullname FROM etree "
        "WHERE upper(fullname) = upper(?) OR upper(longname) = upper(?)",
        (path, path),
    ).fetchone()
    return row["fullname"] if row else None


def split_parent_and_name(fullname: str) -> tuple[str, str]:
    """Returns (parent_path, name). Top-level → ('', name)."""
    i = fullname.rfind("/")
    if i >= 0:
        return fullname[:i], fullname[i + 1 :]
    return "", fullname


# ── Category helpers ─────────────────────────────────────────────────────────

def load_cat_tree(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        f"{CAT_TREE_CTE} SELECT id, name, short_name, is_area_category, parent_id, cat_fullname, cat_depth "
        "FROM cat_tree ORDER BY cat_fullname"
    ).fetchall()


def resolve_category(conn: sqlite3.Connection, category: str | None) -> tuple[int | None, str | None]:
    """
    Returns (id, error). error is non-None when ambiguous.
    (None, None) when not found.
    """
    if not category or not category.strip():
        return None, None
    cat = category.strip().rstrip("/")
    upper = cat.upper()

    if "/" in cat:
        row = conn.execute(
            f"{CAT_TREE_CTE} SELECT id FROM cat_tree WHERE upper(cat_fullname) = ?",
            (upper,),
        ).fetchone()
        return (row["id"] if row else None), None

    rows = conn.execute(
        f"{CAT_TREE_CTE} SELECT id, name, cat_fullname FROM cat_tree "
        "WHERE upper(name) = ? OR upper(short_name) = ?",
        (upper, upper),
    ).fetchall()
    if not rows:
        return None, None
    if len(rows) == 1:
        return rows[0]["id"], None
    paths = ", ".join(f"'{r['cat_fullname']}'" for r in rows)
    return None, f"Error: category '{category.strip()}' is ambiguous ({len(rows)} matches). Use the full path instead: {paths}."


def resolve_category_with_descendants(conn: sqlite3.Connection, category: str) -> tuple[set[int] | None, str | None]:
    """
    Returns (set_of_ids, error).
    Exact Name/ShortName match first; partial text fallback.
    Path notation → exact path.
    """
    cat = category.strip().rstrip("/")
    all_cats = conn.execute(
        f"{CAT_TREE_CTE} SELECT id, name, short_name, cat_fullname FROM cat_tree ORDER BY cat_fullname"
    ).fetchall()

    if "/" in cat:
        matched = [r for r in all_cats if r["cat_fullname"].lower() == cat.lower()]
    else:
        upper = cat.upper()
        matched = [r for r in all_cats
                   if (r["name"] or "").upper() == upper or (r["short_name"] or "").upper() == upper]
        if not matched:
            matched = [r for r in all_cats
                       if upper in (r["name"] or "").upper() or upper in (r["short_name"] or "").upper()]

    if not matched:
        return None, None
    if len(matched) > 1:
        paths = ", ".join(f"'{r['cat_fullname']}'" for r in matched)
        return None, f"Error: category '{category.strip()}' is ambiguous ({len(matched)} matches). Use the full path instead: {paths}."

    mfn = matched[0]["cat_fullname"]
    prefix = mfn + "/"
    ids = {r["id"] for r in all_cats
           if r["cat_fullname"].lower() == mfn.lower()
           or r["cat_fullname"].lower().startswith(prefix.lower())}
    return ids, None


def resolve_status_id(conn: sqlite3.Connection, status: str | None) -> int | None:
    """Exact name match (case-insensitive). Returns id or None."""
    if not status or not status.strip():
        return None
    row = conn.execute(
        "SELECT id FROM status WHERE upper(name) = upper(?)", (status.strip(),)
    ).fetchone()
    return row["id"] if row else None


def next_sort_index(conn: sqlite3.Connection, parent_id: int | None) -> int:
    if parent_id is not None:
        row = conn.execute(
            "SELECT coalesce(MAX(sort_index), 0) AS maxsi FROM element WHERE parent_id = ?",
            (parent_id,),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT coalesce(MAX(sort_index), 0) AS maxsi FROM element WHERE parent_id IS NULL"
        ).fetchone()
    return (row["maxsi"] if row else 0) + 1


def check_sibling_uniqueness(
    conn: sqlite3.Connection,
    name: str,
    short_name: str | None,
    parent_id: int | None,
    exclude_id: int | None = None,
) -> str | None:
    """Returns error string or None."""
    exc_clause = " AND id != ?" if exclude_id is not None else ""
    if parent_id is not None:
        args_n: list = [name, parent_id]
        if exclude_id is not None:
            args_n.append(exclude_id)
        cnt = conn.execute(
            f"SELECT COUNT(*) FROM element WHERE upper(name) = upper(?) AND parent_id = ?{exc_clause}",
            args_n,
        ).fetchone()[0]
    else:
        args_n = [name]
        if exclude_id is not None:
            args_n.append(exclude_id)
        cnt = conn.execute(
            f"SELECT COUNT(*) FROM element WHERE upper(name) = upper(?) AND parent_id IS NULL{exc_clause}",
            args_n,
        ).fetchone()[0]
    if cnt > 0:
        return f"Error: a sibling element with name '{name}' already exists under the same parent."

    if short_name:
        if parent_id is not None:
            args_s: list = [short_name, parent_id]
            if exclude_id is not None:
                args_s.append(exclude_id)
            cnt = conn.execute(
                f"SELECT COUNT(*) FROM element WHERE upper(short_name) = upper(?) AND parent_id = ?{exc_clause}",
                args_s,
            ).fetchone()[0]
        else:
            args_s = [short_name]
            if exclude_id is not None:
                args_s.append(exclude_id)
            cnt = conn.execute(
                f"SELECT COUNT(*) FROM element WHERE upper(short_name) = upper(?) AND parent_id IS NULL{exc_clause}",
                args_s,
            ).fetchone()[0]
        if cnt > 0:
            return f"Error: a sibling element with short name '{short_name}' already exists under the same parent."

    return None


def collect_overwrite_advisories(
    conn: sqlite3.Connection,
    element_id: int,
    description: str | None = None,
    note: str | None = None,
    purpose: str | None = None,
    user_manual: str | None = None,
) -> list[str]:
    def is_text_update(v: str | None) -> bool:
        return v is not None and v != "CLEAR"

    if not any(is_text_update(x) for x in (description, note, purpose, user_manual)):
        return []

    row = conn.execute(
        "SELECT description, note, purpose, user_manual FROM element WHERE id = ?",
        (element_id,),
    ).fetchone()
    if not row:
        return []

    advisories: list[str] = []

    def check(field: str, old: str | None, new: str | None):
        if not is_text_update(new):
            return
        if not old:
            return
        if old == (new or "").strip():
            return
        preview = (old or "").replace("\r", "").replace("\n", " \\n ")
        if len(preview) > 80:
            preview = preview[:80] + "..."
        advisories.append(
            f"'{field}' was overwritten (previous: {len(old)} chars, started with: \"{preview}\")"
        )

    check("description",  row["description"],  description)
    check("note",         row["note"],          note)
    check("purpose",      row["purpose"],       purpose)
    check("user_manual",  row["user_manual"],   user_manual)
    return advisories


def collect_connection_overwrite_advisories(
    conn: sqlite3.Connection,
    connection_id: int,
    description: str | None = None,
    note: str | None = None,
    purpose: str | None = None,
    route: str | None = None,
) -> list[str]:
    def is_text_update(v: str | None) -> bool:
        return v is not None and v != "CLEAR"

    if not any(is_text_update(x) for x in (description, note, purpose, route)):
        return []

    row = conn.execute(
        "SELECT description, note, purpose, route FROM connection WHERE id = ?",
        (connection_id,),
    ).fetchone()
    if not row:
        return []

    advisories: list[str] = []

    def check(field: str, old: str | None, new: str | None):
        if not is_text_update(new):
            return
        if not old:
            return
        if old == (new or "").strip():
            return
        preview = (old or "").replace("\r", "").replace("\n", " \\n ")
        if len(preview) > 80:
            preview = preview[:80] + "..."
        advisories.append(
            f"'{field}' was overwritten (previous: {len(old)} chars, started with: \"{preview}\")"
        )

    check("description",  row["description"],  description)
    check("note",         row["note"],          note)
    check("purpose",      row["purpose"],       purpose)
    check("route",        row["route"],         route)
    return advisories


def check_connection_combo_uniqueness(
    conn: sqlite3.Connection,
    name: str,
    category_id: int,
    source_id: int,
    destination_id: int,
    exclude_id: int | None = None,
) -> str | None:
    exc_clause = " AND id != ?" if exclude_id is not None else ""
    args: list = [name, category_id, source_id, destination_id]
    if exclude_id is not None:
        args.append(exclude_id)
    cnt = conn.execute(
        f"SELECT COUNT(*) FROM connection "
        f"WHERE lower(name) = lower(?) AND category_id = ? AND source_id = ? AND destination_id = ?{exc_clause}",
        args,
    ).fetchone()[0]
    if cnt > 0:
        return "Error: a connection with the same name, category, source, and destination already exists."
    return None


def connection_same_src_dst_hint(
    conn: sqlite3.Connection,
    source_id: int,
    destination_id: int,
    category_id: int,
    exclude_id: int | None = None,
) -> str:
    exc_clause = " AND id != ?" if exclude_id is not None else ""
    args: list = [source_id, destination_id, category_id]
    if exclude_id is not None:
        args.append(exclude_id)
    try:
        cnt = conn.execute(
            f"SELECT COUNT(*) FROM connection WHERE source_id = ? AND destination_id = ? AND category_id = ?{exc_clause}",
            args,
        ).fetchone()[0]
        return (
            f"\n  Note: {cnt} other connection(s) with the same source, destination, and category already exist."
            if cnt > 0
            else ""
        )
    except Exception:
        return ""
