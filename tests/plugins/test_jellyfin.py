import json
import os
from unittest.mock import patch, MagicMock, AsyncMock

import pytest


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in ("JELLYFIN_API_KEY", "JELLYFIN_USER", "JELLYFIN_PASSWORD",
                "JELLYFIN_URL", "JELLYFIN_USER_ID"):
        monkeypatch.delenv(key, raising=False)
    from plugins.jellyfin import jellyfin_client as mod
    with mod._cache_lock:
        mod._cached_token = ""
        mod._cached_user_id = ""
    yield
    with mod._cache_lock:
        mod._cached_token = ""
        mod._cached_user_id = ""


def _get_module():
    from plugins.jellyfin import jellyfin_client as mod
    return mod


class TestSchemas:
    def test_all_tools_have_valid_schemas(self):
        mod = _get_module()
        assert len(mod.JELLYFIN_TOOLS) == 15
        for name, schema, handler in mod.JELLYFIN_TOOLS:
            assert schema["name"] == name
            assert "description" in schema
            assert "parameters" in schema
            assert schema["parameters"]["type"] == "object"
            assert callable(handler)

    def test_all_movies_schema_no_cross_tool_reference(self):
        mod = _get_module()
        desc = mod.JELLYFIN_ALL_MOVIES_SCHEMA["description"]
        assert "jellyfin_search" not in desc


class TestAvailability:
    def test_unavailable_without_credentials(self):
        mod = _get_module()
        assert mod._check_jellyfin_available() is False

    def test_available_with_api_key(self, monkeypatch):
        monkeypatch.setenv("JELLYFIN_API_KEY", "test-key")
        mod = _get_module()
        assert mod._check_jellyfin_available() is True

    def test_available_with_user_password(self, monkeypatch):
        monkeypatch.setenv("JELLYFIN_USER", "admin")
        monkeypatch.setenv("JELLYFIN_PASSWORD", "pass")
        mod = _get_module()
        assert mod._check_jellyfin_available() is True

    def test_unavailable_with_only_user(self, monkeypatch):
        monkeypatch.setenv("JELLYFIN_USER", "admin")
        mod = _get_module()
        assert mod._check_jellyfin_available() is False


class TestSafePathSegment:
    def test_valid_guid(self):
        mod = _get_module()
        assert mod._safe_path_segment("a1b2c3d4-e5f6-7890-abcd-ef1234567890") == "a1b2c3d4-e5f6-7890-abcd-ef1234567890"

    def test_rejects_slash(self):
        mod = _get_module()
        with pytest.raises(ValueError, match="Invalid ID"):
            mod._safe_path_segment("foo/bar")

    def test_rejects_backslash(self):
        mod = _get_module()
        with pytest.raises(ValueError, match="Invalid ID"):
            mod._safe_path_segment("foo\\bar")

    def test_rejects_dotdot(self):
        mod = _get_module()
        with pytest.raises(ValueError, match="Invalid ID"):
            mod._safe_path_segment("..")

    def test_no_value_leak_in_error(self):
        mod = _get_module()
        with pytest.raises(ValueError) as exc_info:
            mod._safe_path_segment("secretslash/injected")
        assert "secretslash" not in str(exc_info.value)

    def test_plain_string_ok(self):
        mod = _get_module()
        assert mod._safe_path_segment("12345") == "12345"


class TestGetHeaders:
    def test_uses_explicit_api_key(self):
        mod = _get_module()
        headers = mod._get_headers("my-key")
        assert headers == {"X-MediaBrowser-Token": "my-key"}

    def test_falls_back_to_env(self, monkeypatch):
        monkeypatch.setenv("JELLYFIN_API_KEY", "env-key")
        mod = _get_module()
        headers = mod._get_headers()
        assert headers == {"X-MediaBrowser-Token": "env-key"}

    def test_none_default_falls_back_to_env(self, monkeypatch):
        monkeypatch.setenv("JELLYFIN_API_KEY", "env-key")
        mod = _get_module()
        headers = mod._get_headers(None)
        assert headers == {"X-MediaBrowser-Token": "env-key"}


class TestGetConfig:
    def test_default_url(self):
        mod = _get_module()
        url, _, _ = mod._get_config()
        assert url == "http://localhost:8096"

    def test_custom_url(self, monkeypatch):
        monkeypatch.setenv("JELLYFIN_URL", "http://myserver:1234/")
        mod = _get_module()
        url, _, _ = mod._get_config()
        assert url == "http://myserver:1234"


class TestValidateUrl:
    def test_rejects_ftp(self, monkeypatch):
        monkeypatch.setenv("JELLYFIN_URL", "ftp://evil.com")
        mod = _get_module()
        with pytest.raises(ValueError, match="http:// or https://"):
            mod._get_config()

    def test_rejects_metadata_endpoint(self, monkeypatch):
        monkeypatch.setenv("JELLYFIN_URL", "http://169.254.169.254/latest/meta-data/")
        mod = _get_module()
        with pytest.raises(ValueError, match="169.254"):
            mod._get_config()

    def test_allows_localhost(self, monkeypatch):
        monkeypatch.setenv("JELLYFIN_URL", "http://localhost:8096")
        mod = _get_module()
        url, _, _ = mod._get_config()
        assert url == "http://localhost:8096"


class TestErrorHandling:
    def test_401_invalidates_cache(self, monkeypatch):
        monkeypatch.setenv("JELLYFIN_API_KEY", "test-key")
        mod = _get_module()
        with mod._cache_lock:
            mod._cached_token = "old-token"
            mod._cached_user_id = "old-uid"

        class Fake401(Exception):
            status = 401

        result_json = mod._jellyfin_error(Fake401("unauthorized"), "test_ctx")
        result = json.loads(result_json)
        assert "401" in result["error"]
        with mod._cache_lock:
            assert mod._cached_token == ""
            assert mod._cached_user_id == ""

    def test_403_no_leak(self):
        mod = _get_module()

        class Fake403(Exception):
            status = 403

        result_json = mod._jellyfin_error(Fake403("forbidden"), "test_ctx")
        result = json.loads(result_json)
        assert "403" in result["error"]

    def test_connection_error_no_url(self):
        mod = _get_module()
        result_json = mod._jellyfin_error(Exception("Cannot connect to host"), "test_ctx")
        result = json.loads(result_json)
        assert "localhost" not in result["error"]
        assert "JELLYFIN_URL" in result["error"]


class TestInvalidateAuthCache:
    def test_clears_cached_values(self, monkeypatch):
        monkeypatch.setenv("JELLYFIN_API_KEY", "test-key")
        mod = _get_module()
        with mod._cache_lock:
            mod._cached_token = "some-token"
            mod._cached_user_id = "some-uid"
        mod._invalidate_auth_cache()
        with mod._cache_lock:
            assert mod._cached_token == ""
            assert mod._cached_user_id == ""


class TestPluginRegistration:
    def test_fifteen_tools_registered(self):
        from plugins.jellyfin import JELLYFIN_TOOLS, CHECK_FN, EMOJI
        assert len(JELLYFIN_TOOLS) == 15
        assert callable(CHECK_FN)
        assert EMOJI == "🎬"

    def test_register_calls_ctx(self):
        from plugins.jellyfin import register
        ctx = MagicMock()
        register(ctx)
        assert ctx.register_tool.call_count == 15
        first_call = ctx.register_tool.call_args_list[0]
        assert first_call.kwargs["toolset"] == "jellyfin"
        assert first_call.kwargs["requires_env"] == ["JELLYFIN_API_KEY"]


class TestResolveUserIdNoEmptyReturn:
    def test_raises_when_no_users(self, monkeypatch):
        mod = _get_module()
        monkeypatch.setenv("JELLYFIN_API_KEY", "test-key")

        resp_me = MagicMock()
        resp_me.status = 404

        resp_users = MagicMock()
        resp_users.raise_for_status = MagicMock()
        resp_users.json = AsyncMock(return_value=[])

        call_idx = [0]

        class _Ctx:
            async def __aenter__(self):
                idx = call_idx[0]
                call_idx[0] += 1
                return resp_me if idx == 0 else resp_users
            async def __aexit__(self, *a):
                pass

        fake_session = MagicMock()
        fake_session.get = MagicMock(return_value=_Ctx())
        fake_session.closed = False

        async def _mock_get_session():
            return fake_session

        with patch.object(mod, "_get_session", _mock_get_session):
            with pytest.raises(RuntimeError, match="Could not resolve"):
                mod._run_async(mod._resolve_user_id())


class TestNewHandlerParamValidation:
    def _get_module(self):
        from plugins.jellyfin import jellyfin_client as mod
        return mod

    def test_get_seasons_missing_series_id(self):
        mod = self._get_module()
        result = json.loads(mod._handle_get_seasons({}))
        assert "error" in result
        assert "series_id" in result["error"]

    def test_get_seasons_empty_series_id(self):
        mod = self._get_module()
        result = json.loads(mod._handle_get_seasons({"series_id": ""}))
        assert "error" in result

    def test_get_episodes_missing_series_id(self):
        mod = self._get_module()
        result = json.loads(mod._handle_get_episodes({}))
        assert "error" in result
        assert "series_id" in result["error"]

    def test_collection_items_missing_id(self):
        mod = self._get_module()
        result = json.loads(mod._handle_collection_items({}))
        assert "error" in result
        assert "collection_id" in result["error"]

    def test_browse_folder_missing_id(self):
        mod = self._get_module()
        result = json.loads(mod._handle_browse_folder({}))
        assert "error" in result
        assert "parent_id" in result["error"]

    def test_get_details_still_validates(self):
        mod = self._get_module()
        result = json.loads(mod._handle_get_details({}))
        assert "error" in result
        assert "item_id" in result["error"]


class TestSearchSchemaNewParams:
    def test_search_has_studio_param(self):
        mod = self._get_module()
        props = mod.JELLYFIN_SEARCH_SCHEMA["parameters"]["properties"]
        assert "studio" in props
        assert "official_rating" in props
        assert "min_runtime_minutes" in props
        assert "max_runtime_minutes" in props
        assert "person" in props

    def _get_module(self):
        from plugins.jellyfin import jellyfin_client as mod
        return mod


class TestProviderIds:
    def test_format_item_includes_providers(self):
        mod = self._get_module()
        item = {
            "Id": "123", "Name": "Test", "Type": "Movie",
            "ProviderIds": {"Imdb": "tt123", "Tmdb": "456", "Tvdb": "789"},
        }
        result = mod._format_item(item)
        assert result["provider_ids"]["imdb"] == "tt123"
        assert result["provider_ids"]["tmdb"] == "456"
        assert result["provider_ids"]["tvdb"] == "789"

    def test_format_item_handles_missing_providers(self):
        mod = self._get_module()
        item = {"Id": "123", "Name": "Test", "Type": "Movie"}
        result = mod._format_item(item)
        assert result["provider_ids"]["imdb"] is None

    def _get_module(self):
        from plugins.jellyfin import jellyfin_client as mod
        return mod


class TestNewSchemasNoCrossToolRefs:
    def test_get_seasons_no_cross_ref(self):
        mod = self._get_module()
        for key in ("description",):
            text = mod.JELLYFIN_GET_SEASONS_SCHEMA.get(key, "")
            assert "jellyfin_" not in text.lower() or "jellyfin" not in text.lower().replace(key, "")

    def test_list_views_no_cross_ref(self):
        mod = self._get_module()
        desc = mod.JELLYFIN_LIST_VIEWS_SCHEMA["description"]
        assert "jellyfin_" not in desc

    def test_browse_folder_no_cross_ref(self):
        mod = self._get_module()
        desc = mod.JELLYFIN_BROWSE_FOLDER_SCHEMA["description"]
        assert "jellyfin_" not in desc

    def test_list_collections_no_cross_ref(self):
        mod = self._get_module()
        desc = mod.JELLYFIN_LIST_COLLECTIONS_SCHEMA["description"]
        assert "jellyfin_" not in desc

    def _get_module(self):
        from plugins.jellyfin import jellyfin_client as mod
        return mod
