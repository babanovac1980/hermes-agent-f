"""
23 tool handler functions — port of HomeMemoryMCP C# tools.
All handlers return a string (success or "Error: ...").
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from pydantic import ValidationError

from .db import get_connection, execute_write, _utcnow
from .models import (
    GetStructureOverviewParams, FindElementParams, ListElementsParams,
    GetElementDetailsParams, GetRecentChangesParams,
    CreateElementParams, UpdateElementParams, DeleteElementParams, MoveElementParams,
    GetConnectionsParams, GetConnectionDetailsParams, CreateConnectionParams,
    UpdateConnectionParams, DeleteConnectionParams,
    ListCategoriesParams, GetByCategoryParams, CreateCategoryParams,
    UpdateCategoryParams, DeleteCategoryParams,
    ListStatusesParams, CreateStatusParams, UpdateStatusParams, DeleteStatusParams,
)
from .query_helpers import (
    ETREE_CTE, CAT_TREE_CTE,
    load_etree, load_cat_tree,
    resolve_element_fullname, split_parent_and_name,
    resolve_category, resolve_category_with_descendants, resolve_status_id,
    next_sort_index, check_sibling_uniqueness,
    collect_overwrite_advisories, collect_connection_overwrite_advisories,
    check_connection_combo_uniqueness, connection_same_src_dst_hint,
)
from .validate import (
    INVALID_CHARS, INVALID_CHARS_CONNECTION,
    MAX_NAME, MAX_NAME_CONNECTION, MAX_SHORT_NAME,
    MAX_PURPOSE, MAX_NOTE, MAX_POSITION, MAX_DESCRIPTION, MAX_USER_MANUAL, MAX_ROUTE,
    check_length, normalize_clear, normalize_singleline, normalize_multiline,
)


def _status_type_name(st: int) -> str:
    return {0: "Existing", 1: "Planned", 2: "Removed"}.get(st, "Unknown")


def _parse_status_type(value: str) -> tuple[int, str | None]:
    """Returns (int, error). -1 on bad input."""
    v = (value or "").strip().lower()
    m = {"existing": 0, "installed": 0, "0": 0,
         "planned": 1, "1": 1,
         "removed": 2, "decommissioned": 2, "2": 2}
    n = m.get(v, -1)
    if n < 0:
        return -1, "Error: status_type must be 'existing', 'planned', or 'removed'."
    return n, None


# ── Explore ───────────────────────────────────────────────────────────────────

def handle_get_structure_overview(args: dict, **_) -> str:
    try:
        p = GetStructureOverviewParams(**args)
    except ValidationError as e:
        return f"Error: {e.errors()[0]['msg']}"

    under = p.under.strip().rstrip("/")
    try:
        conn = get_connection()
        if under:
            resolved = resolve_element_fullname(conn, under)
            if resolved is None:
                return f"Error: element '{under}' not found. Call get_structure_overview or find_element to find the correct path."
            under = resolved
        depth_offset = under.count("/") if under else 0

        params: list = []
        if p.structural_areas_only:
            sql = (
                f"{ETREE_CTE} "
                "SELECT et.id, et.fullname, et.name, et.short_name, et.depth, c.name AS catname, "
                "s.name AS statusname, s.status_type "
                "FROM etree et "
                "JOIN element e ON e.id = et.id "
                "JOIN category c ON c.id = e.category_id "
                "LEFT JOIN status s ON s.id = e.status_id "
                "WHERE c.is_area_category = 1"
            )
            if under:
                sql += " AND (upper(et.fullname) LIKE upper(?) OR upper(et.fullname) = upper(?))"
                params += [under + "/%", under]
            sql += " ORDER BY et.sortpath"
            under_part = f" under '{under}'" if under else ""
            title = f"Building structure (areas{under_part}):"
        else:
            effective_max = p.max_depth if p.max_depth > 0 else (99 if under else 3)
            abs_max = depth_offset + effective_max
            sql = (
                f"{ETREE_CTE} "
                "SELECT et.id, et.fullname, et.name, et.short_name, et.depth, "
                "s.name AS statusname, s.status_type "
                "FROM etree et "
                "LEFT JOIN element e ON e.id = et.id "
                "LEFT JOIN status s ON s.id = e.status_id "
                f"WHERE et.depth <= {abs_max}"
            )
            if under:
                sql += " AND (upper(et.fullname) LIKE upper(?) OR upper(et.fullname) = upper(?))"
                params += [under + "/%", under]
            sql += " ORDER BY et.sortpath"
            depth_label = "full tree" if p.max_depth <= 0 and under else f"depth {effective_max}"
            under_part2 = f" under '{under}'" if under else ""
            title = f"Building structure ({depth_label}{under_part2}):"

        rows = conn.execute(sql, params).fetchall()
        if not rows:
            return "No elements found."

        lines = [f"{title}\n"]
        for row in rows:
            rel = row["depth"] - depth_offset
            indent = "  " * rel
            icon = "[]" if rel == 0 else ("+-" if rel == 1 else " -")
            label = row["name"]
            sn = row["short_name"]
            if sn and sn != label:
                label += f" ({sn})"
            st = row["status_type"] if "status_type" in row.keys() else None
            if st in (1, 2):
                label += f"  {{{row['statusname']}}}"
            lines.append(f"{indent}{icon} {label}")

        item_type = "areas" if p.structural_areas_only else "elements"
        lines += ["", f"Total: {len(rows)} {item_type}"]
        return "\n".join(lines)
    except sqlite3.Error as ex:
        return f"Error: {ex}"


def handle_find_element(args: dict, **_) -> str:
    try:
        p = FindElementParams(**args)
    except ValidationError as e:
        return f"Error: {e.errors()[0]['msg']}"

    search_term = p.search_term.strip()
    under = p.under.strip().rstrip("/")
    status = p.status.strip()
    category = p.category.strip()

    if not search_term and not under and not status and not category:
        return "Error: provide at least one of search_term, under, status, or category."

    try:
        conn = get_connection()
        if under:
            resolved = resolve_element_fullname(conn, under)
            if resolved is None:
                return f"Error: element '{under}' not found. Call get_structure_overview or find_element to find the correct path."
            under = resolved

        cat_ids: set[int] | None = None
        if category:
            ids, err = resolve_category_with_descendants(conn, category)
            if err:
                return err
            if ids is None:
                desc = f"'{search_term}'" if search_term else "all elements"
                scope = f" under '{under}'" if under else ""
                st_f = f" with status '{status}'" if status else ""
                return f"No elements found for {desc}{scope}{st_f} in category '{category}'."
            cat_ids = ids

        conditions: list[str] = []
        params: list = []

        if search_term:
            term = f"%{search_term.upper()}%"
            if p.search_all_fields:
                conditions.append(
                    "(upper(et.name) LIKE ? OR upper(et.fullname) LIKE ?"
                    " OR upper(e.purpose) LIKE ? OR upper(e.note) LIKE ?"
                    " OR upper(e.description) LIKE ? OR upper(e.user_manual) LIKE ?"
                    " OR upper(e.position) LIKE ?)"
                )
                params += [term] * 7
            else:
                conditions.append("(upper(et.name) LIKE ? OR upper(et.fullname) LIKE ?)")
                params += [term, term]

        if under:
            conditions.append("upper(et.fullname) LIKE upper(?)")
            params.append(under + "/%")

        if status:
            st_lower = status.lower()
            st_type_map = {"existing": 0, "planned": 1, "removed": 2, "decommissioned": 2}
            if st_lower in st_type_map:
                conditions.append("s.status_type = ?")
                params.append(st_type_map[st_lower])
            else:
                exact = conn.execute(
                    "SELECT COUNT(*) FROM status WHERE upper(name) = upper(?)", (status,)
                ).fetchone()[0]
                if exact > 0:
                    conditions.append("upper(s.name) = upper(?)")
                else:
                    conditions.append("upper(s.name) LIKE upper(?)")
                    status = f"%{status.upper()}%"
                params.append(status)

        where = " AND ".join(conditions) if conditions else "1=1"
        extra_sel = ", e.purpose, e.note, e.description, e.user_manual" if p.search_all_fields else ""
        cat_id_filter = ""
        if cat_ids is not None:
            placeholders = ",".join("?" * len(cat_ids))
            cat_id_filter = f" AND e.category_id IN ({placeholders})"

        sql = (
            f"{ETREE_CTE} "
            f"SELECT et.fullname, et.name, et.short_name, e.position, s.name AS statusname{extra_sel} "
            "FROM etree et "
            "JOIN element e ON e.id = et.id "
            "LEFT JOIN status s ON s.id = e.status_id "
            f"WHERE {where}{cat_id_filter} "
            "ORDER BY et.fullname LIMIT 101"
        )
        if cat_ids is not None:
            params += list(cat_ids)

        rows = conn.execute(sql, params).fetchall()
        truncated = len(rows) > 100
        if truncated:
            rows = rows[:100]

        desc = f"'{search_term}'" if search_term else "all elements"
        scope = f" under '{under}'" if under else ""
        st_filt = f" with status '{p.status.strip()}'" if p.status.strip() else ""
        cat_filt = f" in category '{category}'" if category else ""

        if not rows:
            return f"No elements found for {desc}{scope}{st_filt}{cat_filt}."

        count_label = f"{len(rows)}+" if truncated else str(len(rows))
        lines = [f"{count_label} result(s) for {desc}{scope}{st_filt}{cat_filt}:\n"]
        current_parent: str | None = None

        for row in rows:
            fullname = row["fullname"]
            parent, name = split_parent_and_name(fullname)
            if parent != current_parent:
                lines.append(f"  {parent}" if parent else "")
                current_parent = parent
            indent = "    " if parent else "  "
            pos = row["position"] or ""
            sn_str = row["statusname"] or ""
            suffix = ""
            if pos:
                suffix += f"  [{pos}]"
            if sn_str:
                suffix += f"  {{{sn_str}}}"
            lines.append(f"{indent}{name}{suffix}")

            if p.search_all_fields and search_term:
                term_up = search_term.upper()
                if term_up not in name.upper() and term_up not in fullname.upper():
                    for field in ("purpose", "note", "description", "user_manual", "position"):
                        val = row[field] if field in row.keys() else None
                        if val and term_up in val.upper():
                            snippet = val[:80].rstrip() + "…" if len(val) > 80 else val
                            lines.append(f"{indent}  ↳ {field}: {snippet}")
                            break

        if truncated:
            lines.append("\n(Showing first 100 results – refine your search or use get_by_category / get_structure_overview for complete results.)")
        return "\n".join(lines)
    except sqlite3.Error as ex:
        return f"Error: {ex}"


def handle_list_elements(args: dict, **_) -> str:
    try:
        p = ListElementsParams(**args)
    except ValidationError as e:
        return f"Error: {e.errors()[0]['msg']}"

    parent_fullname = p.under.strip().rstrip("/")
    try:
        conn = get_connection()
        if parent_fullname:
            resolved = resolve_element_fullname(conn, parent_fullname)
            if resolved is None:
                return f"Error: element '{parent_fullname}' not found. Call get_structure_overview or find_element to find the correct path."
            parent_fullname = resolved

        if not parent_fullname:
            rows = conn.execute(
                f"{ETREE_CTE} "
                "SELECT et.fullname, et.name, et.short_name, e.position, s.name AS statusname, s.status_type "
                "FROM etree et JOIN element e ON e.id = et.id "
                "LEFT JOIN status s ON s.id = e.status_id "
                "WHERE et.depth = 0 ORDER BY e.sort_index, et.name"
            ).fetchall()
            title = "Top-level elements"
        else:
            # Get parent id, then list its children
            parent_row = conn.execute(
                f"{ETREE_CTE} SELECT id FROM etree WHERE upper(fullname) = upper(?)",
                (parent_fullname,),
            ).fetchone()
            if not parent_row:
                return f"Error: element '{parent_fullname}' not found."
            parent_id = parent_row["id"]

            rows = conn.execute(
                f"{ETREE_CTE} "
                "SELECT et.fullname, et.name, et.short_name, e.position, s.name AS statusname, s.status_type "
                "FROM etree et JOIN element e ON e.id = et.id "
                "LEFT JOIN status s ON s.id = e.status_id "
                f"WHERE e.parent_id = {parent_id} ORDER BY e.sort_index, et.name"
            ).fetchall()
            title = f"Children of '{parent_fullname}'"

        if not rows:
            return "No elements found."

        lines = [f"{title} ({len(rows)} items):\n"]
        for row in rows:
            label = row["name"]
            sn = row["short_name"]
            if sn and sn != label:
                label += f" ({sn})"
            st = row["status_type"]
            status_suffix = f"  {{{row['statusname']}}}" if st in (1, 2) else ""
            lines.append(f"  - {row['fullname']}  [{label}]{status_suffix}")
            pos = row["position"]
            if pos:
                lines.append(f"      Position: {pos}")
        return "\n".join(lines)
    except sqlite3.Error as ex:
        return f"Error: {ex}"


def handle_get_element_details(args: dict, **_) -> str:
    try:
        p = GetElementDetailsParams(**args)
    except ValidationError as e:
        return f"Error: {e.errors()[0]['msg']}"

    fn = p.fullname.strip()
    if not fn:
        return "Error: 'fullname' is required."

    try:
        conn = get_connection()
        resolved = resolve_element_fullname(conn, fn)
        if resolved is None:
            return f"Error: element '{fn}' not found. Use find_element to search for the correct path."
        fn = resolved

        all_rows = conn.execute(
            f"{ETREE_CTE} SELECT id, fullname, name, short_name, depth FROM etree"
        ).fetchall()
        id_to_fullname = {r["id"]: r["fullname"] for r in all_rows}

        target = conn.execute(
            f"{ETREE_CTE} SELECT et.id, et.fullname, et.name, et.short_name, "
            "e.position, e.purpose, e.note, e.description, e.user_manual, "
            "c.name AS cat_name, s.name AS status_name "
            "FROM etree et JOIN element e ON e.id = et.id "
            "JOIN category c ON c.id = e.category_id "
            "LEFT JOIN status s ON s.id = e.status_id "
            "WHERE upper(et.fullname) = upper(?)",
            (fn,),
        ).fetchone()

        if not target:
            return f"Error: element '{fn}' not found. Use find_element to search for the correct path."

        eid = target["id"]
        lines = [f"Element: {target['fullname']}\n"]
        lines.append(f"  Name        : {target['name']}")
        if target["short_name"]:
            lines.append(f"  Short name  : {target['short_name']}")
        if target["position"]:
            lines.append(f"  Position    : {target['position']}")
        if target["cat_name"]:
            lines.append(f"  Category    : {target['cat_name']}")
        if target["status_name"]:
            lines.append(f"  Status      : {target['status_name']}")
        if target["purpose"]:
            lines.append(f"  Purpose     : {target['purpose']}")
        if target["note"]:
            lines.append(f"  Note        : {target['note']}")
        if target["description"]:
            lines.append(f"  Description : {target['description']}")
        if target["user_manual"]:
            lines.append(f"  User manual : {target['user_manual']}")

        fn_prefix = fn.rstrip("/") + "/"
        total_descendants = sum(1 for r in all_rows if r["fullname"].lower().startswith(fn_prefix.lower()))

        children = conn.execute(
            "SELECT id, name, sort_index FROM element WHERE parent_id = ? ORDER BY sort_index, name",
            (eid,),
        ).fetchall()

        if children:
            suffix = f", {total_descendants} total" if total_descendants > len(children) else ""
            lines.append(f"\n  Child elements ({len(children)} direct{suffix}):")
            for c in children:
                child_fn = id_to_fullname.get(c["id"], c["name"])
                lines.append(f"    +-- {child_fn}")

        conns_out = conn.execute(
            "SELECT id, name, destination_id, route, length FROM connection WHERE source_id = ? ORDER BY name",
            (eid,),
        ).fetchall()
        if conns_out:
            lines.append(f"\n  Outgoing connections ({len(conns_out)}):")
            for c in conns_out:
                dst_fn = id_to_fullname.get(c["destination_id"], str(c["destination_id"]))
                line = f"    --> {c['name']}  =>  {dst_fn}"
                if c["length"] is not None:
                    line += f"  ({c['length']} m)"
                lines.append(line)
                if c["route"]:
                    lines.append(f"      Route: {c['route']}")

        conns_in = conn.execute(
            "SELECT id, name, source_id, route, length FROM connection WHERE destination_id = ? ORDER BY name",
            (eid,),
        ).fetchall()
        if conns_in:
            lines.append(f"\n  Incoming connections ({len(conns_in)}):")
            for c in conns_in:
                src_fn = id_to_fullname.get(c["source_id"], str(c["source_id"]))
                line = f"    <-- {src_fn}  <=  {c['name']}"
                if c["length"] is not None:
                    line += f"  ({c['length']} m)"
                lines.append(line)
                if c["route"]:
                    lines.append(f"      Route: {c['route']}")

        if not children and not conns_out and not conns_in:
            lines.append("\n  (No child elements or connections)")

        return "\n".join(lines)
    except sqlite3.Error as ex:
        return f"Error: {ex}"


def handle_get_recent_changes(args: dict, **_) -> str:
    try:
        p = GetRecentChangesParams(**args)
    except ValidationError as e:
        return f"Error: {e.errors()[0]['msg']}"

    item_type = p.type.strip().lower()
    limit = max(1, min(200, p.limit))
    if item_type and item_type not in ("element", "connection", "category"):
        return "Error: 'type' must be 'element', 'connection', or 'category'."

    inc_elements = not item_type or item_type == "element"
    inc_connections = not item_type or item_type == "connection"
    inc_categories = not item_type or item_type == "category"

    try:
        conn = get_connection()
        unions: list[str] = []

        if inc_elements:
            unions.append(
                f"{ETREE_CTE.replace('WITH RECURSIVE', '').strip()} "
                "SELECT et.fullname AS item_name, 'element' AS item_type, "
                "e.created_at, e.updated_at, coalesce(e.updated_at, e.created_at) AS change_ts "
                "FROM element e JOIN etree et ON et.id = e.id"
            )
        if inc_connections:
            unions.append(
                "SELECT name AS item_name, 'connection' AS item_type, "
                "created_at, updated_at, coalesce(updated_at, created_at) AS change_ts "
                "FROM connection"
            )
        if inc_categories:
            unions.append(
                f"{CAT_TREE_CTE.replace('WITH RECURSIVE', '').strip()} "
                "SELECT ct.cat_fullname AS item_name, 'category' AS item_type, "
                "c.created_at, c.updated_at, coalesce(c.updated_at, c.created_at) AS change_ts "
                "FROM category c JOIN cat_tree ct ON ct.id = c.id"
            )

        # Build combined query — we need the CTEs at the top level
        cte_parts: list[str] = []
        if inc_elements:
            cte_parts.append(ETREE_CTE.replace("WITH RECURSIVE ", "").rstrip().rstrip(","))
        if inc_categories:
            cte_parts.append(CAT_TREE_CTE.replace("WITH RECURSIVE ", "").rstrip().rstrip(","))

        union_queries: list[str] = []
        if inc_elements:
            union_queries.append(
                "SELECT et.fullname AS item_name, 'element' AS item_type, "
                "e.created_at, e.updated_at, coalesce(e.updated_at, e.created_at) AS change_ts "
                "FROM element e JOIN etree et ON et.id = e.id"
            )
        if inc_connections:
            union_queries.append(
                "SELECT name AS item_name, 'connection' AS item_type, "
                "created_at, updated_at, coalesce(updated_at, created_at) AS change_ts "
                "FROM connection"
            )
        if inc_categories:
            union_queries.append(
                "SELECT ct.cat_fullname AS item_name, 'category' AS item_type, "
                "c.created_at, c.updated_at, coalesce(c.updated_at, c.created_at) AS change_ts "
                "FROM category c JOIN cat_tree ct ON ct.id = c.id"
            )

        cte_prefix = "WITH RECURSIVE " + ",\n".join(cte_parts) + "\n" if cte_parts else ""
        sql = (
            f"{cte_prefix}"
            f"SELECT * FROM ({' UNION ALL '.join(union_queries)}) items "
            f"ORDER BY change_ts DESC LIMIT {limit}"
        )

        rows = conn.execute(sql).fetchall()
        if not rows:
            msg = "No items found in the database." if not item_type else f"No {item_type} items found."
            return msg

        type_lbl = f", type={item_type}" if item_type else ""
        lines = [f"Recent changes ({len(rows)}{type_lbl}, newest first):\n"]
        for row in rows:
            ts = row["change_ts"] or ""
            ts_str = ts[:16].replace("T", " ") if ts else "                "
            created = row["created_at"]
            updated = row["updated_at"]
            label = "[updated]" if (updated and updated != created) else "[created]"
            lines.append(f"{ts_str}  {label:<10}  {row['item_type']:<12}  {row['item_name']}")
        return "\n".join(lines)
    except sqlite3.Error as ex:
        return f"Error: {ex}"


# ── Manage Elements ───────────────────────────────────────────────────────────

def handle_create_element(args: dict, **_) -> str:
    try:
        p = CreateElementParams(**args)
    except ValidationError as e:
        return f"Error: {e.errors()[0]['msg']}"

    name = normalize_singleline(p.name.strip()) or ""
    if not name:
        return "Error: 'name' is required."
    if INVALID_CHARS.search(name):
        return "Error: name contains invalid characters ($*[{}|\\<>?\"/;: or tab)."

    short_name = normalize_singleline((p.short_name or "").strip()) or None
    if short_name == "":
        short_name = None
    if short_name and INVALID_CHARS.search(short_name):
        return "Error: short_name contains invalid characters ($*[{}|\\<>?\"/;: or tab)."

    purpose = normalize_singleline(p.purpose)
    note = normalize_singleline(p.note)
    description = normalize_multiline(p.description)
    user_manual = normalize_multiline(p.user_manual)
    position = normalize_singleline(p.position)

    for err in [
        check_length(name, "name", MAX_NAME),
        check_length(short_name, "short_name", MAX_SHORT_NAME),
        check_length(purpose, "purpose", MAX_PURPOSE),
        check_length(note, "note", MAX_NOTE),
        check_length(description, "description", MAX_DESCRIPTION),
        check_length(user_manual, "user_manual", MAX_USER_MANUAL),
        check_length(position, "position", MAX_POSITION),
    ]:
        if err:
            return err

    category = (p.category or "").strip()
    if not category:
        return "Error: 'category' is required."

    try:
        conn = get_connection()
        _, by_fullname = load_etree(conn)

        parent_id: int | None = None
        parent_fullname: str | None = None
        parent_input = (p.parent or "").strip().rstrip("/")
        if parent_input:
            key = parent_input.lower()
            parent_row = by_fullname.get(key)
            if parent_row is None:
                return f"Error: parent element '{parent_input}' not found."
            parent_id = parent_row["id"]
            parent_fullname = parent_row["fullname"]

        cat_id, cat_err = resolve_category(conn, category)
        if cat_err:
            return cat_err
        if cat_id is None:
            return f"Error: category '{category}' not found. Call list_categories for available categories."

        segment = short_name if short_name else name
        new_fullname = f"{parent_fullname}/{segment}" if parent_fullname else segment
        if new_fullname.lower() in by_fullname:
            return f"Error: an element with full name '{new_fullname}' already exists."

        sibling_err = check_sibling_uniqueness(conn, name, short_name, parent_id)
        if sibling_err:
            return sibling_err

        status_id: int | None = None
        if p.status and p.status.strip():
            status_id = resolve_status_id(conn, p.status.strip())
            if status_id is None:
                return f"Error: status '{p.status.strip()}' not found. Call list_statuses for available statuses."

        def _do(c: sqlite3.Connection):
            si = next_sort_index(c, parent_id)
            now = _utcnow()
            cur = c.execute(
                "INSERT INTO element (name, short_name, parent_id, position, sort_index, category_id, status_id, purpose, note, description, user_manual, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (name, short_name, parent_id, position, si, cat_id, status_id,
                 purpose, note, description, user_manual, now),
            )
            return cur.lastrowid

        eid = execute_write(_do)
        return f"✓ Element '{new_fullname}' created (OID: {eid})."
    except sqlite3.Error as ex:
        return f"Error creating element: {ex}"


def handle_update_element(args: dict, **_) -> str:
    try:
        p = UpdateElementParams(**args)
    except ValidationError as e:
        return f"Error: {e.errors()[0]['msg']}"

    fullname = p.fullname.strip()
    if not fullname:
        return "Error: 'fullname' is required."

    # Normalise CLEAR sentinel on all optional fields
    short_name = normalize_clear(p.short_name)
    status = normalize_clear(p.status)
    purpose = normalize_clear(normalize_singleline(p.purpose))
    note = normalize_clear(normalize_singleline(p.note))
    description = normalize_clear(normalize_multiline(p.description))
    user_manual = normalize_clear(normalize_multiline(p.user_manual))
    position = normalize_clear(normalize_singleline(p.position))

    new_name = p.name
    if new_name is not None:
        new_name = (normalize_singleline(new_name) or "").strip()
        if not new_name:
            return "Error: 'name' cannot be empty."
        if INVALID_CHARS.search(new_name):
            return "Error: name contains invalid characters ($*[{}|\\<>?\"/;: or tab)."
    if short_name is not None and short_name != "CLEAR":
        short_name = (normalize_singleline(short_name) or "").strip()
        if not short_name:
            return "Error: 'short_name' cannot be empty – use 'CLEAR' to remove it."
        if INVALID_CHARS.search(short_name):
            return "Error: short_name contains invalid characters ($*[{}|\\<>?\"/;: or tab)."

    # Check all fields are None or empty (nothing to update)
    if all(x is None for x in (new_name, p.short_name, p.category, p.status, p.purpose, p.note, p.description, p.user_manual, p.position)):
        return "Error: provide at least one field to update."

    for err in [
        check_length(new_name, "name", MAX_NAME),
        check_length(short_name if short_name != "CLEAR" else None, "short_name", MAX_SHORT_NAME),
        check_length(purpose if purpose != "CLEAR" else None, "purpose", MAX_PURPOSE),
        check_length(note if note != "CLEAR" else None, "note", MAX_NOTE),
        check_length(description if description != "CLEAR" else None, "description", MAX_DESCRIPTION),
        check_length(user_manual if user_manual != "CLEAR" else None, "user_manual", MAX_USER_MANUAL),
        check_length(position if position != "CLEAR" else None, "position", MAX_POSITION),
    ]:
        if err:
            return err

    try:
        conn = get_connection()
        _, by_fullname = load_etree(conn)

        target_row = by_fullname.get(fullname.lower())
        if target_row is None:
            return f"Error: element '{fullname}' not found."
        canonical_fn = target_row["fullname"]
        eid = target_row["id"]

        # Resolve category
        cat_id: int | None = None
        if p.category:
            cat_id, cat_err = resolve_category(conn, p.category.strip())
            if cat_err:
                return cat_err
            if cat_id is None:
                return f"Error: category '{p.category.strip()}' not found. Call list_categories for available categories."

        # Resolve status
        status_id_new: int | None | str = "__unchanged__"  # sentinel
        if status is not None:
            if status == "CLEAR":
                status_id_new = None
            else:
                sid = resolve_status_id(conn, status.strip())
                if sid is None:
                    return f"Error: status '{status.strip()}' not found. Call list_statuses for available statuses."
                status_id_new = sid

        # Sibling uniqueness if name/short_name changes
        if new_name is not None or (short_name is not None and short_name != "CLEAR"):
            cur_row = conn.execute("SELECT parent_id FROM element WHERE id = ?", (eid,)).fetchone()
            parent_id = cur_row["parent_id"] if cur_row else None
            eff_name = new_name if new_name is not None else target_row["name"]
            eff_sn: str | None
            if short_name == "CLEAR":
                eff_sn = None
            elif short_name is not None:
                eff_sn = short_name
            else:
                eff_sn = target_row["short_name"]
            sibling_err = check_sibling_uniqueness(conn, eff_name, eff_sn, parent_id, exclude_id=eid)
            if sibling_err:
                return sibling_err

        advisories = collect_overwrite_advisories(
            conn, eid, description=description, note=note,
            purpose=purpose, user_manual=user_manual,
        )

        def _do(c: sqlite3.Connection):
            sets: list[str] = []
            vals: list = []
            now = _utcnow()

            if new_name is not None:
                sets.append("name = ?"); vals.append(new_name)
            if short_name is not None:
                sets.append("short_name = ?")
                vals.append(None if short_name == "CLEAR" else short_name)
            if cat_id is not None:
                sets.append("category_id = ?"); vals.append(cat_id)
            if status_id_new != "__unchanged__":
                sets.append("status_id = ?"); vals.append(status_id_new)
            for field, val in [("purpose", purpose), ("note", note), ("position", position)]:
                if val is not None:
                    sets.append(f"{field} = ?")
                    vals.append(None if val == "CLEAR" else val)
            for field, val in [("description", description), ("user_manual", user_manual)]:
                if val is not None:
                    sets.append(f"{field} = ?")
                    vals.append(None if val == "CLEAR" else val)
            sets.append("updated_at = ?"); vals.append(now)
            vals.append(eid)
            c.execute(f"UPDATE element SET {', '.join(sets)} WHERE id = ?", vals)

        execute_write(_do)

        # Recompute canonical fullname for return message
        new_row = conn.execute(
            f"{ETREE_CTE} SELECT fullname FROM etree WHERE id = ?", (eid,)
        ).fetchone()
        new_fn = new_row["fullname"] if new_row else canonical_fn

        msg = f"✓ Element '{canonical_fn}' updated."
        if new_fn != canonical_fn:
            msg += f" New full name: '{new_fn}'."
        if advisories:
            msg += "\n" + "\n".join(f"  Advisory: {a}" for a in advisories)
        return msg
    except sqlite3.Error as ex:
        return f"Error updating element: {ex}"


def handle_delete_element(args: dict, **_) -> str:
    try:
        p = DeleteElementParams(**args)
    except ValidationError as e:
        return f"Error: {e.errors()[0]['msg']}"

    fullname = p.fullname.strip()
    if not fullname:
        return "Error: 'fullname' is required."

    try:
        conn = get_connection()
        resolved = resolve_element_fullname(conn, fullname)
        if resolved is None:
            return f"Error: element '{fullname}' not found."
        fullname = resolved

        row = conn.execute(
            f"{ETREE_CTE} SELECT id FROM etree WHERE upper(fullname) = upper(?)", (fullname,)
        ).fetchone()
        if not row:
            return f"Error: element '{fullname}' not found."
        eid = row["id"]

        child_count = conn.execute(
            "SELECT COUNT(*) FROM element WHERE parent_id = ?", (eid,)
        ).fetchone()[0]
        if child_count > 0:
            return (
                f"Error: '{fullname}' has {child_count} child element(s). "
                "Report this to the user and ask for explicit confirmation before removing any of them."
            )

        conn_count = conn.execute(
            "SELECT COUNT(*) FROM connection WHERE source_id = ? OR destination_id = ?",
            (eid, eid),
        ).fetchone()[0]
        if conn_count > 0:
            return (
                f"Error: '{fullname}' is referenced by {conn_count} connection(s). "
                "Remove or reassign the connections first."
            )

        def _do(c: sqlite3.Connection):
            c.execute("DELETE FROM element WHERE id = ?", (eid,))

        execute_write(_do)
        return f"✓ Element '{fullname}' deleted."
    except sqlite3.Error as ex:
        return f"Error deleting element: {ex}"


def handle_move_element(args: dict, **_) -> str:
    try:
        p = MoveElementParams(**args)
    except ValidationError as e:
        return f"Error: {e.errors()[0]['msg']}"

    fullname = p.fullname.strip().rstrip("/")
    new_parent = p.new_parent.strip().rstrip("/")

    if not fullname:
        return "Error: 'fullname' is required."

    try:
        conn = get_connection()
        resolved = resolve_element_fullname(conn, fullname)
        if resolved is None:
            return f"Error: element '{fullname}' not found."
        fullname = resolved

        row = conn.execute(
            f"{ETREE_CTE} SELECT id, name, short_name FROM etree WHERE upper(fullname) = upper(?)",
            (fullname,),
        ).fetchone()
        if not row:
            return f"Error: element '{fullname}' not found."
        eid = row["id"]

        new_parent_id: int | None = None
        if new_parent:
            _, by_fullname = load_etree(conn)
            parent_row = by_fullname.get(new_parent.lower())
            if parent_row is None:
                return f"Error: target parent '{new_parent}' not found."
            new_parent_id = parent_row["id"]
            new_parent_fn = parent_row["fullname"]

            # Cycle check
            prefix = fullname.rstrip("/") + "/"
            if new_parent_fn.lower() == fullname.lower():
                return f"Error: cannot move element '{fullname}' to itself."
            if new_parent_fn.lower().startswith(prefix.lower()):
                return f"Error: cannot move element '{fullname}' into its own descendant '{new_parent_fn}'."
        else:
            new_parent_fn = ""

        sibling_err = check_sibling_uniqueness(conn, row["name"], row["short_name"], new_parent_id, exclude_id=eid)
        if sibling_err:
            return sibling_err

        def _do(c: sqlite3.Connection):
            c.execute("UPDATE element SET parent_id = ?, updated_at = ? WHERE id = ?",
                      (new_parent_id, _utcnow(), eid))

        execute_write(_do)

        new_row = conn.execute(
            f"{ETREE_CTE} SELECT fullname FROM etree WHERE id = ?", (eid,)
        ).fetchone()
        new_fn = new_row["fullname"] if new_row else fullname
        target_desc = f"'{new_parent_fn}'" if new_parent_fn else "top level"
        return f"✓ Element '{fullname}' moved to {target_desc}. New full name: '{new_fn}'."
    except sqlite3.Error as ex:
        return f"Error moving element: {ex}"


# ── Connections ───────────────────────────────────────────────────────────────

def _resolve_connection(conn: sqlite3.Connection, name: str, source: str | None, destination: str | None) -> tuple[sqlite3.Row | None, str | None]:
    """Find connection row by name + optional source/destination filters."""
    _, by_fullname = load_etree(conn)
    term = f"%{name.upper()}%"
    rows = conn.execute(
        "SELECT c.id, c.name, c.source_id, c.destination_id, c.category_id, "
        "c.route, c.length, c.purpose, c.note, c.description "
        "FROM connection c WHERE upper(c.name) LIKE ?",
        (term,),
    ).fetchall()
    if not rows:
        return None, f"Error: connection '{name}' not found."
    if source:
        src_row = by_fullname.get(source.strip().lower())
        if src_row:
            rows = [r for r in rows if r["source_id"] == src_row["id"]]
    if destination:
        dst_row = by_fullname.get(destination.strip().lower())
        if dst_row:
            rows = [r for r in rows if r["destination_id"] == dst_row["id"]]
    if not rows:
        return None, f"Error: connection '{name}' not found with the specified source/destination."
    if len(rows) > 1:
        names = ", ".join(f"'{r['name']}'" for r in rows)
        return None, f"Error: multiple connections match '{name}': {names}. Provide source and/or destination to disambiguate."
    return rows[0], None


def handle_get_connections(args: dict, **_) -> str:
    try:
        p = GetConnectionsParams(**args)
    except ValidationError as e:
        return f"Error: {e.errors()[0]['msg']}"

    category = p.category.strip()
    under = p.under.strip().rstrip("/")
    search_term = p.search_term.strip()

    if not category and not under and not search_term:
        return "Error: provide at least one of category, search_term, or under."

    try:
        conn = get_connection()
        _, elem_by_fn = load_etree(conn)
        id_to_fn = {r["id"]: r["fullname"] for r in conn.execute(
            f"{ETREE_CTE} SELECT id, fullname FROM etree"
        ).fetchall()}

        cat_ids: set[int] | None = None
        if category:
            ids, err = resolve_category_with_descendants(conn, category)
            if err:
                return err
            if ids is None:
                return f"Error: category '{category}' not found. Call list_categories for available category names."
            cat_ids = ids

        extra_sel = ", c.purpose, c.note, c.description" if p.search_all_fields else ""
        conditions: list[str] = []
        params: list = []

        if search_term:
            term = f"%{search_term.upper()}%"
            if p.search_all_fields:
                conditions.append(
                    "(upper(c.name) LIKE ? OR upper(c.route) LIKE ?"
                    " OR upper(c.purpose) LIKE ? OR upper(c.note) LIKE ? OR upper(c.description) LIKE ?)"
                )
                params += [term] * 5
            else:
                conditions.append("upper(c.name) LIKE ?")
                params.append(term)

        if cat_ids is not None:
            placeholders = ",".join("?" * len(cat_ids))
            conditions.append(f"c.category_id IN ({placeholders})")
            params += list(cat_ids)

        where = " AND ".join(conditions) if conditions else "1=1"
        sql = (
            f"SELECT c.id, c.name, c.source_id, c.destination_id, c.category_id, "
            f"c.route, c.length, cat.name AS catname{extra_sel} "
            "FROM connection c "
            "JOIN category cat ON cat.id = c.category_id "
            f"WHERE {where} ORDER BY c.source_id, c.name LIMIT 101"
        )
        rows = conn.execute(sql, params).fetchall()

        if under:
            resolved = resolve_element_fullname(conn, under)
            if resolved is None:
                return f"Error: element '{under}' not found. Call get_structure_overview or find_element to find the correct path."
            under = resolved
            prefix = under.rstrip("/") + "/"
            rows = [r for r in rows
                    if id_to_fn.get(r["source_id"], "").lower().startswith(prefix.lower())
                    or id_to_fn.get(r["destination_id"], "").lower().startswith(prefix.lower())
                    or id_to_fn.get(r["source_id"], "").lower() == under.lower()
                    or id_to_fn.get(r["destination_id"], "").lower() == under.lower()]

        truncated = len(rows) > 100
        if truncated:
            rows = rows[:100]

        if not rows:
            return "No connections found."

        count_label = f"{len(rows)}+" if truncated else str(len(rows))
        lines = [f"Connections ({count_label}):\n"]
        current_src: int | None = None
        for row in rows:
            if row["source_id"] != current_src:
                src_fn = id_to_fn.get(row["source_id"], str(row["source_id"]))
                lines.append(f"  {src_fn}")
                current_src = row["source_id"]
            dst_fn = id_to_fn.get(row["destination_id"], str(row["destination_id"]))
            length_str = f"  ({row['length']} m)" if row["length"] is not None else ""
            route_str = f"  [{row['route']}]" if row["route"] else ""
            lines.append(f"    --> {row['name']}  =>  {dst_fn}  [{row['catname']}]{length_str}{route_str}")

        if truncated:
            lines.append("\n(Showing first 100 results – refine with category, under, or search_term.)")
        return "\n".join(lines)
    except sqlite3.Error as ex:
        return f"Error: {ex}"


def handle_get_connection_details(args: dict, **_) -> str:
    try:
        p = GetConnectionDetailsParams(**args)
    except ValidationError as e:
        return f"Error: {e.errors()[0]['msg']}"

    if not p.name.strip():
        return "Error: 'name' is required."

    try:
        conn = get_connection()
        row, err = _resolve_connection(conn, p.name, p.source, p.destination)
        if err:
            return err

        id_to_fn = {r["id"]: r["fullname"] for r in conn.execute(
            f"{ETREE_CTE} SELECT id, fullname FROM etree"
        ).fetchall()}
        cat_row = conn.execute("SELECT name FROM category WHERE id = ?", (row["category_id"],)).fetchone()
        src_fn = id_to_fn.get(row["source_id"], str(row["source_id"]))
        dst_fn = id_to_fn.get(row["destination_id"], str(row["destination_id"]))

        lines = [f"Connection: {row['name']}\n"]
        lines.append(f"  Category    : {cat_row['name'] if cat_row else '?'}")
        lines.append(f"  Source      : {src_fn}")
        lines.append(f"  Destination : {dst_fn}")
        if row["route"]:
            lines.append(f"  Route       : {row['route']}")
        if row["length"] is not None:
            lines.append(f"  Length      : {row['length']} m")
        if row["purpose"]:
            lines.append(f"  Purpose     : {row['purpose']}")
        if row["note"]:
            lines.append(f"  Note        : {row['note']}")
        if row["description"]:
            lines.append(f"  Description : {row['description']}")
        return "\n".join(lines)
    except sqlite3.Error as ex:
        return f"Error: {ex}"


def handle_create_connection(args: dict, **_) -> str:
    try:
        p = CreateConnectionParams(**args)
    except ValidationError as e:
        return f"Error: {e.errors()[0]['msg']}"

    name = normalize_singleline((p.name or "").strip()) or ""
    if not name:
        return "Error: 'name' is required."
    if INVALID_CHARS_CONNECTION.search(name):
        return "Error: name contains invalid characters (* | < > ? \" or tab)."

    category = (p.category or "").strip()
    if not category:
        return "Error: 'category' is required."
    source = (p.source or "").strip()
    destination = (p.destination or "").strip()
    if not source:
        return "Error: 'source' is required."
    if not destination:
        return "Error: 'destination' is required."

    route = normalize_multiline(p.route)
    purpose = normalize_singleline(p.purpose)
    note = normalize_singleline(p.note)
    description = normalize_multiline(p.description)

    for err in [
        check_length(name, "name", MAX_NAME_CONNECTION),
        check_length(route, "route", MAX_ROUTE),
        check_length(purpose, "purpose", MAX_PURPOSE),
        check_length(note, "note", MAX_NOTE),
        check_length(description, "description", MAX_DESCRIPTION),
    ]:
        if err:
            return err

    try:
        conn = get_connection()
        _, by_fullname = load_etree(conn)

        src_row = by_fullname.get(source.lower())
        if src_row is None:
            return f"Error: source element '{source}' not found."
        dst_row = by_fullname.get(destination.lower())
        if dst_row is None:
            return f"Error: destination element '{destination}' not found."
        src_id = src_row["id"]
        dst_id = dst_row["id"]

        cat_id, cat_err = resolve_category(conn, category)
        if cat_err:
            return cat_err
        if cat_id is None:
            return f"Error: category '{category}' not found. Call list_categories for available categories."

        combo_err = check_connection_combo_uniqueness(conn, name, cat_id, src_id, dst_id)
        if combo_err:
            return combo_err

        hint = connection_same_src_dst_hint(conn, src_id, dst_id, cat_id)

        def _do(c: sqlite3.Connection):
            now = _utcnow()
            cur = c.execute(
                "INSERT INTO connection (name, source_id, destination_id, category_id, route, length, purpose, note, description, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (name, src_id, dst_id, cat_id, route, p.length, purpose, note, description, now),
            )
            return cur.lastrowid

        cid = execute_write(_do)
        msg = f"✓ Connection '{name}' created (OID: {cid})."
        if hint:
            msg += hint
        return msg
    except sqlite3.Error as ex:
        return f"Error creating connection: {ex}"


def handle_update_connection(args: dict, **_) -> str:
    try:
        p = UpdateConnectionParams(**args)
    except ValidationError as e:
        return f"Error: {e.errors()[0]['msg']}"

    if not (p.name or "").strip():
        return "Error: 'name' is required."

    route = normalize_clear(normalize_multiline(p.route))
    purpose = normalize_clear(normalize_singleline(p.purpose))
    note = normalize_clear(normalize_singleline(p.note))
    description = normalize_clear(normalize_multiline(p.description))

    new_name = normalize_singleline((p.new_name or "").strip()) or None
    if new_name and INVALID_CHARS_CONNECTION.search(new_name):
        return "Error: new_name contains invalid characters (* | < > ? \" or tab)."

    for err in [
        check_length(new_name, "new_name", MAX_NAME_CONNECTION),
        check_length(route if route != "CLEAR" else None, "route", MAX_ROUTE),
        check_length(purpose if purpose != "CLEAR" else None, "purpose", MAX_PURPOSE),
        check_length(note if note != "CLEAR" else None, "note", MAX_NOTE),
        check_length(description if description != "CLEAR" else None, "description", MAX_DESCRIPTION),
    ]:
        if err:
            return err

    try:
        conn = get_connection()
        row, err = _resolve_connection(conn, p.name, p.source, p.destination)
        if err:
            return err
        cid = row["id"]

        _, by_fullname = load_etree(conn)

        new_src_id = row["source_id"]
        if p.new_source:
            src_row = by_fullname.get(p.new_source.strip().lower())
            if src_row is None:
                return f"Error: new source element '{p.new_source}' not found."
            new_src_id = src_row["id"]

        new_dst_id = row["destination_id"]
        if p.new_destination:
            dst_row = by_fullname.get(p.new_destination.strip().lower())
            if dst_row is None:
                return f"Error: new destination element '{p.new_destination}' not found."
            new_dst_id = dst_row["id"]

        new_cat_id = row["category_id"]
        if p.category:
            cat_id, cat_err = resolve_category(conn, p.category.strip())
            if cat_err:
                return cat_err
            if cat_id is None:
                return f"Error: category '{p.category.strip()}' not found."
            new_cat_id = cat_id

        eff_name = new_name if new_name else row["name"]
        combo_err = check_connection_combo_uniqueness(conn, eff_name, new_cat_id, new_src_id, new_dst_id, exclude_id=cid)
        if combo_err:
            return combo_err

        advisories = collect_connection_overwrite_advisories(conn, cid, description=description, note=note, purpose=purpose)

        def _do(c: sqlite3.Connection):
            sets: list[str] = []
            vals: list = []
            if new_name:
                sets.append("name = ?"); vals.append(new_name)
            if p.new_source:
                sets.append("source_id = ?"); vals.append(new_src_id)
            if p.new_destination:
                sets.append("destination_id = ?"); vals.append(new_dst_id)
            if p.category:
                sets.append("category_id = ?"); vals.append(new_cat_id)
            if route is not None:
                sets.append("route = ?"); vals.append(None if route == "CLEAR" else route)
            if p.length is not None:
                sets.append("length = ?"); vals.append(None if p.length == 0 else p.length)
            for field, val in [("purpose", purpose), ("note", note), ("description", description)]:
                if val is not None:
                    sets.append(f"{field} = ?"); vals.append(None if val == "CLEAR" else val)
            sets.append("updated_at = ?"); vals.append(_utcnow())
            vals.append(cid)
            c.execute(f"UPDATE connection SET {', '.join(sets)} WHERE id = ?", vals)

        execute_write(_do)
        msg = f"✓ Connection '{row['name']}' updated."
        if advisories:
            msg += "\n" + "\n".join(f"  Advisory: {a}" for a in advisories)
        return msg
    except sqlite3.Error as ex:
        return f"Error updating connection: {ex}"


def handle_delete_connection(args: dict, **_) -> str:
    try:
        p = DeleteConnectionParams(**args)
    except ValidationError as e:
        return f"Error: {e.errors()[0]['msg']}"

    if not (p.name or "").strip():
        return "Error: 'name' is required."

    try:
        conn = get_connection()
        row, err = _resolve_connection(conn, p.name, p.source, p.destination)
        if err:
            return err
        cid = row["id"]
        cname = row["name"]

        def _do(c: sqlite3.Connection):
            c.execute("DELETE FROM connection WHERE id = ?", (cid,))

        execute_write(_do)
        return f"✓ Connection '{cname}' deleted."
    except sqlite3.Error as ex:
        return f"Error deleting connection: {ex}"


# ── Categories ────────────────────────────────────────────────────────────────

def handle_list_categories(args: dict, **_) -> str:
    try:
        ListCategoriesParams(**args)
    except ValidationError as e:
        return f"Error: {e.errors()[0]['msg']}"

    try:
        conn = get_connection()
        rows = conn.execute(
            f"{CAT_TREE_CTE} "
            "SELECT ct.id, ct.cat_fullname, ct.name, ct.short_name, ct.cat_depth, ct.is_area_category, "
            "c.description, "
            "COUNT(DISTINCT e.id) AS elem_count, COUNT(DISTINCT cn.id) AS conn_count "
            "FROM cat_tree ct "
            "JOIN category c ON c.id = ct.id "
            "LEFT JOIN element e ON e.category_id = ct.id "
            "LEFT JOIN connection cn ON cn.category_id = ct.id "
            "GROUP BY ct.id, ct.cat_fullname, ct.name, ct.short_name, ct.cat_depth, ct.is_area_category, c.description "
            "ORDER BY ct.cat_fullname"
        ).fetchall()

        if not rows:
            return "No categories found."

        lines = [f"Categories ({len(rows)}):\n"]
        for row in rows:
            indent = "  " * row["cat_depth"]
            label = row["name"]
            if row["short_name"] and row["short_name"] != label:
                label += f" ({row['short_name']})"
            flags: list[str] = []
            if row["is_area_category"]:
                flags.append("[structural area]")
            count = row["elem_count"] + row["conn_count"]
            if count > 0:
                flags.append(f"({count} items)")
            flag_str = "  " + "  ".join(flags) if flags else ""
            lines.append(f"{indent}- {label}{flag_str}")
        return "\n".join(lines)
    except sqlite3.Error as ex:
        return f"Error: {ex}"


def handle_get_by_category(args: dict, **_) -> str:
    try:
        p = GetByCategoryParams(**args)
    except ValidationError as e:
        return f"Error: {e.errors()[0]['msg']}"

    category = p.category.strip()
    under = p.under.strip().rstrip("/")
    if not category:
        return "Error: 'category' is required."

    try:
        conn = get_connection()
        cat_ids, cat_err = resolve_category_with_descendants(conn, category)
        if cat_err:
            return cat_err
        if cat_ids is None:
            return f"Error: category '{category}' not found. Call list_categories for available categories."

        if under:
            resolved = resolve_element_fullname(conn, under)
            if resolved is None:
                return f"Error: element '{under}' not found."
            under = resolved

        placeholders = ",".join("?" * len(cat_ids))
        params: list = list(cat_ids)
        under_clause = ""
        if under:
            under_clause = " AND (upper(et.fullname) LIKE upper(?) OR upper(et.fullname) = upper(?))"
            params += [under + "/%", under]

        rows = conn.execute(
            f"{ETREE_CTE} "
            "SELECT et.fullname, et.name, e.position, s.name AS statusname, s.status_type, c.name AS catname "
            "FROM etree et JOIN element e ON e.id = et.id "
            "JOIN category c ON c.id = e.category_id "
            "LEFT JOIN status s ON s.id = e.status_id "
            f"WHERE e.category_id IN ({placeholders}){under_clause} "
            "ORDER BY et.sortpath",
            params,
        ).fetchall()

        if not rows:
            return f"No elements found in category '{category}'" + (f" under '{under}'" if under else "") + "."

        scope = f" under '{under}'" if under else ""
        lines = [f"Elements in category '{category}'{scope} ({len(rows)}):\n"]
        for row in rows:
            st = row["status_type"]
            st_str = f"  {{{row['statusname']}}}" if st in (1, 2) else ""
            pos_str = f"  [{row['position']}]" if row["position"] else ""
            cat_str = f"  [{row['catname']}]" if row["catname"] != category else ""
            lines.append(f"  - {row['fullname']}{cat_str}{pos_str}{st_str}")
        return "\n".join(lines)
    except sqlite3.Error as ex:
        return f"Error: {ex}"


def handle_create_category(args: dict, **_) -> str:
    try:
        p = CreateCategoryParams(**args)
    except ValidationError as e:
        return f"Error: {e.errors()[0]['msg']}"

    name = normalize_singleline((p.name or "").strip()) or ""
    if not name:
        return "Error: 'name' is required."
    if INVALID_CHARS.search(name):
        return "Error: name contains invalid characters ($*[{}|\\<>?\"/;: or tab)."
    short_name = normalize_singleline((p.short_name or "").strip()) or None
    if short_name and INVALID_CHARS.search(short_name):
        return "Error: short_name contains invalid characters ($*[{}|\\<>?\"/;: or tab)."
    description = normalize_multiline(p.description)

    for err in [
        check_length(name, "name", MAX_NAME),
        check_length(short_name, "short_name", MAX_SHORT_NAME),
        check_length(description, "description", MAX_DESCRIPTION),
    ]:
        if err:
            return err

    is_area = bool(p.is_structural_area) if p.is_structural_area is not None else False

    try:
        conn = get_connection()
        parent_id: int | None = None
        parent_fullname: str | None = None
        if p.parent:
            cat_id, cat_err = resolve_category(conn, p.parent.strip())
            if cat_err:
                return cat_err
            if cat_id is None:
                return f"Error: parent category '{p.parent.strip()}' not found."
            parent_id = cat_id
            parent_row = conn.execute(
                f"{CAT_TREE_CTE} SELECT cat_fullname FROM cat_tree WHERE id = ?", (parent_id,)
            ).fetchone()
            parent_fullname = parent_row["cat_fullname"] if parent_row else None

        seg = short_name if short_name else name
        new_fn = f"{parent_fullname}/{seg}" if parent_fullname else seg

        exists = conn.execute(
            f"{CAT_TREE_CTE} SELECT id FROM cat_tree WHERE upper(cat_fullname) = upper(?)",
            (new_fn,),
        ).fetchone()
        if exists:
            return f"Error: a category with full name '{new_fn}' already exists."

        def _do(c: sqlite3.Connection):
            now = _utcnow()
            cur = c.execute(
                "INSERT INTO category (name, short_name, is_area_category, parent_id, description, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (name, short_name, 1 if is_area else 0, parent_id, description, now),
            )
            return cur.lastrowid

        cid = execute_write(_do)
        return f"✓ Category '{new_fn}' created (OID: {cid})."
    except sqlite3.Error as ex:
        return f"Error creating category: {ex}"


def handle_update_category(args: dict, **_) -> str:
    try:
        p = UpdateCategoryParams(**args)
    except ValidationError as e:
        return f"Error: {e.errors()[0]['msg']}"

    category = (p.category or "").strip()
    if not category:
        return "Error: 'category' is required."

    new_name = normalize_singleline((p.new_name or "").strip()) or None
    if new_name and INVALID_CHARS.search(new_name):
        return "Error: new_name contains invalid characters ($*[{}|\\<>?\"/;: or tab)."

    new_short_name = normalize_clear(p.new_short_name)
    if new_short_name and new_short_name != "CLEAR":
        new_short_name = (normalize_singleline(new_short_name) or "").strip() or None
        if new_short_name and INVALID_CHARS.search(new_short_name):
            return "Error: new_short_name contains invalid characters."

    description = normalize_clear(normalize_multiline(p.description))

    for err in [
        check_length(new_name, "new_name", MAX_NAME),
        check_length(new_short_name if new_short_name != "CLEAR" else None, "new_short_name", MAX_SHORT_NAME),
        check_length(description if description != "CLEAR" else None, "description", MAX_DESCRIPTION),
    ]:
        if err:
            return err

    try:
        conn = get_connection()
        cat_id, cat_err = resolve_category(conn, category)
        if cat_err:
            return cat_err
        if cat_id is None:
            return f"Error: category '{category}' not found. Call list_categories for available categories."

        new_parent_id: int | None | str = "__unchanged__"
        if p.new_parent is not None:
            np = normalize_clear(p.new_parent)
            if np == "CLEAR" or np == "":
                new_parent_id = None
            else:
                pid, perr = resolve_category(conn, (np or "").strip())
                if perr:
                    return perr
                if pid is None:
                    return f"Error: new parent category '{np}' not found."
                new_parent_id = pid

        if all(x is None for x in (new_name, p.new_short_name, p.description, p.is_structural_area)) and new_parent_id == "__unchanged__":
            return "Error: provide at least one field to update."

        def _do(c: sqlite3.Connection):
            sets: list[str] = []
            vals: list = []
            if new_name:
                sets.append("name = ?"); vals.append(new_name)
            if new_short_name is not None:
                sets.append("short_name = ?")
                vals.append(None if new_short_name == "CLEAR" else new_short_name)
            if description is not None:
                sets.append("description = ?")
                vals.append(None if description == "CLEAR" else description)
            if p.is_structural_area is not None:
                sets.append("is_area_category = ?"); vals.append(1 if p.is_structural_area else 0)
            if new_parent_id != "__unchanged__":
                sets.append("parent_id = ?"); vals.append(new_parent_id)
            sets.append("updated_at = ?"); vals.append(_utcnow())
            vals.append(cat_id)
            c.execute(f"UPDATE category SET {', '.join(sets)} WHERE id = ?", vals)

        execute_write(_do)

        new_fn_row = conn.execute(
            f"{CAT_TREE_CTE} SELECT cat_fullname FROM cat_tree WHERE id = ?", (cat_id,)
        ).fetchone()
        new_fn = new_fn_row["cat_fullname"] if new_fn_row else category
        return f"✓ Category '{category}' updated. New full name: '{new_fn}'."
    except sqlite3.Error as ex:
        return f"Error updating category: {ex}"


def handle_delete_category(args: dict, **_) -> str:
    try:
        p = DeleteCategoryParams(**args)
    except ValidationError as e:
        return f"Error: {e.errors()[0]['msg']}"

    category = (p.category or "").strip()
    if not category:
        return "Error: 'category' is required."

    try:
        conn = get_connection()
        cat_id, cat_err = resolve_category(conn, category)
        if cat_err:
            return cat_err
        if cat_id is None:
            return f"Error: category '{category}' not found. Call list_categories for available categories."

        child_count = conn.execute(
            "SELECT COUNT(*) FROM category WHERE parent_id = ?", (cat_id,)
        ).fetchone()[0]
        if child_count > 0:
            return (
                f"Error: category '{category}' has {child_count} child categor{'y' if child_count == 1 else 'ies'}. "
                "Report this to the user and ask for explicit confirmation before removing any of them."
            )

        elem_count = conn.execute(
            "SELECT COUNT(*) FROM element WHERE category_id = ?", (cat_id,)
        ).fetchone()[0]
        conn_count = conn.execute(
            "SELECT COUNT(*) FROM connection WHERE category_id = ?", (cat_id,)
        ).fetchone()[0]
        total = elem_count + conn_count
        if total > 0:
            return (
                f"Error: category '{category}' is used by {total} item(s) ({elem_count} element(s), {conn_count} connection(s)). "
                "Reassign or remove them first."
            )

        def _do(c: sqlite3.Connection):
            c.execute("DELETE FROM category WHERE id = ?", (cat_id,))

        execute_write(_do)
        return f"✓ Category '{category}' deleted."
    except sqlite3.Error as ex:
        return f"Error deleting category: {ex}"


# ── Status ────────────────────────────────────────────────────────────────────

def handle_list_statuses(args: dict, **_) -> str:
    try:
        ListStatusesParams(**args)
    except ValidationError as e:
        return f"Error: {e.errors()[0]['msg']}"

    try:
        conn = get_connection()
        rows = conn.execute(
            "SELECT s.id, s.name, s.status_type, s.note, COUNT(e.id) AS elem_count "
            "FROM status s LEFT JOIN element e ON e.status_id = s.id "
            "GROUP BY s.id, s.name, s.status_type, s.note "
            "ORDER BY s.status_type, s.name"
        ).fetchall()

        if not rows:
            return "No statuses found. Use create_status to add one."

        lines = [f"Statuses ({len(rows)}):\n"]
        current_type: int | None = None
        for row in rows:
            st = row["status_type"]
            if st != current_type:
                lines.append(f"\n  {_status_type_name(st)} (type {st}):")
                current_type = st
            detail = f"  ({row['elem_count']} elem.)" if row["elem_count"] > 0 else ""
            note_str = f"  – {row['note']}" if row["note"] else ""
            lines.append(f"    - {row['name']}{detail}{note_str}")

        lines.append("\n  Use exact name (case-insensitive) in create_element / update_element. find_element also accepts partial matches.")
        return "\n".join(lines)
    except sqlite3.Error as ex:
        return f"Error: {ex}"


def handle_create_status(args: dict, **_) -> str:
    try:
        p = CreateStatusParams(**args)
    except ValidationError as e:
        return f"Error: {e.errors()[0]['msg']}"

    name = normalize_singleline((p.name or "").strip()) or ""
    if not name:
        return "Error: 'name' is required."
    if INVALID_CHARS.search(name):
        return "Error: name contains invalid characters ($*[{}|\\<>?\"/;: or tab)."

    note = normalize_singleline(p.note)
    for err in [check_length(name, "name", MAX_NAME), check_length(note, "note", MAX_NOTE)]:
        if err:
            return err

    st_int, st_err = _parse_status_type(p.status_type or "")
    if st_err:
        return st_err

    try:
        conn = get_connection()
        existing = conn.execute(
            "SELECT COUNT(*) FROM status WHERE upper(name) = upper(?)", (name,)
        ).fetchone()[0]
        if existing > 0:
            return f"Error: a status named '{name}' already exists."

        def _do(c: sqlite3.Connection):
            cur = c.execute(
                "INSERT INTO status (name, status_type, note) VALUES (?, ?, ?)",
                (name, st_int, note),
            )
            return cur.lastrowid

        sid = execute_write(_do)
        return f"✓ Status '{name}' created (type: {_status_type_name(st_int)}, OID: {sid})."
    except sqlite3.Error as ex:
        return f"Error creating status: {ex}"


def handle_update_status(args: dict, **_) -> str:
    try:
        p = UpdateStatusParams(**args)
    except ValidationError as e:
        return f"Error: {e.errors()[0]['msg']}"

    name = (p.name or "").strip()
    if not name:
        return "Error: 'name' is required."
    if p.new_name is None and p.new_status_type is None and p.note is None:
        return "Error: provide at least one of new_name, new_status_type, note."

    new_name = normalize_singleline((p.new_name or "").strip()) or None
    if new_name:
        if INVALID_CHARS.search(new_name):
            return "Error: new_name contains invalid characters ($*[{}|\\<>?\"/;: or tab)."

    note = normalize_clear(normalize_singleline(p.note))

    for err in [check_length(new_name, "new_name", MAX_NAME),
                check_length(note if note != "CLEAR" else None, "note", MAX_NOTE)]:
        if err:
            return err

    st_int = -1
    if p.new_status_type:
        st_int, st_err = _parse_status_type(p.new_status_type)
        if st_err:
            return st_err

    try:
        conn = get_connection()
        row = conn.execute(
            "SELECT id, name, status_type, note FROM status WHERE upper(name) = upper(?)", (name,)
        ).fetchone()
        if not row:
            return f"Error: status '{name}' not found. Call list_statuses to see available statuses."
        sid = row["id"]

        if new_name:
            clash = conn.execute(
                "SELECT COUNT(*) FROM status WHERE upper(name) = upper(?) AND id != ?",
                (new_name, sid),
            ).fetchone()[0]
            if clash > 0:
                return f"Error: a status named '{new_name}' already exists."

        def _do(c: sqlite3.Connection):
            sets: list[str] = []
            vals: list = []
            if new_name:
                sets.append("name = ?"); vals.append(new_name)
            if st_int >= 0:
                sets.append("status_type = ?"); vals.append(st_int)
            if note is not None:
                sets.append("note = ?"); vals.append(None if note == "CLEAR" else note)
            vals.append(sid)
            c.execute(f"UPDATE status SET {', '.join(sets)} WHERE id = ?", vals)

        execute_write(_do)
        changes: list[str] = []
        if new_name:
            changes.append(f"name → '{new_name}'")
        if st_int >= 0:
            changes.append(f"type → {_status_type_name(st_int)}")
        if note is not None:
            changes.append("note → (removed)" if note == "CLEAR" else "note updated")
        return f"✓ Status '{name}' updated: {', '.join(changes)}."
    except sqlite3.Error as ex:
        return f"Error updating status: {ex}"


def handle_delete_status(args: dict, **_) -> str:
    try:
        p = DeleteStatusParams(**args)
    except ValidationError as e:
        return f"Error: {e.errors()[0]['msg']}"

    name = (p.name or "").strip()
    if not name:
        return "Error: 'name' is required."

    try:
        conn = get_connection()
        row = conn.execute(
            "SELECT id FROM status WHERE upper(name) = upper(?)", (name,)
        ).fetchone()
        if not row:
            return f"Error: status '{name}' not found. Call list_statuses to see available statuses."
        sid = row["id"]

        usage = conn.execute(
            "SELECT COUNT(*) FROM element WHERE status_id = ?", (sid,)
        ).fetchone()[0]
        if usage > 0:
            return (
                f"Error: status '{name}' is assigned to {usage} element{'s' if usage != 1 else ''}. "
                f"Reassign or remove their status first (call find_element with status='{name}' to locate them)."
            )

        def _do(c: sqlite3.Connection):
            c.execute("DELETE FROM status WHERE id = ?", (sid,))

        execute_write(_do)
        return f"✓ Status '{name}' deleted."
    except sqlite3.Error as ex:
        return f"Error deleting status: {ex}"
