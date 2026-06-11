"""
Layering contract test — aihydro-data may depend DOWN on core, never SIDEWAYS.

The three-layer architecture:

    ai_hydro (tools)  →  aihydro_data  →  aihydro_core
                              └────────────────►  (DOWN: allowed)

aihydro_data sits above aihydro_core and beside the ai_hydro tools package.
It is allowed to import aihydro_core (the shared substrate — e.g. content_hash),
but must NEVER import ai_hydro: that would be a sideways/upward edge that
couples the data engine to one specific domain consumer.

The vendored GEE snapshot under sources/_gee_vendored is excluded — it is a
frozen third-party reference copy, not first-party data code.

Runs offline with zero extra dependencies (uses ``ast``). import-linter
(configured in pyproject.toml) gives the same guarantee when installed; this
test is the always-on floor.
"""
from __future__ import annotations

import ast
from pathlib import Path

_DATA_ROOT = Path(__file__).resolve().parent.parent / "aihydro_data"
_VENDORED = _DATA_ROOT / "sources" / "_gee_vendored"
_FORBIDDEN_TOP_LEVEL = {"ai_hydro"}


def _python_files() -> list[Path]:
    return [p for p in sorted(_DATA_ROOT.rglob("*.py")) if _VENDORED not in p.parents]


def _forbidden_imports(tree: ast.AST) -> list[str]:
    bad: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in _FORBIDDEN_TOP_LEVEL:
                    bad.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.module.split(".")[0] in _FORBIDDEN_TOP_LEVEL:
                bad.append(node.module)
    return bad


def test_data_does_not_import_tools_package():
    """No file under aihydro_data may import the ai_hydro domain pack."""
    offenders: dict[str, list[str]] = {}
    for path in _python_files():
        tree = ast.parse(path.read_text(), filename=str(path))
        bad = _forbidden_imports(tree)
        if bad:
            offenders[str(path.relative_to(_DATA_ROOT))] = bad

    assert not offenders, (
        "aihydro-data must not import the ai_hydro tools package "
        "(sideways/upward edge):\n"
        + "\n".join(f"  {f}: {mods}" for f, mods in offenders.items())
    )


def test_data_uses_core_hashing():
    """The downward edge exists: cache_key routes through aihydro_core."""
    from aihydro_data.cache import cache_key
    from aihydro_core.primitives.hashing import content_hash

    payload = {"variable": "precipitation", "product": "CHIRPS", "x": 1}
    # data's 24-char key must equal core's content_hash at length=24 — proving
    # they hash through the same implementation, not two drifting copies.
    assert cache_key(payload) == content_hash(payload, length=24)
    assert len(cache_key(payload)) == 24
