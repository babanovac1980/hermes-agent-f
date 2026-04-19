# Plan: `home_memory` Hermes plugin (Python port of HomeMemoryMCP)

Hand-off document for an implementation agent. Written against the Hermes repo at `/home/bogdan/AI_Projects/hermes-agent-f` (Hermes v0.8.0+ plugin system).

---

## Context

Port `impactjo/home-memory` (a .NET 10 / Firebird + DevExpress XPO MCP server, 23 tools) to a native Python plugin for Nous Hermes. Hermes handles the LLM loop and tool dispatch — we just ship a plugin that provides the tools and a local SQLite store.

Goal: **functional parity** with the C# reference. Every tool, every validation rule, every advisory, the same 100+ seeded categories and default house structure — so workflows like "What's in the basement?" or "I have a Bosch washer in the utility room" work identically.

The C# uses Firebird with XPO-style multi-table inheritance (`CEntity` → `CItem`/`Part` → `Element`/`Connection`/`Category`). We **flatten that into one SQLite row per entity** while preserving every domain field (`Purpose`, `Note`, `Description`, `UserManual`, `ShortName`, `SortIndex`, `Position`, `Route`, `Length`, `StatusType`, `IsAreaCategory`, audit fields).

**Reference C# source cloned locally at `/tmp/home-memory-ref/`** — keep it around during implementation. Key files to port:
- `HomeMemoryMCP/Tools/ElementTools.cs` — create/update/delete/move_element, get_element_details
- `HomeMemoryMCP/Tools/ConnectionTools.cs` — all 5 connection tools
- `HomeMemoryMCP/Tools/CategoryTools.cs` — all 5 category tools
- `HomeMemoryMCP/Tools/StatusTools.cs` — all 4 status tools
- `HomeMemoryMCP/Tools/StructureTools.cs` — get_structure_overview, find_element, list_elements, get_recent_changes
- `HomeMemoryMCP/Tools/QueryHelpers.cs` — shared helpers (port to `query_helpers.py`)
- `HomeMemoryMCP/Tools/Validate.cs` — port to `validate.py`
- `HomeMemoryMCP/Db/SqlQueries.cs` — recursive CTEs
- `HomeMemoryMCP/Db/DbSeeder.cs` — seed logic
- `HomeMemoryMCP/SeedData/*.json` — raw seed data

### Decisions already locked with the user

1. **23 tools** (include `get_recent_changes`).
2. **Faithful to C#, flattened** into SQLite. Keep all fields, collapse the XPO inheritance.
3. **Hermes v0.8.0 plugin format** — `plugin.yaml` + `register(ctx)` calling `ctx.register_tool()`. Mirror the pattern in `plugins/jellyfin/__init__.py`.
4. **Default DB path** `/opt/data/home-memory.db`, overridden by `HOME_MEMORY_DB_PATH`.

### Reference: how Hermes plugins register tools

Read `plugins/jellyfin/__init__.py` and `plugins/jellyfin/jellyfin_client.py` — that is the canonical template. The registration call signature lives in `tools/registry.py`:

```python
ctx.register_tool(
    name: str, toolset: str, schema: dict, handler: Callable,
    check_fn: Callable | None = None,
    requires_env: list[str] = None,
    is_async: bool = False,
    description: str = "",
    emoji: str = "",
)
```

Handler contract: `def handler(args: dict, **kw) -> str`. The return value must be a string. The C# tools already return strings (`"✓ Element ... created"`, `"Error: ..."`) — **we match the C# format byte-for-byte** (except OID becomes an integer PK). Do **not** wrap in `tool_result`/`tool_error` JSON helpers.

---

## Package layout

Bundled plugin at `/home/bogdan/AI_Projects/hermes-agent-f/plugins/home_memory/`:

```
plugins/home_memory/
├── plugin.yaml              # Hermes plugin manifest
├── __init__.py              # register(ctx) — iterates TOOLS and calls ctx.register_tool()
├── db.py                    # sqlite3 connection factory, schema init, WAL, seed
├── validate.py              # Port of Validate.cs (InvalidChars regex, Length, Normalize*, NormalizeClear)
├── query_helpers.py         # Port of QueryHelpers.cs (resolve fullname, sibling uniqueness, overwrite advisories, circular-ref check)
├── repository.py            # raw-SQL CRUD + recursive CTEs (element tree, category tree)
├── models.py                # Pydantic v2 input models (one per tool)
├── tools.py                 # The 23 tool handlers (sync, returning str)
├── registry.py              # TOOLS list + TOOL_MAP + JSON schemas, EMOJI, CHECK_FN
└── seed/
    ├── __init__.py
    ├── categories.py        # CATEGORIES: list[dict] — tree, port of SeedData/categories.json
    ├── elements.py          # ELEMENTS: list[dict] — default house structure
    └── statuses.py          # STATUSES: [("Existing",0),("Planned",1),("Removed",2)]
```

### `plugin.yaml`

```yaml
name: home_memory
version: 0.1.0
description: "Home Memory — persistent structured knowledge of every room, device, cable, and pipe in your home. 23 tools ported from impactjo/home-memory."
author: babanovac1980
provides_tools:
  - get_structure_overview
  - find_element
  - list_elements
  - get_element_details
  - get_recent_changes
  - create_element
  - update_element
  - delete_element
  - move_element
  - get_connections
  - get_connection_details
  - create_connection
  - update_connection
  - delete_connection
  - list_categories
  - get_by_category
  - create_category
  - update_category
  - delete_category
  - list_statuses
  - create_status
  - update_status
  - delete_status
```

### `__init__.py`

```python
from .registry import TOOLS, CHECK_FN, EMOJI

def register(ctx):
    for name, schema, handler in TOOLS:
        ctx.register_tool(
            name=name, toolset="home_memory", schema=schema, handler=handler,
            check_fn=CHECK_FN, requires_env=[], is_async=False, emoji=EMOJI,
        )
```

---

## SQLite schema

One table per entity. Column names are snake_cased. Integer PKs replace GUID OIDs (nothing outside this DB references them). `FullName` stays computed via CTE, never stored.

```sql
CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);

CREATE TABLE IF NOT EXISTS category (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    name             TEXT    NOT NULL,
    short_name       TEXT,
    is_area_category INTEGER NOT NULL DEFAULT 0,      -- 0/1 bool
    parent_id        INTEGER REFERENCES category(id),
    description      TEXT,
    created_at       TEXT    NOT NULL,                -- ISO-8601 UTC
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
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT    NOT NULL,
    status_type  INTEGER NOT NULL,                    -- 0=Existing, 1=Planned, 2=Removed
    note         TEXT,
    lock_field   INTEGER NOT NULL DEFAULT 0
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
```

### `db.py` behaviour

- Path resolution: `os.environ.get("HOME_MEMORY_DB_PATH", "/opt/data/home-memory.db")`.
- `mkdir(parents=True, exist_ok=True)` on the parent.
- Single `sqlite3.connect(..., check_same_thread=False, isolation_level=None)` guarded by a module-level `threading.Lock`.
- PRAGMAs: `journal_mode=WAL`, `foreign_keys=ON`, `busy_timeout=5000`.
- Write path: `BEGIN IMMEDIATE` + retry-with-jitter on `SQLITE_BUSY` (see `hermes_state.py:_execute_write` for reference pattern).
- `init_schema()` runs `executescript(SCHEMA_SQL)` then inserts `schema_version=1` if empty.
- `seed_if_empty()` — only when `SELECT COUNT(*) FROM category = 0`; inserts statuses, recursive categories, then elements (parent resolved via a `fullname → id` map).
- **No side effects on module import.** `init()` is triggered the first time `get_connection()` is called.

---

## Key SQL (recursive CTEs)

### Element tree (ported from `SqlQueries.EtreeCte`)

```sql
WITH RECURSIVE etree AS (
    SELECT id, name, short_name, parent_id, position, sort_index,
           CAST(coalesce(nullif(trim(short_name),''), name) AS TEXT) AS fullname,
           CAST(name AS TEXT) AS longname,
           CAST(printf('%010d', coalesce(sort_index, 2147483647))
                || coalesce(nullif(trim(short_name),''), name) AS TEXT) AS sortpath,
           0 AS depth
    FROM element WHERE parent_id IS NULL
  UNION ALL
    SELECT e.id, e.name, e.short_name, e.parent_id, e.position, e.sort_index,
           p.fullname || '/' || coalesce(nullif(trim(e.short_name),''), e.name),
           p.longname || '/' || e.name,
           p.sortpath || '/' || printf('%010d', coalesce(e.sort_index, 2147483647))
                             || coalesce(nullif(trim(e.short_name),''), e.name),
           p.depth + 1
    FROM element e JOIN etree p ON e.parent_id = p.id
)
```

### Category tree (ported from `SqlQueries.CatCte`)

```sql
WITH RECURSIVE cat_tree AS (
    SELECT id, name, short_name, is_area_category, parent_id,
           coalesce(nullif(trim(short_name),''), name) AS cat_fullname, 0 AS cat_depth
    FROM category WHERE parent_id IS NULL
  UNION ALL
    SELECT c.id, c.name, c.short_name, c.is_area_category, c.parent_id,
           p.cat_fullname || '/' || coalesce(nullif(trim(c.short_name),''), c.name),
           p.cat_depth + 1
    FROM category c JOIN cat_tree p ON c.parent_id = p.id
)
```

### `find_element` (shape)

```sql
WITH RECURSIVE etree AS (...)
SELECT et.fullname, et.name, et.short_name, c.name AS cat_name,
       s.name AS status_name, s.status_type, e.position, e.purpose, e.note
FROM etree et
JOIN element  e  ON e.id = et.id
JOIN category c  ON c.id = e.category_id
LEFT JOIN status s ON s.id = e.status_id
WHERE ( upper(et.name) LIKE ? OR upper(et.fullname) LIKE ?
        [ OR upper(e.purpose|e.note|e.description|e.user_manual|e.position) LIKE ? ... ] )
  [ AND upper(et.fullname) LIKE upper(?) ]                  -- under
  [ AND s.status_type = ? | AND upper(s.name) = upper(?) ]  -- status
  [ AND e.category_id IN (<resolved cat ids + descendants>) ]
LIMIT 100;
```

### `get_recent_changes`

```sql
-- single UNION ALL across element, connection, category with
--   (item_name, item_type, created_at, updated_at, coalesce(updated_at, created_at) AS change_ts)
-- element and category rows join the respective CTE to get the full path
ORDER BY change_ts DESC NULLS LAST LIMIT ?
```

---

## The 23 tools

All descriptions come **verbatim** from the C# `[Description(...)]` attributes — they are tuned for LLM tool selection and must not be rewritten. Schemas follow the OpenAI function-calling shape `{"name", "description", "parameters": {"type":"object","properties":{...},"required":[...]}}`. Pydantic v2 models produce the JSON Schema via `model_json_schema()`; post-process to strip `"title"` and unsupported `"default"` fields (same rule as the recent `tools/gemini_provider.py` fixes from commits 232955cc/bccd3107).

### Explore (5)

| Tool | Params |
|---|---|
| `get_structure_overview` | `structural_areas_only: bool = True`, `max_depth: int = 0`, `under: str = ""` |
| `find_element` | `search_term="", under="", status="", category=""`, `search_all_fields: bool\|None = None` |
| `list_elements` | `under: str`, `category: str`, `show_full_path: bool = False` |
| `get_element_details` | `fullname: str` |
| `get_recent_changes` | `limit: int = 20`, `type: str = ""` (one of "element"/"connection"/"category" or empty) |

### Manage Elements (4)

| Tool | Params |
|---|---|
| `create_element` | `name, category, parent?, short_name?, status?, purpose?, note?, description?, user_manual?, position?` |
| `update_element` | `fullname, name?, short_name?, category?, status?, purpose?, note?, description?, user_manual?, position?` — clearable fields accept `"CLEAR"` |
| `delete_element` | `fullname` |
| `move_element` | `fullname, new_parent=""` |

### Connections (5)

| Tool | Params |
|---|---|
| `get_connections` | `category="", under="", search_term=""`, `search_all_fields: bool\|None = None` |
| `get_connection_details` | `name, source?, destination?` |
| `create_connection` | `name, category, source, destination, route?, length: Decimal\|None, purpose?, note?, description?` |
| `update_connection` | `name, source?, destination?, new_name?, category?, new_source?, new_destination?, route?, length?, purpose?, note?, description?` |
| `delete_connection` | `name, source?, destination?` |

### Categories (5)

| Tool | Params |
|---|---|
| `list_categories` | — |
| `get_by_category` | `category, under=""` |
| `create_category` | `name, parent?, short_name?, description?, is_structural_area: bool\|None = None` |
| `update_category` | `category, new_name?, new_short_name?, description?, is_structural_area: bool\|None, new_parent?` |
| `delete_category` | `category` |

### Status (4)

| Tool | Params |
|---|---|
| `list_statuses` | — |
| `create_status` | `name, status_type: str, note?` — status_type ∈ {"existing","planned","removed"} |
| `update_status` | `name, new_name?, new_status_type?, note?` |
| `delete_status` | `name` |

---

## Implementation notes for non-trivial handlers

### `create_element` — location resolution + category resolution

- Load `etree` once via the recursive CTE → build `by_fullname` dict seeded with **both** short-name fullname (`House/GF/Kitchen`) **and** long-name fullname (`House/Ground Floor/Kitchen`), preferring the short-name form on collision. See `QueryHelpers.LoadEtree` line 29 in the C# source.
- Resolve `category`: exact Name/ShortName match wins; fall back to partial match; path-match if input contains `/`. Ambiguity returns the listed error format: `"Error: category 'X' is ambiguous (N matches). Use the full path instead: 'a', 'b'."`.
- Compute `new_fullname = parent_fullname + "/" + (short_name or name)` → reject if already exists.
- Sibling uniqueness check on name and short_name within the same parent (see `check_sibling_uniqueness`).
- `sort_index = next_sort_index(parent_id)` = `MAX(sort_index)+1` within parent (or `1` if no siblings).
- All inserts in one `BEGIN IMMEDIATE` transaction.

### `move_element` — cycle detection

- Resolve `new_parent` via `by_fullname` map.
- Compute `element_prefix = fullname.rstrip("/") + "/"`.
- Reject if `new_parent.lower() == fullname.lower()` or `new_parent.lower().startswith(element_prefix.lower())`.
- Re-check segment-level sibling uniqueness under the new parent, excluding self.

### `get_structure_overview` — single-query tree

- One recursive CTE call. Optional `AND upper(et.fullname) LIKE upper(?)` for `under`.
- `is_area_category = 1` filter when `structural_areas_only=True`.
- Depth clamp (`WHERE depth <= :abs_max_depth`) when `structural_areas_only=False`.
- Default `max_depth`: **3** for building overview, **99** (effectively unlimited) when `under` is given with `structural_areas_only=False` (mirrors `StructureTools.cs:77-79`).

### CLEAR sentinel

Port of `Validate.NormalizeClear`: case-insensitive `"clear"` collapses to literal `"CLEAR"`. Every optional text field on `update_*` is **tri-state**:
- `None` → leave alone
- `"CLEAR"` → set `NULL`
- otherwise → trimmed new value

### Forbidden chars & length

```python
INVALID_CHARS            = re.compile(r'[\$\*\[\{\]\}\|\\<>\?/";:\t]')
INVALID_CHARS_CONNECTION = re.compile(r'[\*\|<>?"\t]')
```

Length limits (from C# source):
- `name` 100 (150 for connection name)
- `short_name` 50
- `purpose`, `note`, `position` 200
- `description`, `user_manual` 4000
- `route` 1000

`normalize_singleline` collapses `\r\n`/`\r`/`\n` → space (Smartconstruct doesn't support line breaks in these fields).
`normalize_multiline` normalises to `\r\n` (compatibility with Smartconstruct round-trips).

### Overwrite advisories

On update of `purpose`/`note`/`description`/`user_manual`: read old value in the same transaction; if non-empty and differs from new value (not `"CLEAR"`), append an advisory line to the success string:

```
'description' was overwritten (previous: 142 chars, started with: "The heat pump was installed...")
```

Preview logic: replace `\r\n` with ` \n `, truncate to 80 chars + `...`.

### Delete blocking rules

- `delete_element`: refuse if children exist (error: `"has N child element(s). Report this to the user and ask for explicit confirmation before removing any of them."`), or if any connection has this element as source/destination.
- `delete_category`: refuse if children categories exist or any element/connection references `category_id`.
- `delete_status`: refuse if any element has `status_id` = this.

Preserve **exact** error phrasing from the C# source.

### Handler return format

Match the C# string format byte-for-byte:
- Success: `"✓ Element 'House/GF/Kitchen/Socket' created (OID: 123)."` (substitute integer PK for the GUID OID).
- Error: `"Error: <message>"`.
- Multi-line listings use `\n` with the same indentation (`  `, `    `, etc.) as C#.

Wrap any `sqlite3.Error` into `f"Error: {ex}"` to match the C# try/catch pattern.

### Pydantic role

`models.py` defines one `BaseModel` per tool. Each field carries `Field(description=...)` lifted verbatim from the C# `[Description(...)]` attribute. Each handler does:

```python
try:
    p = CreateElementParams(**args)
except ValidationError as e:
    return f"Error: {e.errors()[0]['msg']}"
```

The same models feed `model_json_schema()` for the tool schema, so runtime validation and the advertised schema cannot drift apart.

---

## Seed data

- **Categories**: copy the tree from `/tmp/home-memory-ref/HomeMemoryMCP/SeedData/categories.json` → `seed/categories.py` as a Python list-of-dicts literal. Insert recursively (match `DbSeeder.InsertCategory`).
- **Statuses**: `[("Existing", 0), ("Planned", 1), ("Removed", 2)]`.
- **Elements**: port `SeedData/elements.json`. Insert in list order; resolve parent via a running `{fullname: id}` map.

Expected totals after seed: **112 categories**, **3 statuses**, **15 elements**.

---

## Verification

### 1. Unit tests (`tests/plugins/test_home_memory.py`)

Use in-memory SQLite via `monkeypatch.setenv("HOME_MEMORY_DB_PATH", ":memory:")` (or a `tmp_path` file).

- Seed is idempotent (run twice → still 112/3/15).
- `create_element("Socket", "Outlet", parent="House/GF/Kitchen")` → returns `✓ Element 'House/GF/Kitchen/Socket' created...`.
- `create_element("Bosch Washer", "Appliances", parent="House/Basement")` (basement absent) → resolvable parent-not-found error.
- `move_element("House/GF/Kitchen", new_parent="House/GF/Kitchen")` → self-reference error.
- `move_element("House/GF/Kitchen", new_parent="House/GF/Kitchen/Sink")` → circular-reference error.
- `delete_element("House/GF/Kitchen")` with children → "has N child element(s)" error.
- Forbidden-chars + length-limit rejection for every text field.
- CLEAR sentinel works on every clearable field of `update_element`/`update_connection`/`update_category`/`update_status`.
- `find_element(search_term="bosch", search_all_fields=True)` finds element via `note` content.
- `get_structure_overview()` returns the default tree.
- `get_structure_overview(under="House/GF", structural_areas_only=False)` returns full descendants.
- `get_recent_changes(type="element", limit=5)` returns exactly the 5 newest elements.

### 2. Plugin-loading test

Extend `tests/hermes_cli/test_plugins.py` with a test that loads `plugins/home_memory/` and asserts all 23 tools appear in `registry._tools` with `toolset == "home_memory"`.

### 3. End-to-end smoke

Start `hermes` CLI with the plugin enabled and run the three prompts from the reference README:

1. "Show me the structure of my home." → `get_structure_overview`.
2. "I have a Daikin Altherma heat pump in the utility room." → `create_category` (if missing) then `create_element`.
3. "What's in the basement?" → `get_structure_overview(under="House/Basement", structural_areas_only=False)`.

### 4. Container deployment

After `git push`, on the Unraid box (ssh `root@192.168.1.100`, container `hermes-agent`), confirm:
- `/opt/data/home-memory.db` is created on first run.
- DB persists across container restarts (`docker restart hermes-agent`).

---

## Phased roadmap

**Phase 1 — DB + seed + repository** (no tools yet)
1. Create `plugins/home_memory/` with empty `__init__.py`.
2. `seed/categories.py`, `seed/elements.py`, `seed/statuses.py` — straight ports of the JSON files.
3. `db.py` with connection factory, WAL pragmas, `init_schema()`, `seed_if_empty()`.
4. `repository.py` — low-level CRUD: `insert_element/category/status/connection`, `load_etree()`, `load_cat_tree()`, `next_sort_index(parent_id)`, `resolve_element_by_path(path)`, `resolve_category(term)`, `resolve_category_with_descendants(term)`.
5. Phase-1 smoke test: open a temp DB, `seed_if_empty()`, run `etree` CTE, assert `House/GF/Kitchen` resolves.

**Phase 2 — 23 handlers + unit tests**
1. `validate.py` with regexes and `normalize_*` helpers.
2. `query_helpers.py` — `split_parent_and_name`, `check_sibling_uniqueness`, `collect_overwrite_advisories`, `check_connection_combination_uniqueness`.
3. `models.py` — 23 Pydantic `BaseModel`s with verbatim C# descriptions.
4. `tools.py` — handlers in the order of the C# files (structure → elements → connections → categories → statuses).
5. `tests/plugins/test_home_memory.py` — all cases from the Verification section.

**Phase 3 — registry + plugin integration**
1. `registry.py` — `TOOLS = [(name, schema, handler), ...]`, `TOOL_MAP = {name: h for name, _, h in TOOLS}`, `EMOJI = "🏠"`, `CHECK_FN = lambda: True` (no required env; DB auto-provisions on first write). Schema builder post-strips `title`/unsupported `default`.
2. `__init__.py` — `register(ctx)` iterating `TOOLS`.
3. `plugin.yaml` — manifest listing all 23 tool names.
4. Extend `tests/hermes_cli/test_plugins.py`.
5. Manual smoke in `hermes` CLI against a fresh `/opt/data/home-memory.db`.

---

## Step-by-step to-do

1. `mkdir plugins/home_memory/ plugins/home_memory/seed/`; add `__init__.py` files.
2. Port `categories.json` → `seed/categories.py` (Python literal, same order).
3. Port `elements.json` → `seed/elements.py`; write `seed/statuses.py`.
4. Write `db.py` (constants, env-path resolution with `/opt/data/home-memory.db` default, `get_connection()`, `init_schema()`, `seed_if_empty()`, `execute_write(fn)` retry helper).
5. Write `repository.py` — recursive CTE helpers and CRUD.
6. Add Phase-1 smoke test; `pytest tests/plugins/test_home_memory.py::test_seed -v`.
7. Write `validate.py`; then `query_helpers.py`.
8. Write `models.py` — 23 Pydantic models with `Field(description=...)` lifted from C#.
9. Write `tools.py` — handlers grouped by file (structure, elements, connections, categories, statuses).
10. Write per-handler unit tests; `pytest tests/plugins/test_home_memory.py -v`.
11. Write `registry.py` — schema-from-Pydantic helper + `TOOLS` + `TOOL_MAP` + `CHECK_FN` + `EMOJI`.
12. Write `plugin.yaml`; wire `__init__.py` `register(ctx)`.
13. Extend `tests/hermes_cli/test_plugins.py` with a `home_memory` case.
14. Run `pytest tests/ -v` end-to-end (should stay green across the full suite).
15. Launch `hermes` CLI, run the three scripted README prompts.
16. Commit: `feat(plugins): add home_memory plugin (23 tools, sqlite-backed)`, push, then run the standard Unraid container update (per the user's deploy-workflow memory: `ssh root@192.168.1.100` → container `hermes-agent`).

---

## Open decisions

None blocking — all four were locked with the user. One soft item to preserve from the C# behaviour: when building `by_fullname` with both short-name and long-name forms, **short-name entries win on collision** (see `QueryHelpers.LoadEtree:29` in the C# source). Do not overwrite an existing key.
