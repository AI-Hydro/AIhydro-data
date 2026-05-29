"""
Workflow skills shipped with this package.

Registered via the `aihydro.skills` entry-point in pyproject.toml so the
aihydro-tools MCP server can offer them through SKILL DISCOVERY.
"""
from __future__ import annotations

from pathlib import Path


def get_skills_dir() -> Path:
    """Entry-point callable referenced from pyproject.toml.
    Returns the directory containing SKILL.md files bundled with the package."""
    return Path(__file__).parent
