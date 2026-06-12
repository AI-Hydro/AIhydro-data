#!/usr/bin/env python3
"""Generate the Products section of README.md from the live product registry.

Usage:
    python scripts/gen_product_tables.py           # print to stdout
    python scripts/gen_product_tables.py --check   # check counts match README

The output is the full "## Products" section as Markdown. Pipe into a file or
paste into README.md to keep it in sync with the registry.
"""
from __future__ import annotations

import sys
import textwrap
from collections import defaultdict
from pathlib import Path


def _load() -> dict:
    import aihydro_data.products as p
    p._load_registry()
    return dict(p._REGISTRY)


def _table(products: list, *, extra_cols: list[str] | None = None) -> str:
    cols: list[tuple[str, str]] = [
        ("ID", "id"),
        ("Source", "source"),
        ("Coverage", "coverage"),
        ("Resolution", "resolution_m"),
        ("Timestep", "timestep"),
    ]
    if extra_cols:
        for header, attr in extra_cols:
            cols.append((header, attr))
    cols.append(("Notes", "notes"))

    def _coverage(spec):
        c = getattr(spec, "coverage", [])
        return " / ".join(c) if c else "—"

    def _resolution(spec):
        r = getattr(spec, "resolution_m", None)
        if r is None:
            return "—"
        if r >= 1000:
            return f"{r // 1000} km"
        return f"{r} m"

    def _notes(spec):
        bits = []
        if "gee" in getattr(spec, "requires_auth", []):
            bits.append("GEE auth required")
        if not getattr(spec, "requires_auth", []):
            bits.append("auth-free")
        sup = getattr(spec, "spatial_support", "areal")
        if sup != "areal":
            bits.append(f"spatial support: {sup}")
        return "; ".join(bits) or "—"

    def _val(spec, attr):
        if attr == "coverage":
            return _coverage(spec)
        if attr == "resolution_m":
            return _resolution(spec)
        if attr == "notes":
            return _notes(spec)
        v = getattr(spec, attr, "—")
        if v is None:
            return "—"
        if attr == "id":
            return f"`{v}`"
        return str(v)

    header = "| " + " | ".join(h for h, _ in cols) + " |"
    sep = "|" + "|".join("---" for _ in cols) + "|"
    rows = []
    for spec in products:
        rows.append("| " + " | ".join(_val(spec, a) for _, a in cols) + " |")
    return "\n".join([header, sep] + rows)


# Variable display order and pretty names
_VARIABLE_ORDER = [
    ("precipitation", "Precipitation"),
    ("tmax", "Temperature — Tmax"),
    ("tmin", "Temperature — Tmin"),
    ("tmean", "Temperature — Tmean"),
    ("pet", "Potential Evapotranspiration (PET)"),
    ("et", "Actual Evapotranspiration (ET)"),
    ("dem", "DEM"),
    ("soil_moisture", "Soil Moisture"),
    ("landcover", "Land Cover"),
    ("soil", "Soil Properties"),
    ("ndvi", "NDVI"),
    ("lai", "LAI"),
    ("optical", "Optical"),
    ("streamflow", "Streamflow"),
]


def generate(registry: dict) -> str:
    by_var: dict[str, list] = defaultdict(list)
    for spec in registry.values():
        by_var[spec.variable].append(spec)

    total = len(registry)
    n_vars = len(by_var)
    lines = [
        f"## Products",
        "",
        f"{total} products across {n_vars} variables (v0.2.0).",
        "",
    ]

    for var_key, var_label in _VARIABLE_ORDER:
        specs = by_var.get(var_key)
        if not specs:
            continue
        n = len(specs)
        lines.append(f"### {var_label} ({n} product{'s' if n != 1 else ''})")
        lines.append("")
        lines.append(_table(specs))
        lines.append("")

    # any variables not in the explicit order
    for var_key, specs in sorted(by_var.items()):
        if any(k == var_key for k, _ in _VARIABLE_ORDER):
            continue
        n = len(specs)
        lines.append(f"### {var_key.replace('_', ' ').title()} ({n} product{'s' if n != 1 else ''})")
        lines.append("")
        lines.append(_table(specs))
        lines.append("")

    return "\n".join(lines)


def check_readme(registry: dict) -> bool:
    """Return True if README.md counts match the live registry."""
    import re
    readme = (Path(__file__).parent.parent / "README.md").read_text()
    total = len(registry)
    n_vars = len({s.variable for s in registry.values()})

    pattern = r"(\d+) products across (\d+) variables"
    matches = re.findall(pattern, readme)
    ok = True
    for count_str, var_str in matches:
        if int(count_str) != total:
            print(f"FAIL: README says '{count_str} products' but registry has {total}")
            ok = False
        if int(var_str) != n_vars:
            print(f"FAIL: README says '{var_str} variables' but registry has {n_vars}")
            ok = False
    if ok:
        print(f"OK: README counts match registry ({total} products across {n_vars} variables)")
    return ok


if __name__ == "__main__":
    registry = _load()
    if "--check" in sys.argv:
        ok = check_readme(registry)
        sys.exit(0 if ok else 1)
    else:
        print(generate(registry))
