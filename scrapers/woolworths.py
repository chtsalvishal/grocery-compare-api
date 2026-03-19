"""
Woolworths specials scraper.

Strategy: ScrapingBee with render_js=true opens a real Chrome browser that
solves Akamai's JavaScript challenge, waits for the Angular SPA to call the
browse API and render product tiles, then returns the fully rendered HTML.
We parse that HTML with BeautifulSoup.

Credit cost: 5 credits/request × 12 categories = 60 credits/sync.
ScrapingBee free tier = 1,000 credits/month ≈ 16 syncs/month (every 2 days).
"""

import os
import re
import time
import datetime
import logging
import requests
from bs4 import BeautifulSoup
from typing import Optional
from database import ProductRecord

log = logging.getLogger(__name__)

BASE = "https://www.woolworths.com.au"
SCRAPINGBEE_KEY = os.environ.get("SCRAPINGBEE_API_KEY", "")
SCRAPINGBEE_URL = "https://app.scrapingbee.com/api/v1/"

# Seconds to wait after page load for Angular + browse API to finish rendering.
RENDER_WAIT_MS = 8000

SPECIALS_CATEGORIES = [
    ("fruit-veg",            "Produce"),
    ("poultry-meat-seafood", "Meat"),
    ("dairy-eggs-fridge",    "Dairy"),
    ("bakery",               "Bakery"),
    ("snacks-confectionery", "Snacks"),
    ("drinks",               "Beverages"),
    ("pantry",               "Pantry"),
    ("freezer",              "Frozen"),
    ("beauty",               "Personal Care"),
    ("personal-care",        "Personal Care"),
    ("cleaning-maintenance", "Household"),
    ("baby",                 "Household"),
]


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


def _fetch_rendered(url: str) -> requests.Response:
    """Fetch a URL through ScrapingBee with full JavaScript rendering."""
    return requests.get(
        SCRAPINGBEE_URL,
        params={
            "api_key": SCRAPINGBEE_KEY,
            "url": url,
            "render_js": "true",
            "wait": str(RENDER_WAIT_MS),
            "block_resources": "false",
        },
        timeout=90,
    )


def _parse_tiles(html: str, slug: str, default_cat: str, now: str, seen: set) -> list[ProductRecord]:
    soup = BeautifulSoup(html, "lxml")
    products: list[ProductRecord] = []

    # --- Selector strategy: try most-specific first, fall back progressively ---

    # 1. Woolworths Angular renders product tiles as <wc-product-tile> or <shared-product-tile>
    tiles = soup.select("wc-product-tile, shared-product-tile")

    # 2. Angular component rendered as divs with product-tile in class
    if not tiles:
        tiles = soup.select("div[class*='product-tile'], article[class*='product-tile']")

    # 3. Any element carrying a stockcode/productid data attribute
    if not tiles:
        tiles = soup.select("[data-stockcode], [data-productid], [data-product-id]")

    if not tiles:
        # Log rendered HTML so we can identify the correct selector in one fix
        log.warning(f"Woolworths {slug}: no product tiles found (len={len(html)})")
        log.warning(f"Woolworths {slug} rendered snippet: {html[5000:8000]}")
        return []

    log.info(f"Woolworths {slug}: {len(tiles)} tiles found")

    for tile in tiles:
        # --- Product name ---
        # Try heading tags first, then any element with 'name' or 'title' in class
        name_el = (
            tile.select_one("h2, h3, h4")
            or tile.select_one("[class*='title'], [class*='name'], [class*='description']")
        )
        name = name_el.get_text(strip=True) if name_el else ""

        # Fall back to data attributes
        if not name:
            name = (
                tile.get("data-name")
                or tile.get("data-product-name")
                or tile.get("title")
                or ""
            ).strip()

        if not name or len(name) < 3:
            continue

        # --- Price ---
        # Woolworths renders current price in a "price" element
        price_el = (
            tile.select_one("[class*='price--now'], [class*='price-now']")
            or tile.select_one("[class*='primary'] [class*='price']")
            or tile.select_one("[class*='price']")
        )
        price_text = price_el.get_text(strip=True) if price_el else ""
        price = _extract_price(price_text)
        if price is None or price <= 0:
            continue

        # --- Was-price ---
        was_el = (
            tile.select_one("[class*='price--was'], [class*='was-price'], [class*='price-was']")
            or tile.select_one("[class*='was']")
        )
        was_text = was_el.get_text(strip=True) if was_el else ""
        was_price = _extract_price(was_text)

        # --- Deduplicate ---
        stockcode = tile.get("data-stockcode") or tile.get("data-productid") or name
        if stockcode in seen:
            continue
        seen.add(stockcode)

        # --- Image ---
        img_el = tile.find("img")
        img_url = None
        if img_el:
            src = img_el.get("src") or img_el.get("data-src") or ""
            if src.startswith("http"):
                img_url = src

        # --- Unit ---
        unit_el = tile.select_one("[class*='cup'], [class*='unit'], [class*='measure']")
        unit = unit_el.get_text(strip=True) if unit_el else "ea"
        unit = re.sub(r"\s+", " ", unit).strip() or "ea"

        products.append(ProductRecord(
            name=name,
            category=_categorise(name, default_cat),
            woolies_price=price,
            woolies_was_price=was_price,
            unit=unit,
            image_url=img_url,
            last_updated=now,
        ))

    return products


def _extract_price(text: str) -> Optional[float]:
    m = re.search(r"\$?\s*(\d+\.\d{2})", text.replace(",", ""))
    if m:
        return float(m.group(1))
    m = re.search(r"\$\s*(\d+)\b", text)
    if m:
        return float(m.group(1))
    return None


def scrape() -> tuple[list[ProductRecord], Optional[str]]:
    if not SCRAPINGBEE_KEY:
        return [], "SCRAPINGBEE_API_KEY not set"

    now = datetime.datetime.utcnow().isoformat()
    seen: set[str] = set()
    products: list[ProductRecord] = []
    last_error: Optional[str] = None

    for slug, default_cat in SPECIALS_CATEGORIES:
        url = f"{BASE}/shop/specials/half-price/{slug}"
        try:
            r = _fetch_rendered(url)
            log.info(f"Woolworths {slug}: ScrapingBee HTTP {r.status_code} len={len(r.text)}")

            if not r.ok:
                last_error = f"ScrapingBee HTTP {r.status_code} for {slug}"
                time.sleep(1)
                continue

            page_products = _parse_tiles(r.text, slug, default_cat, now, seen)
            products.extend(page_products)

            if not page_products:
                last_error = f"No tiles parsed for {slug}"

        except Exception as e:
            last_error = f"Error on {slug}: {e}"
            log.warning(f"Woolworths {slug} exception: {e}")

        time.sleep(1)

    return (products, None) if products else ([], last_error or "No Woolworths products found")
