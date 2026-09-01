"""Agent attribution for alternate participant names."""

from __future__ import annotations

import os


def agent_display_name() -> str:
    """Model/agent label shown when using alternate_suffix login.

    Set WHEN2MEET_AGENT_NAME in MCP config, e.g. Claude, Composer, GPT-4.
    Defaults to Cursor.
    """
    return os.environ.get("WHEN2MEET_AGENT_NAME", "Cursor").strip() or "Cursor"


def alternate_participant_name(name: str, agent_name: str | None = None) -> str:
    label = (agent_name or agent_display_name()).strip()
    return f"{name} ({label})"
