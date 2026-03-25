from __future__ import annotations

import asyncio

import pytest

from real_estate_scanner.parser.olx_client import fetch_ads


@pytest.mark.asyncio
async def test_fetch_ads_from_real_olx_page() -> None:
    # Real public OLX page (no auth). Limit is kept small for performance.
    url = "https://www.olx.uz/nedvizhimost/kvartiry/prodazha/tashkent/"

    ads = await fetch_ads(url=url, ad_type="sale", city="tashkent", limit=8)
    if not ads:
        # Sometimes Playwright returns before all listing elements are fully rendered.
        # Retry once with a small delay to avoid flaky tests.
        await asyncio.sleep(3)
        ads = await fetch_ads(url=url, ad_type="sale", city="tashkent", limit=8)

    assert isinstance(ads, list)
    assert len(ads) > 0, "Expected non-empty ads list from OLX page"

    ad0 = ads[0]
    assert ad0.olx_id is not None and str(ad0.olx_id).strip() != ""
    assert ad0.price is not None and ad0.price > 0
    assert ad0.link is not None and ad0.link.startswith("http")

