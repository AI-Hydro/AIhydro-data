"""Print a formatted summary table from sweep_results.json.

Run: python tests/benchmarks/_report.py
"""
from __future__ import annotations
import json
from pathlib import Path

HERE = Path(__file__).parent
SWEEP = HERE / "sweep_results.json"
BASINS = HERE / "basins.json"

# ANSI colours (terminal)
G = "\033[92m"   # green
R = "\033[91m"   # red
Y = "\033[93m"   # yellow
B = "\033[94m"   # blue
D = "\033[90m"   # dim
N = "\033[0m"    # reset


def cell(rec: dict) -> tuple[str, str]:
    """Return (display_str, raw_status) for a variable result record."""
    if rec.get("ok"):
        prod = rec.get("product", "?")[:16]
        src  = rec.get("source", "?")[:8]
        return f"{G}✓{N} {prod}", "ok"
    err = rec.get("error_type", "CRASH")
    code = rec.get("code") or ""
    # Expected "failures" (structured error, not a bug)
    expected = {"RegionUnsupported", "DateOutOfRange", "SourceUnavailable"}
    colour = Y if err in expected else R
    short = (code or err)[:18]
    return f"{colour}✗{N} {short}", "expected" if err in expected else "fail"


def main() -> None:
    basins_raw = json.loads(BASINS.read_text()) if BASINS.exists() else {}
    if not SWEEP.exists():
        print("No sweep_results.json yet — run _sweep.py first.")
        return
    results = json.loads(SWEEP.read_text())

    variables = []
    for bdata in results.values():
        for v in bdata.get("vars", {}):
            if v not in variables:
                variables.append(v)

    COL = 24
    VCOL = 14

    # Header
    print("\n" + "=" * (VCOL + COL * len(results)))
    print(f"{'Variable':<{VCOL}}", end="")
    for k, v in results.items():
        region = v.get("region", "?")
        area   = v.get("area_km2", "?")
        hdr = f"{k} [{region}]"[:COL-1]
        print(f"{hdr:<{COL}}", end="")
    print()
    print(f"{'area_km2':<{VCOL}}", end="")
    for v in results.values():
        a = v.get("area_km2", "?")
        print(f"{a:>{COL-1}} ", end="")
    print()
    print("-" * (VCOL + COL * len(results)))

    ok_count = fail_count = expected_count = 0
    for var in variables:
        print(f"{var:<{VCOL}}", end="")
        for bdata in results.values():
            vrec = bdata.get("vars", {}).get(var)
            if vrec is None:
                print(f"{'—':<{COL}}", end="")
                continue
            disp, status = cell(vrec)
            ok_count       += status == "ok"
            fail_count      += status == "fail"
            expected_count  += status == "expected"
            print(f"{disp:<{COL}}", end="")
        print()

    print("=" * (VCOL + COL * len(results)))
    total = ok_count + fail_count + expected_count
    print(f"\n  {G}Passed{N}: {ok_count}/{total}  "
          f"{Y}Expected-fail{N}: {expected_count}  "
          f"{R}Bugs{N}: {fail_count}")

    # Detailed failures section
    bugs = []
    for bkey, bdata in results.items():
        for var, vrec in bdata.get("vars", {}).items():
            if not vrec.get("ok"):
                err = vrec.get("error_type", "CRASH")
                code = vrec.get("code") or ""
                expected = {"RegionUnsupported", "DateOutOfRange", "SourceUnavailable"}
                if err not in expected:
                    bugs.append((bkey, var, err, code, vrec.get("msg", "")[:120]))
    if bugs:
        print(f"\n{R}Bug details:{N}")
        for bkey, var, err, code, msg in bugs:
            print(f"  {bkey}  {var}: {err}:{code} — {msg}")
    print()


if __name__ == "__main__":
    main()
