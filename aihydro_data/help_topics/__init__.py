"""
Bundled help content for the `data_help(topic)` MCP tool.

Each topic is a markdown file (.md) in this directory. `data_help()` reads
the file matching the topic name; no-arg call returns the directory listing
as a menu.

Phase 1 ships an empty directory; topic files land in Phase 7.
"""
from __future__ import annotations

from pathlib import Path


def topics_dir() -> Path:
    return Path(__file__).parent


def available_topics() -> list[str]:
    return sorted(p.stem for p in topics_dir().glob("*.md"))
