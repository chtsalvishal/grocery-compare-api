"""
Woolworths specials scraper.

Strategy: Use Next.js _next/data endpoints to bypass Akamai WAF, which
blocks the /apis/ui/browse/category POST from cloud/datacenter IPs with 403.

We first fetch the main specials page to extract the Next.js buildId from
the __NEXT_DATA__ script tag, then request:
  GET /_next/data/{buildId}/shop/specials/half-price/{category}.json

These are pre-rendered Next.js data payloads served from the CDN layer and
are not protected by the same Akamai rules as the browser API endpoints.
"""

import re
import os
import time
import json
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


def _get(url: str, timeout: int = 30) -> requests.Response:
    """Fetch a URL, routing through ScrapingBee if an API key is configured."""
    if SCRAPINGBEE_KEY:
        return requests.get(
            SCRAPINGBEE_URL,
            params={"api_key": SCRAPINGBEE_KEY, "url": url, "render_js": "false"},
            timeout=timeout,
        )
    return requests.get(url, timeout=timeout)

# URL slug → display category
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


def _get_build_id() -> Optional[str]:
    """Extract Next.js buildId from the Woolworths specials page."""
    try:
        r = _get(f"{BASE}/shop/specials/half-price")
        log.info(f"Woolworths specials page: HTTP {r.status_code} len={len(r.text)}")
        if not r.ok:
            return None

        html = r.text

        # Method 1: __NEXT_DATA__ script tag (standard Next.js SSR)
        tag = BeautifulSoup(html, "lxml").find("script", {"id": "__NEXT_DATA__"})
        if tag:
            build_id = json.loads(tag.string).get("buildId")
            log.info(f"Woolworths buildId (NEXT_DATA): {build_id}")
            return build_id

        # Method 2: buildId embedded in inline script as JSON string
        m = re.search(r'"buildId"\s*:\s*"([^"]+)"', html)
        if m:
            log.info(f"Woolworths buildId (inline script): {m.group(1)}")
            return m.group(1)

        # Method 3: infer from /_next/static/{buildId}/ path references
        m = re.search(r'/_next/static/([^/"]+)/', html)
        if m:
            log.info(f"Woolworths buildId (static path): {m.group(1)}")
            return m.group(1)

        # Log a snippet to diagnose what the page looks like
        log.warning(f"Woolworths: no buildId found. Page snippet: {html[:500]}")
        return None
    except Exception as e:
        log.warning(f"Woolworths buildId error: {e}")
        return None


def _extract_items(page_props: dict, slug: str) -> list[dict]:
    """Pull the product list out of Next.js pageProps regardless of nesting."""
    # Log top-level keys on first call to help debug structure changes
    log.info(f"Woolworths {slug} pageProps keys: {list(page_props.keys())[:20]}")

    # Try known paths
    candidates = [
        page_props.get("searchResults", {}).get("products"),
        page_props.get("products"),
        page_props.get("Products"),
    ]
    for c in candidates:
        if c:
            return c

    # Flatten bundle structure (same as browse API)
    bundles = (
        page_props.get("searchResults", {}).get("Bundles")
        or page_props.get("Bundles")
        or []
    )
    if bundles:
        return [p for b in bundles for p in (b.get("Products") or [])]

    return []


def scrape() -> tuple[list[ProductRecord], Optional[str]]:
    build_id = _get_build_id()
    if not build_id:
        return [], "Could not get Woolworths buildId from specials page"

    now = datetime.datetime.utcnow().isoformat()
    seen: set[int] = set()
    products: list[ProductRecord] = []
    last_error: Optional[str] = None

    for slug, default_cat in SPECIALS_CATEGORIES:
        try:
            url = f"{BASE}/_next/data/{build_id}/shop/specials/half-price/{slug}.json"
            r = _get(url)
            if not r.ok:
                log.warning(f"Woolworths _next {slug}: HTTP {r.status_code}")
                last_error = f"HTTP {r.status_code} for {slug}"
                time.sleep(0.5)
                continue

            try:
                data = r.json()
            except Exception:
                log.warning(f"Woolworths _next {slug}: non-JSON response {r.text[:200]}")
                last_error = f"Non-JSON response for {slug}"
                time.sleep(0.5)
                continue

            page_props = data.get("pageProps", {})
            items = _extract_items(page_props, slug)
            log.info(f"Woolworths {slug}: {len(items)} products")

            for item in items:
                stockcode = item.get("Stockcode") or item.get("stockcode")
                if stockcode and stockcode in seen:
                    continue
                if stockcode:
                    seen.add(stockcode)

                price = item.get("Price") or item.get("price")
                if not price or float(price) <= 0:
                    continue

                name = (item.get("Name") or item.get("name") or "").strip()
                if not name:
                    continue

                was = item.get("WasPrice") or item.get("wasPrice")
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
            last_error = f"Error on {slug}: {e}"
            log.warning(f"Woolworths {slug} exception: {e}")

        time.sleep(0.5)

    return products, last_error if not products else None
