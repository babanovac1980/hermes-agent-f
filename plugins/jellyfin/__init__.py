"""Jellyfin plugin — registers fifteen tools for querying a personal Jellyfin library.

Tools are imported from ``jellyfin_client`` (the ported ``tools/jellyfin_tool.py``).
All handlers are sync wrappers around aiohttp coroutines, so ``is_async=False``.
"""

from .jellyfin_client import JELLYFIN_TOOLS, CHECK_FN, EMOJI


def register(ctx):
    for name, schema, handler in JELLYFIN_TOOLS:
        ctx.register_tool(
            name=name,
            toolset="jellyfin",
            schema=schema,
            handler=handler,
            check_fn=CHECK_FN,
            requires_env=["JELLYFIN_API_KEY"],
            is_async=False,
            emoji=EMOJI,
        )
