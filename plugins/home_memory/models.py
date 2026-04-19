"""
Pydantic v2 input models — one per tool.
Field descriptions are verbatim from the C# [Description(...)] attributes.
"""
from __future__ import annotations

from pydantic import BaseModel, Field


# ── Explore ──────────────────────────────────────────────────────────────────

class GetStructureOverviewParams(BaseModel):
    structural_areas_only: bool = Field(
        True,
        description="Show only structural area elements (structural area category). Default: true.",
    )
    max_depth: int = Field(
        0,
        description="Maximum depth (relative to 'under' if specified). Default: auto – 3 for building overview; unlimited (full tree) when browsing area content (under + structural_areas_only=false).",
    )
    under: str = Field(
        "",
        description="Restrict tree to this sub-path, e.g. 'House/GF/Office'. Empty = entire building.",
    )


class FindElementParams(BaseModel):
    search_term: str = Field(
        "",
        description="Search term, e.g. 'socket'. Empty = all elements (only useful with under, status, or category).",
    )
    under: str = Field(
        "",
        description="Path filter: only elements below this path, e.g. 'House/FF' or 'House/GF/Office'.",
    )
    status: str = Field(
        "",
        description="Status filter: 'Existing'/'Planned'/'Removed' for type-based filter; otherwise exact name match, partial match as fallback. Call list_statuses for names.",
    )
    category: str = Field(
        "",
        description="Category filter: exact name or short name (e.g. 'Socket'), full path with '/' (e.g. 'Electrical/Lighting'). Partial match as fallback; if ambiguous, an error lists full paths. Includes all subcategories.",
    )
    search_all_fields: bool | None = Field(
        None,
        description="Also search in Purpose, Note, Description, UserManual, and Position. Default: false (name/path only).",
    )


class ListElementsParams(BaseModel):
    under: str = Field(
        "",
        description="Full name of the parent element (e.g. 'House/GF/Kitchen'). Empty = top-level.",
    )
    category: str = Field(
        "",
        description="Filter by category (optional).",
    )
    show_full_path: bool = Field(
        False,
        description="Show full path instead of just the element name.",
    )


class GetElementDetailsParams(BaseModel):
    fullname: str = Field(
        description="Full name path, e.g. 'House/GF/Kitchen/South-Wall/Socket'",
    )


class GetRecentChangesParams(BaseModel):
    limit: int = Field(
        20,
        description="Maximum number of results (1–200). Default: 20.",
    )
    type: str = Field(
        "",
        description="Restrict to item type: 'element', 'connection', or 'category'. Empty = all.",
    )


# ── Manage Elements ───────────────────────────────────────────────────────────

class CreateElementParams(BaseModel):
    name: str = Field(description="Element name, e.g. 'Socket left', 'Boiler', 'Sofa'")
    category: str = Field(
        description="Object category: name or short name, e.g. 'Electrical', 'Heating', 'Furniture'. Required!"
    )
    parent: str | None = Field(
        None,
        description="Full path of the parent element, e.g. 'House/GF/Kitchen/South-Wall'. Empty = top-level.",
    )
    short_name: str | None = Field(
        None,
        description="Short name (optional), e.g. 'W-SW' for 'West-Southwest Wall'",
    )
    status: str | None = Field(
        None,
        description="Status name (optional). Most elements need no status – omit for normal existing items. Only set when the user explicitly mentions a status like 'planned' or 'removed'. Call list_statuses for options.",
    )
    purpose: str | None = Field(
        None,
        description="Intended use, when not self-evident from the name (optional). Only fill with information the user explicitly provided.",
    )
    note: str | None = Field(
        None,
        description="Temporary note or to-do during planning/construction – not for permanent records (optional). Only fill with information the user explicitly provided.",
    )
    description: str | None = Field(
        None,
        description="Permanent technical information for professionals: installation specifics, maintenance history, test results, purchase info — anything not already covered by other fields (optional). Only fill with information the user explicitly provided — do not generate or infer.",
    )
    user_manual: str | None = Field(
        None,
        description="User-facing information: operating instructions, feature overview, maintenance schedule (what/when/how), troubleshooting tips (optional). Only fill with information the user explicitly provided.",
    )
    position: str | None = Field(
        None,
        description="Position within the element (optional)",
    )


class UpdateElementParams(BaseModel):
    fullname: str = Field(
        description="Full name of the element, e.g. 'House/GF/Kitchen/South-Wall/Socket'"
    )
    name: str | None = Field(None, description="New name (optional). Changes the full name!")
    short_name: str | None = Field(
        None,
        description="New short name (optional, 'CLEAR' to remove). Changes the full name!",
    )
    category: str | None = Field(
        None,
        description="New category: name or short name (cannot be cleared – required field)",
    )
    status: str | None = Field(
        None,
        description="New status name (optional). Set only when user explicitly mentions a status (e.g. 'Planned', 'Removed'). 'CLEAR' removes a previously set status. Call list_statuses for options.",
    )
    purpose: str | None = Field(
        None,
        description="Intended use, when not already self-evident from the name ('CLEAR' to remove)",
    )
    note: str | None = Field(
        None,
        description="Temporary note or to-do – use during planning/construction for things to address later. Not for permanent records ('CLEAR' to remove)",
    )
    description: str | None = Field(
        None,
        description="Permanent technical information for professionals: installation specifics, maintenance history, test results, purchase info — anything not already covered by other fields ('CLEAR' to remove)",
    )
    user_manual: str | None = Field(
        None,
        description="User-facing information: operating instructions, feature overview, maintenance schedule (what/when/how), troubleshooting tips ('CLEAR' to remove)",
    )
    position: str | None = Field(
        None,
        description="Position ('CLEAR' to remove)",
    )


class DeleteElementParams(BaseModel):
    fullname: str = Field(
        description="Full name of the element to delete, e.g. 'House/GF/Kitchen/Socket'"
    )


class MoveElementParams(BaseModel):
    fullname: str = Field(
        description="Full name of the element to move, e.g. 'House/GF/Kitchen/Socket'"
    )
    new_parent: str = Field(
        "",
        description="Full name of the new parent element. Empty = move to top level.",
    )


# ── Connections ───────────────────────────────────────────────────────────────

class GetConnectionsParams(BaseModel):
    category: str = Field(
        "",
        description="Connection category: exact name or short name (e.g. 'Cable'), full path with '/' (e.g. 'Electrical/Cable'). Partial match as fallback; if ambiguous, an error lists full paths. Includes all subcategories.",
    )
    under: str = Field(
        "",
        description="Spatial filter: connections whose source or destination is under this path.",
    )
    search_term: str = Field(
        "",
        description="Filter by connection name (partial match, case-insensitive), e.g. 'conduit' or 'leerrohr'.",
    )
    search_all_fields: bool | None = Field(
        None,
        description="Also search in Route, Purpose, Note, and Description. Default: false (name only).",
    )


class GetConnectionDetailsParams(BaseModel):
    name: str = Field(description="Connection name (partial match allowed, case-insensitive).")
    source: str | None = Field(
        None,
        description="Source element full path to disambiguate when multiple connections share the name.",
    )
    destination: str | None = Field(
        None,
        description="Destination element full path to disambiguate.",
    )


class CreateConnectionParams(BaseModel):
    name: str = Field(description="Connection name, e.g. 'Circuit L1', 'Hot Water Supply'")
    category: str = Field(
        description="Connection category: name or short name, e.g. 'Cable', 'Pipe'. Required!"
    )
    source: str = Field(
        description="Source element full path, e.g. 'House/GF/Kitchen/Socket'"
    )
    destination: str = Field(
        description="Destination element full path, e.g. 'House/GF/Kitchen/Dishwasher'"
    )
    route: str | None = Field(
        None,
        description="Route description: how the line runs (optional), e.g. 'along south wall, through junction box'",
    )
    length: float | None = Field(
        None,
        description="Length in metres (optional)",
    )
    purpose: str | None = Field(
        None,
        description="Intended use of this connection (optional)",
    )
    note: str | None = Field(
        None,
        description="Temporary note or to-do (optional)",
    )
    description: str | None = Field(
        None,
        description="Technical details, installation history (optional)",
    )


class UpdateConnectionParams(BaseModel):
    name: str = Field(description="Current connection name (partial match, case-insensitive).")
    source: str | None = Field(None, description="Source element path to disambiguate.")
    destination: str | None = Field(None, description="Destination element path to disambiguate.")
    new_name: str | None = Field(None, description="New name (optional).")
    category: str | None = Field(None, description="New category (optional).")
    new_source: str | None = Field(None, description="New source element path (optional).")
    new_destination: str | None = Field(None, description="New destination element path (optional).")
    route: str | None = Field(None, description="New route description ('CLEAR' to remove).")
    length: float | None = Field(None, description="New length in metres.")
    purpose: str | None = Field(None, description="New purpose ('CLEAR' to remove).")
    note: str | None = Field(None, description="New note ('CLEAR' to remove).")
    description: str | None = Field(None, description="New description ('CLEAR' to remove).")


class DeleteConnectionParams(BaseModel):
    name: str = Field(description="Connection name (partial match, case-insensitive).")
    source: str | None = Field(None, description="Source element path to disambiguate.")
    destination: str | None = Field(None, description="Destination element path to disambiguate.")


# ── Categories ────────────────────────────────────────────────────────────────

class ListCategoriesParams(BaseModel):
    pass


class GetByCategoryParams(BaseModel):
    category: str = Field(
        description="Category name or short name. Includes all subcategories."
    )
    under: str = Field(
        "",
        description="Restrict to elements under this path (optional).",
    )


class CreateCategoryParams(BaseModel):
    name: str = Field(description="Category name, e.g. 'Heat Pump', 'Irrigation Valve'")
    parent: str | None = Field(
        None,
        description="Parent category full path (optional), e.g. 'Electrical'. Empty = top-level.",
    )
    short_name: str | None = Field(
        None,
        description="Short name (optional), e.g. 'HP'",
    )
    description: str | None = Field(
        None,
        description="Description of what this category covers (optional)",
    )
    is_structural_area: bool | None = Field(
        None,
        description="Mark as structural area category (room, floor, building, etc.). Default: false.",
    )


class UpdateCategoryParams(BaseModel):
    category: str = Field(
        description="Current category name or path to find the category."
    )
    new_name: str | None = Field(None, description="New name (optional).")
    new_short_name: str | None = Field(
        None, description="New short name (optional, 'CLEAR' to remove)."
    )
    description: str | None = Field(
        None, description="New description ('CLEAR' to remove)."
    )
    is_structural_area: bool | None = Field(
        None, description="Change structural area flag (optional)."
    )
    new_parent: str | None = Field(
        None,
        description="New parent category path (optional, 'CLEAR' to make top-level).",
    )


class DeleteCategoryParams(BaseModel):
    category: str = Field(
        description="Category name or path to delete."
    )


# ── Status ────────────────────────────────────────────────────────────────────

class ListStatusesParams(BaseModel):
    pass


class CreateStatusParams(BaseModel):
    name: str = Field(
        description="Status name, e.g. 'Existing', 'Under Construction', 'Decommissioned'"
    )
    status_type: str = Field(
        description="Status type: 'existing' (present in the building), 'planned' (not yet built), 'removed' (decommissioned)"
    )
    note: str | None = Field(
        None,
        description="Short note describing when to use this status (optional)",
    )


class UpdateStatusParams(BaseModel):
    name: str = Field(
        description="Current name of the status to update (case-insensitive)."
    )
    new_name: str | None = Field(None, description="New name (optional).")
    new_status_type: str | None = Field(
        None,
        description="New status type: 'existing', 'planned', or 'removed' (optional).",
    )
    note: str | None = Field(None, description="New note (optional). Use 'CLEAR' to remove.")


class DeleteStatusParams(BaseModel):
    name: str = Field(
        description="Name of the status to delete (case-insensitive). Call list_statuses if unsure."
    )
