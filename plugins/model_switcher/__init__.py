"""Model Switcher plugin — adds the /model slash command.

Usage:
  /model                  List available models (Anthropic + free OpenRouter)
  /model <model_name>     Switch directly to any model by name
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def register(ctx):
    """Called by the plugin system to register the /model command."""
    ctx.register_command(
        name="model",
        handler=_handle_model_command,
        description="List models or switch by name",
        args_hint="[model_name]",
    )


# ---------------------------------------------------------------------------
# Command handler
# ---------------------------------------------------------------------------

def _handle_model_command(args: str) -> str | None:
    """Handle /model [model_name]."""
    args = args.strip()
    if args:
        return _switch_direct(args)
    else:
        return _list_models()


# ---------------------------------------------------------------------------
# Direct switch: /model <name>
# ---------------------------------------------------------------------------

def _switch_direct(raw_model: str) -> str | None:
    cli = _get_cli()
    if cli is None:
        return "  Model switching is only available in CLI mode."

    from hermes_cli.model_switch import switch_model

    result = switch_model(
        raw_model,
        current_provider=cli.provider,
        current_base_url=cli.base_url or "",
        current_api_key=cli.api_key or "",
    )

    if not result.success:
        return f"  Switch failed: {result.error_message}"

    _apply_switch(cli, result)
    msg = f"  Switched to {result.new_model} via {result.provider_label}"
    if result.warning_message:
        msg += f"\n  Note: {result.warning_message}"
    return msg


# ---------------------------------------------------------------------------
# List models: /model (no args)
# ---------------------------------------------------------------------------

def _list_models() -> str:
    cli = _get_cli()
    current_model = cli.model if cli else "unknown"

    lines: list[str] = []
    lines.append(f"  Current model: {current_model}")
    lines.append("")

    # Anthropic
    anthropic = _get_anthropic_models()
    if anthropic:
        lines.append("  -- Anthropic (direct API) --")
        for mid in anthropic:
            marker = "  <-- current" if mid == current_model else ""
            lines.append(f"    {mid}{marker}")
        lines.append("")

    # OpenRouter free
    openrouter = _get_openrouter_free_models()
    if openrouter:
        lines.append("  -- OpenRouter (free) --")
        for mid in openrouter:
            marker = "  <-- current" if mid == current_model else ""
            lines.append(f"    {mid}{marker}")
        lines.append("")

    lines.append("  Usage: /model <model_name>")
    lines.append("  Examples:")
    lines.append("    /model claude-sonnet-4-6")
    lines.append("    /model openrouter:google/gemma-3-27b-it:free")
    lines.append("    /model anthropic:claude-opus-4-6")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Model list builders
# ---------------------------------------------------------------------------

def _get_anthropic_models() -> list[str]:
    """Get Anthropic models — live API first, static fallback."""
    try:
        from hermes_cli.models import _fetch_anthropic_models
        live = _fetch_anthropic_models(timeout=3.0)
        if live:
            return live
    except Exception:
        pass
    from hermes_cli.models import _PROVIDER_MODELS
    return list(_PROVIDER_MODELS.get("anthropic", []))


def _get_openrouter_free_models() -> list[str]:
    """Get free models from OpenRouter API (live), falling back to curated list."""
    try:
        from agent.model_metadata import fetch_model_metadata
        metadata = fetch_model_metadata()
        free = [
            model_id for model_id, entry in metadata.items()
            if "/" in model_id
            and str(entry.get("pricing", {}).get("prompt", "1")) in ("0", "0.0")
        ]
        if free:
            return sorted(free)
    except Exception:
        pass
    from hermes_cli.models import OPENROUTER_MODELS
    return [mid for mid, desc in OPENROUTER_MODELS if ":free" in mid or desc == "free"]


# ---------------------------------------------------------------------------
# Apply switch + persist
# ---------------------------------------------------------------------------

def _apply_switch(cli, result) -> None:
    """Apply a successful ModelSwitchResult to the CLI and persist to config."""
    cli.model = result.new_model
    cli.provider = result.target_provider
    if result.api_key:
        cli.api_key = result.api_key
    if result.base_url:
        cli.base_url = result.base_url
    if result.api_mode:
        cli.api_mode = result.api_mode
    cli.agent = None  # force re-init on next message
    _persist_model(result.new_model, result.target_provider, result.base_url or "")


def _persist_model(model: str, provider: str, base_url: str) -> None:
    """Write the model choice to config.yaml so it survives restarts."""
    try:
        from hermes_cli.config import load_config, save_config
        config = load_config()
        model_cfg = config.get("model", {})
        if not isinstance(model_cfg, dict):
            model_cfg = {"default": model_cfg}
        model_cfg["default"] = model
        model_cfg["provider"] = provider
        if base_url:
            model_cfg["base_url"] = base_url
        config["model"] = model_cfg
        save_config(config)
    except Exception as e:
        logger.warning("Failed to persist model to config: %s", e)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_cli():
    """Get the CLI instance from the plugin manager."""
    try:
        from hermes_cli.plugins import get_plugin_manager
        return get_plugin_manager()._cli_ref
    except Exception:
        return None
