"""
Woolworths specials scraper — free, no proxy required.

Strategy: browse the Woolworths specials category directly
  1. Fetch page 1 of /apis/ui/browse/category?categoryId=specialsland
     to get TotalRecordCount and the first batch of products.
  2. Calculate remaining pages and fetch them all in parallel batches
     of CONCURRENCY (8 concurrent requests).
  3. Keep products where IsHalfPrice=True OR WasPrice > Price.
  4. Deduplicate by Stockcode.

This replaces the old keyword-search approach which only captured ~400
products. The browse approach returns ALL current specials (~1000+).
"""

import asyncio
import datetime
import logging
import math
from typing import Optional
from urllib.parse import urlparse

from curl_cffi.requests import AsyncSession

from database import ProductRecord

_ALLOWED_IMAGE_HOSTS = {
    "cdn0.woolworths.com.au",
    "www.woolworths.com.au",
    "productimages.coles.com.au",
    "www.coles.com.au",
    "www.aldi.com.au",
}


def _safe_image_url(url: str) -> str | None:
    if not url or not url.startswith("https://"):
        return None
    host = urlparse(url).hostname or ""
    return url if host in _ALLOWED_IMAGE_HOSTS else None


log = logging.getLogger(__name__)

BASE        = "https://www.woolworths.com.au"
CONCURRENCY = 8
TIMEOUT     = 20
PAGE_SIZE   = 36
MAX_PAGES   = 60   # 60 × 36 = up to 2,160 specials

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-AU,en;q=0.9",
    "Referer": f"{BASE}/shop/specials/half-price",
    "Origin": BASE,
}


def _categorise(name: str) -> str:
    n = name.lower()
    if any(w in n for w in ["milk", "cheese", "yogurt", "yoghurt", "butter", "cream", "egg", "feta"]):
        return "Dairy"
    if any(w in n for w in ["chicken", "beef", "pork", "lamb", "mince", "steak", "sausage", "bacon", "meat", "prawn", "salmon", "fish", "turkey", "tuna", "seafood"]):
        return "Meat"
    if any(w in n for w in ["apple", "banana", "tomato", "onion", "potato", "carrot", "lettuce", "salad", "fruit", "veg", "broccoli", "capsicum"]):
        return "Produce"
    if any(w in n for w in ["bread", "roll", "cake", "cookie", "pastry", "donut", "muffin", "croissant", "crumpet", "wrap"]):
        return "Bakery"
    if any(w in n for w in ["pasta", "rice", "flour", "sugar", "oil", "sauce", "cereal", "canned", "tinned", "soup", "noodle", "oat", "muesli", "beans"]):
        return "Pantry"
    if any(w in n for w in ["frozen", "ice cream", "pizza"]):
        return "Frozen"
    if any(w in n for w in ["water", "juice", "drink", "soda", "beer", "wine", "cider", "coffee", "tea", "cordial", "energy", "cola", "sparkling", "premix"]):
        return "Beverages"
    if any(w in n for w in ["chip", "chocolate", "biscuit", "snack", "lolly", "lollies", "candy", "popcorn", "cracker", "pretzel", "tim tam", "muesli bar", "shapes", "licorice", "caramel", "gummy"]):
        return "Snacks"
    if any(w in n for w in ["shampoo", "toothpaste", "deodorant", "soap", "body wash", "moisturiser", "sunscreen", "perfume", "makeup", "razor", "face wash", "conditioner", "vitamin", "protein"]):
        return "Personal Care"
    if any(w in n for w in ["cleaning", "detergent", "paper", "tissue", "bin", "nappy", "dishwash", "bleach", "spray", "wipe"]):
        return "Household"
    if any(w in n for w in ["dog", "cat", "pet", "puppy", "kitten"]):
        return "Pet"
    if any(w in n for w in ["baby", "infant", "formula", "toddler"]):
        return "Baby"
    return "Weekly Specials"


def _flatten(data: dict) -> tuple[list[dict], int]:
    """Extract flat product list and total record count from an API response."""
    total = (
        data.get("TotalRecordCount")
        or data.get("SearchResultsCount")
        or 0
    )
    # Browse endpoint returns Bundles; search endpoint returns Products
    outer = data.get("Bundles") or data.get("Products") or []
    flat: list[dict] = []
    for item in outer:
        inner = item.get("Products") or []
        if inner:
            flat.extend(inner)
        elif item.get("Stockcode"):
            flat.append(item)
    return flat, int(total)


async def _fetch_page(session: AsyncSession, page: int) -> tuple[list[dict], int]:
    """Fetch one page from the Woolworths specials browse endpoint."""
    try:
        r = await session.get(
            f"{BASE}/apis/ui/browse/category",
            params={
                "categoryId":  "specialsland",
                "pageNumber":  page,
                "pageSize":    PAGE_SIZE,
                "sortType":    "TraderRelevance",
                "isFeatured":  "false",
            },
            headers=HEADERS,
            timeout=TIMEOUT,
        )
        if r.status_code != 200:
            log.debug(f"Woolworths browse p{page}: HTTP {r.status_code}")
            return [], 0
        return _flatten(r.json())
    except Exception as e:
        log.debug(f"Woolworths browse p{page}: {e}")
        return [], 0


def _parse_product(item: dict, now: str) -> Optional[ProductRecord]:
    is_half_price = item.get("IsHalfPrice", False)
    was   = item.get("WasPrice")
    price = item.get("Price")
    has_discount = (
        was and price
        and float(was) > 0
        and float(price) > 0
        and float(was) > float(price)
    )
    if not is_half_price and not has_discount:
        return None

    price_val = float(price) if price else None
    if not price_val or price_val <= 0:
        return None

    name = (item.get("Name") or "").strip()
    if not name:
        return None

    img  = item.get("MediumImageFile") or item.get("LargeImageFile") or ""
    unit = item.get("PackageSize") or item.get("CupMeasure") or "ea"

    return ProductRecord(
        name=name,
        category=_categorise(name),
        woolies_price=price_val,
        woolies_was_price=float(was) if was else None,
        unit=unit,
        image_url=_safe_image_url(img),
        last_updated=now,
    )


async def _scrape_async(now: str) -> tuple[list[ProductRecord], Optional[str]]:
    seen: set[int] = set()
    products: list[ProductRecord] = []

    async with AsyncSession(impersonate="chrome124") as session:
        # Page 1 first to get total count
        items, total = await _fetch_page(session, 1)
        if not items and total == 0:
            # Fall back: maybe the browse endpoint responded with empty data
            log.warning("Woolworths browse returned no data on page 1")
            return [], "No Woolworths specials found"

        total_pages = min(MAX_PAGES, math.ceil(total / PAGE_SIZE)) if total else MAX_PAGES
        log.info(f"Woolworths: {total} specials across {total_pages} pages")

        # Process page 1
        for item in items:
            stockcode = item.get("Stockcode")
            if stockcode in seen:
                continue
            if stockcode:
                seen.add(stockcode)
            record = _parse_product(item, now)
            if record:
                products.append(record)

        # Fetch remaining pages in parallel batches
        remaining_pages = list(range(2, total_pages + 1))
        for i in range(0, len(remaining_pages), CONCURRENCY):
            batch = remaining_pages[i : i + CONCURRENCY]
            results = await asyncio.gather(
                *[_fetch_page(session, p) for p in batch],
                return_exceptions=True,
            )
            for result in results:
                if isinstance(result, Exception):
                    continue
                page_items, _ = result
                for item in page_items:
                    stockcode = item.get("Stockcode")
                    if stockcode in seen:
                        continue
                    if stockcode:
                        seen.add(stockcode)
                    record = _parse_product(item, now)
                    if record:
                        products.append(record)

    log.info(f"Woolworths: {len(products)} specials collected")
    if products:
        return products, None
    return [], "No Woolworths specials found"


def scrape() -> tuple[list[ProductRecord], Optional[str]]:
    now = datetime.datetime.utcnow().isoformat()
    try:
        return asyncio.run(_scrape_async(now))
    except Exception as e:
        log.exception("Woolworths scrape failed")
        return [], str(e)
