"""
Aldi specials scraper.

Strategy:
  Aldi Australia's grocery page is server-rendered HTML with minimal bot protection.
  Each product tile has class "product-tile" and contains:
    - .product-tile__brandname p  → brand (e.g. "HARIBO")
    - .product-tile__name p       → product name (e.g. "Mega Roulette 45g")
    - .base-price__regular span   → current price (e.g. "$0.99")
    - .product-tile__unit-of-measurement p → unit (e.g. "45 g")
    - img[src]                    → product image

  We combine brand + name for the full product name.
"""

import re
import time
import requests
from bs4 import BeautifulSoup, Tag
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

SPECIALS_URLS = [
    f"{BASE}/en/groceries/",
    f"{BASE}/specials/",
    f"{BASE}/en/specials/",
]


def _extract_price(text: str) -> Optional[float]:
    match = re.search(r"\$?\s*(\d+\.\d{2})", text.replace(",", ""))
    if match:
        return float(match.group(1))
    match = re.search(r"\$\s*(\d+)\b", text)
    if match:
        return float(match.group(1))
    return None


def _categorise(name: str) -> str:
    n = name.lower()
    if any(w in n for w in ["milk", "cheese", "yogurt", "butter", "cream", "egg"]):
        return "Dairy"
    if any(w in n for w in ["chicken", "beef", "pork", "lamb", "mince", "steak", "sausage", "bacon"]):
        return "Meat"
    if any(w in n for w in ["apple", "banana", "tomato", "onion", "potato", "carrot", "lettuce", "salad"]):
        return "Produce"
    if any(w in n for w in ["bread", "roll", "cake", "cookie", "pastry", "donut"]):
        return "Bakery"
    if any(w in n for w in ["pasta", "rice", "flour", "sugar", "oil", "sauce", "cereal", "canned"]):
        return "Pantry"
    if any(w in n for w in ["frozen", "ice cream", "pizza"]):
        return "Frozen"
    if any(w in n for w in ["water", "juice", "drink", "soda", "coffee", "tea"]):
        return "Beverages"
    if any(w in n for w in ["chip", "chocolate", "biscuit", "snack", "lolly"]):
        return "Snacks"
    if any(w in n for w in ["shampoo", "toothpaste", "deodorant", "soap", "body wash"]):
        return "Personal Care"
    if any(w in n for w in ["cleaning", "detergent", "paper", "tissue", "bin", "nappy"]):
        return "Household"
    return "Weekly Specials"


def _parse_tiles(html: str, now: str) -> list[ProductRecord]:
    soup = BeautifulSoup(html, "lxml")
    products: list[ProductRecord] = []
    seen: set[str] = set()

    tiles = soup.select(".product-tile")
    if not tiles:
        return []

    for tile in tiles:
        # Brand + product name
        brand_el = tile.select_one(".product-tile__brandname p")
        name_el = tile.select_one(".product-tile__name p")
        brand = brand_el.get_text(strip=True) if brand_el else ""
        prod_name = name_el.get_text(strip=True) if name_el else ""

        # Fall back to the tile's title attribute
        if not prod_name:
            prod_name = tile.get("title", "").strip()

        full_name = f"{brand} {prod_name}".strip() if brand else prod_name
        if not full_name or len(full_name) < 3:
            continue
        if full_name in seen:
            continue
        seen.add(full_name)

        # Price — use .base-price__regular, not comparison price
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
            category=_categorise(full_name),
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

    for url in SPECIALS_URLS:
        try:
            resp = session.get(url, timeout=30)
            if not resp.ok:
                last_error = f"HTTP {resp.status_code} from {url}"
                time.sleep(1)
                continue

            products = _parse_tiles(resp.text, now)
            if products:
                return products, None

            last_error = f"No product tiles found at {url}"
        except Exception as e:
            last_error = f"Request error at {url}: {e}"

        time.sleep(1)

    return [], last_error or "No Aldi products found"
