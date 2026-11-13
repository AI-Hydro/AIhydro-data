"""Pin the GEOGLOWS_RETRO citation / licence text and the NWIS pitfall pointer.

The AWS Open Data registry entry names Hales et al., J. Flood Risk Manag.,
doi:10.1111/jfr3.12859, under CC BY 4.0 (docs/vision-2040/findings/
streamflow-dataset-options-2026-11-10.md). An earlier registry entry carried an
unverified "Ashby et al. (2023)" author list and a licence line that omitted the
attribution requirement. Offline: reads the registry only.
"""
from __future__ import annotations

import re

from aihydro_data.products import get_product, list_products


def test_geoglows_citation_is_hales_with_the_registry_doi():
    spec = get_product("GEOGLOWS_RETRO")
    assert "Hales" in spec.citation
    assert "10.1111/jfr3.12859" in spec.citation
    assert "Ashby" not in spec.citation
    assert "10.1111/jfr3.12859" in spec.bibtex
    assert "Ashby" not in spec.bibtex


def test_geoglows_licence_is_cc_by_with_attribution_required():
    lic = get_product("GEOGLOWS_RETRO").license
    assert "CC BY 4.0" in lic
    assert "attribution is required" in lic.lower()
    assert "Hales" in lic


def test_no_pitfall_points_to_a_product_id_that_does_not_exist():
    ids = {p.id for p in list_products()}
    ghost = re.compile(r"\b([A-Z][A-Z0-9]+(?:_[A-Z0-9]+)+)\b")
    for p in list_products():
        for text in p.common_pitfalls:
            for token in ghost.findall(text):
                # Product-id-shaped tokens with a product suffix must resolve to a real product.
                if token.endswith(("_STREAMFLOW", "_RETRO", "_FLOOD")):
                    assert token in ids, f"{p.id} pitfall names unknown product {token!r}"


def test_nwis_pitfall_no_longer_names_grdc_streamflow():
    text = " ".join(get_product("NWIS_STREAMFLOW").common_pitfalls)
    assert "GRDC_STREAMFLOW" not in text
    assert "GRDC-Caravan" in text
