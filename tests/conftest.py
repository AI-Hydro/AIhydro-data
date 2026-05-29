"""
Test-suite conftest.

Disables the transient-retry helper unless the user explicitly opts in
for a live run. Without this, offline tests that exercise the real
fallback chain (e.g. test_batch_returns_expected_structure) would burn
~3 minutes per test attempting to retry network failures that will
never succeed in an offline env.

Live tests still need retries because real upstreams are flaky. They
opt back in by setting `AIHYDRO_DATA_NO_RETRY=0` (or unsetting it) in
their own fixtures — we do NOT touch the env var when `-m live` is
selected, so the user's env wins.
"""
from __future__ import annotations

import os
import pytest


def pytest_configure(config: pytest.Config) -> None:
    """Default to no-retry. Live runs can opt back in via env or fixtures."""
    # Only disable retries if the user hasn't explicitly set the var.
    if "AIHYDRO_DATA_NO_RETRY" not in os.environ:
        # If the user is selecting only live tests, don't kill retries —
        # they need them for upstream flakiness.
        markexpr = (config.getoption("-m") or "").strip()
        if markexpr != "live":
            os.environ["AIHYDRO_DATA_NO_RETRY"] = "1"
