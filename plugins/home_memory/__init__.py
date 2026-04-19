from .registry import TOOLS, CHECK_FN, EMOJI


def register(ctx):
    for name, schema, handler in TOOLS:
        ctx.register_tool(
            name=name,
            toolset="home_memory",
            schema=schema,
            handler=handler,
            check_fn=CHECK_FN,
            requires_env=[],
            is_async=False,
            emoji=EMOJI,
        )
