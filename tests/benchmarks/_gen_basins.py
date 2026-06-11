"""Generate benchmark watershed fixtures by delineating a spread of global outlets.

Writes basins.json: {key: {lat, lon, region_hint, method_used, area_km2, geometry}}.
Run: python tests/benchmarks/_gen_basins.py
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

OUT = Path(__file__).parent / "basins.json"

# Outlets chosen to be MODERATE basins spread across routing regions, plus two
# stress cases (huge Congo, plus a CONUS reference). (key, lat, lon, hint)
OUTLETS = [
    ("conus_boulder",     40.0500, -105.2000, "north_america/CONUS"),
    ("africa_congo_trib", -3.5000,   23.0000, "africa"),
    ("africa_congo_full", -4.3000,   15.3000, "africa (stress: whole Congo)"),
    ("europe_alps",       46.8000,    9.8000, "europe"),
    ("sasia_nepal",       27.8000,   85.3000, "south_asia"),
    ("samerica_andes",   -13.5000,  -72.0000, "south_america"),
    ("australia_se",     -36.5000,  148.3000, "oceania"),
]


def main() -> int:
    from ai_hydro.analysis.delineation import delineate_from_point

    out: dict = {}
    if OUT.exists():
        out = json.loads(OUT.read_text())

    for key, lat, lon, hint in OUTLETS:
        if key in out and out[key].get("geometry"):
            print(f"[skip] {key} already present", flush=True)
            continue
        t0 = time.time()
        try:
            r = delineate_from_point(lat=lat, lon=lon, method="auto")
            d = r.data
            out[key] = {
                "lat": lat, "lon": lon, "region_hint": hint,
                "method_used": d.get("method_used"),
                "area_km2": round(d.get("area_km2", 0.0), 2),
                "quality_flags": d.get("quality_flags"),
                "geometry": d.get("geometry_geojson"),
            }
            print(f"[ok]   {key:18s} {d.get('method_used'):22s} "
                  f"area={out[key]['area_km2']:>12.1f} km2 ({time.time()-t0:.0f}s)",
                  flush=True)
        except Exception as e:
            out[key] = {"lat": lat, "lon": lon, "region_hint": hint,
                        "error": f"{type(e).__name__}: {e}"}
            print(f"[FAIL] {key:18s} {type(e).__name__}: {e} ({time.time()-t0:.0f}s)",
                  flush=True)
        OUT.write_text(json.dumps(out, indent=2))

    print(f"\nWrote {OUT} ({len(out)} basins)", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
