"""
Unit tests for the home_memory plugin.
All tests use an in-memory SQLite database via HOME_MEMORY_DB_PATH=:memory:.
"""
import os
import sys

import pytest

# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _memory_db(monkeypatch, tmp_path):
    """Redirect the plugin to a fresh in-memory DB for each test."""
    # Each test gets a fresh file-based DB so :memory: works across modules
    db_file = str(tmp_path / "home_memory_test.db")
    monkeypatch.setenv("HOME_MEMORY_DB_PATH", db_file)

    # Reset module state so a new connection is opened
    import plugins.home_memory.db as db_mod
    db_mod.reset_for_testing()
    yield
    db_mod.reset_for_testing()


@pytest.fixture
def tools():
    from plugins.home_memory.registry import TOOL_MAP
    return TOOL_MAP


# ── Seed ──────────────────────────────────────────────────────────────────────


class TestSeed:
    def test_seed_counts(self, tools):
        from plugins.home_memory.db import get_connection
        conn = get_connection()
        assert conn.execute("SELECT COUNT(*) FROM category").fetchone()[0] == 120
        assert conn.execute("SELECT COUNT(*) FROM status").fetchone()[0] == 3
        assert conn.execute("SELECT COUNT(*) FROM element").fetchone()[0] == 15

    def test_seed_is_idempotent(self, tools):
        from plugins.home_memory.db import get_connection, _seed_if_empty
        conn = get_connection()
        _seed_if_empty()  # second call — should be a no-op
        assert conn.execute("SELECT COUNT(*) FROM category").fetchone()[0] == 120
        assert conn.execute("SELECT COUNT(*) FROM element").fetchone()[0] == 15

    def test_default_structure(self, tools):
        result = tools["get_structure_overview"]({})
        assert "House" in result
        assert "Ground Floor" in result or "GF" in result
        assert "Upper Floor" in result or "UF" in result
        assert "Garage" in result


# ── get_structure_overview ────────────────────────────────────────────────────


class TestGetStructureOverview:
    def test_structural_areas_only(self, tools):
        result = tools["get_structure_overview"]({"structural_areas_only": True})
        assert "House" in result
        assert "Total:" in result

    def test_under_filter(self, tools):
        result = tools["get_structure_overview"]({"under": "House/GF", "structural_areas_only": False})
        assert "Kitchen" in result
        assert "Garage" not in result

    def test_under_not_found(self, tools):
        result = tools["get_structure_overview"]({"under": "House/Nonexistent"})
        assert result.startswith("Error:")

    def test_long_name_alias(self, tools):
        result = tools["get_structure_overview"]({"under": "House/Ground Floor", "structural_areas_only": False})
        assert "Kitchen" in result


# ── find_element ──────────────────────────────────────────────────────────────


class TestFindElement:
    def test_no_params_error(self, tools):
        result = tools["find_element"]({})
        assert result.startswith("Error:")

    def test_search_by_name(self, tools):
        result = tools["find_element"]({"search_term": "kitchen"})
        assert "Kitchen" in result

    def test_under_filter(self, tools):
        result = tools["find_element"]({"search_term": "Hallway", "under": "House/GF"})
        assert "Hallway" in result
        # Hallway under UF should NOT appear
        assert "UF" not in result or result.count("Hallway") == 1

    def test_category_filter(self, tools):
        result = tools["find_element"]({"category": "Room"})
        assert "Kitchen" in result
        assert "Garage" not in result

    def test_status_filter_existing(self, tools):
        result = tools["find_element"]({"status": "Existing"})
        # All default elements have no status (NULL), so they're not "Existing" typed
        # Just check it doesn't error
        assert not result.startswith("Error:")

    def test_search_all_fields(self, tools):
        # Create an element with a note, then find it by note content
        tools["create_element"]({"name": "Bosch Washer", "category": "Appliances",
                                 "parent": "House/GF/Kitchen", "note": "serial number XYZ-123"})
        result = tools["find_element"]({"search_term": "XYZ-123", "search_all_fields": True})
        assert "Bosch Washer" in result

    def test_not_found(self, tools):
        result = tools["find_element"]({"search_term": "nonexistent_xyz_abc"})
        assert "No elements found" in result


# ── list_elements ─────────────────────────────────────────────────────────────


class TestListElements:
    def test_list_children(self, tools):
        result = tools["list_elements"]({"under": "House/GF"})
        assert "Kitchen" in result
        assert "Living Room" in result

    def test_top_level(self, tools):
        result = tools["list_elements"]({})
        assert "House" in result
        assert "Garage" in result

    def test_not_found(self, tools):
        result = tools["list_elements"]({"under": "House/Nonexistent"})
        assert result.startswith("Error:")


# ── get_element_details ───────────────────────────────────────────────────────


class TestGetElementDetails:
    def test_details(self, tools):
        result = tools["get_element_details"]({"fullname": "House/GF/Kitchen"})
        assert "Kitchen" in result
        assert "Category" in result

    def test_not_found(self, tools):
        result = tools["get_element_details"]({"fullname": "House/GF/Pantry"})
        assert result.startswith("Error:")

    def test_long_name(self, tools):
        result = tools["get_element_details"]({"fullname": "House/Ground Floor/Kitchen"})
        assert "Kitchen" in result


# ── create_element ────────────────────────────────────────────────────────────


class TestCreateElement:
    def test_create_success(self, tools):
        result = tools["create_element"]({"name": "Socket", "category": "Outlet", "parent": "House/GF/Kitchen"})
        assert "✓" in result
        assert "House/GF/Kitchen/Socket" in result

    def test_parent_not_found(self, tools):
        result = tools["create_element"]({"name": "Washer", "category": "Appliances", "parent": "House/Basement"})
        assert result.startswith("Error:")
        assert "parent" in result.lower() or "not found" in result.lower()

    def test_category_not_found(self, tools):
        result = tools["create_element"]({"name": "Washer", "category": "NoSuchCategory"})
        assert result.startswith("Error:")

    def test_duplicate_fullname(self, tools):
        tools["create_element"]({"name": "Socket", "category": "Outlet", "parent": "House/GF/Kitchen"})
        result = tools["create_element"]({"name": "Socket", "category": "Outlet", "parent": "House/GF/Kitchen"})
        assert result.startswith("Error:")

    def test_forbidden_chars(self, tools):
        result = tools["create_element"]({"name": "Bad/Name", "category": "Room"})
        assert result.startswith("Error:")

    def test_name_too_long(self, tools):
        result = tools["create_element"]({"name": "x" * 101, "category": "Room"})
        assert result.startswith("Error:")

    def test_with_short_name(self, tools):
        result = tools["create_element"]({"name": "Ground Floor", "category": "Floor",
                                          "parent": "Garage", "short_name": "GF2"})
        assert "✓" in result
        assert "GF2" in result

    def test_top_level(self, tools):
        result = tools["create_element"]({"name": "Pool House", "category": "House"})
        assert "✓" in result
        assert "Pool House" in result

    def test_with_status(self, tools):
        result = tools["create_element"]({"name": "Planned Room", "category": "Room",
                                          "parent": "House/GF", "status": "Planned"})
        assert "✓" in result

    def test_status_not_found(self, tools):
        result = tools["create_element"]({"name": "X", "category": "Room", "parent": "House/GF",
                                          "status": "NoSuchStatus"})
        assert result.startswith("Error:")


# ── update_element ────────────────────────────────────────────────────────────


class TestUpdateElement:
    def _create(self, tools, name="Socket", note=None):
        kwargs = {"name": name, "category": "Outlet", "parent": "House/GF/Kitchen"}
        if note:
            kwargs["note"] = note
        tools["create_element"](kwargs)

    def test_update_name(self, tools):
        self._create(tools)
        result = tools["update_element"]({"fullname": "House/GF/Kitchen/Socket", "name": "Socket-New"})
        assert "✓" in result

    def test_clear_field(self, tools):
        self._create(tools, note="temp note")
        result = tools["update_element"]({"fullname": "House/GF/Kitchen/Socket", "note": "CLEAR"})
        assert "✓" in result

    def test_overwrite_advisory(self, tools):
        self._create(tools, note="original note")
        result = tools["update_element"]({"fullname": "House/GF/Kitchen/Socket", "note": "new note"})
        assert "Advisory" in result or "overwritten" in result

    def test_not_found(self, tools):
        result = tools["update_element"]({"fullname": "House/GF/Kitchen/Nonexistent", "name": "X"})
        assert result.startswith("Error:")

    def test_no_fields(self, tools):
        self._create(tools)
        result = tools["update_element"]({"fullname": "House/GF/Kitchen/Socket"})
        assert result.startswith("Error:")

    def test_clear_sentinel_case_insensitive(self, tools):
        self._create(tools, note="something")
        result = tools["update_element"]({"fullname": "House/GF/Kitchen/Socket", "note": "clear"})
        assert "✓" in result


# ── delete_element ────────────────────────────────────────────────────────────


class TestDeleteElement:
    def test_delete_leaf(self, tools):
        tools["create_element"]({"name": "Socket", "category": "Outlet", "parent": "House/GF/Kitchen"})
        result = tools["delete_element"]({"fullname": "House/GF/Kitchen/Socket"})
        assert "✓" in result

    def test_delete_with_children(self, tools):
        # Ground Floor has children (Hallway, Kitchen, etc.)
        result = tools["delete_element"]({"fullname": "House/GF"})
        assert result.startswith("Error:")
        assert "child" in result.lower()

    def test_delete_not_found(self, tools):
        result = tools["delete_element"]({"fullname": "House/GF/Nonexistent"})
        assert result.startswith("Error:")


# ── move_element ──────────────────────────────────────────────────────────────


class TestMoveElement:
    def test_move_success(self, tools):
        tools["create_element"]({"name": "Socket", "category": "Outlet", "parent": "House/GF/Kitchen"})
        result = tools["move_element"]({"fullname": "House/GF/Kitchen/Socket", "new_parent": "House/GF/Bedroom"})
        assert "✓" in result
        assert "Bedroom" in result

    def test_move_self_reference(self, tools):
        result = tools["move_element"]({"fullname": "House/GF/Kitchen", "new_parent": "House/GF/Kitchen"})
        assert result.startswith("Error:")

    def test_move_circular(self, tools):
        result = tools["move_element"]({"fullname": "House/GF", "new_parent": "House/GF/Kitchen"})
        assert result.startswith("Error:")
        assert "descendant" in result.lower() or "circular" in result.lower() or "into" in result.lower()

    def test_move_to_top_level(self, tools):
        tools["create_element"]({"name": "Socket", "category": "Outlet", "parent": "House/GF/Kitchen"})
        result = tools["move_element"]({"fullname": "House/GF/Kitchen/Socket", "new_parent": ""})
        assert "✓" in result


# ── Connections ───────────────────────────────────────────────────────────────


class TestConnections:
    def _create_elements(self, tools):
        tools["create_element"]({"name": "Panel", "category": "Distribution Board", "parent": "House/GF/Kitchen"})
        tools["create_element"]({"name": "Fridge", "category": "Appliances", "parent": "House/GF/Kitchen"})

    def test_create_connection(self, tools):
        self._create_elements(tools)
        result = tools["create_connection"]({
            "name": "L1", "category": "Cable",
            "source": "House/GF/Kitchen/Panel",
            "destination": "House/GF/Kitchen/Fridge",
        })
        assert "✓" in result

    def test_get_connections_by_category(self, tools):
        self._create_elements(tools)
        tools["create_connection"]({"name": "L1", "category": "Cable",
                                    "source": "House/GF/Kitchen/Panel",
                                    "destination": "House/GF/Kitchen/Fridge"})
        result = tools["get_connections"]({"category": "Cable"})
        assert "L1" in result

    def test_get_connection_details(self, tools):
        self._create_elements(tools)
        tools["create_connection"]({"name": "L1", "category": "Cable",
                                    "source": "House/GF/Kitchen/Panel",
                                    "destination": "House/GF/Kitchen/Fridge"})
        result = tools["get_connection_details"]({"name": "L1"})
        assert "Panel" in result
        assert "Fridge" in result

    def test_delete_connection(self, tools):
        self._create_elements(tools)
        tools["create_connection"]({"name": "L1", "category": "Cable",
                                    "source": "House/GF/Kitchen/Panel",
                                    "destination": "House/GF/Kitchen/Fridge"})
        result = tools["delete_connection"]({"name": "L1"})
        assert "✓" in result

    def test_no_params_error(self, tools):
        result = tools["get_connections"]({})
        assert result.startswith("Error:")

    def test_element_blocked_by_connection(self, tools):
        self._create_elements(tools)
        tools["create_connection"]({"name": "L1", "category": "Cable",
                                    "source": "House/GF/Kitchen/Panel",
                                    "destination": "House/GF/Kitchen/Fridge"})
        result = tools["delete_element"]({"fullname": "House/GF/Kitchen/Panel"})
        assert result.startswith("Error:")


# ── Categories ────────────────────────────────────────────────────────────────


class TestCategories:
    def test_list_categories(self, tools):
        result = tools["list_categories"]({})
        assert "Electrical" in result
        assert "Household" in result
        assert "120" in result

    def test_create_category(self, tools):
        result = tools["create_category"]({"name": "Heat Pump", "parent": "HVAC"})
        assert "✓" in result

    def test_delete_category_with_children(self, tools):
        result = tools["delete_category"]({"category": "Electrical"})
        assert result.startswith("Error:")

    def test_get_by_category(self, tools):
        result = tools["get_by_category"]({"category": "Room"})
        assert "Kitchen" in result

    def test_ambiguous_category(self, tools):
        result = tools["find_element"]({"category": "Other"})
        # "Other" appears many times in the tree; should return ambiguous error
        assert result.startswith("Error:") and "ambiguous" in result

    def test_update_category(self, tools):
        tools["create_category"]({"name": "TestCat", "parent": "Tools"})
        result = tools["update_category"]({"category": "Tools/TestCat", "new_name": "RenamedCat"})
        assert "✓" in result


# ── Statuses ──────────────────────────────────────────────────────────────────


class TestStatuses:
    def test_list_statuses(self, tools):
        result = tools["list_statuses"]({})
        assert "Existing" in result
        assert "Planned" in result
        assert "Removed" in result

    def test_create_status(self, tools):
        result = tools["create_status"]({"name": "Under Construction", "status_type": "planned"})
        assert "✓" in result

    def test_duplicate_status(self, tools):
        result = tools["create_status"]({"name": "Existing", "status_type": "existing"})
        assert result.startswith("Error:")

    def test_delete_unused_status(self, tools):
        tools["create_status"]({"name": "Temp", "status_type": "existing"})
        result = tools["delete_status"]({"name": "Temp"})
        assert "✓" in result

    def test_delete_used_status(self, tools):
        tools["create_element"]({"name": "PlannedSocket", "category": "Outlet",
                                 "parent": "House/GF/Kitchen", "status": "Planned"})
        result = tools["delete_status"]({"name": "Planned"})
        assert result.startswith("Error:")
        assert "element" in result.lower()

    def test_update_status(self, tools):
        result = tools["update_status"]({"name": "Existing", "new_name": "Installed"})
        assert "✓" in result
        tools["update_status"]({"name": "Installed", "new_name": "Existing"})

    def test_bad_status_type(self, tools):
        result = tools["create_status"]({"name": "X", "status_type": "invalid"})
        assert result.startswith("Error:")


# ── get_recent_changes ────────────────────────────────────────────────────────


class TestGetRecentChanges:
    def test_all_types(self, tools):
        result = tools["get_recent_changes"]({"limit": 5})
        assert "element" in result or "category" in result

    def test_type_filter(self, tools):
        result = tools["get_recent_changes"]({"type": "element", "limit": 5})
        assert "element" in result

    def test_bad_type(self, tools):
        result = tools["get_recent_changes"]({"type": "badtype"})
        assert result.startswith("Error:")

    def test_limit(self, tools):
        result = tools["get_recent_changes"]({"limit": 3})
        # Should show at most 3 entries after header
        lines = [l for l in result.splitlines() if "[created]" in l or "[updated]" in l]
        assert len(lines) <= 3


# ── Validation ────────────────────────────────────────────────────────────────


class TestValidation:
    def test_forbidden_chars_in_element_name(self, tools):
        for char in ["$", "*", "[", "{", "|", "\\", "<", ">", "?", '"', ";", ":", "\t"]:
            result = tools["create_element"]({"name": f"bad{char}name", "category": "Room"})
            assert result.startswith("Error:"), f"Expected error for char {char!r}"

    def test_forbidden_chars_connection(self, tools):
        tools["create_element"]({"name": "A", "category": "Outlet", "parent": "House/GF/Kitchen"})
        tools["create_element"]({"name": "B", "category": "Appliances", "parent": "House/GF/Kitchen"})
        for char in ["*", "|", "<", ">", "?", '"', "\t"]:
            result = tools["create_connection"]({
                "name": f"bad{char}name", "category": "Cable",
                "source": "House/GF/Kitchen/A", "destination": "House/GF/Kitchen/B",
            })
            assert result.startswith("Error:"), f"Expected error for char {char!r}"

    def test_note_length(self, tools):
        result = tools["create_element"]({"name": "X", "category": "Room",
                                          "note": "x" * 201})
        assert result.startswith("Error:")

    def test_description_length(self, tools):
        result = tools["create_element"]({"name": "X", "category": "Room",
                                          "description": "x" * 4001})
        assert result.startswith("Error:")
