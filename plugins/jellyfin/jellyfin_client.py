"""Jellyfin media server tool for querying and exploring a personal media library.

Registers fifteen LLM-callable tools:
- ``jellyfin_search`` -- search/filter media by title, genre, year, type, studio,
  content rating, runtime range, and person
- ``jellyfin_library_stats`` -- library overview with genre breakdown
- ``jellyfin_get_details`` -- full metadata for a specific item
- ``jellyfin_similar`` -- find similar items to a given one
- ``jellyfin_recent`` -- recently added media
- ``jellyfin_all_movies`` -- complete movie list with IMDB/TMDB/TVDB IDs
- ``jellyfin_get_seasons`` -- list seasons for a TV series
- ``jellyfin_get_episodes`` -- list episodes for a series (optionally by season)
- ``jellyfin_next_up`` -- next-up queue for tracked series
- ``jellyfin_list_collections`` -- browse all collections (BoxSets)
- ``jellyfin_collection_items`` -- items inside a specific collection
- ``jellyfin_list_views`` -- top-level library views (Movies, TV Shows, etc.)
- ``jellyfin_browse_folder`` -- navigate into a specific library folder
- ``jellyfin_genres`` -- list all genres in the library
- ``jellyfin_studios`` -- list all studios in the library

Authentication (pick one, in priority order):
1. JELLYFIN_API_KEY  -- raw token/API key (set in ~/.hermes/.env)
2. JELLYFIN_USER + JELLYFIN_PASSWORD -- authenticates via username/password on
   first use and caches the access token for the process lifetime.

The Jellyfin instance URL is read from ``JELLYFIN_URL`` (default: http://localhost:8096).
User ID can be set via ``JELLYFIN_USER_ID`` or is auto-detected from the server.
"""

import asyncio
import atexit
import json
import logging
import os
import threading
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_cached_user_id: str = ""
_cached_token: str = ""
_cache_lock = threading.Lock()

_tool_loop = None
_tool_loop_thread = None
_tool_loop_lock = threading.Lock()
_auth_lock = threading.Lock()
_shared_session = None

_COMMON_FIELDS = (
    "Overview,Genres,CommunityRating,ProductionYear,"
    "RunTimeTicks,Studios,People,OfficialRating,Taglines,ProviderIds"
)

_CLIENT_AUTH_HEADER = (
    'MediaBrowser Client="HermesAgent", Device="Server", '
    'DeviceId="hermes-agent-1", Version="1.0.0"'
)


def _get_tool_loop():
    global _tool_loop, _tool_loop_thread
    if _tool_loop is None or _tool_loop.is_closed():
        with _tool_loop_lock:
            if _tool_loop is None or _tool_loop.is_closed():
                _tool_loop = asyncio.new_event_loop()
                _tool_loop_thread = threading.Thread(target=_tool_loop.run_forever, daemon=True)
                _tool_loop_thread.start()
    return _tool_loop


def _invalidate_auth_cache():
    global _cached_token, _cached_user_id
    with _cache_lock:
        _cached_token = ""
        _cached_user_id = ""


def _safe_path_segment(value: str) -> str:
    if "/" in value or "\\" in value or ".." in value:
        raise ValueError("Invalid ID: contains forbidden characters")
    return value


def _validate_jellyfin_url(url: str) -> str:
    from urllib.parse import urlparse
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"JELLYFIN_URL must use http:// or https://, got: {parsed.scheme}")
    host = parsed.hostname or ""
    if host.startswith("169.254."):
        raise ValueError("JELLYFIN_URL must not point to cloud metadata endpoint (169.254.x.x)")
    return url


def _get_config():
    return (
        _validate_jellyfin_url(os.getenv("JELLYFIN_URL", "http://localhost:8096").rstrip("/")),
        os.getenv("JELLYFIN_API_KEY", ""),
        os.getenv("JELLYFIN_USER_ID", ""),
    )


async def _get_token() -> str:
    global _cached_token, _cached_user_id

    _, api_key, _ = _get_config()
    if api_key:
        return api_key
    with _cache_lock:
        if _cached_token:
            return _cached_token

    with _auth_lock:
        with _cache_lock:
            if _cached_token:
                return _cached_token

        username = os.getenv("JELLYFIN_USER", "")
        password = os.getenv("JELLYFIN_PASSWORD", "")
        if not username:
            raise RuntimeError(
                "No Jellyfin credentials. Set JELLYFIN_API_KEY or "
                "JELLYFIN_USER + JELLYFIN_PASSWORD in ~/.hermes/.env"
            )

        jf_url, _, _ = _get_config()
        session = await _get_session()
        import aiohttp
        async with session.post(
            f"{jf_url}/Users/AuthenticateByName",
            headers={"Authorization": _CLIENT_AUTH_HEADER, "Content-Type": "application/json"},
            json={"Username": username, "Pw": password},
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()

        with _cache_lock:
            _cached_token = data["AccessToken"]
            _cached_user_id = data["User"]["Id"]
        logger.debug("Jellyfin: authenticated as %s", data["User"].get("Name"))
        return data["AccessToken"]


def _get_headers(api_key: str = None) -> Dict[str, str]:
    if api_key is None:
        _, api_key, _ = _get_config()
    return {"X-MediaBrowser-Token": api_key}


async def _get_session():
    global _shared_session
    try:
        current_loop = asyncio.get_running_loop()
    except RuntimeError:
        current_loop = None
    tool_loop = _get_tool_loop()
    if current_loop is not tool_loop:
        import aiohttp
        return aiohttp.ClientSession()
    if _shared_session is None or _shared_session.closed:
        import aiohttp
        _shared_session = aiohttp.ClientSession()
    return _shared_session


def _cleanup_session():
    global _shared_session
    if _shared_session is not None and not _shared_session.closed:
        try:
            loop = _get_tool_loop()
            if loop.is_running():
                asyncio.run_coroutine_threadsafe(_shared_session.close(), loop).result(timeout=5)
        except Exception:
            pass

atexit.register(_cleanup_session)


async def _resolve_user_id() -> str:
    global _cached_user_id
    jf_url, _, explicit_uid = _get_config()
    if explicit_uid:
        return explicit_uid
    with _cache_lock:
        if _cached_user_id:
            return _cached_user_id

    import aiohttp

    token = await _get_token()
    session = await _get_session()
    async with session.get(
        f"{jf_url}/Users/Me",
        headers=_get_headers(token),
        timeout=aiohttp.ClientTimeout(total=10),
    ) as resp:
        if resp.status == 200:
            data = await resp.json()
            with _cache_lock:
                _cached_user_id = data["Id"]
            return _cached_user_id
        async with session.get(
            f"{jf_url}/Users",
            headers=_get_headers(token),
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp2:
            resp2.raise_for_status()
            users = await resp2.json()

    for u in users:
        if u.get("Policy", {}).get("IsAdministrator"):
            with _cache_lock:
                _cached_user_id = u["Id"]
            return _cached_user_id
    if users:
        with _cache_lock:
            _cached_user_id = users[0]["Id"]
        return _cached_user_id
    raise RuntimeError(
        "Could not resolve Jellyfin user ID. "
        "Set JELLYFIN_USER_ID in ~/.hermes/.env"
    )


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
    providers = item.get("ProviderIds", {})
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
        "provider_ids": {
            "imdb": providers.get("Imdb") or providers.get("imdb"),
            "tmdb": providers.get("Tmdb") or providers.get("tmdb"),
            "tvdb": providers.get("Tvdb") or providers.get("tvdb"),
        },
    }


async def _async_search(
    query: Optional[str] = None,
    media_type: str = "Movie",
    genres: Optional[str] = None,
    years: Optional[str] = None,
    sort_by: str = "Name",
    sort_order: str = "Ascending",
    limit: int = 20,
    studio: Optional[str] = None,
    official_rating: Optional[str] = None,
    min_runtime_minutes: Optional[int] = None,
    max_runtime_minutes: Optional[int] = None,
    person: Optional[str] = None,
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
    if official_rating:
        params["OfficialRatings"] = official_rating
    if min_runtime_minutes is not None:
        params["MinRunTimeTicks"] = str(min_runtime_minutes * 600_000_000)
    if max_runtime_minutes is not None:
        params["MaxRunTimeTicks"] = str(max_runtime_minutes * 600_000_000)
    if person:
        params["Person"] = person

    session = await _get_session()
    async with session.get(
        f"{jf_url}/Users/{_safe_path_segment(user_id)}/Items",
        headers=_get_headers(token),
        params=params,
        timeout=aiohttp.ClientTimeout(total=15),
    ) as resp:
        resp.raise_for_status()
        data = await resp.json()

    items = [_format_item(i) for i in data.get("Items", [])]

    if studio:
        studio_lower = studio.lower()
        items = [
            it for it in items
            if any(studio_lower in s.lower() for s in it.get("studios", []))
        ]

    total = len(items) if studio else data.get("TotalRecordCount", len(items))
    return {"total": total, "items": items}


async def _async_library_stats() -> Dict[str, Any]:
    """Get library statistics matching the Jellyfin dashboard.

    Uses /Items/Counts for accurate totals (movies, series, episodes, collections),
    then fetches all movies with Genres to produce real per-genre counts.
    """
    import aiohttp
    from collections import Counter

    jf_url, _, _ = _get_config()
    token = await _get_token()
    user_id = await _resolve_user_id()
    headers = _get_headers(token)

    session = await _get_session()
    async with session.get(
        f"{jf_url}/Items/Counts",
        headers=headers,
        params={"UserId": user_id},
        timeout=aiohttp.ClientTimeout(total=15),
    ) as resp:
        resp.raise_for_status()
        counts = await resp.json()

    async with session.get(
        f"{jf_url}/Users/{_safe_path_segment(user_id)}/Items",
        headers=headers,
        params={"IncludeItemTypes": "BoxSet", "Recursive": "true", "Limit": "0"},
        timeout=aiohttp.ClientTimeout(total=15),
    ) as resp:
        resp.raise_for_status()
        collections_data = await resp.json()

    async with session.get(
        f"{jf_url}/Users/{_safe_path_segment(user_id)}/Items",
        headers=headers,
        params={
            "IncludeItemTypes": "Movie",
            "Recursive": "true",
            "Fields": "Genres",
            "Limit": "10000",
        },
        timeout=aiohttp.ClientTimeout(total=30),
    ) as resp:
        resp.raise_for_status()
        movie_data = await resp.json()

    genre_counter: Counter = Counter()
    for item in movie_data.get("Items", []):
        for g in item.get("Genres", []):
            genre_counter[g] += 1

    genres = [
        {"name": name, "count": count}
        for name, count in genre_counter.most_common()
    ]

    result = {
        "movies": counts.get("MovieCount", 0),
        "series": counts.get("SeriesCount", 0),
        "episodes": counts.get("EpisodeCount", 0),
        "collections": collections_data.get("TotalRecordCount", 0),
        "songs": counts.get("SongCount", 0),
        "genres": genres,
    }
    if len(movie_data.get("Items", [])) >= 10000:
        result["warning"] = "Genre counts may be incomplete — library exceeds 10000-item page limit."
    return result


async def _async_get_details(item_id: str) -> Dict[str, Any]:
    """Get detailed information about a specific media item."""
    import aiohttp

    jf_url, _, _ = _get_config()
    token = await _get_token()
    user_id = await _resolve_user_id()

    session = await _get_session()
    async with session.get(
        f"{jf_url}/Users/{_safe_path_segment(user_id)}/Items/{_safe_path_segment(item_id)}",
        headers=_get_headers(token),
        timeout=aiohttp.ClientTimeout(total=10),
    ) as resp:
        resp.raise_for_status()
        data = await resp.json()

    result = _format_item(data, truncate_overview=False)
    result["taglines"] = data.get("Taglines", [])
    result["critic_rating"] = data.get("CriticRating")
    return result


async def _async_similar(item_id: str, limit: int = 10) -> Dict[str, Any]:
    """Find items similar to a given one."""
    import aiohttp

    jf_url, _, _ = _get_config()
    token = await _get_token()

    session = await _get_session()
    async with session.get(
        f"{jf_url}/Items/{_safe_path_segment(item_id)}/Similar",
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

    session = await _get_session()
    async with session.get(
        f"{jf_url}/Users/{_safe_path_segment(user_id)}/Items/Latest",
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

    items = [_format_item(i) for i in (data if isinstance(data, list) else data.get("Items", []))]
    return {"items": items}


def _run_async(coro):
    import concurrent.futures
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        future = pool.submit(lambda: asyncio.run(coro))
        try:
            return future.result(timeout=300)
        except concurrent.futures.TimeoutError:
            future.cancel()
            raise
        finally:
            pool.shutdown(wait=False)

    tool_loop = _get_tool_loop()
    future = asyncio.run_coroutine_threadsafe(coro, tool_loop)
    try:
        return future.result(timeout=300)
    except concurrent.futures.TimeoutError:
        future.cancel()
        raise


def _jellyfin_error(e: Exception, context: str) -> str:
    msg = str(e)
    status = getattr(e, "status", None)
    if status == 401:
        _invalidate_auth_cache()
        msg = "401 Unauthorized — invalid or missing API key. Set JELLYFIN_API_KEY in ~/.hermes/.env"
    elif status == 403:
        msg = (
            "403 Forbidden — the API key/token was rejected. "
            "Use a proper API key: Jellyfin Dashboard → API Keys → + button. "
            "Browser session tokens expire and don't work as API keys."
        )
    elif "Cannot connect" in msg or "Connection refused" in msg:
        msg = "Cannot reach Jellyfin server. Check JELLYFIN_URL and that the server is running."
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
            limit=args.get("limit") or 20,
            studio=args.get("studio"),
            official_rating=args.get("official_rating"),
            min_runtime_minutes=args.get("min_runtime_minutes"),
            max_runtime_minutes=args.get("max_runtime_minutes"),
            person=args.get("person"),
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
            limit=args.get("limit") or 10,
        ))
        return json.dumps({"result": result})
    except Exception as e:
        return _jellyfin_error(e, "jellyfin_similar")


def _handle_recent(args: dict, **kw) -> str:
    """Handler for jellyfin_recent tool."""
    try:
        result = _run_async(_async_recent(
            media_type=args.get("media_type", "Movie"),
            limit=args.get("limit") or 15,
        ))
        return json.dumps({"result": result})
    except Exception as e:
        return _jellyfin_error(e, "jellyfin_recent")


def _handle_all_movies(args: dict, **kw) -> str:
    """Handler for jellyfin_all_movies tool."""
    try:
        result = _run_async(_async_all_movies())
        return json.dumps({"result": result})
    except Exception as e:
        return _jellyfin_error(e, "jellyfin_all_movies")


async def _async_all_movies() -> Dict[str, Any]:
    """Fetch all movies from the library with title, year, and IMDB ID.

    Returns a compact list optimised for cross-referencing against external
    lists (e.g. IMDB Top 250). IMDB IDs enable exact matching without
    fuzzy title comparison.
    """
    import aiohttp

    jf_url, _, _ = _get_config()
    token = await _get_token()
    user_id = await _resolve_user_id()

    session = await _get_session()
    async with session.get(
        f"{jf_url}/Users/{_safe_path_segment(user_id)}/Items",
        headers=_get_headers(token),
        params={
            "IncludeItemTypes": "Movie",
            "Recursive": "true",
            "Fields": "ProviderIds,ProductionYear",
            "Limit": "10000",
            "SortBy": "Name",
            "SortOrder": "Ascending",
        },
        timeout=aiohttp.ClientTimeout(total=30),
    ) as resp:
        resp.raise_for_status()
        data = await resp.json()

    movies = []
    for item in data.get("Items", []):
        providers = item.get("ProviderIds", {})
        movies.append({
            "name": item.get("Name"),
            "year": item.get("ProductionYear"),
            "imdb_id": providers.get("Imdb") or providers.get("imdb"),
            "tmdb_id": providers.get("Tmdb") or providers.get("tmdb"),
            "tvdb_id": providers.get("Tvdb") or providers.get("tvdb"),
        })

    result = {"total": len(movies), "movies": movies}
    if len(movies) >= 10000:
        result["warning"] = "Library may be larger than 10000 items — result is truncated."
    return result


def _format_season(item: Dict[str, Any]) -> Dict[str, Any]:
    overview = item.get("Overview", "") or ""
    if len(overview) > 300:
        overview = overview[:300] + "..."
    return {
        "id": item.get("Id"),
        "name": item.get("Name"),
        "series_name": item.get("SeriesName"),
        "season_number": item.get("IndexNumber"),
        "year": item.get("ProductionYear"),
        "overview": overview or None,
    }


def _format_episode(item: Dict[str, Any]) -> Dict[str, Any]:
    ticks = item.get("RunTimeTicks")
    runtime = round(ticks / 600_000_000) if ticks else None
    overview = item.get("Overview", "") or ""
    if len(overview) > 300:
        overview = overview[:300] + "..."
    return {
        "id": item.get("Id"),
        "name": item.get("Name"),
        "series_name": item.get("SeriesName"),
        "season_number": item.get("ParentIndexNumber"),
        "episode_number": item.get("IndexNumber"),
        "overview": overview or None,
        "runtime_minutes": runtime,
        "rating": item.get("CommunityRating"),
    }


async def _async_get_seasons(series_id: str) -> Dict[str, Any]:
    import aiohttp

    jf_url, _, _ = _get_config()
    token = await _get_token()
    user_id = await _resolve_user_id()

    session = await _get_session()
    async with session.get(
        f"{jf_url}/Shows/{_safe_path_segment(series_id)}/Seasons",
        headers=_get_headers(token),
        params={"UserId": user_id, "Fields": "Overview,ProductionYear"},
        timeout=aiohttp.ClientTimeout(total=15),
    ) as resp:
        resp.raise_for_status()
        data = await resp.json()

    seasons = [_format_season(i) for i in data.get("Items", [])]
    return {"series_id": series_id, "seasons": seasons}


async def _async_get_episodes(
    series_id: str,
    season_id: Optional[str] = None,
    limit: int = 50,
) -> Dict[str, Any]:
    import aiohttp

    jf_url, _, _ = _get_config()
    token = await _get_token()
    user_id = await _resolve_user_id()

    params = {
        "UserId": user_id,
        "Fields": _COMMON_FIELDS,
        "Limit": str(min(max(limit, 1), 100)),
    }
    if season_id:
        params["SeasonId"] = season_id

    session = await _get_session()
    async with session.get(
        f"{jf_url}/Shows/{_safe_path_segment(series_id)}/Episodes",
        headers=_get_headers(token),
        params=params,
        timeout=aiohttp.ClientTimeout(total=15),
    ) as resp:
        resp.raise_for_status()
        data = await resp.json()

    episodes = [_format_episode(i) for i in data.get("Items", [])]
    return {
        "series_id": series_id,
        "total": data.get("TotalRecordCount", len(episodes)),
        "episodes": episodes,
    }


async def _async_next_up(limit: int = 15) -> Dict[str, Any]:
    import aiohttp

    jf_url, _, _ = _get_config()
    token = await _get_token()
    user_id = await _resolve_user_id()

    session = await _get_session()
    async with session.get(
        f"{jf_url}/Shows/NextUp",
        headers=_get_headers(token),
        params={
            "UserId": user_id,
            "Limit": str(min(max(limit, 1), 30)),
            "Fields": _COMMON_FIELDS,
        },
        timeout=aiohttp.ClientTimeout(total=15),
    ) as resp:
        resp.raise_for_status()
        data = await resp.json()

    items = [_format_item(i) for i in data.get("Items", [])]
    return {"items": items}


async def _async_list_collections(limit: int = 50) -> Dict[str, Any]:
    import aiohttp

    jf_url, _, _ = _get_config()
    token = await _get_token()
    user_id = await _resolve_user_id()

    session = await _get_session()
    async with session.get(
        f"{jf_url}/Users/{_safe_path_segment(user_id)}/Items",
        headers=_get_headers(token),
        params={
            "IncludeItemTypes": "BoxSet",
            "Recursive": "true",
            "Fields": "Overview,ChildCount",
            "SortBy": "Name",
            "SortOrder": "Ascending",
            "Limit": str(min(max(limit, 1), 100)),
        },
        timeout=aiohttp.ClientTimeout(total=15),
    ) as resp:
        resp.raise_for_status()
        data = await resp.json()

    collections = []
    for item in data.get("Items", []):
        overview = (item.get("Overview") or "")[:200]
        collections.append({
            "id": item.get("Id"),
            "name": item.get("Name"),
            "child_count": item.get("ChildCount", 0),
            "overview": overview or None,
        })

    return {
        "total": data.get("TotalRecordCount", len(collections)),
        "collections": collections,
    }


async def _async_collection_items(collection_id: str) -> Dict[str, Any]:
    import aiohttp

    jf_url, _, _ = _get_config()
    token = await _get_token()
    user_id = await _resolve_user_id()

    session = await _get_session()
    async with session.get(
        f"{jf_url}/Users/{_safe_path_segment(user_id)}/Items",
        headers=_get_headers(token),
        params={
            "ParentId": _safe_path_segment(collection_id),
            "Fields": _COMMON_FIELDS,
            "SortBy": "Name",
            "SortOrder": "Ascending",
            "Limit": "100",
        },
        timeout=aiohttp.ClientTimeout(total=15),
    ) as resp:
        resp.raise_for_status()
        data = await resp.json()

    items = [_format_item(i) for i in data.get("Items", [])]
    result = {
        "collection_id": collection_id,
        "total": data.get("TotalRecordCount", len(items)),
        "items": items,
    }
    if len(items) >= 100:
        result["warning"] = "Collection may be larger than 100 items — result is truncated."
    return result


async def _async_list_views() -> Dict[str, Any]:
    import aiohttp

    jf_url, _, _ = _get_config()
    token = await _get_token()
    user_id = await _resolve_user_id()

    session = await _get_session()
    async with session.get(
        f"{jf_url}/Users/{_safe_path_segment(user_id)}/Views",
        headers=_get_headers(token),
        timeout=aiohttp.ClientTimeout(total=10),
    ) as resp:
        resp.raise_for_status()
        data = await resp.json()

    views = []
    for item in data.get("Items", []):
        views.append({
            "id": item.get("Id"),
            "name": item.get("Name"),
            "collection_type": item.get("CollectionType"),
        })

    return {"views": views}


async def _async_browse_folder(
    parent_id: str,
    media_type: Optional[str] = None,
    limit: int = 50,
) -> Dict[str, Any]:
    import aiohttp

    jf_url, _, _ = _get_config()
    token = await _get_token()
    user_id = await _resolve_user_id()

    params = {
        "ParentId": _safe_path_segment(parent_id),
        "Fields": _COMMON_FIELDS,
        "SortBy": "Name",
        "SortOrder": "Ascending",
        "Limit": str(min(max(limit, 1), 100)),
    }
    if media_type:
        params["IncludeItemTypes"] = media_type

    session = await _get_session()
    async with session.get(
        f"{jf_url}/Users/{_safe_path_segment(user_id)}/Items",
        headers=_get_headers(token),
        params=params,
        timeout=aiohttp.ClientTimeout(total=15),
    ) as resp:
        resp.raise_for_status()
        data = await resp.json()

    items = [_format_item(i) for i in data.get("Items", [])]
    return {
        "parent_id": parent_id,
        "total": data.get("TotalRecordCount", len(items)),
        "items": items,
    }


async def _async_genres(media_type: Optional[str] = None) -> Dict[str, Any]:
    import aiohttp

    jf_url, _, _ = _get_config()
    token = await _get_token()
    user_id = await _resolve_user_id()

    params = {
        "UserId": user_id,
        "SortBy": "SortName",
        "SortOrder": "Ascending",
        "Limit": "500",
    }
    if media_type:
        params["IncludeItemTypes"] = media_type

    session = await _get_session()
    async with session.get(
        f"{jf_url}/Genres",
        headers=_get_headers(token),
        params=params,
        timeout=aiohttp.ClientTimeout(total=15),
    ) as resp:
        resp.raise_for_status()
        data = await resp.json()

    genres = [{"name": i.get("Name"), "id": i.get("Id")} for i in data.get("Items", [])]
    return {"total": data.get("TotalRecordCount", len(genres)), "genres": genres}


async def _async_studios(media_type: Optional[str] = None) -> Dict[str, Any]:
    import aiohttp

    jf_url, _, _ = _get_config()
    token = await _get_token()
    user_id = await _resolve_user_id()

    params = {
        "UserId": user_id,
        "SortBy": "SortName",
        "SortOrder": "Ascending",
        "Limit": "500",
    }
    if media_type:
        params["IncludeItemTypes"] = media_type

    session = await _get_session()
    async with session.get(
        f"{jf_url}/Studios",
        headers=_get_headers(token),
        params=params,
        timeout=aiohttp.ClientTimeout(total=15),
    ) as resp:
        resp.raise_for_status()
        data = await resp.json()

    studios = [{"name": i.get("Name"), "id": i.get("Id")} for i in data.get("Items", [])]
    return {"total": data.get("TotalRecordCount", len(studios)), "studios": studios}


def _handle_get_seasons(args: dict, **kw) -> str:
    series_id = args.get("series_id", "")
    if not series_id:
        return json.dumps({"error": "Missing required parameter: series_id"})
    try:
        result = _run_async(_async_get_seasons(series_id))
        return json.dumps({"result": result})
    except Exception as e:
        return _jellyfin_error(e, "jellyfin_get_seasons")


def _handle_get_episodes(args: dict, **kw) -> str:
    series_id = args.get("series_id", "")
    if not series_id:
        return json.dumps({"error": "Missing required parameter: series_id"})
    try:
        result = _run_async(_async_get_episodes(
            series_id=series_id,
            season_id=args.get("season_id"),
            limit=args.get("limit") or 50,
        ))
        return json.dumps({"result": result})
    except Exception as e:
        return _jellyfin_error(e, "jellyfin_get_episodes")


def _handle_next_up(args: dict, **kw) -> str:
    try:
        result = _run_async(_async_next_up(limit=args.get("limit") or 15))
        return json.dumps({"result": result})
    except Exception as e:
        return _jellyfin_error(e, "jellyfin_next_up")


def _handle_list_collections(args: dict, **kw) -> str:
    try:
        result = _run_async(_async_list_collections(limit=args.get("limit") or 50))
        return json.dumps({"result": result})
    except Exception as e:
        return _jellyfin_error(e, "jellyfin_list_collections")


def _handle_collection_items(args: dict, **kw) -> str:
    collection_id = args.get("collection_id", "")
    if not collection_id:
        return json.dumps({"error": "Missing required parameter: collection_id"})
    try:
        result = _run_async(_async_collection_items(collection_id))
        return json.dumps({"result": result})
    except Exception as e:
        return _jellyfin_error(e, "jellyfin_collection_items")


def _handle_list_views(args: dict, **kw) -> str:
    try:
        result = _run_async(_async_list_views())
        return json.dumps({"result": result})
    except Exception as e:
        return _jellyfin_error(e, "jellyfin_list_views")


def _handle_browse_folder(args: dict, **kw) -> str:
    parent_id = args.get("parent_id", "")
    if not parent_id:
        return json.dumps({"error": "Missing required parameter: parent_id"})
    try:
        result = _run_async(_async_browse_folder(
            parent_id=parent_id,
            media_type=args.get("media_type"),
            limit=args.get("limit") or 50,
        ))
        return json.dumps({"result": result})
    except Exception as e:
        return _jellyfin_error(e, "jellyfin_browse_folder")


def _handle_genres(args: dict, **kw) -> str:
    try:
        result = _run_async(_async_genres(media_type=args.get("media_type")))
        return json.dumps({"result": result})
    except Exception as e:
        return _jellyfin_error(e, "jellyfin_genres")


def _handle_studios(args: dict, **kw) -> str:
    try:
        result = _run_async(_async_studios(media_type=args.get("media_type")))
        return json.dumps({"result": result})
    except Exception as e:
        return _jellyfin_error(e, "jellyfin_studios")


def _check_jellyfin_available() -> bool:
    """Tool is available when JELLYFIN_API_KEY or JELLYFIN_USER+PASSWORD are set."""
    return bool(os.getenv("JELLYFIN_API_KEY")) or bool(
        os.getenv("JELLYFIN_USER") and os.getenv("JELLYFIN_PASSWORD")
    )


JELLYFIN_SEARCH_SCHEMA = {
    "name": "jellyfin_search",
    "description": (
        "Search the Jellyfin media library. Supports filtering by title, genre, "
        "year, media type, studio, content rating, runtime range, and person name. "
        "Returns matching items with title, year, rating, genres, overview, runtime, "
        "and provider IDs (IMDB, TMDB, TVDB)."
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
            "studio": {
                "type": "string",
                "description": "Filter by studio name (case-insensitive partial match, e.g. 'Warner').",
            },
            "official_rating": {
                "type": "string",
                "description": "Filter by content rating (e.g. 'PG', 'R', 'TV-MA').",
            },
            "min_runtime_minutes": {
                "type": "integer",
                "description": "Minimum runtime in minutes.",
            },
            "max_runtime_minutes": {
                "type": "integer",
                "description": "Maximum runtime in minutes.",
            },
            "person": {
                "type": "string",
                "description": "Filter by person name (actor, director, etc.).",
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


JELLYFIN_ALL_MOVIES_SCHEMA = {
    "name": "jellyfin_all_movies",
    "description": (
        "Fetch the complete list of all movies in the Jellyfin library (title, year, "
        "IMDB/TMDB/TVDB IDs). Use this for cross-referencing against external lists like "
        "IMDB Top 250 — provider IDs allow exact matching without fuzzy title comparison. "
        "Use this instead of search when you need the complete list."
    ),
    "parameters": {
        "type": "object",
        "properties": {},
        "required": [],
    },
}

JELLYFIN_GET_SEASONS_SCHEMA = {
    "name": "jellyfin_get_seasons",
    "description": (
        "Get all seasons for a TV series. Returns season names, numbers, "
        "and basic metadata with season IDs for further exploration."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "series_id": {
                "type": "string",
                "description": "The Jellyfin item ID of the series (from search results).",
            },
        },
        "required": ["series_id"],
    },
}

JELLYFIN_GET_EPISODES_SCHEMA = {
    "name": "jellyfin_get_episodes",
    "description": (
        "Get episodes for a TV series. Optionally filter by season ID. "
        "Returns episode titles, numbers, overviews, and runtimes."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "series_id": {
                "type": "string",
                "description": "The Jellyfin item ID of the series.",
            },
            "season_id": {
                "type": "string",
                "description": "Optional season ID to filter episodes to a specific season.",
            },
            "limit": {
                "type": "integer",
                "description": "Max episodes to return (1-100). Default: 50.",
            },
        },
        "required": ["series_id"],
    },
}

JELLYFIN_NEXT_UP_SCHEMA = {
    "name": "jellyfin_next_up",
    "description": (
        "Get the Next Up queue — next episode to watch for series with recent viewing. "
        "Note: returns empty results if the plugin user has no watch history."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "limit": {
                "type": "integer",
                "description": "Max results (1-30). Default: 15.",
            },
        },
        "required": [],
    },
}

JELLYFIN_LIST_COLLECTIONS_SCHEMA = {
    "name": "jellyfin_list_collections",
    "description": (
        "List all collections (BoxSets) in the Jellyfin library. "
        "Returns collection names and item counts."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "limit": {
                "type": "integer",
                "description": "Max collections to return (1-100). Default: 50.",
            },
        },
        "required": [],
    },
}

JELLYFIN_COLLECTION_ITEMS_SCHEMA = {
    "name": "jellyfin_collection_items",
    "description": (
        "Get all items inside a Jellyfin collection. Returns full metadata "
        "for each item including provider IDs."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "collection_id": {
                "type": "string",
                "description": "The Jellyfin item ID of the collection (from jellyfin_list_collections).",
            },
        },
        "required": ["collection_id"],
    },
}

JELLYFIN_LIST_VIEWS_SCHEMA = {
    "name": "jellyfin_list_views",
    "description": (
        "Get top-level library views (e.g. Movies, TV Shows, Collections). "
        "Returns view IDs for navigating into each section."
    ),
    "parameters": {
        "type": "object",
        "properties": {},
        "required": [],
    },
}

JELLYFIN_BROWSE_FOLDER_SCHEMA = {
    "name": "jellyfin_browse_folder",
    "description": (
        "Browse items inside a specific library folder. Pass a parent folder ID "
        "to see its children. Optionally filter by media type."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "parent_id": {
                "type": "string",
                "description": "The Jellyfin ID of the parent folder (from list_views or a previous browse).",
            },
            "media_type": {
                "type": "string",
                "enum": ["Movie", "Series", "Episode"],
                "description": "Optional media type filter.",
            },
            "limit": {
                "type": "integer",
                "description": "Max items to return (1-100). Default: 50.",
            },
        },
        "required": ["parent_id"],
    },
}

JELLYFIN_GENRES_SCHEMA = {
    "name": "jellyfin_genres",
    "description": (
        "List all genres available in the Jellyfin library. Optionally filter "
        "by media type to see genres with counts for specific content."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "media_type": {
                "type": "string",
                "enum": ["Movie", "Series", "Episode"],
                "description": "Optional media type to scope genre listing.",
            },
        },
        "required": [],
    },
}

JELLYFIN_STUDIOS_SCHEMA = {
    "name": "jellyfin_studios",
    "description": (
        "List all studios represented in the Jellyfin library. Optionally filter "
        "by media type to see studios for specific content."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "media_type": {
                "type": "string",
                "enum": ["Movie", "Series", "Episode"],
                "description": "Optional media type to scope studio listing.",
            },
        },
        "required": [],
    },
}


JELLYFIN_TOOLS = [
    ("jellyfin_search", JELLYFIN_SEARCH_SCHEMA, _handle_search),
    ("jellyfin_library_stats", JELLYFIN_LIBRARY_STATS_SCHEMA, _handle_library_stats),
    ("jellyfin_get_details", JELLYFIN_GET_DETAILS_SCHEMA, _handle_get_details),
    ("jellyfin_similar", JELLYFIN_SIMILAR_SCHEMA, _handle_similar),
    ("jellyfin_recent", JELLYFIN_RECENT_SCHEMA, _handle_recent),
    ("jellyfin_all_movies", JELLYFIN_ALL_MOVIES_SCHEMA, _handle_all_movies),
    ("jellyfin_get_seasons", JELLYFIN_GET_SEASONS_SCHEMA, _handle_get_seasons),
    ("jellyfin_get_episodes", JELLYFIN_GET_EPISODES_SCHEMA, _handle_get_episodes),
    ("jellyfin_next_up", JELLYFIN_NEXT_UP_SCHEMA, _handle_next_up),
    ("jellyfin_list_collections", JELLYFIN_LIST_COLLECTIONS_SCHEMA, _handle_list_collections),
    ("jellyfin_collection_items", JELLYFIN_COLLECTION_ITEMS_SCHEMA, _handle_collection_items),
    ("jellyfin_list_views", JELLYFIN_LIST_VIEWS_SCHEMA, _handle_list_views),
    ("jellyfin_browse_folder", JELLYFIN_BROWSE_FOLDER_SCHEMA, _handle_browse_folder),
    ("jellyfin_genres", JELLYFIN_GENRES_SCHEMA, _handle_genres),
    ("jellyfin_studios", JELLYFIN_STUDIOS_SCHEMA, _handle_studios),
]

CHECK_FN = _check_jellyfin_available
EMOJI = "🎬"
