"""Offline test: README / ARCHITECTURE product counts stay in sync with the live registry.

Run with: pytest tests/test_doc_consistency.py -v
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest


ROOT = Path(__file__).parent.parent


def _get_registry():
    import aihydro_data.products as p
    p._load_registry()
    return dict(p._REGISTRY)


@pytest.fixture(scope="module")
def registry():
    return _get_registry()


@pytest.fixture(scope="module")
def readme_text():
    return (ROOT / "README.md").read_text()


@pytest.fixture(scope="module")
def arch_text():
    return (ROOT / "ARCHITECTURE.md").read_text()


@pytest.fixture(scope="module")
def paper_text():
    return (ROOT / "PAPER.md").read_text()


def test_readme_total_product_count(registry, readme_text):
    """README must state the correct total product count."""
    total = len(registry)
    # Matches "45 products across 14 variables" style
    matches = re.findall(r"(\d+) products across (\d+) variables", readme_text)
    assert matches, "README must contain '<N> products across <M> variables'"
    for count_str, _ in matches:
        assert int(count_str) == total, (
            f"README says '{count_str} products' but registry has {total}. "
            "Run `python scripts/gen_product_tables.py --check` to identify mismatches."
        )


def test_readme_total_variable_count(registry, readme_text):
    """README must state the correct variable count."""
    n_vars = len({s.variable for s in registry.values()})
    matches = re.findall(r"\d+ products across (\d+) variables", readme_text)
    assert matches, "README must contain 'N products across M variables'"
    for var_str in matches:
        assert int(var_str) == n_vars, (
            f"README says '{var_str} variables' but registry has {n_vars}."
        )


def test_all_product_ids_present_in_readme(registry, readme_text):
    """Every product ID in the registry must appear at least once in the README."""
    missing = [pid for pid in registry if f"`{pid}`" not in readme_text]
    assert not missing, (
        f"These product IDs are in the registry but not in README.md: {missing}"
    )


def test_paper_product_count(registry, paper_text):
    """PAPER.md Section 4 must state the correct total product count."""
    total = len(registry)
    matches = re.findall(r"(\d+) across 14 variables", paper_text)
    assert matches, "PAPER.md must contain 'N across 14 variables' in Section 4"
    for count_str in matches:
        assert int(count_str) == total, (
            f"PAPER.md says '{count_str}' products but registry has {total}."
        )


def test_paper_version_matches_package(paper_text):
    """PAPER.md must reference the current package version."""
    from aihydro_data._version import __version__
    assert __version__ in paper_text, (
        f"PAPER.md does not mention the current version {__version__}. "
        "Update Section 4."
    )
