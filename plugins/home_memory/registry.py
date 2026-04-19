"""
Builds TOOLS = [(name, schema, handler), ...] from Pydantic models + tool descriptions.
Post-processes schemas to strip 'title' and unsupported 'default' keys (same rule as
tools/gemini_provider.py fixes in commits 232955cc/bccd3107).
"""
from __future__ import annotations

import copy
from typing import Any

from pydantic import BaseModel

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
from .tools import (
    handle_get_structure_overview, handle_find_element, handle_list_elements,
    handle_get_element_details, handle_get_recent_changes,
    handle_create_element, handle_update_element, handle_delete_element, handle_move_element,
    handle_get_connections, handle_get_connection_details, handle_create_connection,
    handle_update_connection, handle_delete_connection,
    handle_list_categories, handle_get_by_category, handle_create_category,
    handle_update_category, handle_delete_category,
    handle_list_statuses, handle_create_status, handle_update_status, handle_delete_status,
)

EMOJI = "🏠"
CHECK_FN = lambda: True  # no required env vars; DB auto-provisions on first write


# ── Tool descriptions (verbatim from C# [Description(...)] attributes) ────────

_DESCRIPTIONS = {
    "get_structure_overview": (
        "Entry point: shows the building skeleton. "
        "Elements are physical items (installed equipment, appliances, furniture, fixtures, tools, "
        "structural components) organised in a location hierarchy (building → floor → room → wall → item). "
        "Default (structural_areas_only=true): only structural area elements – i.e. elements whose category is marked "
        "as structural area category (building, floor, room, outdoor area, etc.). "
        "Structural area elements are navigable containers, not devices or components. "
        "Returns ~60 elements, ideal as a first overview or to find the right path for "
        "subsequent find_element/list_elements calls. "
        "With structural_areas_only=false and 'under': preferred way to browse all content of a specific area "
        "(e.g. 'What is in the basement?') – no result limit, full hierarchical tree, "
        "more efficient than multiple find_element calls. "
        "With 'under': restrict tree to a sub-path, e.g. 'House/GF/Office' – "
        "shows only elements within that area, depth relative to that root. "
        "Elements with status Planned or Removed are marked with their status name."
    ),
    "find_element": (
        "Searches ALL elements by name or full path (partial text, case-insensitive). Up to 100 results. "
        "Elements are physical items at a location: installed equipment (socket, boiler, radiator), "
        "appliances (washing machine, fridge), furniture (sofa, wardrobe), fixtures, tools, "
        "and structural area containers (building, floor, room, wall). "
        "With 'status': filter by status type name (Existing / Planned / Removed – language-independent) "
        "or by status name (exact match first, partial match as fallback). "
        "Call list_statuses for available status names. "
        "With 'under': restrict search to a sub-tree – more efficient and token-saving "
        "than a global search. At least one of search_term, under, status, or category must be provided. "
        "With 'search_all_fields=true': also searches in Purpose, Note, Description, UserManual, and Position – "
        "useful when a keyword appears in a field but not in the element name. "
        "Not suitable for browsing all content of an area – for that use get_structure_overview(under=..., structural_areas_only=false) which returns a complete tree without a result limit. "
        "Note: physical lines (pipes, cables, conduits) are often documented as connections, not elements – "
        "also call get_connections with search_term when searching for cables, pipes, or conduits."
    ),
    "list_elements": (
        "Lists the direct child elements of an element – both structural area elements "
        "(rooms, areas) and devices/components. Shows exactly one level of children. "
        "Best for: 'What is in the kitchen?' or 'What rooms are on the ground floor?' "
        "when the exact parent path is already known. "
        "Unlike get_structure_overview (hierarchical tree) or find_element (search by name), "
        "this returns a flat list of immediate children only. "
        "Elements with status Planned or Removed are marked with their status name."
    ),
    "get_element_details": (
        "Full details of a single element: properties (category, status, part type, "
        "purpose, description, user manual), direct child elements, "
        "and all incoming and outgoing connections (physical lines: pipes, cables, ducts). "
        "Use when the exact path is known and you need details or connections. "
        "An element is any physical item in the building: installed equipment, appliance, "
        "furniture, fixture, tool, or structural container (room, floor, etc.)."
    ),
    "get_recent_changes": (
        "Shows recently created or updated items across elements, connections, and categories – "
        "newest first. Useful after a documentation session to review what was added or changed. "
        "With 'type': restrict to 'element', 'connection', or 'category'."
    ),
    "create_element": (
        "Creates a new element. An element is any physical item in the building: "
        "installed equipment (socket, boiler, radiator, circuit breaker), appliances (washing machine, fridge), "
        "furniture (sofa, wardrobe), fixtures, tools, or structural area containers (room, wall, floor). "
        "Structural area elements (room, floor, ceiling, outdoor area, garage, etc.) are also created with this tool – "
        "simply choose a category marked [structural area] in list_categories (is_structural_area=true). "
        "Required fields: name, category. "
        "IMPORTANT – category workflow: call list_categories first to find the best matching category "
        "by name or context (e.g. 'Heating' for a boiler, 'Electrical' for a socket, 'Room' for a room). "
        "If no suitable category exists, call create_category first, then use the new category here. "
        "IMPORTANT – parent path: if the parent element's full path is not known exactly, "
        "call get_structure_overview or find_element first to find the correct path. Do not guess paths. "
        "Optional: parent (full path of the parent element), "
        "short_name, status, purpose, note, description, user_manual, position. "
        "Forbidden characters in name/short_name: $*[{}|\\<>?\"/;: and tab."
    ),
    "update_element": (
        "Updates an existing element. Required: fullname (exact full path – use find_element to look it up if unsure). "
        "Only provided fields are changed; omitted fields stay untouched. "
        "Pass 'CLEAR' to empty an optional field (status, purpose, note, description, user_manual, position, short_name). "
        "When changing category: call list_categories first. When changing status: call list_statuses first. "
        "CAUTION: changing 'name' or 'short_name' changes the full path of this element and all its descendants. "
        "IMPORTANT: ALWAYS call get_element_details before updating purpose, description, note, or user_manual. "
        "If the field already has content, inform the user and ask whether to replace or extend. "
        "Forbidden characters in name/short_name: $*[{}|\\<>?\"/;: and tab."
    ),
    "delete_element": (
        "Permanently deletes an element. "
        "Blocked if the element has child elements or is referenced by connections. "
        "Required: fullname (exact full path). Use find_element to look it up if unsure. "
        "This action cannot be undone."
    ),
    "move_element": (
        "Moves an element to a different parent (or to the top level). "
        "Required: fullname. Optional: new_parent (full path of new parent; empty = top level). "
        "The element keeps its name and short name; only its position in the hierarchy changes. "
        "Cycle detection: cannot move an element into its own subtree."
    ),
    "get_connections": (
        "All connections of a category, grouped by source element. "
        "A connection is a physical line (pipe, cable, duct, conduit) running from one element to another – "
        "it has a source element, a destination element, an optional route description, and an optional length. "
        "Examples: get_connections('Pipe') → all pipelines; "
        "get_connections('Cable', under='House/GF') → all cables on the ground floor. "
        "Without category: all connections under 'under'. "
        "Category: partial name (e.g. 'Cable'), full path (e.g. 'Electrical/Cable'), or short name – includes all subcategories. "
        "If the exact category name is uncertain, use search_term first – "
        "results show the category of each match, which you can then pass as the category parameter. "
        "search_term filters by connection name (partial, case-insensitive) – "
        "use this to find connections by keyword, e.g. search_term='conduit'. "
        "With search_all_fields=true, search_term also matches Route, Purpose, Note, and Description. "
        "Returns up to 100 connections; refine with category, under, or search_term for complete results."
    ),
    "get_connection_details": (
        "Full details of a single connection: category, source, destination, route, length, "
        "purpose, note, description. "
        "Use name (partial match) to find the connection; add source/destination to disambiguate."
    ),
    "create_connection": (
        "Creates a new connection (physical line: cable, pipe, duct, conduit). "
        "Required: name, category, source (full path), destination (full path). "
        "Optional: route, length (metres), purpose, note, description. "
        "The combination of name+category+source+destination must be unique. "
        "Forbidden characters in name: * | < > ? \" or tab."
    ),
    "update_connection": (
        "Updates an existing connection. "
        "Required: name (partial match; add source/destination to disambiguate). "
        "Only provided fields are changed. Pass 'CLEAR' to empty route, purpose, note, or description. "
        "Use new_source / new_destination to re-route the connection."
    ),
    "delete_connection": (
        "Permanently deletes a connection. "
        "Required: name (partial match; add source/destination to disambiguate). "
        "This action cannot be undone."
    ),
    "list_categories": (
        "Lists all object categories with element/connection counts. "
        "Categories classify both elements (physical items: equipment, furniture, fixtures) "
        "and connections (physical lines: pipes, cables, ducts). "
        "IMPORTANT: Call this before create_element or create_connection to pick the right category. "
        "If no suitable category exists, use create_category first. "
        "Structural area categories (Building, Floor, Room, ...) are marked with [structural area]. "
        "When a category has a short name, its path segment uses that short name "
        "(e.g. 'Photovoltaics' → 'PV', so its children appear as 'Electrical/PV/Solar Panel')."
    ),
    "get_by_category": (
        "Lists all elements in a category (and all its subcategories). "
        "Use for a complete inventory of a category, e.g. 'show all appliances' or 'list all circuit breakers'. "
        "With 'under': restrict to elements under a specific path."
    ),
    "create_category": (
        "Creates a new category. "
        "Required: name. Optional: parent (full path of parent category), short_name, description, is_structural_area. "
        "Categories classify both elements and connections. "
        "Structural area categories are navigable containers (rooms, floors, buildings). "
        "Forbidden characters in name/short_name: $*[{}|\\<>?\"/;: and tab."
    ),
    "update_category": (
        "Updates an existing category: rename, change short name, description, parent, or structural area flag. "
        "Required: category (name or path to find it). "
        "Pass 'CLEAR' for new_short_name or description to remove them. "
        "Pass 'CLEAR' or empty string for new_parent to make the category top-level. "
        "Forbidden characters in new_name/new_short_name: $*[{}|\\<>?\"/;: and tab."
    ),
    "delete_category": (
        "Permanently deletes a category. "
        "Blocked if it has child categories or is referenced by any element or connection. "
        "Required: category (name or path). "
        "This action cannot be undone."
    ),
    "list_statuses": (
        "Lists all available statuses with element counts, grouped by status type. "
        "Status types: Existing (0), Planned (1), Removed (2). "
        "Use status names from this list in create_element, update_element, and find_element. "
        "create_element and update_element require the exact status name (case-insensitive). "
        "find_element also accepts partial name matches. "
        "If no suitable status exists, use create_status."
    ),
    "create_status": (
        "Creates a new status value. "
        "Required: name, status_type ('existing', 'planned', or 'removed'). "
        "Optional: note (short description of what this status means). "
        "Name must be globally unique. "
        "Forbidden characters: $*[{}|\\<>?\"/;: and tab. "
        "After creating, use the name in create_element or update_element."
    ),
    "update_status": (
        "Updates an existing status: rename, change type, or update the note. "
        "Required: name (current name to find the status). "
        "Optional: new_name, new_status_type ('existing', 'planned', or 'removed'), "
        "note (CLEAR to remove). "
        "At least one optional field must be provided. "
        "Forbidden characters in new_name: $*[{}|\\<>?\"/;: and tab."
    ),
    "delete_status": (
        "Permanently deletes a status value. "
        "Required: name (current status name, case-insensitive). "
        "Blocked if any element references this status – reassign or remove their status first "
        "(call find_element with that status name to locate them). "
        "If the status name is not known, call list_statuses first. "
        "Note: the three default statuses (Existing, Planned, Removed) should rarely be deleted."
    ),
}


def _strip_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Remove 'title' and 'default' keys recursively (Gemini/OpenAI compat)."""
    schema = copy.deepcopy(schema)

    def _clean(obj: Any):
        if isinstance(obj, dict):
            obj.pop("title", None)
            obj.pop("default", None)
            for v in obj.values():
                _clean(v)
        elif isinstance(obj, list):
            for item in obj:
                _clean(item)

    _clean(schema)
    return schema


def _build_schema(tool_name: str, model: type[BaseModel]) -> dict[str, Any]:
    raw = model.model_json_schema()
    params = _strip_schema(raw)
    # Remove top-level title
    params.pop("title", None)
    return {
        "name": tool_name,
        "description": _DESCRIPTIONS[tool_name],
        "parameters": params,
    }


# ── The 23 tools ──────────────────────────────────────────────────────────────

TOOLS: list[tuple[str, dict, Any]] = [
    # Explore
    ("get_structure_overview", _build_schema("get_structure_overview", GetStructureOverviewParams), handle_get_structure_overview),
    ("find_element",           _build_schema("find_element",           FindElementParams),           handle_find_element),
    ("list_elements",          _build_schema("list_elements",          ListElementsParams),          handle_list_elements),
    ("get_element_details",    _build_schema("get_element_details",    GetElementDetailsParams),    handle_get_element_details),
    ("get_recent_changes",     _build_schema("get_recent_changes",     GetRecentChangesParams),     handle_get_recent_changes),
    # Manage Elements
    ("create_element",         _build_schema("create_element",         CreateElementParams),         handle_create_element),
    ("update_element",         _build_schema("update_element",         UpdateElementParams),         handle_update_element),
    ("delete_element",         _build_schema("delete_element",         DeleteElementParams),         handle_delete_element),
    ("move_element",           _build_schema("move_element",           MoveElementParams),           handle_move_element),
    # Connections
    ("get_connections",        _build_schema("get_connections",        GetConnectionsParams),        handle_get_connections),
    ("get_connection_details", _build_schema("get_connection_details", GetConnectionDetailsParams), handle_get_connection_details),
    ("create_connection",      _build_schema("create_connection",      CreateConnectionParams),      handle_create_connection),
    ("update_connection",      _build_schema("update_connection",      UpdateConnectionParams),      handle_update_connection),
    ("delete_connection",      _build_schema("delete_connection",      DeleteConnectionParams),      handle_delete_connection),
    # Categories
    ("list_categories",        _build_schema("list_categories",        ListCategoriesParams),        handle_list_categories),
    ("get_by_category",        _build_schema("get_by_category",        GetByCategoryParams),         handle_get_by_category),
    ("create_category",        _build_schema("create_category",        CreateCategoryParams),        handle_create_category),
    ("update_category",        _build_schema("update_category",        UpdateCategoryParams),        handle_update_category),
    ("delete_category",        _build_schema("delete_category",        DeleteCategoryParams),        handle_delete_category),
    # Status
    ("list_statuses",          _build_schema("list_statuses",          ListStatusesParams),          handle_list_statuses),
    ("create_status",          _build_schema("create_status",          CreateStatusParams),          handle_create_status),
    ("update_status",          _build_schema("update_status",          UpdateStatusParams),          handle_update_status),
    ("delete_status",          _build_schema("delete_status",          DeleteStatusParams),          handle_delete_status),
]

TOOL_MAP: dict[str, Any] = {name: handler for name, _, handler in TOOLS}
