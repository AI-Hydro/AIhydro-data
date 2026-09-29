"""Product identity survives fallback, serialization and registry changes."""
from types import SimpleNamespace

import pandas as pd
import pytest
from shapely.geometry import box

from aihydro_data import _pipeline, cache, products
from aihydro_data.mcp import _data_list_products, _result_to_dict
from aihydro_data.sources import base


@pytest.fixture
def offline(monkeypatch, tmp_path):
    monkeypatch.setattr(cache, "cache_dir", lambda: tmp_path)

    def backend(source):
        def fetch(spec, *args):
            frame = pd.DataFrame({"date": pd.date_range("2024-01-01", periods=2), "precipitation": [2., 3.]})
            frame.attrs["aihydro_notes"] = ["fixture scientific limitation"]
            return frame
        return SimpleNamespace(is_available=lambda: (True, None), fetch_timeseries=fetch)
    monkeypatch.setattr(base, "get_backend", backend)


def fetch(**kwargs):
    return _pipeline.fetch("precipitation", box(80, 25, 81, 26), "2024-01-01", "2024-01-02",
                           mode="manual", product="CHIRPS", region="global", **kwargs)


def test_discovery_distinguishes_versions():
    specs = {s["id"]: s for s in _data_list_products(variable="precipitation")}
    assert specs["CHIRPS"]["dataset_version"] == "3.0"
    assert specs["CHIRPS"]["variant"] == "daily_sat"
    assert "NASA IMERG Late V07" in specs["CHIRPS"]["input_dependencies"]
    assert specs["CHIRPS_IRI"]["dataset_version"] == "2.0"
    assert ".v2p0/" in specs["CHIRPS_IRI"]["source_dataset_id"]
    assert "10.15780/G2JQ0P" in products.get_product("CHIRPS").citation


def test_fresh_mcp_and_cached_identity_survive_registry_change(offline, monkeypatch):
    result = fetch(fallback=[])
    old_identity = dict(result.product_identity)
    real_get = products.get_product
    monkeypatch.setattr(products, "get_product", lambda pid: real_get(pid).model_copy(update={"dataset_version": "future", "units": "changed", "common_pitfalls": ["changed"]}))
    restored = fetch(fallback=[])
    assert restored.cache_hit
    assert restored.product_identity == old_identity
    assert restored.product_identity["dataset_version"] == "3.0"
    assert restored.units == result.units
    assert restored.common_pitfalls == result.common_pitfalls
    assert restored.fallback_history == result.fallback_history
    assert "fixture scientific limitation" in restored.notes
    wire = _result_to_dict(restored)
    assert wire["product_identity"] == old_identity
    assert wire["units"] == "mm/day" and wire["timestep"] == "daily"
    assert wire["common_pitfalls"] and wire["coverage_complete"]


def test_fallback_reports_served_version_and_retains_trail(offline, monkeypatch):
    real_backend = base.get_backend
    def backend(source):
        if source == "gee":
            return SimpleNamespace(is_available=lambda: (False, "fixture outage"))
        return real_backend(source)
    monkeypatch.setattr(base, "get_backend", backend)
    result = fetch(fallback=["CHIRPS_IRI"])
    assert result.product == "CHIRPS_IRI"
    assert result.product_identity["dataset_version"] == "2.0"
    assert result.product_identity["variant"] == "daily-improved"
    assert result.fallback_history[0]["product"] == "CHIRPS"
    assert result.fallback_history[-1]["product"] == "CHIRPS_IRI"
    assert fetch(fallback=["CHIRPS_IRI"]).product_identity == result.product_identity


def test_warm_fallback_cache_does_not_defeat_strict_pin(offline, monkeypatch):
    from aihydro_data.exceptions import SourceUnavailable

    real_backend = base.get_backend
    def backend(source):
        if source == "gee":
            return SimpleNamespace(is_available=lambda: (False, "fixture outage"))
        return real_backend(source)
    monkeypatch.setattr(base, "get_backend", backend)
    first = fetch(fallback=["CHIRPS_IRI"])
    assert first.product == "CHIRPS_IRI"
    with pytest.raises(SourceUnavailable):
        fetch(fallback=[])


def test_cached_result_is_rechecked_by_validator(offline):
    from aihydro_data.exceptions import SourceUnavailable
    first = fetch(fallback=[])
    assert first.product == "CHIRPS"
    seen = []
    with pytest.raises(SourceUnavailable):
        fetch(fallback=[], validate=lambda result: seen.append(result.cache_hit) or False)
    assert seen and seen[0] is True


def test_outlet_changes_cache_identity(offline):
    first = fetch(fallback=[], outlet=(25.2, 80.2))
    second = fetch(fallback=[], outlet=(25.8, 80.8))
    assert first.cache_key != second.cache_key
    assert not second.cache_hit


def test_legacy_manifest_identity_is_unknown(offline):
    import json
    result = fetch(fallback=[])
    path = cache.cache_dir() / f"{result.cache_key}.manifest.json"
    entries = json.loads(path.read_text())
    for entry in entries:
        entry.pop("product_identity")
    path.write_text(json.dumps(entries))
    restored = cache.cache_read(result.cache_key)
    assert restored.product_identity == {}
    assert _result_to_dict(restored)["product_identity"] == {}
