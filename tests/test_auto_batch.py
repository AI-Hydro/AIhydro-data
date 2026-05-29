"""
Regression tests for fetch() → fetch_batch() auto-dispatch.

The cookbook Recipe 3 originally suggested `fetch("streamflow", [g1, g2], ...)`
but that raised GeometryInvalid because the pipeline's coerce_geometry only
accepted single geometries. v0.1.6 makes fetch() detect collection inputs and
forward them to fetch_batch() transparently.

These tests cover the detection logic (offline, no backend calls).
"""
from __future__ import annotations

import pytest
from shapely.geometry import Point


# ── Collection detection ────────────────────────────────────────────────────

class TestLooksLikeCollection:
    def test_list_of_strings(self):
        from aihydro_data._pipeline import _looks_like_collection
        assert _looks_like_collection(["03245500", "01646500"]) is True

    def test_list_of_points(self):
        from aihydro_data._pipeline import _looks_like_collection
        assert _looks_like_collection([Point(0, 0), Point(1, 1)]) is True

    def test_dict_of_points(self):
        from aihydro_data._pipeline import _looks_like_collection
        assert _looks_like_collection({"a": Point(0, 0), "b": Point(1, 1)}) is True

    def test_single_point_is_not_collection(self):
        from aihydro_data._pipeline import _looks_like_collection
        assert _looks_like_collection(Point(0, 0)) is False

    def test_single_string_is_not_collection(self):
        from aihydro_data._pipeline import _looks_like_collection
        assert _looks_like_collection("03245500") is False

    def test_latlon_tuple_is_not_collection(self):
        from aihydro_data._pipeline import _looks_like_collection
        # Scalar (lat, lon) — single Point, NOT a batch
        assert _looks_like_collection((39.1, -94.5)) is False

    def test_bbox_tuple_is_not_collection(self):
        from aihydro_data._pipeline import _looks_like_collection
        # Scalar (minx, miny, maxx, maxy) — single bbox, NOT a batch
        assert _looks_like_collection((-95.0, 38.0, -94.0, 40.0)) is False

    def test_single_item_list_is_not_collection(self):
        from aihydro_data._pipeline import _looks_like_collection
        assert _looks_like_collection([Point(0, 0)]) is False

    def test_geojson_dict_is_not_collection(self):
        from aihydro_data._pipeline import _looks_like_collection
        gj = {"type": "Point", "coordinates": [0, 0]}
        assert _looks_like_collection(gj) is False

    def test_geojson_polygon_is_not_collection(self):
        from aihydro_data._pipeline import _looks_like_collection
        gj = {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]]}
        assert _looks_like_collection(gj) is False


# ── End-to-end: fetch() with a list dispatches to fetch_batch() ─────────────

class TestFetchListDispatch:
    def test_fetch_list_calls_fetch_batch(self, monkeypatch):
        """fetch() with a list of geometries forwards to fetch_batch()."""
        import aihydro_data._pipeline as pipeline

        # Capture what fetch_batch sees
        captured = {}

        def fake_batch(variable, geometries, start, end, **kwargs):
            captured["variable"] = variable
            captured["geometries"] = list(geometries)
            captured["start"] = start
            captured["end"] = end
            captured["kwargs"] = kwargs
            # Return a fake batch result
            return {"results": {}, "errors": {}, "labels": [],
                    "variable": variable, "start": start, "end": end}

        monkeypatch.setattr(pipeline, "fetch_batch", fake_batch)

        result = pipeline.fetch("streamflow", ["03245500", "01646500"],
                                "2020-01-01", "2020-12-31")

        # fetch_batch should have been called
        assert captured["variable"] == "streamflow"
        assert captured["geometries"] == ["03245500", "01646500"]
        assert captured["start"] == "2020-01-01"
        # And fetch() returned a list (empty in this fake case)
        assert isinstance(result, list)

    def test_fetch_single_geometry_does_not_dispatch_to_batch(self, monkeypatch):
        """fetch() with a single Point goes through the normal pipeline."""
        import aihydro_data._pipeline as pipeline

        # If fetch_batch is called for a single geometry, fail loudly
        def boom(*a, **kw):
            raise AssertionError("fetch_batch should NOT be called for a single Point")
        monkeypatch.setattr(pipeline, "fetch_batch", boom)

        # We expect this to attempt the normal pipeline (which will fail because
        # the backend isn't available in test env — but it should fail in the
        # pipeline, not by calling batch).
        try:
            pipeline.fetch("precipitation", Point(-94.5, 39.1),
                           "2020-01-01", "2020-01-31")
        except AssertionError:
            raise   # batch was called — test fail
        except Exception:
            pass    # any other error means batch was correctly skipped

    def test_fetch_latlon_tuple_does_not_dispatch_to_batch(self, monkeypatch):
        """A (lat, lon) tuple is a single Point, not a batch."""
        import aihydro_data._pipeline as pipeline
        def boom(*a, **kw):
            raise AssertionError("fetch_batch should NOT be called for a (lat,lon) tuple")
        monkeypatch.setattr(pipeline, "fetch_batch", boom)
        try:
            pipeline.fetch("precipitation", (39.1, -94.5),
                           "2020-01-01", "2020-01-31")
        except AssertionError:
            raise
        except Exception:
            pass
