"""
Woolworths specials scraper.

Strategy: Use the browse/category API with IsSpecial=True to fetch products
on special from each major grocery category. Deduplicates by Stockcode.

The old approach searched by keyword and relied on IsOnSpecial=True in results,
but the search API rarely surfaces specials for generic terms. The browse API
with IsSpecial=True correctly filters to on-special products and supports
pagination via TotalRecordCount.
"""

import time
import datetime
import math
from typing import Optional

import requests

from database import ProductRecord

BASE = "https://www.woolworths.com.au"
IMG_BASE = "https://cdn0.woolworths.media/content/wowproductimages/large/"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-AU,en-US;q=0.9,en;q=0.8",
    "Origin": BASE,
    "Referer": f"{BASE}/shop/specials/half-price",
    "X-Requested-With": "XMLHttpRequest",
    "Content-Type": "application/json",
}

# Each tuple: (categoryId, url_path, display_name)
SPECIALS_CATEGORIES = [
    ("1-E5BEE36E", "/shop/specials/half-price/fruit-veg",             "Produce"),
    ("1_D5A2236",  "/shop/specials/half-price/poultry-meat-seafood",   "Meat"),
    ("1_6E4F4E4",  "/shop/specials/half-price/dairy-eggs-fridge",      "Dairy"),
    ("1_DEB537E",  "/shop/specials/half-price/bakery",                 "Bakery"),
    ("1_717445A",  "/shop/specials/half-price/snacks-confectionery",   "Snacks"),
    ("1_5AF3A0A",  "/shop/specials/half-price/drinks",                 "Beverages"),
    ("1_39FD49C",  "/shop/specials/half-price/pantry",                 "Pantry"),
    ("1_ACA2FC2",  "/shop/specials/half-price/freezer",                "Frozen"),
    ("1_8D61DD6",  "/shop/specials/half-price/beauty",                 "Personal Care"),
    ("1_894D0A8",  "/shop/specials/half-price/personal-care",          "Personal Care"),
    ("1_2432B58",  "/shop/specials/half-price/cleaning-maintenance",   "Household"),
    ("1_717A94B",  "/shop/specials/half-price/baby",                   "Household"),
]

PAGE_SIZE = 36
# Maximum pages to fetch per category (36*10 = 360 products max per cat)
MAX_PAGES = 10


def _categorise(name: str, default_cat: str) -> str:
    n = name.lower()
    if any(w in n for w in ["milk", "cheese", "yogurt", "butter", "cream", "egg"]):
        return "Dairy"
    if any(w in n for w in ["chicken", "beef", "pork", "lamb", "mince", "steak", "sausage", "bacon", "meat", "prawn", "salmon", "fish"]):
        return "Meat"
    if any(w in n for w in ["apple", "banana", "tomato", "onion", "potato", "carrot", "lettuce", "salad", "fruit", "veg", "broccoli", "capsicum"]):
        return "Produce"
    if any(w in n for w in ["bread", "roll", "cake", "cookie", "pastry", "donut", "muffin", "croissant"]):
        return "Bakery"
    if any(w in n for w in ["pasta", "rice", "flour", "sugar", "oil", "sauce", "cereal", "canned", "tinned", "soup", "noodle"]):
        return "Pantry"
    if any(w in n for w in ["frozen", "ice cream", "pizza"]):
        return "Frozen"
    if any(w in n for w in ["water", "juice", "drink", "soda", "beer", "wine", "coffee", "tea", "cordial", "energy"]):
        return "Beverages"
    if any(w in n for w in ["chip", "chocolate", "biscuit", "snack", "lolly", "candy", "popcorn", "cracker"]):
        return "Snacks"
    if any(w in n for w in ["shampoo", "toothpaste", "deodorant", "soap", "body wash", "moisturiser", "sunscreen", "perfume", "makeup"]):
        return "Personal Care"
    if any(w in n for w in ["cleaning", "detergent", "paper", "tissue", "bin", "nappy", "dishwash", "bleach"]):
        return "Household"
    return default_cat


def _fetch_category_page(
    session: requests.Session,
    cat_id: str,
    url_path: str,
    page: int,
) -> tuple[list[dict], int]:
    """Fetch one page of specials for a category. Returns (items, total_count)."""
    resp = session.post(
        f"{BASE}/apis/ui/browse/category",
        json={
            "CategoryId": cat_id,
            "PageNumber": page,
            "PageSize": PAGE_SIZE,
            "SortType": "TraderRelevance",
            "Url": url_path,
            "FormatObject": {},
            "IsSpecial": True,
        },
        timeout=30,
    )
    if not resp.ok:
        return [], 0

    data = resp.json()
    total = data.get("TotalRecordCount") or 0
    bundles = data.get("Bundles") or []
    items = [item for bundle in bundles for item in (bundle.get("Products") or [])]
    return items, total


def scrape(max_categories: int = len(SPECIALS_CATEGORIES)) -> tuple[list[ProductRecord], Optional[str]]:
    session = requests.Session()
    session.headers.update(HEADERS)

    # Warmup — establishes Akamai session cookies
    try:
        session.get(f"{BASE}/", timeout=30)
        time.sleep(1.5)
    except Exception as e:
        return [], f"Warmup failed: {e}"

    now = datetime.datetime.utcnow().isoformat()
    seen: set[int] = set()
    products: list[ProductRecord] = []
    last_error: Optional[str] = None

    for cat_id, url_path, default_cat in SPECIALS_CATEGORIES[:max_categories]:
        try:
            # Fetch first page to get total count
            items, total = _fetch_category_page(session, cat_id, url_path, 1)
            if not items and total == 0:
                time.sleep(0.5)
                continue

            all_items = list(items)

            # Fetch remaining pages
            total_pages = min(math.ceil(total / PAGE_SIZE), MAX_PAGES)
            for page in range(2, total_pages + 1):
                time.sleep(0.4)
                page_items, _ = _fetch_category_page(session, cat_id, url_path, page)
                if not page_items:
                    break
                all_items.extend(page_items)

            # Process items — keep those on special or with a reduced WasPrice
            for item in all_items:
                is_special = (
                    item.get("IsOnSpecial")
                    or item.get("IsHalfPrice")
                    or (
                        item.get("WasPrice")
                        and item.get("Price")
                        and float(item["WasPrice"]) > float(item["Price"])
                    )
                )
                if not is_special:
                    continue

                stockcode = item.get("Stockcode")
                if stockcode and stockcode in seen:
                    continue
                if stockcode:
                    seen.add(stockcode)

                price = item.get("Price")
                if not price or float(price) <= 0:
                    continue
                name = (item.get("Name") or "").strip()
                if not name:
                    continue

                was = item.get("WasPrice")
                img = item.get("MediumImageFile") or item.get("LargeImageFile") or ""
                unit = item.get("PackageSize") or item.get("CupMeasure") or "ea"

                products.append(ProductRecord(
                    name=name,
                    category=_categorise(name, default_cat),
                    woolies_price=float(price),
                    woolies_was_price=float(was) if was else None,
                    unit=unit,
                    image_url=img if img.startswith("http") else None,
                    last_updated=now,
                ))

        except Exception as e:
            last_error = f"Error on category '{url_path}': {e}"

        time.sleep(0.5)

    return products, last_error if not products else None
