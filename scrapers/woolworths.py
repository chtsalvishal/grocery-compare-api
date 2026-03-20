"""
Woolworths specials scraper — free, no proxy required.

Replaces the ScrapingBee/browser approach entirely.

Strategy: async search-API sweep
  1. Call GET /apis/ui/Search/products for 90 grocery terms, pages 1-2
     (8 concurrent requests, ~25 seconds total)
  2. Keep products where IsHalfPrice=True OR WasPrice > Price
  3. Deduplicate by Stockcode
  4. Map to ProductRecord — same interface as before

No SCRAPINGBEE_API_KEY, no browser, no proxy needed.
Proven via GitHub Actions tests: ~400 unique half-price products per run.
"""

import asyncio
import datetime
import logging
from typing import Optional

from curl_cffi.requests import AsyncSession

from database import ProductRecord

log = logging.getLogger(__name__)

BASE = "https://www.woolworths.com.au"
CONCURRENCY = 8   # concurrent requests — safe without triggering rate limits
TIMEOUT = 20

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

# 90 terms covering every major Woolworths half-price category.
# Proven to yield ~400 unique half-price products across pages 1+2.
SEARCH_TERMS = [
    # Snacks & confectionery — highest half-price yield
    "lollies", "chocolate", "biscuits", "chips", "crackers", "muesli bar",
    "popcorn", "shapes", "licorice", "caramel", "gummy", "twisties",
    "tim tam", "pretzels",
    # Beverages
    "cola", "soft drink", "energy drink", "sports drink", "iced tea",
    "sparkling water", "cordial", "juice",
    # Cleaning & household — very high yield
    "detergent", "bleach", "dishwasher tablets", "spray cleaner",
    # Personal care
    "deodorant", "shampoo", "conditioner", "body wash", "toothpaste",
    "face wash", "moisturiser", "sunscreen", "razors",
    # Health & vitamins
    "vitamins", "protein bar",
    # Meat & seafood
    "bacon", "chicken", "steak", "sausages", "ham", "turkey", "fish",
    "salmon", "tuna", "prawns", "lamb", "pork", "mince",
    # Dairy & fridge
    "yoghurt", "cheese", "butter", "cream cheese", "dip", "cream", "feta",
    # Bread & bakery
    "bread", "rolls", "wraps", "crumpets",
    # Breakfast
    "cereal", "muesli", "oats",
    # Frozen
    "ice cream", "frozen pizza", "frozen chips", "frozen vegetables",
    # Pantry
    "soup", "rice", "coffee", "tea", "pasta sauce", "baked beans",
    "olive oil", "coconut milk",
    # Baby & pet
    "nappy", "baby food", "dog food", "cat food",
    # Alcohol
    "beer", "wine", "cider", "premix",
]


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


async def _fetch_page(session: AsyncSession, term: str, page: int) -> list[dict]:
    """Fetch one page of search results and return the flat product list."""
    try:
        r = await session.get(
            f"{BASE}/apis/ui/Search/products",
            params={
                "searchTerm": term,
                "pageNumber": page,
                "pageSize": 36,
                "sortType": "TraderRelevance",
                "isFeatured": "false",
            },
            headers=HEADERS,
            timeout=TIMEOUT,
        )
        if r.status_code != 200:
            return []
        data = r.json()
        # Flatten the nested Products structure
        outer = data.get("Products") or []
        flat = []
        for item in outer:
            inner = item.get("Products") or []
            if inner:
                flat.extend(inner)
            elif item.get("Stockcode"):
                flat.append(item)
        return flat
    except Exception as e:
        log.debug(f"Woolworths search '{term}' p{page}: {e}")
        return []


async def _scrape_async(now: str) -> tuple[list[ProductRecord], Optional[str]]:
    seen: set[int] = set()
    products: list[ProductRecord] = []

    # Build all (term, page) tasks — page 1 and 2 for each term
    tasks = [
        (term, page)
        for term in SEARCH_TERMS
        for page in [1, 2]
    ]

    async with AsyncSession(impersonate="chrome124") as session:
        # Process in batches of CONCURRENCY
        for i in range(0, len(tasks), CONCURRENCY):
            batch = tasks[i : i + CONCURRENCY]
            results = await asyncio.gather(
                *[_fetch_page(session, term, page) for term, page in batch],
                return_exceptions=True,
            )
            for items in results:
                if isinstance(items, Exception) or not items:
                    continue
                for item in items:
                    # Only keep items that are on special
                    is_half_price = item.get("IsHalfPrice", False)
                    was = item.get("WasPrice")
                    price = item.get("Price")
                    has_discount = (
                        was and price
                        and float(was) > 0
                        and float(price) > 0
                        and float(was) > float(price)
                    )
                    if not is_half_price and not has_discount:
                        continue

                    stockcode = item.get("Stockcode")
                    if stockcode in seen:
                        continue
                    if stockcode:
                        seen.add(stockcode)

                    price_val = float(price) if price else None
                    if not price_val or price_val <= 0:
                        continue
                    name = (item.get("Name") or "").strip()
                    if not name:
                        continue

                    img = item.get("MediumImageFile") or item.get("LargeImageFile") or ""
                    unit = item.get("PackageSize") or item.get("CupMeasure") or "ea"

                    products.append(ProductRecord(
                        name=name,
                        category=_categorise(name),
                        woolies_price=price_val,
                        woolies_was_price=float(was) if was else None,
                        unit=unit,
                        image_url=img if img.startswith("http") else None,
                        last_updated=now,
                    ))

    log.info(f"Woolworths: {len(products)} specials collected ({len(SEARCH_TERMS) * 2} API calls)")
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
