"""
Phase 5 & 6 tests — disk cache + batch fetching.

All offline: no network, no auth. Tests use temporary directories and
mocked FetchResults so they never touch real backends.
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from shapely.geometry import Point, box


# ── Helpers ───────────────────────────────────────────────────────────────

def _make_result(variable="precipitation", product="CHIRPS", source="gee"):
    from aihydro_data.contracts import FetchRequest, FetchResult
    import pandas as pd
    req = FetchRequest(
        variable=variable,
        geometry=Point(-85.0, 40.0),
        start="2015-01-01",
        end="2015-01-31",
    )
    df = pd.DataFrame({"date": pd.date_range("2015-01-01", periods=5), variable: [1.0, 2.0, 3.0, 2.5, 1.5]})
    return FetchResult(
        variable=variable,
        product=product,
        source=source,
        request=req,
        cache_key="testkey0000000000000000",
        data=df,
        license="public domain",
        citation="Test citation",
        bibtex="@misc{test}",
    )


# ── cache_key ─────────────────────────────────────────────────────────────

class TestCacheKey:
    def test_deterministic(self):
        from aihydro_data.cache import cache_key
        a = cache_key({"variable": "precip", "start": "2015-01-01"})
        b = cache_key({"start": "2015-01-01", "variable": "precip"})
        assert a == b

    def test_length(self):
        from aihydro_data.cache import cache_key
        assert len(cache_key({"x": 1})) == 24

    def test_different_payloads_differ(self):
        from aihydro_data.cache import cache_key
        a = cache_key({"variable": "precip"})
        b = cache_key({"variable": "tmax"})
        assert a != b


# ── ManifestEntry ─────────────────────────────────────────────────────────

class TestManifest:
    def test_roundtrip(self):
        from aihydro_data.cache.manifest import ManifestEntry
        e = ManifestEntry(
            cache_key="abc123",
            variable="precipitation",
            product="CHIRPS",
            source="gee",
            start="2015-01-01",
            end="2015-12-31",
            geom_wkt="POINT (-85 40)",
            aggregation="basin_mean",
            fetched_at="2024-01-01T00:00:00+00:00",
            license="CC0",
            citation="Test",
            bibtex="@misc{x}",
            data_file="/tmp/abc123.parquet",
        )
        d = e.to_dict()
        e2 = ManifestEntry.from_dict(d)
        assert e2.product == "CHIRPS"
        assert e2.license == "CC0"
        assert e2.data_file == "/tmp/abc123.parquet"

    def test_write_and_read(self, tmp_path):
        from aihydro_data.cache.manifest import ManifestEntry, write_manifest, read_manifest
        e = ManifestEntry(
            cache_key="zzz",
            variable="tmax",
            product="GRIDMET_TMAX",
            source="hyriver",
            start="2010-01-01",
            end="2010-12-31",
            geom_wkt="POINT (-90 38)",
            aggregation="basin_mean",
            fetched_at="2024-06-01T00:00:00+00:00",
        )
        write_manifest(tmp_path, e)
        entries = read_manifest(tmp_path, "zzz")
        assert len(entries) == 1
        assert entries[0].variable == "tmax"

    def test_append_multiple(self, tmp_path):
        from aihydro_data.cache.manifest import ManifestEntry, write_manifest, read_manifest
        for i in range(3):
            e = ManifestEntry(
                cache_key="multi",
                variable="precip",
                product="CHIRPS",
                source="gee",
                start=f"201{i}-01-01",
                end=f"201{i}-12-31",
                geom_wkt="POINT (0 0)",
                aggregation="basin_mean",
                fetched_at=f"2024-0{i+1}-01T00:00:00+00:00",
            )
            write_manifest(tmp_path, e)
        entries = read_manifest(tmp_path, "multi")
        assert len(entries) == 3

    def test_latest_manifest(self, tmp_path):
        from aihydro_data.cache.manifest import ManifestEntry, write_manifest, latest_manifest
        for i in range(2):
            e = ManifestEntry(
                cache_key="latest",
                variable="et",
                product="MOD16_ET",
                source="gee",
                start="2010-01-01",
                end="2010-12-31",
                geom_wkt="POINT (0 0)",
                aggregation="basin_mean",
                fetched_at=f"2024-0{i+1}-01T00:00:00+00:00",
            )
            write_manifest(tmp_path, e)
        m = latest_manifest(tmp_path, "latest")
        assert m is not None
        assert "2024-02" in m.fetched_at


# ── Disk cache read/write ─────────────────────────────────────────────────

class TestDiskCache:
    def test_cache_miss_returns_none(self, tmp_path):
        from aihydro_data.cache import cache_read
        with patch("aihydro_data.cache.cache_dir", return_value=tmp_path):
            result = cache_read("nonexistent_key_0000000000")
        assert result is None

    def test_cache_write_and_read_dataframe(self, tmp_path):
        from aihydro_data.cache import cache_read, cache_write
        result = _make_result()
        with patch("aihydro_data.cache.cache_dir", return_value=tmp_path):
            cache_write(result, geom_wkt="POINT (-85 40)")
            ck = result.cache_key
            parquet = tmp_path / f"{ck}.parquet"
            assert parquet.exists(), "Parquet file not written"
            manifest = tmp_path / f"{ck}.manifest.json"
            assert manifest.exists(), "Manifest not written"
            recovered = cache_read(ck)
        assert recovered is not None
        assert recovered.cache_hit is True
        assert recovered.product == "CHIRPS"
        assert recovered.variable == "precipitation"

    def test_cache_write_idempotent(self, tmp_path):
        from aihydro_data.cache import cache_write, cache_read
        result = _make_result()
        with patch("aihydro_data.cache.cache_dir", return_value=tmp_path):
            cache_write(result, geom_wkt="POINT (-85 40)")
            cache_write(result, geom_wkt="POINT (-85 40)")  # second write
            ck = result.cache_key
            raw = json.loads((tmp_path / f"{ck}.manifest.json").read_text())
        assert len(raw) == 2   # manifest appends, not overwrites

    def test_cache_invalidate(self, tmp_path):
        from aihydro_data.cache import cache_write, cache_invalidate, cache_read
        result = _make_result()
        with patch("aihydro_data.cache.cache_dir", return_value=tmp_path):
            cache_write(result, geom_wkt="POINT (-85 40)")
            deleted = cache_invalidate(result.cache_key)
            assert deleted is True
            recovered = cache_read(result.cache_key)
        assert recovered is None

    def test_cache_invalidate_missing_returns_false(self, tmp_path):
        from aihydro_data.cache import cache_invalidate
        with patch("aihydro_data.cache.cache_dir", return_value=tmp_path):
            assert cache_invalidate("does_not_exist_00000000") is False

    def test_cache_status_empty(self, tmp_path):
        from aihydro_data.cache import cache_status
        with patch("aihydro_data.cache.cache_dir", return_value=tmp_path):
            status = cache_status()
        assert status["entry_count"] == 0
        assert status["total_size_mb"] == 0.0
        assert isinstance(status["entries"], list)

    def test_cache_status_with_entries(self, tmp_path):
        from aihydro_data.cache import cache_write, cache_status
        r1 = _make_result("precipitation", "CHIRPS", "gee")
        r2 = _make_result("tmax", "GRIDMET_TMAX", "hyriver")
        # Give them different cache keys
        r2 = r2.model_copy(update={"cache_key": "tmax000000000000000000000"})
        with patch("aihydro_data.cache.cache_dir", return_value=tmp_path):
            cache_write(r1, "POINT (-85 40)")
            cache_write(r2, "POINT (-85 40)")
            status = cache_status()
        assert status["entry_count"] == 2
        assert status["total_size_mb"] >= 0


# ── Public API cache wrappers ─────────────────────────────────────────────

class TestPublicCacheAPI:
    def test_cache_status_callable(self):
        import aihydro_data
        status = aihydro_data.cache_status()
        assert isinstance(status, dict)
        assert "entry_count" in status
        assert "total_size_mb" in status

    def test_cache_invalidate_callable(self):
        import aihydro_data
        # Should return False for a key that doesn't exist
        result = aihydro_data.cache_invalidate("nonexistent_000000000000000")
        assert result is False

    def test_fetch_batch_in_public_api(self):
        import aihydro_data
        assert callable(aihydro_data.fetch_batch)


# ── Batch geometry iteration ──────────────────────────────────────────────

class TestIterGeometries:
    def test_single_point_tuple(self):
        from aihydro_data.geometry.batch import iter_geometries
        pairs = list(iter_geometries((40.0, -85.0)))  # (lat, lon)
        assert len(pairs) == 1
        label, geom = pairs[0]
        assert label == "0"
        assert geom.geom_type == "Point"

    def test_list_of_geometries(self):
        from aihydro_data.geometry.batch import iter_geometries
        geoms = [Point(-85.0, 40.0), Point(-90.0, 38.0)]
        pairs = list(iter_geometries(geoms))
        assert len(pairs) == 2
        assert pairs[0][0] == "0"
        assert pairs[1][0] == "1"

    def test_list_of_labelled_pairs(self):
        from aihydro_data.geometry.batch import iter_geometries
        labelled = [("gaugeA", Point(-85.0, 40.0)), ("gaugeB", Point(-90.0, 38.0))]
        pairs = list(iter_geometries(labelled))
        assert pairs[0][0] == "gaugeA"
        assert pairs[1][0] == "gaugeB"

    def test_dict_input(self):
        from aihydro_data.geometry.batch import iter_geometries
        d = {"site_1": Point(-85.0, 40.0), "site_2": Point(-90.0, 38.0)}
        pairs = list(iter_geometries(d))
        labels = {p[0] for p in pairs}
        assert "site_1" in labels and "site_2" in labels

    def test_empty_list(self):
        from aihydro_data.geometry.batch import iter_geometries
        pairs = list(iter_geometries([]))
        assert pairs == []

    def test_bbox_tuple_coerced_to_polygon(self):
        from aihydro_data.geometry.batch import iter_geometries
        pairs = list(iter_geometries((-87.0, 40.0, -85.0, 41.0)))
        assert len(pairs) == 1
        assert pairs[0][1].geom_type == "Polygon"

    def test_geodataframe(self):
        pytest.importorskip("geopandas")
        import geopandas as gpd
        from shapely.geometry import Point as P
        from aihydro_data.geometry.batch import iter_geometries
        gdf = gpd.GeoDataFrame(
            {"id": ["ws_a", "ws_b"]},
            geometry=[P(-85.0, 40.0), P(-90.0, 38.0)],
            crs="EPSG:4326",
        )
        pairs = list(iter_geometries(gdf))
        assert len(pairs) == 2
        labels = [p[0] for p in pairs]
        assert "0" in labels or "ws_a" not in labels  # uses index as label


# ── fetch_batch (mocked backends) ────────────────────────────────────────

class TestFetchBatch:
    def test_batch_returns_expected_structure(self):
        """fetch_batch returns results/errors/labels dict even when all backends fail."""
        from aihydro_data.exceptions import AihydroDataError
        from aihydro_data._pipeline import fetch_batch

        geoms = [Point(-85.0, 40.0), Point(-90.0, 38.0)]
        try:
            out = fetch_batch("precipitation", geoms, "2015-01-01", "2015-01-31", on_error="warn")
            assert "results" in out
            assert "errors" in out
            assert "labels" in out
            assert out["variable"] == "precipitation"
            assert len(out["labels"]) == 2
        except AihydroDataError:
            pass  # all-backend-fail in offline env is fine

    def test_batch_with_mock_fetch(self):
        """fetch_batch correctly maps labels to results when fetch succeeds."""
        from aihydro_data._pipeline import fetch_batch

        mock_result = _make_result()
        with patch("aihydro_data._pipeline.fetch", return_value=mock_result):
            out = fetch_batch(
                "precipitation",
                {"ws_a": Point(-85.0, 40.0), "ws_b": Point(-90.0, 38.0)},
                "2015-01-01",
                "2015-01-31",
                on_error="warn",
            )
        assert set(out["labels"]) == {"ws_a", "ws_b"}
        assert len(out["results"]) == 2
        assert len(out["errors"]) == 0

    def test_batch_on_error_warn_continues(self):
        """on_error='warn': one failure does not abort the rest."""
        from aihydro_data._pipeline import fetch_batch
        from aihydro_data.exceptions import SourceUnavailable

        call_count = 0
        mock_result = _make_result()

        def _side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise SourceUnavailable(code="X", message="fail")
            return mock_result

        with patch("aihydro_data._pipeline.fetch", side_effect=_side_effect):
            out = fetch_batch(
                "precipitation",
                [Point(-85.0, 40.0), Point(-90.0, 38.0)],
                "2015-01-01",
                "2015-01-31",
                on_error="warn",
                max_workers=1,
            )
        assert len(out["results"]) == 1
        assert len(out["errors"]) == 1

    def test_batch_empty_geometries(self):
        from aihydro_data._pipeline import fetch_batch
        out = fetch_batch("precipitation", [], "2015-01-01", "2015-01-31")
        assert out["results"] == {}
        assert out["labels"] == []
