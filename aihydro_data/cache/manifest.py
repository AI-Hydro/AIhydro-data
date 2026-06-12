"""
Cache manifest — provenance record for each cached file.

Each cache entry writes a sidecar <cache_key>.manifest.json next to the
data file. The manifest captures who fetched what, from where, and when —
so agents and users can always trace a cached result back to its source.

The manifest is intentionally append-only: if you re-fetch the same key
with a different backend, a new manifest entry is appended (the old one is
preserved). The most-recent entry wins for display purposes.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

# Serialises manifest read-modify-write cycles within this process —
# fetch_batch() runs fetches in a thread pool, and two workers caching the
# same key concurrently would otherwise clobber each other's appends.
_MANIFEST_LOCK = threading.Lock()


class ManifestEntry:
    """A single provenance record for one cached fetch."""

    def __init__(
        self,
        cache_key: str,
        variable: str,
        product: str,
        source: str,
        start: str,
        end: str,
        geom_wkt: str,
        aggregation: str,
        fetched_at: str,
        license: str = "",
        citation: str = "",
        bibtex: str = "",
        data_file: str = "",
        notes: list[str] | None = None,
        spatial_support: str = "areal",
        aggregation_actual: str = "",
    ) -> None:
        self.cache_key = cache_key
        self.variable = variable
        self.product = product
        self.source = source
        self.start = start
        self.end = end
        self.geom_wkt = geom_wkt
        self.aggregation = aggregation
        self.fetched_at = fetched_at
        self.license = license
        self.citation = citation
        self.bibtex = bibtex
        self.data_file = data_file
        self.notes = notes or []
        self.spatial_support = spatial_support
        self.aggregation_actual = aggregation_actual

    def to_dict(self) -> dict[str, Any]:
        return {
            "cache_key": self.cache_key,
            "variable": self.variable,
            "product": self.product,
            "source": self.source,
            "start": self.start,
            "end": self.end,
            "geom_wkt": self.geom_wkt,
            "aggregation": self.aggregation,
            "fetched_at": self.fetched_at,
            "license": self.license,
            "citation": self.citation,
            "bibtex": self.bibtex,
            "data_file": self.data_file,
            "notes": self.notes,
            "spatial_support": self.spatial_support,
            "aggregation_actual": self.aggregation_actual,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ManifestEntry":
        return cls(
            cache_key=d.get("cache_key", ""),
            variable=d.get("variable", ""),
            product=d.get("product", ""),
            source=d.get("source", ""),
            start=d.get("start", ""),
            end=d.get("end", ""),
            geom_wkt=d.get("geom_wkt", ""),
            aggregation=d.get("aggregation", ""),
            fetched_at=d.get("fetched_at", ""),
            license=d.get("license", ""),
            citation=d.get("citation", ""),
            bibtex=d.get("bibtex", ""),
            data_file=d.get("data_file", ""),
            notes=d.get("notes", []),
            spatial_support=d.get("spatial_support", "areal"),
            aggregation_actual=d.get("aggregation_actual", ""),
        )


def manifest_path(cache_dir: Path, cache_key: str) -> Path:
    return cache_dir / f"{cache_key}.manifest.json"


def write_manifest(cache_dir: Path, entry: ManifestEntry) -> None:
    """Append a ManifestEntry to the sidecar JSON list for this cache key.

    Thread-safe and crash-safe: the read-append-write cycle is serialised by
    a process-wide lock, and the file is published atomically (temp file +
    ``os.replace``) so a reader never sees a half-written manifest.
    """
    path = manifest_path(cache_dir, entry.cache_key)
    with _MANIFEST_LOCK:
        entries: list[dict[str, Any]] = []
        if path.exists():
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(existing, list):
                    entries = existing
            except Exception:
                pass
        entries.append(entry.to_dict())
        fd, tmp_name = tempfile.mkstemp(
            dir=str(cache_dir), prefix=f".{entry.cache_key}.", suffix=".manifest.tmp",
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(entries, fh, indent=2, ensure_ascii=False)
            os.replace(tmp_name, path)
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise


def read_manifest(cache_dir: Path, cache_key: str) -> list[ManifestEntry]:
    """Return all manifest entries for a cache key (most recent last)."""
    path = manifest_path(cache_dir, cache_key)
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(raw, list):
            return [ManifestEntry.from_dict(d) for d in raw]
    except Exception:
        pass
    return []


def latest_manifest(cache_dir: Path, cache_key: str) -> Optional[ManifestEntry]:
    entries = read_manifest(cache_dir, cache_key)
    return entries[-1] if entries else None
