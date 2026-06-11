"""
Phase D — shared robustness affordances on fetch().

Offline tests (no network) that pin the three capabilities lifted from
delineation/router.py into the generalized engine:

  1. fallback_history — every FetchResult records the ordered decision trail
     ({product, source, outcome, reason}) of the candidates the router tried,
     ending in the one that served.
  2. validate= callback — a caller can reject a successful-but-low-quality
     result and force the next product in the chain (quality escalation).
  3. exception envelope — when all candidates fail, the raised error carries
     recovery + next_tools and the fallback_history in details.

Strategy: drive real routing (precipitation/CONUS yields a multi-product
chain) but patch the single-product fetch (`_fetch_one`) so outcomes are
deterministic and offline.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest
from shapely.geometry import Point

from aihydro_data.contracts import FetchRequest, FetchResult


# A CONUS point → precipitation policy gives a real multi-candidate chain.
_GEOM = Point(-90.0, 40.0)


def _result_for(spec, req):
    """Build a minimal FetchResult standing in for spec's data."""
    import pandas as pd
    df = pd.DataFrame({"date": pd.date_range("2020-06-01", periods=3),
                       "precipitation": [1.0, 2.0, 3.0]})
    return FetchResult(
        variable=spec.variable, product=spec.id, source=spec.source,
        request=req, data=df, license=spec.license, citation=spec.citation,
    )


class TestFallbackHistory:
    def test_history_records_served_product(self):
        from aihydro_data import fetch

        def _fake_fetch_one(spec, geom, start, end, agg, req, **kw):
            return _result_for(spec, req)

        with patch("aihydro_data._pipeline._fetch_one", side_effect=_fake_fetch_one):
            r = fetch("precipitation", _GEOM, "2020-06-01", "2020-06-03", cache=False)

        assert r.fallback_history, "fallback_history must never be empty"
        last = r.fallback_history[-1]
        assert last["outcome"] == "served"
        assert last["product"] == r.product

    def test_history_records_failed_then_served(self):
        from aihydro_data import fetch

        calls = {"n": 0}

        def _fake_fetch_one(spec, geom, start, end, agg, req, **kw):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("primary boom")
            return _result_for(spec, req)

        with patch("aihydro_data._pipeline._fetch_one", side_effect=_fake_fetch_one):
            r = fetch("precipitation", _GEOM, "2020-06-01", "2020-06-03", cache=False)

        outcomes = [h["outcome"] for h in r.fallback_history]
        assert outcomes[0] == "failed"
        assert outcomes[-1] == "served"
        assert "primary boom" in r.fallback_history[0]["reason"]


class TestValidateCallback:
    def test_validate_rejection_forces_next(self):
        from aihydro_data import fetch

        def _fake_fetch_one(spec, geom, start, end, agg, req, **kw):
            return _result_for(spec, req)

        seen = {}

        def _reject_first(result):
            if not seen:
                seen["first"] = result.product
                return False
            return True

        with patch("aihydro_data._pipeline._fetch_one", side_effect=_fake_fetch_one):
            r = fetch("precipitation", _GEOM, "2020-06-01", "2020-06-03",
                      cache=False, validate=_reject_first)

        assert r.product != seen["first"], "rejected product must not be served"
        outcomes = [h["outcome"] for h in r.fallback_history]
        assert "rejected" in outcomes
        assert outcomes[-1] == "served"

    def test_validate_raising_treated_as_rejection(self):
        from aihydro_data import fetch

        def _fake_fetch_one(spec, geom, start, end, agg, req, **kw):
            return _result_for(spec, req)

        state = {"n": 0}

        def _raise_once(result):
            state["n"] += 1
            if state["n"] == 1:
                raise ValueError("validator blew up")
            return True

        with patch("aihydro_data._pipeline._fetch_one", side_effect=_fake_fetch_one):
            r = fetch("precipitation", _GEOM, "2020-06-01", "2020-06-03",
                      cache=False, validate=_raise_once)

        rejected = [h for h in r.fallback_history if h["outcome"] == "rejected"]
        assert rejected and "validator blew up" in rejected[0]["reason"]


class TestExceptionEnvelope:
    def test_all_fail_carries_recovery_and_history(self):
        from aihydro_data import fetch
        from aihydro_data.exceptions import AihydroDataError

        def _always_fail(spec, geom, start, end, agg, req, **kw):
            raise RuntimeError(f"{spec.id} down")

        with patch("aihydro_data._pipeline._fetch_one", side_effect=_always_fail):
            with pytest.raises(AihydroDataError) as ei:
                fetch("precipitation", _GEOM, "2020-06-01", "2020-06-03", cache=False)

        err = ei.value
        env = err.to_dict()
        assert env["recovery"], "exhausted-chain error must carry a recovery hint"
        assert env["next_tools"], "exhausted-chain error must carry next_tools"
        hist = env["details"]["fallback_history"]
        assert hist and all(h["outcome"] == "failed" for h in hist)
