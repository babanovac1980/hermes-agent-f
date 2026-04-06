"""Jellyfin media server tool for querying and exploring a personal media library.

Registers five LLM-callable tools:
- ``jellyfin_search`` -- search/filter media by title, genre, year, type
- ``jellyfin_library_stats`` -- library overview with genre breakdown
- ``jellyfin_get_details`` -- full metadata for a specific item
- ``jellyfin_similar`` -- find similar items to a given one
- ``jellyfin_recent`` -- recently added media

Authentication (pick one, in priority order):
1. JELLYFIN_API_KEY  -- raw token/API key (set in ~/.hermes/.env)
2. JELLYFIN_USER + JELLYFIN_PASSWORD -- authenticates via username/password on
   first use and caches the access token for the process lifetime.

The Jellyfin instance URL is read from ``JELLYFIN_URL`` (default: http://localhost:8096).
User ID can be set via ``JELLYFIN_USER_ID`` or is auto-detected from the server.
"""

import asyncio
import json
import logging
import os
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_JELLYFIN_URL: str = ""
_JELLYFIN_API_KEY: str = ""
_JELLYFIN_USER_ID: str = ""
_cached_user_id: str = ""
_cached_token: str = ""   # token obtained via username/password auth

_COMMON_FIELDS = (
    "Overview,Genres,CommunityRating,ProductionYear,"
    "RunTimeTicks,Studios,People,OfficialRating,Taglines"
)

# Jellyfin requires a client identifier in the Authorization header for
# username/password auth. These values are arbitrary but must be consistent.
_CLIENT_AUTH_HEADER = (
    'MediaBrowser Client="HermesAgent", Device="Server", '
    'DeviceId="hermes-agent-1", Version="1.0.0"'
)


def _get_config():
    """Return (jellyfin_url, api_key, user_id) from env vars at call time."""
    return (
        (_JELLYFIN_URL or os.getenv("JELLYFIN_URL", "http://localhost:8096")).rstrip("/"),
        _JELLYFIN_API_KEY or os.getenv("JELLYFIN_API_KEY", ""),
        _JELLYFIN_USER_ID or os.getenv("JELLYFIN_USER_ID", ""),
    )


async def _get_token() -> str:
    """Return the active auth token, authenticating via user/password if needed."""
    global _cached_token, _cached_user_id

    _, api_key, _ = _get_config()
    if api_key:
        return api_key
    if _cached_token:
        return _cached_token

    username = os.getenv("JELLYFIN_USER", "")
    password = os.getenv("JELLYFIN_PASSWORD", "")
    if not username:
        raise RuntimeError(
            "No Jellyfin credentials. Set JELLYFIN_API_KEY or "
            "JELLYFIN_USER + JELLYFIN_PASSWORD in ~/.hermes/.env"
        )

    import aiohttp

    jf_url, _, _ = _get_config()
    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{jf_url}/Users/AuthenticateByName",
            headers={"Authorization": _CLIENT_AUTH_HEADER, "Content-Type": "application/json"},
            json={"Username": username, "Pw": password},
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()

    _cached_token = data["AccessToken"]
    _cached_user_id = data["User"]["Id"]  # also cache user ID from auth response
    logger.debug("Jellyfin: authenticated as %s", data["User"].get("Name"))
    return _cached_token


def _get_headers(api_key: str = "") -> Dict[str, str]:
    """Return authorization headers for Jellyfin REST API."""
    if not api_key:
        _, api_key, _ = _get_config()
    return {"X-MediaBrowser-Token": api_key}


# ---------------------------------------------------------------------------
# User ID resolution
# ---------------------------------------------------------------------------

async def _resolve_user_id() -> str:
    """Resolve the Jellyfin user ID, auto-detecting from the server if needed."""
    global _cached_user_id
    jf_url, _, explicit_uid = _get_config()
    if explicit_uid:
        return explicit_uid
    if _cached_user_id:
        return _cached_user_id

    import aiohttp

    token = await _get_token()
    async with aiohttp.ClientSession() as session:
        # /Users/Me works for any authenticated user token or API key
        async with session.get(
            f"{jf_url}/Users/Me",
            headers=_get_headers(token),
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status == 200:
                data = await resp.json()
                _cached_user_id = data["Id"]
                return _cached_user_id
            # Fall back to /Users for admin-scoped API keys
            async with session.get(
                f"{jf_url}/Users",
                headers=_get_headers(token),
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp2:
                resp2.raise_for_status()
                users = await resp2.json()

    for u in users:
        if u.get("Policy", {}).get("IsAdministrator"):
            _cached_user_id = u["Id"]
            return _cached_user_id
    if users:
        _cached_user_id = users[0]["Id"]
    return _cached_user_id


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _format_item(item: Dict[str, Any], truncate_overview: bool = True) -> Dict[str, Any]:
    """Normalize a Jellyfin item into a compact dict for LLM context."""
    ticks = item.get("RunTimeTicks")
    runtime = round(ticks / 600_000_000) if ticks else None
    people = item.get("People", [])
    cast = [p["Name"] for p in people if p.get("Type") == "Actor"][:10]
    directors = [p["Name"] for p in people if p.get("Type") == "Director"]
    overview = item.get("Overview", "") or ""
    if truncate_overview and len(overview) > 300:
        overview = overview[:300] + "..."
    return {
        "id": item.get("Id"),
        "name": item.get("Name"),
        "year": item.get("ProductionYear"),
        "rating": item.get("CommunityRating"),
        "official_rating": item.get("OfficialRating"),
        "genres": item.get("Genres", []),
        "overview": overview,
        "runtime_minutes": runtime,
        "type": item.get("Type"),
        "studios": [s.get("Name") for s in item.get("Studios", [])],
        "cast": cast,
        "directors": directors,
    }


# ---------------------------------------------------------------------------
# Async implementations
# ---------------------------------------------------------------------------

async def _async_search(
    query: Optional[str] = None,
    media_type: str = "Movie",
    genres: Optional[str] = None,
    years: Optional[str] = None,
    sort_by: str = "Name",
    sort_order: str = "Ascending",
    limit: int = 20,
) -> Dict[str, Any]:
    """Search the Jellyfin media library with filters."""
    import aiohttp

    jf_url, _, _ = _get_config()
    token = await _get_token()
    user_id = await _resolve_user_id()
    params = {
        "Recursive": "true",
        "Fields": _COMMON_FIELDS,
        "IncludeItemTypes": media_type,
        "SortBy": sort_by,
        "SortOrder": sort_order,
        "Limit": str(min(max(limit, 1), 50)),
    }
    if query:
        params["SearchTerm"] = query
    if genres:
        params["Genres"] = genres
    if years:
        params["Years"] = years

    async with aiohttp.ClientSession() as session:
        async with session.get(
            f"{jf_url}/Users/{user_id}/Items",
            headers=_get_headers(token),
            params=params,
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()

    items = [_format_item(i) for i in data.get("Items", [])]
    return {"total": data.get("TotalRecordCount", len(items)), "items": items}


async def _async_library_stats() -> Dict[str, Any]:
    """Get library statistics: counts and genre breakdown.

    Jellyfin's /Genres endpoint does not return per-genre counts, so we fetch
    all movies with their Genres field and aggregate locally.
    """
    import aiohttp
    from collections import Counter

    jf_url, _, _ = _get_config()
    token = await _get_token()
    user_id = await _resolve_user_id()
    headers = _get_headers(token)

    async with aiohttp.ClientSession() as session:
        # Get all movies with genres in one request (Fields=Genres keeps payload small)
        async with session.get(
            f"{jf_url}/Users/{user_id}/Items",
            headers=headers,
            params={
                "IncludeItemTypes": "Movie",
                "Recursive": "true",
                "Fields": "Genres",
                "Limit": "5000",
            },
            timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            resp.raise_for_status()
            movie_data = await resp.json()

        # Get series count
        async with session.get(
            f"{jf_url}/Users/{user_id}/Items",
            headers=headers,
            params={"IncludeItemTypes": "Series", "Recursive": "true", "Limit": "0"},
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            resp.raise_for_status()
            series_data = await resp.json()

    # Count genres from actual movie data
    genre_counter: Counter = Counter()
    for item in movie_data.get("Items", []):
        for g in item.get("Genres", []):
            genre_counter[g] += 1

    genres = [
        {"name": name, "count": count}
        for name, count in genre_counter.most_common()
    ]

    return {
        "movies": movie_data.get("TotalRecordCount", len(movie_data.get("Items", []))),
        "series": series_data.get("TotalRecordCount", 0),
        "genres": genres,
    }


async def _async_get_details(item_id: str) -> Dict[str, Any]:
    """Get detailed information about a specific media item."""
    import aiohttp

    jf_url, _, _ = _get_config()
    token = await _get_token()
    user_id = await _resolve_user_id()

    async with aiohttp.ClientSession() as session:
        async with session.get(
            f"{jf_url}/Users/{user_id}/Items/{item_id}",
            headers=_get_headers(token),
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()

    result = _format_item(data, truncate_overview=False)
    # Add extra detail fields
    result["taglines"] = data.get("Taglines", [])
    result["critic_rating"] = data.get("CriticRating")
    return result


async def _async_similar(item_id: str, limit: int = 10) -> Dict[str, Any]:
    """Find items similar to a given one."""
    import aiohttp

    jf_url, _, _ = _get_config()
    token = await _get_token()

    async with aiohttp.ClientSession() as session:
        async with session.get(
            f"{jf_url}/Items/{item_id}/Similar",
            headers=_get_headers(token),
            params={
                "Limit": str(min(max(limit, 1), 20)),
                "Fields": _COMMON_FIELDS,
            },
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()

    items = [_format_item(i) for i in data.get("Items", [])]
    return {"items": items}


async def _async_recent(
    media_type: str = "Movie",
    limit: int = 15,
) -> Dict[str, Any]:
    """Get recently added media items."""
    import aiohttp

    jf_url, _, _ = _get_config()
    token = await _get_token()
    user_id = await _resolve_user_id()

    async with aiohttp.ClientSession() as session:
        async with session.get(
            f"{jf_url}/Users/{user_id}/Items/Latest",
            headers=_get_headers(token),
            params={
                "IncludeItemTypes": media_type,
                "Limit": str(min(max(limit, 1), 30)),
                "Fields": _COMMON_FIELDS,
            },
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()

    # /Latest returns a flat list, not a paged result
    items = [_format_item(i) for i in (data if isinstance(data, list) else data.get("Items", []))]
    return {"items": items}


# ---------------------------------------------------------------------------
# Sync wrappers (handler signature: (args, **kw) -> str)
# ---------------------------------------------------------------------------

def _run_async(coro):
    """Run an async coroutine from a sync handler."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(asyncio.run, coro)
            return future.result(timeout=30)
    else:
        return asyncio.run(coro)


def _jellyfin_error(e: Exception, context: str) -> str:
    """Return a user-friendly error JSON, with specific guidance for auth failures."""
    msg = str(e)
    if "403" in msg or "Forbidden" in msg:
        msg = (
            "403 Forbidden — the API key/token was rejected. "
            "Use a proper API key: Jellyfin Dashboard → API Keys → + button. "
            "Browser session tokens expire and don't work as API keys."
        )
    elif "401" in msg or "Unauthorized" in msg:
        msg = "401 Unauthorized — invalid or missing API key. Set JELLYFIN_API_KEY in ~/.hermes/.env"
    elif "Cannot connect" in msg or "Connection refused" in msg:
        msg = f"Cannot reach Jellyfin at {_get_config()[0]} — check JELLYFIN_URL"
    logger.error("%s: %s", context, e)
    return json.dumps({"error": msg})


def _handle_search(args: dict, **kw) -> str:
    """Handler for jellyfin_search tool."""
    try:
        result = _run_async(_async_search(
            query=args.get("query"),
            media_type=args.get("media_type", "Movie"),
            genres=args.get("genres"),
            years=args.get("years"),
            sort_by=args.get("sort_by", "Name"),
            sort_order=args.get("sort_order", "Ascending"),
            limit=args.get("limit", 20),
        ))
        return json.dumps({"result": result})
    except Exception as e:
        return _jellyfin_error(e, "jellyfin_search")


def _handle_library_stats(args: dict, **kw) -> str:
    """Handler for jellyfin_library_stats tool."""
    try:
        result = _run_async(_async_library_stats())
        return json.dumps({"result": result})
    except Exception as e:
        return _jellyfin_error(e, "jellyfin_library_stats")


def _handle_get_details(args: dict, **kw) -> str:
    """Handler for jellyfin_get_details tool."""
    item_id = args.get("item_id", "")
    if not item_id:
        return json.dumps({"error": "Missing required parameter: item_id"})
    try:
        result = _run_async(_async_get_details(item_id))
        return json.dumps({"result": result})
    except Exception as e:
        return _jellyfin_error(e, "jellyfin_get_details")


def _handle_similar(args: dict, **kw) -> str:
    """Handler for jellyfin_similar tool."""
    item_id = args.get("item_id", "")
    if not item_id:
        return json.dumps({"error": "Missing required parameter: item_id"})
    try:
        result = _run_async(_async_similar(
            item_id=item_id,
            limit=args.get("limit", 10),
        ))
        return json.dumps({"result": result})
    except Exception as e:
        return _jellyfin_error(e, "jellyfin_similar")


def _handle_recent(args: dict, **kw) -> str:
    """Handler for jellyfin_recent tool."""
    try:
        result = _run_async(_async_recent(
            media_type=args.get("media_type", "Movie"),
            limit=args.get("limit", 15),
        ))
        return json.dumps({"result": result})
    except Exception as e:
        return _jellyfin_error(e, "jellyfin_recent")


# ---------------------------------------------------------------------------
# Availability check
# ---------------------------------------------------------------------------

def _check_jellyfin_available() -> bool:
    """Tool is available when JELLYFIN_API_KEY or JELLYFIN_USER+PASSWORD are set."""
    return bool(os.getenv("JELLYFIN_API_KEY")) or bool(
        os.getenv("JELLYFIN_USER") and os.getenv("JELLYFIN_PASSWORD")
    )


# ---------------------------------------------------------------------------
# Tool schemas
# ---------------------------------------------------------------------------

JELLYFIN_SEARCH_SCHEMA = {
    "name": "jellyfin_search",
    "description": (
        "Search the Jellyfin media library. Supports filtering by title, genre, "
        "year, and media type (Movie, Series, Episode, Audio). Returns matching "
        "items with title, year, rating, genres, overview, and runtime."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": (
                    "Search term to match against titles. "
                    "Omit to browse by filters only."
                ),
            },
            "media_type": {
                "type": "string",
                "enum": ["Movie", "Series", "Episode", "Audio"],
                "description": "Filter by media type. Default: Movie.",
            },
            "genres": {
                "type": "string",
                "description": "Comma-separated genre filter (e.g. 'Action,Comedy').",
            },
            "years": {
                "type": "string",
                "description": "Comma-separated year filter (e.g. '2020,2021,2022').",
            },
            "sort_by": {
                "type": "string",
                "enum": ["Name", "CommunityRating", "DateCreated", "ProductionYear", "Random"],
                "description": "Sort field. Default: Name.",
            },
            "sort_order": {
                "type": "string",
                "enum": ["Ascending", "Descending"],
                "description": "Sort direction. Default: Ascending.",
            },
            "limit": {
                "type": "integer",
                "description": "Max results to return (1-50). Default: 20.",
            },
        },
        "required": [],
    },
}

JELLYFIN_LIBRARY_STATS_SCHEMA = {
    "name": "jellyfin_library_stats",
    "description": (
        "Get Jellyfin library statistics: total movies, series, and genre "
        "breakdown with counts. Useful for understanding the user's collection."
    ),
    "parameters": {
        "type": "object",
        "properties": {},
        "required": [],
    },
}

JELLYFIN_GET_DETAILS_SCHEMA = {
    "name": "jellyfin_get_details",
    "description": (
        "Get detailed information about a specific Jellyfin media item by its ID. "
        "Returns full metadata including cast, directors, studios, overview, "
        "ratings, runtime, and taglines."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "item_id": {
                "type": "string",
                "description": "The Jellyfin item ID (from search results).",
            },
        },
        "required": ["item_id"],
    },
}

JELLYFIN_SIMILAR_SCHEMA = {
    "name": "jellyfin_similar",
    "description": (
        "Find media similar to a given Jellyfin item. Useful for recommendations "
        "like 'find movies similar to X'."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "item_id": {
                "type": "string",
                "description": "The Jellyfin item ID to find similar items for.",
            },
            "limit": {
                "type": "integer",
                "description": "Max similar items to return (1-20). Default: 10.",
            },
        },
        "required": ["item_id"],
    },
}

JELLYFIN_RECENT_SCHEMA = {
    "name": "jellyfin_recent",
    "description": (
        "Get recently added media from the Jellyfin library. Shows the newest "
        "additions to the collection."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "media_type": {
                "type": "string",
                "enum": ["Movie", "Series", "Episode"],
                "description": "Filter by media type. Default: Movie.",
            },
            "limit": {
                "type": "integer",
                "description": "Max results (1-30). Default: 15.",
            },
        },
        "required": [],
    },
}


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

from tools.registry import registry

_JELLYFIN_TOOLS = [
    ("jellyfin_search", JELLYFIN_SEARCH_SCHEMA, _handle_search),
    ("jellyfin_library_stats", JELLYFIN_LIBRARY_STATS_SCHEMA, _handle_library_stats),
    ("jellyfin_get_details", JELLYFIN_GET_DETAILS_SCHEMA, _handle_get_details),
    ("jellyfin_similar", JELLYFIN_SIMILAR_SCHEMA, _handle_similar),
    ("jellyfin_recent", JELLYFIN_RECENT_SCHEMA, _handle_recent),
]

for _name, _schema, _handler in _JELLYFIN_TOOLS:
    registry.register(
        name=_name,
        toolset="jellyfin",
        schema=_schema,
        handler=_handler,
        check_fn=_check_jellyfin_available,
        emoji="🎬",
    )
