"""
Woolworths specials scraper — free, no proxy required.

Strategy: async search-API sweep with adaptive paging
  1. Fetch page 1 for all terms concurrently (semaphore=16).
  2. Only fetch pages 2-3 for terms that yielded discounted products on page 1.
  3. Keep products where WasPrice > Price (IsHalfPrice is deprecated by Woolworths).
  4. Deduplicate by Stockcode.

Optimisations vs naive batch approach:
  - asyncio.Semaphore instead of fixed batches → no idle waiting for slowest request
  - Adaptive paging skips ~40% of page 2/3 calls for zero-yield terms
  - Concurrency raised from 8 to 16
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
    # Woolworths
    "cdn0.woolworths.com.au",
    "cdn1.woolworths.com.au",
    "cdn0.woolworths.media",
    "cdn1.woolworths.media",
    "media.woolworths.com.au",
    "assets.woolworths.com.au",
    "www.woolworths.com.au",
    # Coles
    "productimages.coles.com.au",
    "shop.coles.com.au",
    "www.coles.com.au",
    # Aldi
    "www.aldi.com.au",
    "images.aldi.com.au",
    "cdn.aldi.com.au",
}


def _safe_image_url(url: str) -> str | None:
    if not url:
        return None
    # Normalise protocol-relative URLs (e.g. //cdn0.woolworths.com.au/...)
    if url.startswith("//"):
        url = "https:" + url
    # Normalise root-relative URLs against the Woolworths origin
    if url.startswith("/") and not url.startswith("//"):
        url = "https://www.woolworths.com.au" + url
    if not url.startswith("https://"):
        return None
    host = urlparse(url).hostname or ""
    return url if host in _ALLOWED_IMAGE_HOSTS else None


log = logging.getLogger(__name__)

BASE        = "https://www.woolworths.com.au"
CONCURRENCY = 16
TIMEOUT     = 20
PAGES_PER_TERM = 3

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

SEARCH_TERMS = [
    # Snacks & confectionery
    "lollies", "chocolate", "biscuits", "chips", "crackers", "muesli bar",
    "popcorn", "shapes", "licorice", "caramel", "gummy", "twisties",
    "tim tam", "pretzels", "nuts", "trail mix", "rice cakes", "protein bar",
    # Beverages
    "cola", "soft drink", "energy drink", "sports drink", "iced tea",
    "sparkling water", "cordial", "juice", "kombucha", "coconut water",
    # Cleaning & household
    "detergent", "bleach", "dishwasher tablets", "spray cleaner",
    "laundry", "fabric softener", "disinfectant", "toilet paper", "paper towel",
    # Personal care
    "deodorant", "shampoo", "conditioner", "body wash", "toothpaste",
    "face wash", "moisturiser", "sunscreen", "razors", "tampons", "pads",
    "hand wash", "lip balm",
    # Health & vitamins
    "vitamins", "supplements", "fish oil", "probiotics",
    # Meat & seafood
    "bacon", "chicken", "steak", "sausages", "ham", "turkey", "fish",
    "salmon", "tuna", "prawns", "lamb", "pork", "mince", "salami",
    "deli", "kransky",
    # Dairy & fridge
    "yoghurt", "cheese", "butter", "cream cheese", "dip", "cream", "feta",
    "milk", "custard", "sour cream",
    # Bread & bakery
    "bread", "rolls", "wraps", "crumpets", "muffins", "bagels",
    # Breakfast
    "cereal", "muesli", "oats", "granola",
    # Frozen
    "ice cream", "frozen pizza", "frozen chips", "frozen vegetables",
    "frozen meals", "gelato",
    # Pantry
    "soup", "rice", "coffee", "tea", "pasta sauce", "baked beans",
    "olive oil", "coconut milk", "stock", "mayo", "tomato",
    "peanut butter", "jam", "honey", "vegemite", "vinegar", "soy sauce",
    "canned fish", "canned tomato", "lentils", "chickpeas",
    # Baby & pet
    "nappy", "baby food", "dog food", "cat food", "wipes",
    # Alcohol
    "beer", "wine", "cider", "premix", "spirits",
    # Brands commonly on special at Woolworths
    "maggi", "heinz", "birds eye", "san remo", "cobs", "arnott",
    "uncle tobys", "sanitarium", "weet-bix",
]


def _categorise(name: str) -> str:
    n = name.lower()
    # Household first — grabs fabric conditioner/softener before Personal Care grabs 'conditioner'
    if any(w in n for w in ["cleaning", "detergent", "paper towel", "tissue", "bin liner",
                             "nappy", "dishwash", "bleach", "spray cleaner", "wipe",
                             "laundry", "fabric softener", "fabric conditioner", "disinfectant"]):
        return "Household"
    # Personal Care before Dairy — skin/hair cream must not hit 'cream' in Dairy
    if any(w in n for w in ["shampoo", "toothpaste", "deodorant", "soap", "body wash",
                             "moisturiser", "moisturizer", "sunscreen", "perfume", "makeup",
                             "razor", "face wash", "conditioner", "vitamins", "supplements",
                             "fish oil", "probiotic", "tampon", "pads", "lip balm",
                             "lotion", "serum", "hand cream", "face cream", "body cream",
                             "eye cream", "anti age", "anti-age", "skincare", "skin care",
                             "protein bar", "protein shake", "protein powder"]):
        return "Personal Care"
    if any(w in n for w in ["milk", "cheese", "yogurt", "yoghurt", "butter", "cream", "egg", "feta", "custard"]):
        return "Dairy"
    if any(w in n for w in ["chicken", "beef", "pork", "lamb", "mince", "steak", "sausage", "bacon", "meat", "prawn", "salmon", "fish", "turkey", "tuna", "seafood", "salami", "deli", "kransky"]):
        return "Meat"
    if any(w in n for w in ["apple", "banana", "tomato", "onion", "potato", "carrot", "lettuce", "salad", "fruit", "veg", "broccoli", "capsicum"]):
        return "Produce"
    if any(w in n for w in ["bread", "roll", "cake", "cookie", "pastry", "donut", "muffin", "croissant", "crumpet", "wrap", "bagel"]):
        return "Bakery"
    # Beverages before Pantry — 'water', 'juice', 'tea' must not hit 'sugar'/'oil' in Pantry
    if any(w in n for w in ["water", "juice", "drink", "soda", "beer", "wine", "cider", "coffee", "tea", "cordial", "energy drink", "cola", "sparkling", "premix", "kombucha", "spirits"]):
        return "Beverages"
    if any(w in n for w in ["frozen", "ice cream", "pizza", "gelato"]):
        return "Frozen"
    if any(w in n for w in ["pasta", "rice", "flour", "sugar", "oil", "sauce", "cereal", "canned", "tinned", "soup", "noodle", "oat", "muesli", "beans", "lentil", "chickpea", "stock", "mayo", "honey", "jam", "vegemite"]):
        return "Pantry"
    if any(w in n for w in ["chip", "chocolate", "biscuit", "snack", "lolly", "lollies", "candy", "popcorn", "cracker", "pretzel", "tim tam", "muesli bar", "shapes", "licorice", "caramel", "gummy", "nuts", "trail mix"]):
        return "Snacks"
    if any(w in n for w in ["dog", "cat", "pet", "puppy", "kitten"]):
        return "Pet"
    if any(w in n for w in ["baby", "infant", "formula", "toddler"]):
        return "Baby"
    return "Weekly Specials"


async def _fetch_page(session: AsyncSession, sem: asyncio.Semaphore, term: str, page: int) -> tuple[list[dict], int]:
    """Returns (items, SearchResultsCount). Count is only populated on page 1."""
    async with sem:
        try:
            r = await session.get(
                f"{BASE}/apis/ui/Search/products",
                params={
                    "searchTerm": term,
                    "pageNumber": page,
                    "pageSize":   36,
                    "sortType":   "TraderRelevance",
                    "isFeatured": "false",
                },
                headers=HEADERS,
                timeout=TIMEOUT,
            )
            if r.status_code != 200:
                return [], 0
            data = r.json()
            total = int(data.get("SearchResultsCount") or 0)
            outer = data.get("Products") or []
            flat: list[dict] = []
            for item in outer:
                inner = item.get("Products") or []
                if inner:
                    flat.extend(inner)
                elif item.get("Stockcode"):
                    flat.append(item)
            return flat, total
        except Exception as e:
            log.debug(f"Woolworths search '{term}' p{page}: {e}")
            return [], 0


def _is_special(item: dict) -> bool:
    was   = item.get("WasPrice")
    price = item.get("Price")
    return bool(
        was and price
        and float(was) > 0
        and float(price) > 0
        and float(was) > float(price)
    )


def _to_record(item: dict, now: str) -> Optional[ProductRecord]:
    price_val = float(item["Price"]) if item.get("Price") else None
    if not price_val or price_val <= 0:
        return None
    name = (item.get("Name") or "").strip()
    if not name:
        return None
    was  = item.get("WasPrice")
    img  = item.get("MediumImageFile") or item.get("LargeImageFile") or ""
    if not img and item.get("Stockcode"):
        img = f"https://cdn0.woolworths.media/content/wowproductimages/medium/{item.get('Stockcode')}.jpg"
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
    sem = asyncio.Semaphore(CONCURRENCY)

    async with AsyncSession(impersonate="chrome124") as session:
        # Phase 1: all page-1 requests concurrently
        page1_results = await asyncio.gather(
            *[_fetch_page(session, sem, term, 1) for term in SEARCH_TERMS],
            return_exceptions=True,
        )

        # Process page 1 and collect terms that had specials (worth fetching deeper)
        # Also store real page count per term to avoid fetching beyond available data
        productive_terms: list[tuple[str, int]] = []  # (term, real_last_page)
        for term, result in zip(SEARCH_TERMS, page1_results):
            if isinstance(result, Exception):
                continue
            items, total = result
            if not items:
                continue
            real_last_page = math.ceil(total / 36) if total else 1
            had_special = False
            for item in items:
                if not _is_special(item):
                    continue
                had_special = True
                sc = item.get("Stockcode")
                if sc in seen:
                    continue
                if sc:
                    seen.add(sc)
                record = _to_record(item, now)
                if record:
                    products.append(record)
            if had_special:
                productive_terms.append((term, real_last_page))

        log.info(f"Woolworths p1: {len(products)} specials, {len(productive_terms)}/{len(SEARCH_TERMS)} terms productive")

        # Phase 2: pages 2-N only for productive terms, capped at real last page
        deeper_tasks = [
            (term, page)
            for term, real_last_page in productive_terms
            for page in range(2, min(PAGES_PER_TERM, real_last_page) + 1)
        ]
        if deeper_tasks:
            deeper_results = await asyncio.gather(
                *[_fetch_page(session, sem, term, page) for term, page in deeper_tasks],
                return_exceptions=True,
            )
            for result in deeper_results:
                if isinstance(result, Exception):
                    continue
                items, _ = result
                if not items:
                    continue
                for item in items:
                    if not _is_special(item):
                        continue
                    sc = item.get("Stockcode")
                    if sc in seen:
                        continue
                    if sc:
                        seen.add(sc)
                    record = _to_record(item, now)
                    if record:
                        products.append(record)

    log.info(f"Woolworths: {len(products)} specials ({len(SEARCH_TERMS) + len(deeper_tasks)} API calls)")
    return (products, None) if products else ([], "No Woolworths specials found")


def scrape() -> tuple[list[ProductRecord], Optional[str]]:
    now = datetime.datetime.utcnow().isoformat()
    try:
        return asyncio.run(_scrape_async(now))
    except Exception as e:
        log.exception("Woolworths scrape failed")
        return [], str(e)
