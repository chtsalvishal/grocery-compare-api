"""
Coles specials scraper.

Strategy:
  1. Fetch a non-WAF-blocked path (/api/2.0/page/x) to extract the current
     Next.js buildId from __NEXT_DATA__.
  2. Use the buildId to call /_next/data/{buildId}/on-special.json?page=N
     with a fresh session (no Incapsula cookies) — this endpoint returns
     real JSON without triggering the bot challenge.
  3. Paginate until MAX_PAGES is reached or no more results are returned.
  4. Filter to products where pricing.was > 0 or promotionType == 'SPECIAL'.
"""

import json
import time
import datetime
import requests
from bs4 import BeautifulSoup
from typing import Optional
from urllib.parse import urlparse

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
    # Normalise protocol-relative URLs (e.g. //productimages.coles.com.au/...)
    if url.startswith("//"):
        url = "https:" + url
    # Normalise root-relative URLs against the Coles image origin
    if url.startswith("/") and not url.startswith("//"):
        url = "https://productimages.coles.com.au" + url
    if not url.startswith("https://"):
        return None
    host = urlparse(url).hostname or ""
    return url if host in _ALLOWED_IMAGE_HOSTS else None

BASE = "https://www.coles.com.au"
IMG_BASE = "https://productimages.coles.com.au/productimages"
PAGE_SIZE = 48
MAX_PAGES = 25   # 25 × 48 = up to 1,200 specials per sync

HEADERS_HTML = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-AU,en;q=0.9",
}

HEADERS_JSON = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Accept-Language": "en-AU,en;q=0.9",
    "Referer": f"{BASE}/on-special",
}


def _get_build_id() -> Optional[str]:
    """
    Fetch a known-404 API path that returns the real Next.js 404 page
    (not the Incapsula challenge). Extract buildId from __NEXT_DATA__.
    """
    s = requests.Session()
    s.headers.update(HEADERS_HTML)
    try:
        r = s.get(f"{BASE}/api/2.0/page/x", timeout=20)
        soup = BeautifulSoup(r.text, "lxml")
        tag = soup.find("script", {"id": "__NEXT_DATA__"})
        if not tag or not tag.string:
            return None
        data = json.loads(tag.string)
        return data.get("buildId")
    except Exception:
        return None


def _categorise(name: str, merchandise_category: str = "") -> str:
    combined = f"{name.lower()} {merchandise_category.lower()}"
    # Household first — grabs fabric conditioner/softener before Personal Care grabs 'conditioner'
    if any(w in combined for w in ["cleaning", "detergent", "paper towel", "tissue", "bin liner",
                                    "nappy", "dishwash", "bleach", "spray cleaner", "wipe",
                                    "laundry", "fabric softener", "fabric conditioner", "disinfectant"]):
        return "Household"
    # Personal Care before Dairy — skin/hair cream must not hit 'cream' in Dairy
    if any(w in combined for w in ["shampoo", "toothpaste", "deodorant", "soap", "body wash",
                                    "moisturiser", "moisturizer", "sunscreen", "perfume", "makeup",
                                    "razor", "face wash", "conditioner", "vitamins", "supplements",
                                    "fish oil", "probiotic", "tampon", "pads", "lip balm",
                                    "lotion", "serum", "hand cream", "face cream", "body cream",
                                    "eye cream", "anti age", "anti-age", "skincare", "skin care",
                                    "protein bar", "protein shake", "protein powder"]):
        return "Personal Care"
    if any(w in combined for w in ["milk", "cheese", "yogurt", "butter", "cream", "egg"]):
        return "Dairy"
    if any(w in combined for w in ["chicken", "beef", "pork", "lamb", "mince", "steak", "sausage", "bacon", "meat"]):
        return "Meat"
    if any(w in combined for w in ["apple", "banana", "tomato", "onion", "potato", "carrot", "lettuce", "salad", "fruit", "veg"]):
        return "Produce"
    if any(w in combined for w in ["bread", "roll", "cake", "cookie", "pastry", "donut", "muffin"]):
        return "Bakery"
    # Beverages before Pantry — 'water', 'juice', 'tea' must not hit 'sugar'/'oil' in Pantry
    if any(w in combined for w in ["water", "juice", "drink", "soda", "coffee", "tea", "beer", "wine"]):
        return "Beverages"
    if any(w in combined for w in ["frozen", "ice cream", "pizza"]):
        return "Frozen"
    if any(w in combined for w in ["pasta", "rice", "flour", "sugar", "oil", "sauce", "cereal", "canned", "tinned"]):
        return "Pantry"
    if any(w in combined for w in ["chip", "chocolate", "biscuit", "snack", "lolly", "candy"]):
        return "Snacks"
    return "Weekly Specials"


def scrape() -> tuple[list[ProductRecord], Optional[str]]:
    build_id = _get_build_id()
    if not build_id:
        return [], "Could not retrieve Coles build ID"

    session = requests.Session()
    session.headers.update(HEADERS_JSON)

    now = datetime.datetime.utcnow().isoformat()
    seen: set[int] = set()
    products: list[ProductRecord] = []
    last_error: Optional[str] = None

    for page_num in range(1, MAX_PAGES + 1):
        try:
            url = f"{BASE}/_next/data/{build_id}/on-special.json"
            resp = session.get(url, params={"page": str(page_num)}, timeout=30)

            if not resp.ok:
                last_error = f"HTTP {resp.status_code} on page {page_num}"
                break

            page_data = resp.json()
            sr = page_data.get("pageProps", {}).get("searchResults", {})
            results = sr.get("results") or []

            if not results:
                break  # no more pages

            for item in results:
                if item.get("_type") != "PRODUCT":
                    continue

                product_id = item.get("id")
                if product_id and product_id in seen:
                    continue
                if product_id:
                    seen.add(product_id)

                pricing = item.get("pricing") or {}
                price_now = pricing.get("now")
                if price_now is None or float(price_now) <= 0:
                    continue

                # Only include actual specials (has a was-price or is flagged as special)
                price_was = pricing.get("was") or 0
                promo_type = pricing.get("promotionType", "")
                if float(price_was) <= 0 and promo_type != "SPECIAL":
                    continue

                name = (item.get("name") or "").strip()
                if not name:
                    continue

                size = item.get("size") or "ea"
                merch = (item.get("merchandiseHeir") or {}).get("category", "")

                img_uris = item.get("imageUris") or []
                img_url = None
                if img_uris and isinstance(img_uris[0], dict):
                    uri = img_uris[0].get("uri", "")
                    if uri:
                        img_url = _safe_image_url(f"{IMG_BASE}{uri}")

                products.append(ProductRecord(
                    name=name,
                    category=_categorise(name, merch),
                    coles_price=float(price_now),
                    coles_was_price=float(price_was) if float(price_was) > 0 else None,
                    unit=size,
                    image_url=img_url,
                    last_updated=now,
                ))

        except Exception as e:
            last_error = f"Error on page {page_num}: {e}"
            break

        time.sleep(0.5)

    return products, last_error if not products else None
