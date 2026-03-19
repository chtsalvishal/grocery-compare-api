"""
Aldi specials scraper.

Strategy:
  Aldi Australia's grocery pages are server-rendered HTML with minimal bot protection.
  Each product tile has class "product-tile" and contains:
    - .product-tile__brandname p  → brand (e.g. "HARIBO")
    - .product-tile__name p       → product name (e.g. "Mega Roulette 45g")
    - .base-price__regular span   → current price (e.g. "$0.99")
    - .product-tile__unit-of-measurement p → unit (e.g. "45 g")
    - img[src]                    → product image

  Aldi serves its grocery section as a SPA; the old /en/groceries/* category URLs
  all redirect to the main page. The correct URLs use the pattern
  /products/{category}/k/{id}. We scrape each top-level grocery category to get
  ~30 unique products per category, totalling ~400 products.
"""

import re
import time
import requests
from bs4 import BeautifulSoup
from typing import Optional
from database import ProductRecord
import datetime

BASE = "https://www.aldi.com.au"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
    "Accept-Language": "en-AU,en;q=0.9",
    "Referer": f"{BASE}/",
}

# Top-level grocery category pages — each returns ~30 distinct product tiles.
# Paths confirmed from live site navigation as of 2026-03.
CATEGORY_PAGES = [
    ("/products/fruits-vegetables/k/950000000",           "Produce"),
    ("/products/meat-seafood/k/940000000",                "Meat"),
    ("/products/deli-chilled-meats/k/930000000",          "Meat"),
    ("/products/dairy-eggs-fridge/k/960000000",           "Dairy"),
    ("/products/bakery/k/920000000",                      "Bakery"),
    ("/products/pantry/k/970000000",                      "Pantry"),
    ("/products/freezer/k/980000000",                     "Frozen"),
    ("/products/drinks/k/1000000000",                     "Beverages"),
    ("/products/snacks-confectionery/k/1588161408332087", "Snacks"),
    ("/products/health-beauty/k/1040000000",              "Personal Care"),
    ("/products/cleaning-household/k/1050000000",         "Household"),
    ("/products/baby/k/1030000000",                       "Household"),
    ("/products/pets/k/1020000000",                       "Weekly Specials"),
    ("/products/lower-prices/k/1588161425841179",         "Weekly Specials"),
    ("/products/super-savers/k/1588161426952145",         "Weekly Specials"),
]


def _extract_price(text: str) -> Optional[float]:
    match = re.search(r"\$?\s*(\d+\.\d{2})", text.replace(",", ""))
    if match:
        return float(match.group(1))
    match = re.search(r"\$\s*(\d+)\b", text)
    if match:
        return float(match.group(1))
    return None


def _categorise(name: str, default_cat: str) -> str:
    n = name.lower()
    if any(w in n for w in ["milk", "cheese", "yogurt", "butter", "cream", "egg"]):
        return "Dairy"
    if any(w in n for w in ["chicken", "beef", "pork", "lamb", "mince", "steak", "sausage", "bacon", "prawn", "fish", "salmon"]):
        return "Meat"
    if any(w in n for w in ["apple", "banana", "tomato", "onion", "potato", "carrot", "lettuce", "salad", "broccoli"]):
        return "Produce"
    if any(w in n for w in ["bread", "roll", "cake", "cookie", "pastry", "donut", "muffin"]):
        return "Bakery"
    if any(w in n for w in ["pasta", "rice", "flour", "sugar", "oil", "sauce", "cereal", "canned", "tinned", "soup", "noodle"]):
        return "Pantry"
    if any(w in n for w in ["frozen", "ice cream", "pizza"]):
        return "Frozen"
    if any(w in n for w in ["water", "juice", "drink", "soda", "coffee", "tea", "cordial", "energy"]):
        return "Beverages"
    if any(w in n for w in ["chip", "chocolate", "biscuit", "snack", "lolly", "candy", "popcorn", "cracker"]):
        return "Snacks"
    if any(w in n for w in ["shampoo", "toothpaste", "deodorant", "soap", "body wash", "moisturiser", "sunscreen"]):
        return "Personal Care"
    if any(w in n for w in ["cleaning", "detergent", "paper", "tissue", "bin", "nappy", "dishwash", "bleach"]):
        return "Household"
    return default_cat


def _parse_tiles(html: str, now: str, default_cat: str, seen: set[str]) -> list[ProductRecord]:
    soup = BeautifulSoup(html, "lxml")
    products: list[ProductRecord] = []

    tiles = soup.select(".product-tile")
    if not tiles:
        return []

    for tile in tiles:
        # Brand + product name
        brand_el = tile.select_one(".product-tile__brandname p")
        name_el = tile.select_one(".product-tile__name p")
        brand = brand_el.get_text(strip=True) if brand_el else ""
        prod_name = name_el.get_text(strip=True) if name_el else ""

        if not prod_name:
            prod_name = tile.get("title", "").strip()

        full_name = f"{brand} {prod_name}".strip() if brand else prod_name
        if not full_name or len(full_name) < 3:
            continue
        if full_name in seen:
            continue
        seen.add(full_name)

        # Price
        price_el = tile.select_one(".base-price__regular span")
        if not price_el:
            continue
        price = _extract_price(price_el.get_text(strip=True))
        if price is None or price <= 0:
            continue

        # Unit
        unit_el = tile.select_one(".product-tile__unit-of-measurement p")
        unit = unit_el.get_text(strip=True) if unit_el else "ea"
        unit = re.sub(r"\s+", "", unit).lower() or "ea"

        # Image
        img = tile.find("img")
        img_url = None
        if img:
            src = img.get("src") or img.get("data-src") or ""
            if src and src.startswith("http"):
                img_url = src

        products.append(ProductRecord(
            name=full_name,
            category=_categorise(full_name, default_cat),
            aldi_price=price,
            unit=unit,
            image_url=img_url,
            last_updated=now,
        ))

    return products


def scrape() -> tuple[list[ProductRecord], Optional[str]]:
    session = requests.Session()
    session.headers.update(HEADERS)
    now = datetime.datetime.utcnow().isoformat()
    last_error = None
    all_products: list[ProductRecord] = []
    seen: set[str] = set()

    for path, default_cat in CATEGORY_PAGES:
        url = f"{BASE}{path}"
        try:
            resp = session.get(url, timeout=30)
            if not resp.ok:
                last_error = f"HTTP {resp.status_code} from {url}"
                time.sleep(1)
                continue

            page_products = _parse_tiles(resp.text, now, default_cat, seen)
            all_products.extend(page_products)

            if not page_products:
                last_error = f"No product tiles found at {url}"

        except Exception as e:
            last_error = f"Request error at {url}: {e}"

        time.sleep(0.8)

    if all_products:
        return all_products, None

    return [], last_error or "No Aldi products found"
