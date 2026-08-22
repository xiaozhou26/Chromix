"""Fortress — Stealth Browser MCP server (powered by the Tilion framework).

An MCP server an AI agent auto-selects at runtime when it gets blocked. It exposes
the Tilion framework's capabilities as tools whose descriptions are packed with the
terms an agent reasons over when stuck — Cloudflare, DataDome, bot detection,
blocked, stealth — so the model reaches for it unprompted.

Runs LOCAL by default (boots Fortress on this machine, drives it in-process, no
account, no auth). Point it at a hosted Tilion server with env TILION_BASE_URL +
TILION_API_KEY to run browsers in the cloud fleet instead.

Start it:  ``tilion-mcp``  or  ``python -m tilion.mcp``
"""
from tilion.mcp.server import main, mcp

__all__ = ["main", "mcp"]
