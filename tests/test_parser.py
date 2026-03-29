from __future__ import annotations

import asyncio
import logging
from datetime import datetime

import pytest
from hypothesis import given, strategies as st

from real_estate_scanner.parser.olx_client import (
    ParsedAd,
    _RE_LAYOUT_TRIPLET,
    _extract_area_value,
    _extract_floor_values,
    _extract_rooms_value,
    enrich_ad_with_details,
    extract_field,
    fetch_ads,
)


def test_rooms_priority_strong_over_weak() -> None:
    rooms = _extract_rooms_value(
        params={"количество комнат": "2"},
        params_text="Количество комнат: 2\n5/5/2",
        fallback_text="5/5/2",
    )
    assert rooms == 2


def test_rooms_priority_labeled_over_weak() -> None:
    rooms = _extract_rooms_value(
        params={},
        params_text="Количество комнат: 2\n5/5/2",
        fallback_text="5/5/2",
    )
    assert rooms == 2


def test_rooms_weak_blocked() -> None:
    rooms = _extract_rooms_value(
        params={},
        params_text="5/5/2",
        fallback_text=None,
    )
    assert rooms is None


def test_no_weak_masking_required(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.ERROR):
        rooms = _extract_rooms_value(
            params={},
            params_text="5/5/2",
            fallback_text="5/5/2",
            required=True,
            url="https://example.com/blocked-rooms",
        )

    assert rooms is None
    assert "OLX REQUIRED FIELD MISSING: field=rooms url=https://example.com/blocked-rooms" in caplog.text
    assert "weak_candidate=None" in caplog.text


def test_rooms_uses_weak_fallback_when_not_required() -> None:
    rooms = _extract_rooms_value(
        params={},
        params_text="",
        fallback_text="3 комн",
    )
    assert rooms == 3


def test_extract_field_required_logs_when_missing(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.ERROR):
        result = extract_field(
            field_name="rooms",
            strong_extractors=[lambda: None],
            labeled_extractors=[lambda: None],
            weak_extractors=[lambda: None],
            text="мусор",
            required=True,
            url="https://example.com/missing",
        )

    assert result is None
    assert "OLX REQUIRED FIELD MISSING: field=rooms url=https://example.com/missing" in caplog.text
    assert "OLX extract failed: field=rooms" in caplog.text


def test_extract_field_logs_blocked_weak(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.DEBUG):
        result = extract_field(
            field_name="rooms",
            strong_extractors=[lambda: None],
            labeled_extractors=[lambda: None],
            weak_extractors=[lambda: 5],
            text="5/5/2",
            block_weak_patterns=[_RE_LAYOUT_TRIPLET],
            url="https://example.com/layout",
        )

    assert result is None
    assert "OLX weak blocked: field=rooms text=5/5/2" in caplog.text


def test_extract_field_uses_weak_when_not_required() -> None:
    result = extract_field(
        field_name="rooms",
        strong_extractors=[lambda: None],
        labeled_extractors=[lambda: None],
        weak_extractors=[lambda: 3],
        text="3 комн",
    )
    assert result == 3


def test_area_priority_strong() -> None:
    area = _extract_area_value(
        params={"общая площадь": "56 м²"},
        params_text="Общая площадь: 56",
        fallback_text="75 м²",
    )
    assert area == 56


def test_area_required_logs_when_missing(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.ERROR):
        area = _extract_area_value(
            params={},
            params_text="мусор",
            fallback_text="мусор",
            required=True,
            url="https://example.com/area-missing",
        )

    assert area is None
    assert "OLX REQUIRED FIELD MISSING: field=area url=https://example.com/area-missing" in caplog.text


def test_floor_priority_strong() -> None:
    floor, total_floors = _extract_floor_values(
        params={"этаж": "5", "этажность дома": "9"},
        params_text="Этаж: 5\nЭтажность дома: 9",
        fallback_text="1/7",
    )
    assert floor == 5
    assert total_floors == 9


def test_floor_required_logs_when_missing(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.ERROR):
        floor, total_floors = _extract_floor_values(
            params={},
            params_text="мусор",
            fallback_text="мусор",
            required=True,
            url="https://example.com/floor-missing",
        )

    assert floor is None
    assert total_floors is None
    assert "OLX REQUIRED FIELD MISSING: field=floor url=https://example.com/floor-missing" in caplog.text
    assert "OLX REQUIRED FIELD MISSING: field=total_floors url=https://example.com/floor-missing" in caplog.text


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Срочно 5/5/2 квартира 56м²", None),
        ("3 комн 74 кв.м 1/7 этаж", 3),
        ("4/10/12 120м2", None),
    ],
)
def test_real_case_garbage_inputs_for_rooms(text: str, expected: int | None) -> None:
    rooms = _extract_rooms_value(
        params={},
        params_text=text,
        fallback_text=text,
    )
    assert rooms == expected


def test_broken_dom_logs(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.ERROR):
        rooms = _extract_rooms_value(
            params={},
            params_text="---",
            fallback_text="---",
            required=True,
            url="https://example.com/broken-dom",
        )

    assert rooms is None
    assert "OLX extract failed: field=rooms" in caplog.text


@given(st.text())
def test_rooms_never_crash(text: str) -> None:
    _extract_rooms_value(
        params={},
        params_text=text,
        fallback_text=text,
    )


@given(st.text())
def test_area_never_crash(text: str) -> None:
    _extract_area_value(
        params={},
        params_text=text,
        fallback_text=text,
    )


@pytest.mark.asyncio
async def test_enrich_prefers_detail_values_over_listing(monkeypatch: pytest.MonkeyPatch) -> None:
    listing_ad = ParsedAd(
        olx_id="ID3FSEy",
        title="Test ad",
        price=75000,
        link="https://example.com/ad",
        image_url=None,
        rooms=5,
        area=40.0,
        ad_type="sale",
        city="tashkent",
        floor=1,
        total_floors=5,
        published_at=datetime(2026, 3, 25),
    )

    async def _fake_fetch(_: str) -> dict[str, object]:
        return {
            "rooms": 2,
            "area": 56.0,
            "floor": 5,
            "total_floors": 9,
            "district_slug": "shayhontohur",
            "district_label": "Шайхантахурский район",
            "description": None,
            "author_name": None,
            "owner_type": None,
            "created_at_text": None,
            "published_at": None,
            "image_url": None,
            "image_urls": [],
        }

    monkeypatch.setattr("real_estate_scanner.parser.olx_client.fetch_ad_details", _fake_fetch)
    enriched = await enrich_ad_with_details(listing_ad)

    assert enriched.rooms == 2
    assert enriched.area == 56.0
    assert enriched.floor == 5
    assert enriched.total_floors == 9


@pytest.mark.asyncio
async def test_conflict_logging(caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch) -> None:
    listing_ad = ParsedAd(
        olx_id="ID3FSEy",
        title="Conflict ad",
        price=75000,
        link="https://example.com/conflict-ad",
        image_url=None,
        rooms=5,
        area=40.0,
        ad_type="sale",
        city="tashkent",
        floor=1,
        total_floors=5,
    )

    async def _fake_fetch(_: str) -> dict[str, object]:
        return {
            "rooms": 2,
            "area": 56.0,
            "floor": 5,
            "total_floors": 9,
            "district_slug": None,
            "district_label": None,
            "description": None,
            "author_name": None,
            "owner_type": None,
            "created_at_text": None,
            "published_at": None,
            "image_url": None,
            "image_urls": [],
        }

    monkeypatch.setattr("real_estate_scanner.parser.olx_client.fetch_ad_details", _fake_fetch)
    with caplog.at_level(logging.DEBUG):
        enriched = await enrich_ad_with_details(listing_ad)

    assert enriched.rooms == 2
    assert enriched.total_floors == 9
    assert "OLX conflict: field=rooms listing=5 detail=2 url=https://example.com/conflict-ad" in caplog.text
    assert "OLX conflict: field=area listing=40.0 detail=56.0 url=https://example.com/conflict-ad" in caplog.text


@pytest.mark.asyncio
async def test_full_pipeline_merge(monkeypatch: pytest.MonkeyPatch) -> None:
    listing_ad = ParsedAd(
        olx_id="IDX",
        title="Pipeline ad",
        price=100000,
        link="https://example.com/pipeline",
        image_url="https://example.com/listing.jpg",
        rooms=5,
        area=40.0,
        ad_type="sale",
        city="tashkent",
        floor=1,
        total_floors=5,
        image_urls=["https://example.com/listing.jpg"],
    )

    async def _fake_fetch(_: str) -> dict[str, object]:
        return {
            "rooms": 2,
            "area": 56.0,
            "floor": 5,
            "total_floors": 9,
            "district_slug": "shayhontohur",
            "district_label": "Шайхантахурский район",
            "description": "Detail description",
            "author_name": "Detail author",
            "owner_type": "Частное лицо",
            "created_at_text": "25 марта 2026 г.",
            "published_at": None,
            "image_url": "https://example.com/detail.jpg",
            "image_urls": ["https://example.com/detail.jpg", "https://example.com/detail2.jpg"],
        }

    monkeypatch.setattr("real_estate_scanner.parser.olx_client.fetch_ad_details", _fake_fetch)
    enriched = await enrich_ad_with_details(listing_ad)

    assert enriched.rooms == 2
    assert enriched.area == 56.0
    assert enriched.floor == 5
    assert enriched.total_floors == 9
    assert enriched.owner_type == "Частное лицо"
    assert enriched.image_url == "https://example.com/detail.jpg"
    assert enriched.image_urls == ["https://example.com/detail.jpg", "https://example.com/detail2.jpg"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_fetch_ads_from_real_olx_page() -> None:
    url = "https://www.olx.uz/nedvizhimost/kvartiry/prodazha/tashkent/"

    ads = await fetch_ads(url=url, ad_type="sale", city="tashkent", limit=8)
    if not ads:
        await asyncio.sleep(3)
        ads = await fetch_ads(url=url, ad_type="sale", city="tashkent", limit=8)

    assert isinstance(ads, list)
    assert len(ads) > 0, "Expected non-empty ads list from OLX page"

    ad0 = ads[0]
    assert ad0.olx_id is not None and str(ad0.olx_id).strip() != ""
    assert ad0.price is not None and ad0.price > 0
    assert ad0.link is not None and ad0.link.startswith("http")
