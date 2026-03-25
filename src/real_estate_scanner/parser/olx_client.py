from __future__ import annotations

import asyncio
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin

from playwright.async_api import Browser, Page, async_playwright
from playwright_stealth import Stealth

from real_estate_scanner.config import settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ParsedAd:
    olx_id: str
    title: str
    price: int
    link: str
    image_url: str | None
    rooms: int | None
    area: float | None
    ad_type: str
    city: str
    district: str | None = None


_TASHKENT_DISTRICT_SLUGS: list[tuple[str, tuple[str, ...]]] = [
    ("yashnabadskiy", ("Яшнабад", "Яшнабод")),
    ("yunusabadskiy", ("Юнусабад",)),
    ("mirzoulugbek", ("Мирзо-Улугбек", "Мирзо Улугбек", "М.Улугбек", "М-Улугбек", "Мирзо")),
    ("chilanzarskiy", ("Чиланзар",)),
    ("yakkasarayskiy", ("Яккасарай",)),
]


_RE_PRICE_WITH_SUM = re.compile(r"([\d\s\u00A0]+)\s*сум\b", re.IGNORECASE)
_RE_PRICE_ANY = re.compile(r"([\d\s\u00A0]+)")

# Rooms patterns (OLX uses many localized variants)
#
# Examples we want to support:
# - "3 хона" / "4 хона"
# - "3 хонали"
# - "4 х. к." / "4 х к." / "4 х.ком." (abbrev)
# - "2 комн" / "2 комнат"
#
_RE_ROOMS_HONA = re.compile(r"(?P<rooms>\d+)\s*(?:хона|xona)\b", re.IGNORECASE)
_RE_ROOMS_HONALI = re.compile(r"(?P<rooms>\d+)\s*хонали\b", re.IGNORECASE)
_RE_ROOMS_XONALI = re.compile(r"(?P<rooms>\d+)\s*xonali\b", re.IGNORECASE)
_RE_ROOMS_DASH_COMN = re.compile(r"(?P<rooms>\d+)\s*-\s*комн\b", re.IGNORECASE)
_RE_ROOMS_KOMNATNAYA = re.compile(r"(?P<rooms>\d+)\s*-\s*комнатн(?:ая|ую|ой)\b", re.IGNORECASE)
_RE_ROOMS_4X_COM = re.compile(r"(?P<rooms>\d+)\s*[хx]\s*ком\b", re.IGNORECASE)
_RE_ROOMS_COM = re.compile(r"(?P<rooms>\d+)\s*х\s*ком\b", re.IGNORECASE)
_RE_ROOMS_COMN = re.compile(r"(?P<rooms>\d+)\s*(?:комн|комнат)\b", re.IGNORECASE)
_RE_ROOMS_HDOTK = re.compile(r"(?P<rooms>\d+)\s*х\.?\s*к\.?", re.IGNORECASE)  # "х. к."
_RE_ROOMS_COMN_SHORT = re.compile(r"(?P<rooms>\d+)\s*комн\b", re.IGNORECASE)
_RE_ROOMS_SLASH_LAYOUT = re.compile(
    r"(?P<rooms>\d+)\s*/\s*\d+\s*/\s*\d+(?:\s*(?:хона|хонали|xona|xonali))?\b",
    re.IGNORECASE,
)

_RE_ROOMS_ANY_DIGIT_BEFORE_COM = re.compile(
    r"(?P<rooms>\d+)\s*(?:х\.?\s*к\.?|комн|комнат)\b",
    re.IGNORECASE,
)

# Area patterns (strict: must include m2 / кв.м / м² tokens)
_RE_AREA_UNITS = re.compile(
    r"(?P<area>\d+(?:[.,]\d+)?)\s*(?:m2|м2|м²|кв\.?\s*м)",
    re.IGNORECASE,
)


def _normalize_space(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def _clean_text_for_parsing(text: str) -> str:
    """
    OLX иногда вставляет служебные символы (например, фигурные скобки),
    которые мешают regex-поиску.
    """
    if not text:
        return ""
    cleaned = text.replace("m²", "m2").replace("м²", "m2")
    # Remove service punctuation and brackets that often break regex parsing.
    cleaned = re.sub(r"[{}\[\]()]", " ", cleaned)
    cleaned = re.sub(r"[|]+", " ", cleaned)
    # Normalize weird whitespace to plain spaces.
    cleaned = cleaned.replace("\u00A0", " ").replace("\u202F", " ")
    cleaned = re.sub(r"[^\w\s/.,:+-]", " ", cleaned, flags=re.UNICODE)
    cleaned = _normalize_space(cleaned)
    return cleaned


def _parse_price(text: str) -> Optional[int]:
    if not text:
        return None

    # Prefer dedicated "price" patterns.
    m = _RE_PRICE_WITH_SUM.search(text)
    if not m:
        m = _RE_PRICE_ANY.search(text)
    if not m:
        return None

    raw = m.group(1).replace("\u00A0", " ").replace(" ", "")
    try:
        return int(raw)
    except ValueError:
        return None


def _parse_rooms(text: str) -> int | None:
    if not text:
        return None
    text = _clean_text_for_parsing(text)

    # Common explicit patterns first
    for pattern in (
        _RE_ROOMS_SLASH_LAYOUT,
        _RE_ROOMS_DASH_COMN,
        _RE_ROOMS_KOMNATNAYA,
        _RE_ROOMS_4X_COM,
        _RE_ROOMS_HONALI,
        _RE_ROOMS_XONALI,
        _RE_ROOMS_HONA,
        _RE_ROOMS_HDOTK,
        _RE_ROOMS_ANY_DIGIT_BEFORE_COM,
        _RE_ROOMS_COMN,
        _RE_ROOMS_COM,
        _RE_ROOMS_COMN_SHORT,
    ):
        m = pattern.search(text)
        if not m:
            continue
        try:
            v = int(m.group("rooms"))
        except Exception:
            return None
        # Room counts on OLX are usually 1..10; ignore weird matches.
        if 1 <= v <= 10:
            return v

    # Fallback: if card contains "5+" as "5+" and doesn't match above, try that too.
    if re.search(r"\b5\+\b", text):
        return 5

    return None


def _parse_area(text: str) -> float | None:
    if not text:
        return None
    text = _clean_text_for_parsing(text)
    # 1) Preferred: with units.
    m = _RE_AREA_UNITS.search(text)
    if m:
        raw = m.group("area").replace(",", ".")
        try:
            area = float(raw)
            if area < 20 or area > 500:
                logger.warning("OLX: area out of bounds parsed=%s raw=%r", area, raw)
                return None
            return area
        except ValueError:
            return None

    # 2) No fallback here: strict unit-based parsing avoids mixing floors/other numbers.
    return None


def _parse_district_label(text: str) -> str | None:
    """
    Tries to extract something like:
      "Ташкент, Чиланзарский район - ..."
    """
    # Keep it simple: capture text between "Ташкент," and "район"
    m = re.search(r"Ташкент,\s*([^-\n]+?район)\b", text or "", re.IGNORECASE)
    if not m:
        return None
    return _normalize_space(m.group(1))


def _map_tashkent_district_label_to_slug(district_label: str | None) -> str | None:
    if not district_label:
        return None
    norm = _normalize_space(_clean_text_for_parsing(district_label)).casefold()
    for slug, needles in _TASHKENT_DISTRICT_SLUGS:
        if any(needle.casefold() in norm for needle in needles):
            return slug
    return None


def _detect_tashkent_district_slug(text: str | None) -> str | None:
    if not text:
        return None
    norm = _normalize_space(_clean_text_for_parsing(text)).casefold()
    for slug, needles in _TASHKENT_DISTRICT_SLUGS:
        if any(needle.casefold() in norm for needle in needles):
            return slug
    return None


def _parse_olx_id_from_href(href: str) -> str:
    # Example:
    # https://www.olx.uz/d/obyavlenie/svoya-3-2-4-novza-metro-ID4gCJv.html
    # We want "ID4gCJv".
    m = re.search(r"(ID[A-Za-z0-9]+)", href)
    if m:
        return m.group(1)
    # fallback: last number sequence
    m2 = re.findall(r"\d+", href)
    return m2[-1] if m2 else href


def _extract_ads_from_dom_text(
    ad_text: str,
    href: str,
    title_fallback: str,
    ad_type: str,
    city: str,
    base_url: str,
    price_text: str | None = None,
    image_url: str | None = None,
    rooms_text: str | None = None,
    area_text: str | None = None,
) -> ParsedAd | None:
    try:
        title = _normalize_space(title_fallback or ad_text[:80])
        price = _parse_price(price_text or ad_text)
        if price is None:
            return None
        cleaned_full_text = _clean_text_for_parsing(ad_text)
        rooms_source = rooms_text or cleaned_full_text
        area_source = area_text or cleaned_full_text

        rooms = _parse_rooms(rooms_source)
        area = _parse_area(area_source)
        district = _parse_district_label(cleaned_full_text)
        district_slug = _map_tashkent_district_label_to_slug(district) or _detect_tashkent_district_slug(
            f"{title} {cleaned_full_text}"
        )
        # If we can map district label -> slug, overwrite `city` with district slug.
        # This is crucial when worker fetches from `/tashkent/` but we need per-district matching.
        parsed_city = district_slug or city

        # Rooms fallback: try to parse rooms from title if not found in the card text.
        if rooms is None:
            rooms = _parse_rooms(title)
        link = href if href.startswith("http") else urljoin(base_url, href)
        resolved_image = image_url
        if resolved_image and not resolved_image.startswith("http"):
            resolved_image = urljoin(base_url, resolved_image)
        olx_id = _parse_olx_id_from_href(link)

        # Debug logging for parsing failures.
        sample = _normalize_space(ad_text)[:100]
        if rooms is None:
            logger.warning("OLX parse: rooms not found. text[:100]=%r", sample)
        if area is None:
            logger.warning("OLX parse: area not found. text[:100]=%r", sample)

        return ParsedAd(
            olx_id=olx_id,
            title=title,
            price=price,
            link=link,
            image_url=resolved_image,
            rooms=rooms,
            area=area,
            ad_type=ad_type,
            city=parsed_city,
            district=district,
        )
    except Exception:
        logger.exception("Failed to build ParsedAd from DOM")
        return None


async def _auto_scroll(page: Page, steps: int = 4, delay_ms: int = 600) -> None:
    for _ in range(steps):
        await page.mouse.wheel(0, 2500)
        await page.wait_for_timeout(delay_ms)


async def _collect_candidate_ads(
    page: Page,
) -> list[tuple[str, str, str, str, str | None, str | None, str | None]]:
    """
    Возвращает кортежи (href, title, price_text, card_text, rooms_text, area_text, image_url).

    Селекторы зависят от разметки OLX, поэтому используем общий подход:
    берём все ссылки, ведущие на объявления (`/d/obyavlenie/...`),
    и читаем innerText родственного карточного контейнера.
    """
    async def _extract_with_selector(
        selector: str,
    ) -> list[tuple[str, str, str, str, str | None, str | None, str | None]]:
        candidates = await page.eval_on_selector_all(
            selector,
        """
        (els) => els
          .slice(0, 60)
          .map(el => {
            const href = el.href || el.getAttribute('href') || '';
            const card = el.closest('[data-cy="l-card"]') || el.closest('li') || el.closest('article') || el.closest('div') || el.parentElement;
            const titleNode =
              card?.querySelector('[data-testid="ad-title"]') ||
              card?.querySelector('h3') ||
              card?.querySelector('a') ||
              el;

            const priceNode =
              card?.querySelector('p[data-testid="ad-price"]') ||
              card?.querySelector('[data-testid="ad-price"]') ||
              null;

            const title = (titleNode?.innerText || titleNode?.textContent || '').trim();
            const priceText = priceNode ? (priceNode.innerText || priceNode.textContent || '').trim() : null;
            const text = (card && card.innerText ? card.innerText : (el.innerText || el.textContent || '')).trim();
            const roomsNode =
              card?.querySelector('[data-testid*="rooms"]') ||
              card?.querySelector('[data-testid*="комн"]') ||
              card?.querySelector('[data-testid*="комнат"]') ||
              null;
            const areaNode =
              card?.querySelector('[data-testid*="m2"]') ||
              card?.querySelector('[data-testid*="area"]') ||
              card?.querySelector('[data-testid*="м2"]') ||
              null;

            const roomsText = roomsNode ? (roomsNode.innerText || roomsNode.textContent || '').trim() : null;
            const areaText = areaNode ? (areaNode.innerText || areaNode.textContent || '').trim() : null;

            const img = (card && card.querySelector('img')) || el.querySelector('img');
            const imageUrl = img
              ? (img.currentSrc || img.src || img.getAttribute('src') || img.getAttribute('data-src') || null)
              : null;

            return { href, title, priceText, text, roomsText, areaText, imageUrl };
          })
          .filter(x => x.href)
        """,
        )

        result: list[tuple[str, str, str, str, str | None, str | None, str | None]] = []
        for c in candidates:
            result.append(
                (
                    c["href"],
                    c["title"],
                    c["priceText"] or "",
                    c["text"],
                    c["roomsText"],
                    c["areaText"],
                    c["imageUrl"],
                )
            )
        return result

    # 1) Plan A: use known grid/card markers if present.
    primary_selector = (
        'div[data-testid="listing-grid"] a[href*="/d/obyavlenie/"], '
        'a[href*="/d/obyavlenie/"]'
    )
    result = await _extract_with_selector(primary_selector)
    if result:
        return result

    # 2) Plan B: if OLX markup differs - take every ad link on the page.
    logger.warning("OLX: primary selector returned 0 candidates, trying plan B...")
    fallback_selector = 'a[href^="/d/obyavlenie/"], a[href*="/d/obyavlenie/"]'
    result = await _extract_with_selector(fallback_selector)
    return result


async def fetch_ads_from_search(
    *,
    url: str,
    ad_type: str,
    city: str,
    limit: int = 50,
) -> list[ParsedAd]:
    """
    Асинхронно заходит на OLX страницу поиска и парсит последние карточки.

    Важно: extraction на OLX сделан эвристиками и может потребовать точечной правки
    под фактическую разметку (после первых прогонов).
    """
    base_url = settings.OLX_BASE_URL
    ads: list[ParsedAd] = []
    headless_env = os.getenv("OLX_HEADLESS", "true").strip().lower()
    headless = headless_env in {"1", "true", "yes", "y", "on"}

    repo_root = Path(__file__).resolve().parents[3]

    try:
        async with Stealth().use_async(async_playwright()) as p:
            browser: Browser = await p.chromium.launch(headless=headless)
            page: Page = await browser.new_page()

            logger.info("OLX navigate: %s", url)
            await page.goto(url, wait_until="domcontentloaded")
            # Strong waits: OLX is heavily JS-driven; we need deterministic rendering.
            try:
                await page.wait_for_load_state("networkidle")
            except Exception:
                logger.debug("OLX: wait_for_load_state(networkidle) failed; continuing anyway")

            await asyncio.sleep(5)

            try:
                page_title = await page.title()
                logger.info("Page title: %s", page_title)
                if page_title and ("Access Denied" in page_title or "Just a moment" in page_title):
                    logger.warning("OLX: possible block/captcha detected (title=%s)", page_title)
            except Exception:
                logger.debug("OLX: page.title() failed")

            candidates = await _collect_candidate_ads(page)
            logger.info("OLX: candidates=%s for url=%s", len(candidates), url)

            if len(candidates) == 0:
                screenshot_path = str(repo_root / "debug_screenshot.png")
                try:
                    await page.screenshot(path=screenshot_path, full_page=True)
                    logger.warning("OLX: candidates==0, screenshot saved: %s", screenshot_path)
                except Exception:
                    logger.warning("OLX: candidates==0, failed to save screenshot")

            count = 0
            for href, title, price_text, text, rooms_text, area_text, image_url in candidates:
                if count >= limit:
                    break

                parsed = _extract_ads_from_dom_text(
                    ad_text=text,
                    href=href,
                    title_fallback=title or text,
                    ad_type=ad_type,
                    city=city,
                    base_url=base_url,
                    price_text=price_text or None,
                    image_url=image_url,
                    rooms_text=rooms_text,
                    area_text=area_text,
                )
                if parsed is not None:
                    ads.append(parsed)
                    count += 1

            if not ads:
                # Print HTML of the first card to debug selector breakage.
                try:
                    card_html = await page.inner_html('[data-cy="l-card"]')
                except Exception:
                    card_html = None

                if not card_html:
                    try:
                        card_html = await page.inner_html('div[data-testid="listing-grid"]')
                    except Exception:
                        card_html = None

                if card_html:
                    logger.warning(
                        "OLX: ads_count==0. First card html (truncated): %s",
                        card_html[:2500],
                    )
                else:
                    logger.warning("OLX: ads_count==0. Could not extract card HTML for debug.")

            await browser.close()

    except Exception:
        logger.exception("fetch_ads_from_search failed (url=%s)", url)

    return ads


def build_search_url(*, ad_type: str, city_slug: str) -> str:
    if ad_type == "sale":
        path = f"/nedvizhimost/kvartiry/prodazha/{city_slug}/"
    else:
        path = f"/nedvizhimost/kvartiry/arenda-dolgosrochnaya/{city_slug}/"
    return urljoin(settings.OLX_BASE_URL, path)


# Compatibility alias (requested by QA tests)
async def fetch_ads(*, url: str, ad_type: str, city: str, limit: int = 50) -> list[ParsedAd]:
    return await fetch_ads_from_search(url=url, ad_type=ad_type, city=city, limit=limit)

