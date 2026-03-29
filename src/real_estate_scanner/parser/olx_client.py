from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse
from zoneinfo import ZoneInfo

from playwright.async_api import Browser, Page, async_playwright
from playwright_stealth import Stealth
from typing import Optional

from real_estate_scanner.config import settings

logger = logging.getLogger(__name__)
_LOCAL_TZ = ZoneInfo("Asia/Tashkent")
_RU_MONTHS = {
    "января": 1,
    "февраля": 2,
    "марта": 3,
    "апреля": 4,
    "мая": 5,
    "июня": 6,
    "июля": 7,
    "августа": 8,
    "сентября": 9,
    "октября": 10,
    "ноября": 11,
    "декабря": 12,
}


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
    image_urls: list[str] = field(default_factory=list)
    district: str | None = None
    floor: int | None = None
    total_floors: int | None = None
    description: str | None = None
    author_name: str | None = None
    owner_type: str | None = None
    created_at_text: str | None = None
    seller_phone: str | None = None
    published_at: datetime | None = None
    details_loaded: bool = False


_TASHKENT_DISTRICT_SLUGS: list[tuple[str, tuple[str, ...]]] = [
        (
            "mirzoulugbek",
            (
            "Мирзо-Улугбекский",
            "Мирзо Улугбекский",
            "Mirzo Ulug'bek",
            "Mirzo Ulugbek",
            "Мирзо Улуғбек",
            "Мирзо Улугбек",
            "ТТЗ",
            "TTZ",
            "Максим Горький",
            "M Gorkiy",
            "M.Gorkiy",
            "БИЙ",
            "BIY",
            "Буюк Ипак Йули",
            "Buyuk Ipak Yoli",
            "Buyuk Ipak Yo'li",
            "Феруза",
            "Feruza",
            "Паркентский",
            "Parkentskiy",
            "Аккурган",
            "Okkurgan",
            "Новомосковская",
            "Novomoskovskaya",
            "Карасу",
            "Qorasuv",
            "Ц 1",
            "C 1",
            "Ц 2",
            "C 2",
        ),
    ),
        (
            "yashnabadskiy",
            (
            "Яшнабадский",
            "Yashnobod",
            "Яшнобод",
            "Яшнабод",
            "Дустлик",
            "Do'stlik",
            "Dostlik",
            "Авиасозлар",
            "Aviasozlar",
            "Кадышева",
            "Kadysheva",
            "Рохат",
            "Rohat",
            "Лисунова",
            "Lisunova",
            "Ташсельмаш",
            "Tashselmash",
            "40 лет Победы",
            "40 let",
            "Панельный",
            "Panelniy",
        ),
    ),
        (
            "yakkasarayskiy",
            (
            "Яккасарайский",
            "Yakkasaroy",
            "Яккасарой",
            "ЦУМ",
            "TSUM",
            "Космонавтов",
            "Kosmonavtlar",
            "Шота Руставели",
            "Shota Rustaveli",
            "Бабура",
            "Bobur",
            "Аския",
            "Askiya",
            "Ракат",
            "Rakat",
            "Кушбеги",
            "Kushbegi",
            "Башлык",
            "Bashliq",
        ),
    ),
        (
            "chilanzarskiy",
            (
            "Чиланзарский",
            "Chilonzor",
            "Чилонзор",
            "Чиланзор",
            "Фархадский",
            "Farhod",
            "Актепа",
            "Oqtepa",
            "Катартал",
            "Qatortol",
            "Альгоритм",
            "Algoritm",
            "Домрабад",
            "Domrobod",
            "Шухрат",
            "Shuhrat",
        ),
    ),
        (
            "yunusabadskiy",
            (
            "Юнусабадский",
            "Yunusobod",
            "Юнусобод",
            "Шахристан",
            "Shahriston",
            "Мегапланет",
            "Megaplanet",
            "Туркистон",
            "Turkiston",
            "Зенит",
            "Zenit",
            "Бодомзор",
            "Bodomzor",
            "Юнусобод",
            "Юнус Абад",
            "Юнус-Абад",
        ),
    ),
        (
            "mirabadskiy",
            (
            "Мирабадский",
            "Mirobod",
            "Миробод",
            "Госпитальный",
            "Gospitalniy",
            "Ойбек",
            "Oybek",
            "Мирабад",
            "Mirabod",
            "Первушка",
            "Pervushka",
            "Саракулька",
            "Sarakulka",
            "Мирабад Авеню",
            "Mirabad Avenue",
        ),
    ),
        (
            "almazarskiy",
            (
            "Алмазарский",
            "Olmazor",
            "Олмазор",
            "Себзор",
            "Sebzor",
            "Каракамыш",
            "Qoraqamish",
            "Тансыкбаева",
            "Tansiqboyev",
            "Ганга",
            "Ganga",
            "Беруни",
            "Beruniy",
            "Чорсу",
            "Chorsu",
        ),
    ),
        (
            "uchtepinskiy",
            (
            "Учтепинский",
            "Uchtepa",
            "Учтепа",
            "Бешкайрагач",
            "Beshqayragoch",
            "Beshqayrag'och",
            "Чиланзар 2",
            "Chilonzor 2",
        ),
    ),
        (
            "sergeli",
            (
            "Сергелийский",
            "Sergeli",
            "Сергели",
            "Спутник",
            "Sputnik",
            "Янгихаёт",
            "Yangihayot",
            "Чоштепа",
            "Choshtepa",
            "Куйлюк",
            "Qoyliq",
            "Qo'yliq",
        ),
    ),
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
_RE_ROOMS_DASH_X_COMN = re.compile(r"(?P<rooms>\d+)\s*-\s*[хx]\s*комн\b", re.IGNORECASE)
_RE_ROOMS_DASH_COM = re.compile(r"(?P<rooms>\d+)\s*-\s*ком\b", re.IGNORECASE)
_RE_ROOMS_KOMNATNAYA = re.compile(r"(?P<rooms>\d+)\s*-\s*комнатн(?:ая|ую|ой)\b", re.IGNORECASE)
_RE_ROOMS_KOMNATNAYA_NO_DASH = re.compile(r"(?P<rooms>\d+)\s*комнатн(?:ая|ую|ой)\b", re.IGNORECASE)
_RE_ROOMS_KOMNAT = re.compile(r"(?P<rooms>\d+)\s*комнат[аы]?\b", re.IGNORECASE)
_RE_ROOMS_COM_YA = re.compile(r"(?P<rooms>\d+)\s*ком-?я\b", re.IGNORECASE)
_RE_ROOMS_COM_WITH_PUNCT = re.compile(r"(?P<rooms>\d+)\s*[,.]?\s*комн?\b", re.IGNORECASE)
_RE_ROOMS_XCOM_YA = re.compile(r"(?P<rooms>\d+)\s*[хx]\s*ком-?я\b", re.IGNORECASE)
_RE_ROOMS_4X_COM = re.compile(r"(?P<rooms>\d+)\s*[хx]\s*ком\b", re.IGNORECASE)
_RE_ROOMS_COM = re.compile(r"(?P<rooms>\d+)\s*х\s*ком\b", re.IGNORECASE)
_RE_ROOMS_COMN = re.compile(r"(?P<rooms>\d+)\s*(?:комн|комнат)\b", re.IGNORECASE)
_RE_ROOMS_HDOTK = re.compile(r"(?P<rooms>\d+)\s*х\.?\s*к\.?", re.IGNORECASE)  # "х. к."
_RE_ROOMS_COMN_SHORT = re.compile(r"(?P<rooms>\d+)\s*ком(?:н)?\b", re.IGNORECASE)
_RE_ROOMS_SLASH_LAYOUT = re.compile(
    r"(?P<rooms>\d+)\s*/\s*\d+\s*/\s*\d+(?:\s*(?:хона|хонали|xona|xonali))?\b",
    re.IGNORECASE,
)

_RE_ROOMS_ANY_DIGIT_BEFORE_COM = re.compile(
    r"(?P<rooms>\d+)\s*(?:х\.?\s*к\.?|комн|комнат)\b",
    re.IGNORECASE,
)

# Area patterns (strict: must include m2 / кв.м / м² tokens)
_RE_AREA_UNITS = re.compile(r"(?P<area>\d+(?:[.,]\d+)?)\s*sqm\b", re.IGNORECASE)
_RE_AREA_WORDY = re.compile(r"(?P<area>\d+(?:[.,]\d+)?)\s*квад\w*", re.IGNORECASE)
_RE_AREA_LAT_WORDY = re.compile(r"(?P<area>\d+(?:[.,]\d+)?)\s*(?:kv|kvm|kvmetr|kv metr)\b", re.IGNORECASE)
_RE_AREA_INLINE = re.compile(r"(?P<area>\d+(?:[.,]\d+)?)\s*(?:sq\s*m|square\s*meters?)", re.IGNORECASE)
_RE_AREA_KV_SHORT = re.compile(r"(?P<area>\d+(?:[.,]\d+)?)\s*кв\b", re.IGNORECASE)
_RE_LAYOUT_TRIPLET = re.compile(r"\b\d+\s*/\s*\d+\s*/\s*\d+\b")
REQUIRED_FIELDS = {"rooms", "area", "floor", "total_floors"}


def _normalize_space(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def _safe_int(value: str | None) -> int | None:
    if not value:
        return None
    match = re.search(r"\d+", value)
    return int(match.group()) if match else None


def _safe_float(value: str | None) -> float | None:
    if not value:
        return None
    match = re.search(r"\d+(?:[.,]\d+)?", value)
    if not match:
        return None
    try:
        return float(match.group().replace(",", "."))
    except ValueError:
        return None


def _contains_layout_triplet(text: str | None) -> bool:
    return bool(text and _RE_LAYOUT_TRIPLET.search(text))


def _clean_text_for_parsing(text: str) -> str:
    """
    OLX иногда вставляет служебные символы (например, фигурные скобки),
    которые мешают regex-поиску.
    """
    if not text:
        return ""
    cleaned = (
        text.replace("m²", " sqm ")
        .replace("м²", " sqm ")
        .replace("м2", " sqm ")
        .replace("m2", " sqm ")
        .replace("m 2", " sqm ")
        .replace("м 2", " sqm ")
        .replace("кв.м", " sqm ")
        .replace("кв м", " sqm ")
        .replace("кв. м", " sqm ")
        .replace("кв/м", " sqm ")
        .replace("квм", " sqm ")
        .replace("квметр", " sqm ")
        .replace("квадратных", " sqm ")
        .replace("квадратный", " sqm ")
        .replace("квадтраных", " sqm ")
    )
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
        _RE_ROOMS_DASH_X_COMN,
        _RE_ROOMS_DASH_COMN,
        _RE_ROOMS_DASH_COM,
        _RE_ROOMS_KOMNATNAYA,
        _RE_ROOMS_KOMNATNAYA_NO_DASH,
        _RE_ROOMS_KOMNAT,
        _RE_ROOMS_XCOM_YA,
        _RE_ROOMS_COM_YA,
        _RE_ROOMS_COM_WITH_PUNCT,
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
    if not m:
        m = _RE_AREA_WORDY.search(text)
    if not m:
        m = _RE_AREA_LAT_WORDY.search(text)
    if not m:
        m = _RE_AREA_INLINE.search(text)
    if not m:
        m = _RE_AREA_KV_SHORT.search(text)
    if m:
        raw = m.group("area").replace(",", ".")
        try:
            area = float(raw)
            if area < 5 or area > 30_000:
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
        if any(_normalize_space(_clean_text_for_parsing(needle)).casefold() in norm for needle in needles):
            return slug
    return None


def _detect_tashkent_district_slug(text: str | None) -> str | None:
    if not text:
        return None
    norm = _normalize_space(_clean_text_for_parsing(text)).casefold()
    for slug, needles in _TASHKENT_DISTRICT_SLUGS:
        if any(_normalize_space(_clean_text_for_parsing(needle)).casefold() in norm for needle in needles):
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


def _extract_floor_info(text: str) -> tuple[int | None, int | None]:
    if not text:
        return None, None
    cleaned = _clean_text_for_parsing(text)

    patterns = (
        re.compile(r"(?P<floor>\d+)\s*/\s*(?P<total>\d+)", re.IGNORECASE),
        re.compile(r"этаж\s*[:\-]?\s*(?P<floor>\d+)\D+этажн(?:ость)?\s*[:\-]?\s*(?P<total>\d+)", re.IGNORECASE),
        re.compile(r"этажн(?:ость)?\s*[:\-]?\s*(?P<total>\d+)\D+этаж\s*[:\-]?\s*(?P<floor>\d+)", re.IGNORECASE),
    )
    for pattern in patterns:
        match = pattern.search(cleaned)
        if not match:
            continue
        try:
            floor = int(match.group("floor"))
            total = int(match.group("total"))
        except (TypeError, ValueError):
            continue
        return floor, total
    return None, None


def _extract_labeled_area(text: str) -> float | None:
    area_tokens = (
        "\u043e\u0431\u0449\u0430\u044f \u043f\u043b\u043e\u0449\u0430\u0434\u044c",
        "\u0436\u0438\u043b\u0430\u044f \u043f\u043b\u043e\u0449\u0430\u0434\u044c",
        "total area",
        "living area",
    )
    for raw_line in (text or "").splitlines():
        line = _normalize_space(raw_line.replace("\u00A0", " ").replace("\u202F", " "))
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key_norm = key.casefold()
        if not any(token in key_norm for token in area_tokens):
            continue
        area = _safe_float(value)
        if area is not None and 5 <= area <= 30_000:
            return area
    return None


def _extract_labeled_floor_info(text: str) -> tuple[int | None, int | None]:
    floor = None
    total = None
    floor_tokens = (
        "\u044d\u0442\u0430\u0436",
        "floor",
    )
    total_tokens = (
        "\u044d\u0442\u0430\u0436\u043d\u043e\u0441\u0442\u044c \u0434\u043e\u043c\u0430",
        "\u044d\u0442\u0430\u0436\u043d\u043e\u0441\u0442\u044c",
        "total floors",
        "building floors",
    )

    for raw_line in (text or "").splitlines():
        line = _normalize_space(raw_line.replace("\u00A0", " ").replace("\u202F", " "))
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key_norm = key.casefold()
        if total is None and any(token in key_norm for token in total_tokens):
            total = _safe_int(value)
            continue
        if floor is None and any(token in key_norm for token in floor_tokens):
            floor = _safe_int(value)

    if floor is None and total is None:
        return None, None
    return floor, total


def _extract_created_at_from_text(text: str) -> tuple[str | None, datetime | None]:
    if not text:
        return None, None

    normalized = _normalize_space(text)
    now = datetime.now(_LOCAL_TZ)

    rel_match = re.search(r"\b(\u0421\u0435\u0433\u043e\u0434\u043d\u044f|\u0412\u0447\u0435\u0440\u0430)\s+\u0432\s+(\d{1,2}):(\d{2})", normalized, re.IGNORECASE)
    if rel_match:
        day_word = rel_match.group(1).casefold()
        hour = int(rel_match.group(2))
        minute = int(rel_match.group(3))
        base_date = now.date() if day_word == "\u0441\u0435\u0433\u043e\u0434\u043d\u044f" else (now - timedelta(days=1)).date()
        created_at = datetime(base_date.year, base_date.month, base_date.day, hour, minute, tzinfo=_LOCAL_TZ)
        return rel_match.group(0), created_at

    abs_match = re.search(
        r"\b(\d{1,2})\s+([\u0410-\u042f\u0430-\u044f\u0401\u0451]+)\s*(\d{4})?\s*(?:\u0433\.?)?(?:\s+\u0432\s+(\d{1,2}):(\d{2}))?",
        normalized,
        re.IGNORECASE,
    )
    if abs_match:
        day = int(abs_match.group(1))
        month_name = abs_match.group(2).casefold()
        month = _RU_MONTHS.get(month_name)
        if month:
            year = int(abs_match.group(3)) if abs_match.group(3) else now.year
            hour = int(abs_match.group(4)) if abs_match.group(4) else 0
            minute = int(abs_match.group(5)) if abs_match.group(5) else 0
            try:
                created_at = datetime(year, month, day, hour, minute, tzinfo=_LOCAL_TZ)
                return abs_match.group(0), created_at
            except ValueError:
                return abs_match.group(0), None

    return None, None




def _extract_first_src_from_srcset(srcset: str | None) -> str | None:
    if not srcset:
        return None
    first_item = srcset.split(",")[0].strip()
    if not first_item:
        return None
    return first_item.split(" ")[0].strip() or None


def _pick_best_image_url(*candidates: str | None) -> str | None:
    for candidate in candidates:
        if not candidate:
            continue
        normalized = candidate.strip()
        if not normalized:
            continue
        if "," in normalized and "http" in normalized:
            normalized = _extract_first_src_from_srcset(normalized) or normalized
        if normalized.startswith("//"):
            return f"https:{normalized}"
        if normalized.startswith("http"):
            return normalized
    return None


def _first_param_value(params: dict[str, str], *keys: str) -> str | None:
    for key in keys:
        normalized_key = _normalize_space(key).casefold()
        if normalized_key in params:
            return params[normalized_key]
    return None


def _extract_parameter_map(text: str | None) -> dict[str, str]:
    result: dict[str, str] = {}
    for raw_line in (text or "").splitlines():
        line = _normalize_space(raw_line)
        if not line or ":" not in line:
            continue
        key, value = line.split(":", 1)
        normalized_key = _normalize_space(key).casefold()
        normalized_value = _normalize_space(value)
        if normalized_key and normalized_value:
            result[normalized_key] = normalized_value
    return result


async def _extract_parameters_from_dom(page: Page) -> dict[str, str]:
    selectors = (
        '[data-testid="ad-parameters-container"] p',
        '[data-testid="qa-advert-parameters"] p',
        '[data-cy="ad-parameters"] p',
    )

    lines: list[str] = []
    for selector in selectors:
        try:
            extracted = await page.eval_on_selector_all(
                selector,
                """(els) => els
                    .map(el => (el.innerText || el.textContent || '').trim())
                    .filter(Boolean)
                """,
            )
        except Exception:
            continue
        if extracted:
            lines.extend(str(item) for item in extracted if str(item).strip())

    return _extract_parameter_map("\n".join(lines))


def extract_field(
    *,
    field_name: str,
    strong_extractors: list[Callable[[], Any]],
    labeled_extractors: list[Callable[[], Any]],
    weak_extractors: list[Callable[[], Any]],
    text: str | None = None,
    block_weak_patterns: list[re.Pattern[str]] | None = None,
    required: bool = False,
    url: str | None = None,
) -> Any:
    strong_result = None
    for extractor in strong_extractors:
        value = extractor()
        if value is not None:
            return value
        strong_result = value

    labeled_result = None
    for extractor in labeled_extractors:
        value = extractor()
        if value is not None:
            return value
        labeled_result = value

    weak_blocked = bool(text and block_weak_patterns and any(pattern.search(text) for pattern in block_weak_patterns))
    if weak_blocked:
        logger.debug(
            "OLX weak blocked: field=%s text=%s",
            field_name,
            (text or "")[:100],
        )

    if required:
        debug_value = None
        if not weak_blocked:
            for extractor in weak_extractors:
                value = extractor()
                if value is not None:
                    debug_value = value
                    break
        logger.error(
            "OLX REQUIRED FIELD MISSING: field=%s url=%s",
            field_name,
            url,
        )
        logger.error(
            "OLX extract failed: field=%s | strong=%s | labeled=%s | weak_candidate=%s | text_sample=%s | url=%s",
            field_name,
            strong_result,
            labeled_result,
            debug_value,
            text[:120] if text else None,
            url,
        )
        return None

    if not weak_blocked:
        for extractor in weak_extractors:
            value = extractor()
            if value is not None:
                return value

    logger.debug(
        "OLX missing field after extraction: field=%s url=%s",
        field_name,
        url,
    )
    return None


def _extract_ads_from_dom_text(
    ad_text: str,
    href: str,
    title_fallback: str,
    ad_type: str,
    city: str,
    base_url: str,
    price_text: str | None = None,
    image_url: str | None = None,
    params: list[str] | None = None,
    rooms_text: str | None = None,
    area_text: str | None = None,
) -> ParsedAd | None:
    try:
        title = _normalize_space(title_fallback or ad_text[:80])
        price = _parse_price(price_text or ad_text)
        if price is None:
            return None
        cleaned_full_text = _clean_text_for_parsing(ad_text)
        params_lines = [_normalize_space(item) for item in (params or []) if _normalize_space(item)]
        params_text = "\n".join(dict.fromkeys(params_lines))
        parameter_map = _extract_parameter_map(params_text)
        rooms = _extract_rooms_value(
            params=parameter_map,
            params_text=params_text or (rooms_text or ""),
            fallback_text=rooms_text or title,
        )
        area = _extract_area_value(
            params=parameter_map,
            params_text=params_text or (area_text or ""),
            fallback_text=area_text,
        )
        district = _parse_district_label(cleaned_full_text)
        district_slug = _map_tashkent_district_label_to_slug(district) or _detect_tashkent_district_slug(
            f"{title} {cleaned_full_text}"
        )
        created_at_text, published_at = _extract_created_at_from_text(cleaned_full_text)
        # If we can map district label -> slug, overwrite `city` with district slug.
        # This is crucial when worker fetches from `/tashkent/` but we need per-district matching.
        parsed_city = district_slug or city

        link = href if href.startswith("http") else urljoin(base_url, href)
        resolved_image = image_url
        if resolved_image and not resolved_image.startswith("http"):
            resolved_image = urljoin(base_url, resolved_image)
        olx_id = _parse_olx_id_from_href(link)

        # Debug logging for parsing failures.
        sample = _normalize_space(ad_text)[:100]
        if rooms is None:
            logger.debug("OLX parse: rooms not found. text[:100]=%r", sample)
        if area is None:
            logger.debug("OLX parse: area not found. text[:100]=%r", sample)

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
            image_urls=[resolved_image] if resolved_image else [],
            district=district,
            created_at_text=created_at_text,
            published_at=published_at,
        )
    except Exception:
        logger.exception("Failed to build ParsedAd from DOM")
        return None


def _extract_labeled_room_count(text: str) -> int | None:
    room_tokens = (
        "\u043a\u043e\u043c\u043d\u0430\u0442",
        "\u043a\u043e\u043c\u043d\u0430\u0442\u044b",
        "\u043a\u043e\u043b\u0438\u0447\u0435\u0441\u0442\u0432\u043e \u043a\u043e\u043c\u043d\u0430\u0442",
        "\u043a\u043e\u043c\u043d\u0430\u0442\u044b",
        "\u043a\u043e\u043c\u043d\u0430\u0442\u0430",
        "\u043a\u043e\u043c\u043d\u0430\u0442\u043d\u043e\u0441\u0442\u044c",
    )
    for raw_line in (text or "").splitlines():
        line = _normalize_space(raw_line.replace("\u00A0", " ").replace("\u202F", " "))
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key_norm = key.casefold()
        if not any(token in key_norm for token in room_tokens):
            continue
        rooms = _safe_int(value)
        if rooms is not None and 1 <= rooms <= 10:
            return rooms
    return None


def _extract_area_value(
    *,
    params: dict[str, str],
    params_text: str,
    fallback_text: str | None = None,
    required: bool = False,
    url: str | None = None,
) -> float | None:
    def _strong_param_area() -> float | None:
        value = _safe_float(
            _first_param_value(
                params,
                "\u043e\u0431\u0449\u0430\u044f \u043f\u043b\u043e\u0449\u0430\u0434\u044c",
                "\u0436\u0438\u043b\u0430\u044f \u043f\u043b\u043e\u0449\u0430\u0434\u044c",
                "\u043e\u0431\u0449\u0430\u044f \u043f\u043b\u043e\u0449\u0430\u0434\u044c",
                "\u0436\u0438\u043b\u0430\u044f \u043f\u043b\u043e\u0449\u0430\u0434\u044c",
                "\u043e\u0431\u0449\u0435\u0439 \u043f\u043b\u043e\u0449\u0430\u0434\u0438",
                "\u0436\u0438\u043b\u043e\u0439 \u043f\u043b\u043e\u0449\u0430\u0434\u0438",
            )
        )
        if value is not None and 20 <= value <= 500:
            return value
        return None

    return extract_field(
        field_name="area",
        strong_extractors=[_strong_param_area],
        labeled_extractors=[lambda: _extract_labeled_area(params_text)],
        weak_extractors=[
            lambda: _parse_area(params_text),
            lambda: _extract_labeled_area(fallback_text or ""),
            lambda: _parse_area(fallback_text or ""),
        ],
        text=params_text or fallback_text,
        required=required,
        url=url,
    )


def _extract_rooms_value(
    *,
    params: dict[str, str],
    params_text: str,
    fallback_text: str | None = None,
    required: bool = False,
    url: str | None = None,
) -> int | None:
    return extract_field(
        field_name="rooms",
        strong_extractors=[
            lambda: _safe_int(
                _first_param_value(
                    params,
                    "\u043a\u043e\u043b\u0438\u0447\u0435\u0441\u0442\u0432\u043e \u043a\u043e\u043c\u043d\u0430\u0442",
                    "\u043a\u043e\u043c\u043d\u0430\u0442\u044b",
                    "\u043a\u043e\u043b\u0438\u0447\u0435\u0441\u0442\u0432\u043e \u043a\u043e\u043c\u043d\u0430\u0442",
                    "\u043a\u043e\u043c\u043d\u0430\u0442\u044b",
                    "\u043a\u043e\u043b\u0438\u0447\u0435\u0441\u0442\u0432\u0430 \u043a\u043e\u043c\u043d\u0430\u0442",
                    "\u043a\u043e\u043c\u043d\u0430\u0442\u0430",
                )
            )
        ],
        labeled_extractors=[lambda: _extract_labeled_room_count(params_text)],
        weak_extractors=[
            lambda: _parse_rooms(params_text),
            lambda: _extract_labeled_room_count(fallback_text or ""),
            lambda: _parse_rooms(fallback_text or ""),
        ],
        text=params_text or fallback_text,
        block_weak_patterns=[_RE_LAYOUT_TRIPLET],
        required=required,
        url=url,
    )


def _extract_floor_values(
    *,
    params: dict[str, str],
    params_text: str,
    fallback_text: str | None = None,
    required: bool = False,
    url: str | None = None,
) -> tuple[int | None, int | None]:
    labeled_floor, labeled_total = _extract_labeled_floor_info(params_text)
    weak_floor, weak_total = _extract_floor_info(params_text)
    fallback_labeled_floor, fallback_labeled_total = _extract_labeled_floor_info(fallback_text or "")
    fallback_weak_floor, fallback_weak_total = _extract_floor_info(fallback_text or "")

    floor = extract_field(
        field_name="floor",
        strong_extractors=[
            lambda: _safe_int(
                _first_param_value(
                    params,
                    "\u044d\u0442\u0430\u0436",
                    "\u044d\u0442\u0430\u0436",
                    "\u044d\u0442\u0430\u0436\u0430",
                )
            )
        ],
        labeled_extractors=[lambda: labeled_floor],
        weak_extractors=[
            lambda: weak_floor,
            lambda: fallback_labeled_floor,
            lambda: fallback_weak_floor,
        ],
        text=params_text or fallback_text,
        required=required,
        url=url,
    )
    total_floors = extract_field(
        field_name="total_floors",
        strong_extractors=[
            lambda: _safe_int(
                _first_param_value(
                    params,
                    "\u044d\u0442\u0430\u0436\u043d\u043e\u0441\u0442\u044c \u0434\u043e\u043c\u0430",
                    "\u044d\u0442\u0430\u0436\u043d\u043e\u0441\u0442\u044c",
                    "\u044d\u0442\u0430\u0436\u043d\u043e\u0441\u0442\u044c \u0434\u043e\u043c\u0430",
                    "\u044d\u0442\u0430\u0436\u043d\u043e\u0441\u0442\u044c",
                    "\u044d\u0442\u0430\u0436\u043d\u043e\u0441\u0442\u0438 \u0434\u043e\u043c\u0430",
                    "\u044d\u0442\u0430\u0436\u043d\u043e\u0441\u0442\u0438",
                )
            )
        ],
        labeled_extractors=[lambda: labeled_total],
        weak_extractors=[
            lambda: weak_total,
            lambda: fallback_labeled_total,
            lambda: fallback_weak_total,
        ],
        text=params_text or fallback_text,
        required=required,
        url=url,
    )
    return floor, total_floors


async def _auto_scroll(page: Page, steps: int = 4, delay_ms: int = 300) -> None:
    try:
        await page.locator('[data-testid="ad-photos-container"]').first.scroll_into_view_if_needed(timeout=1500)
    except Exception:
        pass
    for _ in range(steps):
        await page.mouse.wheel(0, 2500)
        await page.wait_for_timeout(delay_ms)


async def _collect_candidate_ads(
    page: Page,
) -> list[dict[str, Any]]:
    """
    Возвращает кортежи (href, title, price_text, card_text, rooms_text, area_text, image_url).

    Селекторы зависят от разметки OLX, поэтому используем общий подход:
    берём все ссылки, ведущие на объявления (`/d/obyavlenie/...`),
    и читаем innerText родственного карточного контейнера.
    """
    async def _extract_from_cards(selector: str) -> list[dict[str, Any]]:
        candidates = await page.eval_on_selector_all(
            selector,
            """
        (cards) => cards
          .slice(0, 80)
          .map(card => {
            const linkNode =
              card?.querySelector('a[href*="/d/obyavlenie/"]') ||
              card?.querySelector('a[href^="/d/obyavlenie/"]') ||
              null;
            const href = linkNode?.href || linkNode?.getAttribute('href') || '';
            const titleNode =
              card?.querySelector('[data-testid="ad-title"]') ||
              card?.querySelector('h3') ||
              linkNode ||
              card;

            const priceNode =
              card?.querySelector('p[data-testid="ad-price"]') ||
              card?.querySelector('[data-testid="ad-price"]') ||
              null;

            const title = (titleNode?.innerText || titleNode?.textContent || '').trim();
            const priceText = priceNode ? (priceNode.innerText || priceNode.textContent || '').trim() : null;
            const text = (card && card.innerText ? card.innerText : (card.textContent || '')).trim();
            const params = Array.from(card?.querySelectorAll('p') || [])
              .map(p => (p.innerText || p.textContent || '').trim())
              .filter(Boolean);
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

            const img = card?.querySelector('img');
            const imageUrl = img
              ? (img.currentSrc || img.src || img.getAttribute('src') || img.getAttribute('data-src') || null)
              : null;

            return { href, title, priceText, text, params, roomsText, areaText, imageUrl };
          })
          .filter(x => x.href)
        """,
        )
        return list(candidates)

    # 1) Plan A: collect listing cards directly so each ad appears once.
    primary_selector = (
        '[data-cy="l-card"], '
        '[data-testid="listing-grid"] > div, '
        '[data-testid="listing-grid"] > li, '
        'article[data-testid], '
        'li[data-testid]'
    )
    result = await _extract_from_cards(primary_selector)
    if result:
        unique_result: list[dict[str, Any]] = []
        seen_hrefs: set[str] = set()
        for item in result:
            href = (item.get("href") or "").strip()
            if not href or href in seen_hrefs:
                continue
            seen_hrefs.add(href)
            unique_result.append(item)
        return unique_result

    # 2) Plan B: if card markup differs, fall back to ad links and dedupe by href.
    logger.warning("OLX: primary card selector returned 0 candidates, trying link fallback...")
    fallback_selector = 'a[href^="/d/obyavlenie/"], a[href*="/d/obyavlenie/"]'
    result = await page.eval_on_selector_all(
        fallback_selector,
        """
        (els) => els
          .slice(0, 100)
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
            const params = Array.from(card?.querySelectorAll('p') || [])
              .map(p => (p.innerText || p.textContent || '').trim())
              .filter(Boolean);
            const roomsNode =
              card?.querySelector('[data-testid*="rooms"]') ||
              card?.querySelector('[data-testid*="РєРѕРјРЅ"]') ||
              card?.querySelector('[data-testid*="РєРѕРјРЅР°С‚"]') ||
              null;
            const areaNode =
              card?.querySelector('[data-testid*="m2"]') ||
              card?.querySelector('[data-testid*="area"]') ||
              card?.querySelector('[data-testid*="Рј2"]') ||
              null;
            const roomsText = roomsNode ? (roomsNode.innerText || roomsNode.textContent || '').trim() : null;
            const areaText = areaNode ? (areaNode.innerText || areaNode.textContent || '').trim() : null;
            const img = (card && card.querySelector('img')) || el.querySelector('img');
            const imageUrl = img
              ? (img.currentSrc || img.src || img.getAttribute('src') || img.getAttribute('data-src') || null)
              : null;
            return { href, title, priceText, text, params, roomsText, areaText, imageUrl };
          })
          .filter(x => x.href)
        """,
    )
    unique_result: list[dict[str, Any]] = []
    seen_hrefs: set[str] = set()
    for item in result:
        href = (item.get("href") or "").strip()
        if not href or href in seen_hrefs:
            continue
        seen_hrefs.add(href)
        unique_result.append(item)
    return unique_result


def _build_search_page_url(
    *,
    base_url: str,
    page_number: int = 1,
    price_from: int | None = None,
    price_to: int | None = None,
) -> str:
    parsed = urlparse(base_url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))

    if page_number > 1:
        query["page"] = str(page_number)
    else:
        query.pop("page", None)

    if price_from is not None:
        query["search[filter_float_price:from]"] = str(price_from)
    else:
        query.pop("search[filter_float_price:from]", None)

    if price_to is not None:
        query["search[filter_float_price:to]"] = str(price_to)
    else:
        query.pop("search[filter_float_price:to]", None)

    encoded_query = urlencode(query)
    return urlunparse(parsed._replace(query=encoded_query))


async def detect_last_page_for_search(
    *,
    url: str,
    price_from: int | None = None,
    price_to: int | None = None,
    max_page: int = 25,
) -> int | None:
    headless_env = os.getenv("OLX_HEADLESS", "true").strip().lower()
    headless = headless_env in {"1", "true", "yes", "y", "on"}

    target_url = _build_search_page_url(
        base_url=url,
        page_number=1,
        price_from=price_from,
        price_to=price_to,
    )

    try:
        async with Stealth().use_async(async_playwright()) as p:
            browser: Browser | None = None
            context = None
            page: Page | None = None
            try:
                browser = await p.chromium.launch(headless=headless)
                context = await browser.new_context()
                page = await context.new_page()

                logger.info(
                    "OLX detect last page: %s (price_from=%s, price_to=%s)",
                    target_url,
                    price_from,
                    price_to,
                )
                await page.goto(target_url, wait_until="domcontentloaded")
                try:
                    await page.wait_for_load_state("networkidle")
                except Exception:
                    logger.debug("OLX: wait_for_load_state(networkidle) failed during last-page detection")

                try:
                    await page.wait_for_selector(
                        '[data-testid="pagination-list"], a[href*="/d/obyavlenie/"], div[data-testid="listing-grid"]',
                        timeout=5000,
                    )
                except Exception:
                    logger.debug("OLX: pagination/listing did not appear quickly during last-page detection")

                no_result = await page.evaluate(
                    """
                    () => {
                      const text = (document.body?.innerText || '').toLowerCase();
                      return text.includes('объявлений не найдено') ||
                             text.includes('нічого не знайдено') ||
                             text.includes('no results') ||
                             text.includes('ничего не найдено');
                    }
                    """
                )
                if no_result:
                    return None

                last_page = await page.eval_on_selector_all(
                    '[data-testid="pagination-list"] a, [data-testid="pagination-list"] button',
                    """
                    (els) => {
                      const values = els
                        .map(el => (el.innerText || el.textContent || '').trim())
                        .map(text => Number(text))
                        .filter(value => Number.isFinite(value) && value > 0);
                      return values.length ? Math.max(...values) : 1;
                    }
                    """,
                )
                return min(int(last_page or 1), max_page)
            finally:
                if page is not None:
                    try:
                        await page.close()
                    except Exception:
                        logger.debug("OLX: page.close() failed during last-page detection")
                if context is not None:
                    try:
                        await context.close()
                    except Exception:
                        logger.debug("OLX: context.close() failed during last-page detection")
                if browser is not None:
                    try:
                        await browser.close()
                    except Exception:
                        logger.debug("OLX: browser.close() failed during last-page detection")
    except Exception:
        logger.exception(
            "detect_last_page_for_search failed (url=%s, price_from=%s, price_to=%s)",
            url,
            price_from,
            price_to,
        )
        return None


async def fetch_ads_from_search(
    *,
    url: str,
    ad_type: str,
    city: str,
    limit: int = 50,
    price_from: int | None = None,
    price_to: int | None = None,
    page_number: int = 1,
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

    target_url = _build_search_page_url(
        base_url=url,
        page_number=page_number,
        price_from=price_from,
        price_to=price_to,
    )

    try:
        async with Stealth().use_async(async_playwright()) as p:
            browser: Browser | None = None
            context = None
            page: Page | None = None
            try:
                browser = await p.chromium.launch(headless=headless)
                context = await browser.new_context()
                page = await context.new_page()

                logger.info(
                    "OLX navigate: %s (page=%s, price_from=%s, price_to=%s)",
                    target_url,
                    page_number,
                    price_from,
                    price_to,
                )
                await page.goto(target_url, wait_until="domcontentloaded")
                # Strong waits: OLX is heavily JS-driven; we need deterministic rendering.
                try:
                    await page.wait_for_load_state("networkidle")
                except Exception:
                    logger.debug("OLX: wait_for_load_state(networkidle) failed; continuing anyway")

                try:
                    await page.wait_for_selector(
                        "a[href*=\"/d/obyavlenie/\"], div[data-testid=\"listing-grid\"]",
                        timeout=5000,
                    )
                except Exception:
                    await page.wait_for_timeout(1200)

                try:
                    page_title = await page.title()
                    logger.info("Page title: %s", page_title)
                    if page_title and ("Access Denied" in page_title or "Just a moment" in page_title):
                        logger.warning("OLX: possible block/captcha detected (title=%s)", page_title)
                except Exception:
                    logger.debug("OLX: page.title() failed")

                candidates = await _collect_candidate_ads(page)
                logger.info(
                    "OLX: candidates=%s for url=%s (page=%s, price_from=%s, price_to=%s)",
                    len(candidates),
                    target_url,
                    page_number,
                    price_from,
                    price_to,
                )

                if len(candidates) == 0:
                    screenshot_path = str(repo_root / "debug_screenshot.png")
                    try:
                        await page.screenshot(path=screenshot_path, full_page=True)
                        logger.warning("OLX: candidates==0, screenshot saved: %s", screenshot_path)
                    except Exception:
                        logger.warning("OLX: candidates==0, failed to save screenshot")

                count = 0
                for candidate in candidates:
                    if count >= limit:
                        break

                    href = candidate.get("href") or ""
                    title = candidate.get("title") or ""
                    price_text = candidate.get("priceText") or ""
                    text = candidate.get("text") or ""
                    rooms_text = candidate.get("roomsText")
                    area_text = candidate.get("areaText")
                    image_url = candidate.get("imageUrl")
                    params = candidate.get("params") or []

                    parsed = _extract_ads_from_dom_text(
                        ad_text=text,
                        href=href,
                        title_fallback=title or text,
                        ad_type=ad_type,
                        city=city,
                        base_url=base_url,
                        price_text=price_text or None,
                        image_url=image_url,
                        params=params,
                        rooms_text=rooms_text,
                        area_text=area_text,
                    )
                    if parsed is not None:
                        ads.append(parsed)
                        count += 1

                if not ads:
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
            finally:
                if page is not None:
                    try:
                        await page.close()
                    except Exception:
                        logger.debug("OLX: page.close() failed for search url=%s", url)
                if context is not None:
                    try:
                        await context.close()
                    except Exception:
                        logger.debug("OLX: context.close() failed for search url=%s", url)
                if browser is not None:
                    try:
                        await browser.close()
                    except Exception:
                        logger.debug("OLX: browser.close() failed for search url=%s", url)

    except Exception:
        logger.exception(
            "fetch_ads_from_search failed (url=%s, page=%s, price_from=%s, price_to=%s)",
            target_url,
            page_number,
            price_from,
            price_to,
        )

    return ads


async def fetch_ad_details(url: str, *, ad_type: str | None = None) -> dict[str, str | int | None]:
    headless_env = os.getenv("OLX_HEADLESS", "true").strip().lower()
    headless = headless_env in {"1", "true", "yes", "y", "on"}

    details: dict[str, str | int | None] = {
        "rooms": None,
        "floor": None,
        "total_floors": None,
        "area": None,
        "description": None,
        "author_name": None,
        "owner_type": None,
        "created_at_text": None,
        "seller_phone": None,
        "published_at": None,
        "district_slug": None,
        "district_label": None,
        "image_url": None,
        "image_urls": [],
    }

    try:
        async with Stealth().use_async(async_playwright()) as p:
            browser: Browser | None = None
            context = None
            page: Page | None = None
            try:
                browser = await p.chromium.launch(headless=headless)
                context = await browser.new_context()
                page = await context.new_page()
                page.set_default_timeout(8000)

                await page.goto(url, wait_until="domcontentloaded", timeout=8000)
                try:
                    await page.wait_for_load_state("domcontentloaded")
                except Exception:
                    logger.debug("OLX details: wait_for_load_state(domcontentloaded) failed for %s", url)

                try:
                    await page.wait_for_selector('[data-testid="ad-photos-container"]', timeout=5000)
                except Exception:
                    logger.debug("OLX details: ad-photos-container not found quickly for %s", url)

                parameter_map = await _extract_parameters_from_dom(page)
                params_text = "\n".join(f"{key}: {value}" for key, value in parameter_map.items() if value)
                page_text: str | None = None

                async def _collect_image_urls() -> tuple[str | None, list[str]]:
                    found_image_url = None
                    found_image_urls: list[str] = []
                    for selector in (
                        'img[data-testid="swiper-image"]',
                        '[data-testid="ad-photo"] img',
                        '[data-testid="image-gallery-container"] img',
                        '.swiper-slide-active img',
                        '.swiper img',
                    ):
                        try:
                            image_candidates = await page.eval_on_selector_all(
                                selector,
                                """
                                (els) => els.map((el) => ({
                                  src: el.getAttribute('src'),
                                  currentSrc: el.currentSrc || null,
                                  srcset: el.getAttribute('srcset'),
                                  dataSrc: el.getAttribute('data-src'),
                                }))
                                """,
                            )
                        except Exception:
                            continue
                        for item in image_candidates:
                            candidate_url = _pick_best_image_url(
                                item.get("currentSrc"),
                                item.get("src"),
                                item.get("dataSrc"),
                                item.get("srcset"),
                            )
                            if candidate_url and candidate_url not in found_image_urls:
                                found_image_urls.append(candidate_url)
                        if found_image_urls:
                            found_image_url = found_image_urls[0]
                    return found_image_url, found_image_urls

                await _auto_scroll(page)
                image_url, image_urls = await _collect_image_urls()
                if not image_urls:
                    await page.wait_for_timeout(1500)
                    await _auto_scroll(page, steps=2, delay_ms=300)
                    image_url, image_urls = await _collect_image_urls()

                parsed_rooms = _extract_rooms_value(
                    params=parameter_map,
                    params_text=params_text,
                    required=("rooms" in REQUIRED_FIELDS and ad_type not in {"commercial_sale", "commercial_rent"}),
                    url=url,
                )
                area = _extract_area_value(
                    params=parameter_map,
                    params_text=params_text,
                    required="area" in REQUIRED_FIELDS,
                    url=url,
                )
                floor, total_floors = _extract_floor_values(
                    params=parameter_map,
                    params_text=params_text,
                    required=(
                        ("floor" in REQUIRED_FIELDS or "total_floors" in REQUIRED_FIELDS)
                        and ad_type not in {"commercial_sale", "commercial_rent"}
                    ),
                    url=url,
                )

                description = None
                for selector in (
                    '[data-cy="ad_description"]',
                    '[data-testid="ad-description"]',
                    'div[data-testid="description-content"]',
                    'section div',
                ):
                    try:
                        candidate = await page.locator(selector).first.inner_text(timeout=1500)
                    except Exception:
                        continue
                    candidate = "\n".join(
                        line
                        for line in (_normalize_space(part) for part in candidate.splitlines())
                        if line
                    )
                    if candidate and len(candidate) > 20:
                        description = candidate
                        break

                author_name = None
                for selector in (
                    '[data-testid="user-profile-name"]',
                    '[data-cy="seller_card"] h4',
                    '[data-testid="aside"] h4',
                    'aside h4',
                ):
                    try:
                        candidate = await page.locator(selector).first.inner_text(timeout=1500)
                    except Exception:
                        continue
                    candidate = _normalize_space(candidate)
                    if candidate:
                        author_name = candidate
                        break

                owner_type = None
                owner_keys = set(parameter_map)
                if "\u0447\u0430\u0441\u0442\u043d\u043e\u0435 \u043b\u0438\u0446\u043e" in owner_keys:
                    owner_type = "\u0427\u0430\u0441\u0442\u043d\u043e\u0435 \u043b\u0438\u0446\u043e"
                elif "\u0431\u0438\u0437\u043d\u0435\u0441" in owner_keys:
                    owner_type = "\u0411\u0438\u0437\u043d\u0435\u0441"
                else:
                    page_text = page_text or await page.locator("body").inner_text()
                    for pattern in (
                        r"\b(\u0447\u0430\u0441\u0442\u043d\u043e\u0435 \u043b\u0438\u0446\u043e)\b",
                        r"\b(\u0431\u0438\u0437\u043d\u0435\u0441)\b",
                        r"\b(\u0447\u0430\u0441\u0442\u043d\u043e\u0435)\b",
                    ):
                        try:
                            owner_match = re.search(pattern, page_text or "", re.IGNORECASE)
                        except re.error:
                            logger.warning("Invalid regex pattern: %s", pattern)
                            continue
                        if owner_match:
                            owner_type = _normalize_space(owner_match.group(1))
                            break

                seller_phone = None

                page_text = page_text or await page.locator("body").inner_text()
                created_at_text = None
                published_at = None
                created_match = re.search(
                    r"(?:\u041e\u043f\u0443\u0431\u043b\u0438\u043a\u043e\u0432\u0430\u043d\u043e|\u0420\u0430\u0437\u043c\u0435\u0449\u0435\u043d\u043e|\u0421\u043e\u0437\u0434\u0430\u043d\u043e)\s*[:\-]?\s*([^\n]+)",
                    page_text or "",
                    re.IGNORECASE,
                )
                if created_match:
                    created_at_text = _normalize_space(created_match.group(1))
                    _, published_at = _extract_created_at_from_text(created_at_text)
                else:
                    created_at_text, published_at = _extract_created_at_from_text(page_text)

                district_label = _parse_district_label(f"{params_text}\n{page_text}")
                district_slug = _map_tashkent_district_label_to_slug(district_label) or _detect_tashkent_district_slug(
                    f"{params_text}\n{page_text}\n{description or ''}"
                )
                details.update(
                    {
                        "rooms": parsed_rooms,
                        "floor": floor,
                        "total_floors": total_floors,
                        "area": area,
                        "description": description,
                        "author_name": author_name,
                        "owner_type": owner_type,
                        "created_at_text": created_at_text,
                        "seller_phone": seller_phone,
                        "published_at": published_at.isoformat() if published_at else None,
                        "district_slug": district_slug,
                        "district_label": district_label,
                        "image_url": image_url,
                        "image_urls": image_urls,
                    }
                )
            finally:
                if page is not None:
                    try:
                        await page.close()
                    except Exception:
                        logger.debug("OLX: page.close() failed for details url=%s", url)
                if context is not None:
                    try:
                        await context.close()
                    except Exception:
                        logger.debug("OLX: context.close() failed for details url=%s", url)
                if browser is not None:
                    try:
                        await browser.close()
                    except Exception:
                        logger.debug("OLX: browser.close() failed for details url=%s", url)
    except Exception:
        logger.exception("fetch_ad_details failed (url=%s)", url)

    return details


async def enrich_ad_with_details(ad: ParsedAd) -> ParsedAd:
    try:
        details = await fetch_ad_details(ad.link, ad_type=ad.ad_type)
    except TypeError:
        details = await fetch_ad_details(ad.link)
    district_slug = details.get("district_slug")
    district_label = details.get("district_label")
    rooms = details.get("rooms")
    area = details.get("area")
    floor = details.get("floor")
    total_floors = details.get("total_floors")
    published_at_raw = details.get("published_at")
    published_at = None
    if isinstance(published_at_raw, str):
        try:
            published_at = datetime.fromisoformat(published_at_raw)
        except ValueError:
            published_at = None

    for field, listing_value, detail_value in (
        ("rooms", ad.rooms, rooms),
        ("area", ad.area, area),
        ("floor", ad.floor, floor),
        ("total_floors", ad.total_floors, total_floors),
    ):
        if listing_value is not None and detail_value is not None and listing_value != detail_value:
            logger.debug(
                "OLX conflict: field=%s listing=%s detail=%s url=%s",
                field,
                listing_value,
                detail_value,
                ad.link,
            )

    return replace(
        ad,
        city=district_slug if isinstance(district_slug, str) and district_slug else ad.city,
        district=district_label if isinstance(district_label, str) and district_label else ad.district,
        rooms=rooms if isinstance(rooms, int) else ad.rooms,
        area=area if isinstance(area, (int, float)) else ad.area,
        floor=floor if isinstance(floor, int) else ad.floor,
        total_floors=total_floors if isinstance(total_floors, int) else ad.total_floors,
        description=details.get("description") or ad.description,
        author_name=details.get("author_name") or ad.author_name,
        owner_type=details.get("owner_type") or ad.owner_type,
        created_at_text=details.get("created_at_text") or ad.created_at_text,
        seller_phone=details.get("seller_phone") or ad.seller_phone,
        published_at=published_at or ad.published_at,
        image_url=details.get("image_url") if isinstance(details.get("image_url"), str) and details.get("image_url") else ad.image_url,
        image_urls=list(details.get("image_urls") or ad.image_urls or ([ad.image_url] if ad.image_url else [])),
        details_loaded=True,
    )


def build_search_url(*, ad_type: str, city_slug: str) -> str:
    if ad_type == "sale":
        path = f"/nedvizhimost/kvartiry/prodazha/{city_slug}/"
    else:
        path = f"/nedvizhimost/kvartiry/arenda-dolgosrochnaya/{city_slug}/"
    return urljoin(settings.OLX_BASE_URL, path)


# Compatibility alias (requested by QA tests)
async def fetch_ads(*, url: str, ad_type: str, city: str, limit: int = 50) -> list[ParsedAd]:
    return await fetch_ads_from_search(url=url, ad_type=ad_type, city=city, limit=limit)
