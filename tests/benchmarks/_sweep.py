"""Global robustness sweep: run aihydro_data.fetch(mode='auto') for every core
variable across the benchmark basins, recording region routing + served product
+ outcome. Produces sweep_results.json + a printed matrix.

Run: python tests/benchmarks/_sweep.py
"""
from __future__ import annotations

import json
import time
from pathlib import Path

HERE = Path(__file__).parent
BASINS = HERE / "basins.json"
OUT = HERE / "sweep_results.json"

# Core hydrologic variables we expect to be globally robust (auto-routed).
# streamflow is US-only (NWIS) by design — included to confirm it fails
# *gracefully* (structured RegionUnsupported) outside CONUS, not to pass.
VARIABLES = [
    "precipitation", "tmax", "tmin", "pet", "et",
    "dem", "landcover", "soil", "ndvi", "lai", "streamflow",
]

# Raster-heavy variables that return full spatial grids (not basin_mean scalars).
# Skip these for very large basins (>50 000 km²) — the rasters are enormous
# (tens of GB), hit GEE payload limits anyway, and are not useful benchmarks.
RASTER_VARS = {"dem", "landcover", "soil"}
AREA_RASTER_LIMIT_KM2 = 50_000

START, END = "2021-06-01", "2021-06-10"


def shape_of(data) -> str:
    try:
        if hasattr(data, "shape"):
            return f"{type(data).__name__}{tuple(data.shape)}"
        if hasattr(data, "dims"):
            return f"{type(data).__name__}{dict(data.sizes)}"
    except Exception:
        pass
    return type(data).__name__


def main() -> int:
    import aihydro_data as A
    from aihydro_data.routing import detect_region
    from aihydro_data.geometry import coerce_geometry
    from aihydro_data.exceptions import AihydroDataError

    basins = json.loads(BASINS.read_text())
    results: dict = json.loads(OUT.read_text()) if OUT.exists() else {}

    for key, b in basins.items():
        geom = b.get("geometry")
        if not geom:
            print(f"[skip basin] {key}: no geometry ({b.get('error')})")
            continue

        area_km2 = b.get("area_km2") or 0.0
        is_stress_basin = area_km2 > AREA_RASTER_LIMIT_KM2

        try:
            region = detect_region(coerce_geometry(geom))
        except Exception as e:
            region = f"<detect_failed:{e}>"

        # Build the variable list for this basin
        basin_vars = [
            v for v in VARIABLES
            if not (is_stress_basin and v in RASTER_VARS)
        ]

        # Skip if already fully swept
        if key in results and set(results[key].get("vars", {}).keys()) >= set(basin_vars):
            print(f"[skip sweep] {key} already complete", flush=True)
            continue

        tag = "STRESS" if is_stress_basin else ""
        print(f"\n=== {key}  region={region}  area={area_km2:,.0f} km² "
              f"(via {b.get('method_used')}) {tag} ===", flush=True)
        if is_stress_basin:
            print(f"  [raster vars skipped for basin > {AREA_RASTER_LIMIT_KM2:,} km²]",
                  flush=True)

        if key not in results:
            results[key] = {
                "region": region,
                "area_km2": area_km2,
                "delineation_method": b.get("method_used"),
                "vars": {},
            }

        for var in basin_vars:
            # Skip already-recorded entries
            if var in results[key].get("vars", {}):
                print(f"  {var:14s} [cached result, skip]", flush=True)
                continue

            t0 = time.time()
            rec: dict = {}
            # Never cache raster fetches for large basins (even smaller ones)
            # to avoid disk bloat; time-series basin_mean results are tiny.
            use_cache = var not in RASTER_VARS
            try:
                r = A.fetch(var, geom, START, END, mode="auto",
                            aggregation="basin_mean", cache=use_cache)
                rec = {
                    "ok": True,
                    "product": r.product,
                    "source": r.source,
                    "shape": shape_of(r.data),
                    "n_fallbacks": len(r.fallback_history or []),
                    "secs": round(time.time() - t0, 1),
                }
                print(f"  {var:14s} OK    {r.product:22s} [{r.source:10s}] "
                      f"{rec['shape']:18s} ({rec['secs']}s)", flush=True)
            except AihydroDataError as e:
                rec = {
                    "ok": False,
                    "error_type": type(e).__name__,
                    "code": getattr(e, "code", None),
                    "msg": str(e)[:160],
                    "secs": round(time.time() - t0, 1),
                }
                print(f"  {var:14s} ERR   {type(e).__name__}:{getattr(e,'code',None)} "
                      f"— {str(e)[:90]}", flush=True)
            except Exception as e:
                rec = {
                    "ok": False,
                    "error_type": type(e).__name__,
                    "msg": str(e)[:160],
                    "secs": round(time.time() - t0, 1),
                }
                print(f"  {var:14s} CRASH {type(e).__name__}: {str(e)[:90]}", flush=True)

            results[key]["vars"][var] = rec
            OUT.write_text(json.dumps(results, indent=2))

    OUT.write_text(json.dumps(results, indent=2))
    print(f"\nWrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
