import json, sys, time, unittest.mock as um
import pandas as pd
from shapely.geometry import box
import aihydro_data
from aihydro_data.sources import hyriver, _gridmet_ncss as g
assert "data-gridmet-fix" in aihydro_data.__file__, aihydro_data.__file__
FISH = box(-68.966, 46.684, -68.073, 47.316)
SPEC = type("S", (), {"variable": "precipitation", "source": "hyriver", "backend_config": {"pygridmet_variable": "pr"}})()
PRIOR = 7          # requests already spent on probes (A,B,A,A,B,D,E) against the 50 cap
CAP = 50 - PRIOR
n = [0]
def capped(url, timeout):
    if n[0] >= CAP: raise SystemExit("request cap reached")
    n[0] += 1
    return g._requests_get(url, timeout)
def fetch(start, end):
    cl = g.NcssClient(http_get=capped, min_interval_s=1.5)
    with um.patch.object(hyriver, "_gridmet_client", lambda: cl):
        t = time.time()
        df = hyriver.Backend()._fetch_gridmet(SPEC, {"pygridmet_variable": "pr"}, FISH, start, end, "basin_mean")
    return df, cl, round(time.time() - t, 1)
out = {}
for label, (a, b) in {"WY2000": ("1999-10-01", "2000-09-30"), "CY2000_leap_dec31": ("2000-01-01", "2000-12-31")}.items():
    df, cl, secs = fetch(a, b)
    exp = pd.date_range(a, b)
    s = df.set_index("date")["precipitation"]
    r = dict(window=[a, b], days_expected=len(exp), days_returned=len(df), missing=[str(d.date()) for d in exp.difference(df["date"])],
             seconds=secs, requests=len(cl.ledger.request_urls), nan_days=int(s.isna().sum()),
             mean_mm_day=round(float(s.mean()), 4), total_mm=round(float(s.sum()), 1),
             basin_mean=cl.ledger.upstream()["gridmet_ncss"]["basin_mean"], rejected=cl.ledger.rejected, splits=cl.ledger.splits,
             chunk_validation=df.attrs.get("aihydro_chunk_validation"), notes=df.attrs.get("aihydro_notes"),
             response_sha256=cl.ledger.response_sha256)
    if label.startswith("CY2000"):
        r["2000-12-30"] = float(s[pd.Timestamp("2000-12-30")]); r["2000-12-31"] = float(s[pd.Timestamp("2000-12-31")])
        r["2000-02-29"] = float(s[pd.Timestamp("2000-02-29")])
    out[label] = r
out["requests_this_script"] = n[0]; out["requests_prior_probes"] = PRIOR; out["requests_total"] = n[0] + PRIOR
json.dump(out, open(__file__.replace("run_live.py", "live_result.json"), "w"), indent=1)
print(json.dumps(out, indent=1)[:6000])
