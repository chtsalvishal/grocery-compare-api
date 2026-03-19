"""
Woolworths specials scraper.

Strategy: ScrapingBee js_scenario
  1. Open the specials page in a real Chrome browser (solves Akamai JS challenge,
     sets _abck / bm_* session cookies).
  2. After 5 s (Akamai + Angular bootstrap), execute a fetch() call to the
     browse API from INSIDE the browser — cookies are included automatically.
  3. Store the raw JSON response in a <script id="wdata"> tag in the DOM.
  4. Wait 8 s for the fetch to complete.
  5. ScrapingBee returns the final HTML; we extract and parse the JSON.

Cost: 5 ScrapingBee credits/request × 12 categories = 60 credits/sync.
Free tier = 1,000 credits/month ≈ 16 syncs/month.
"""

import os
import re
import json
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

# (category_id, url_path, display_category)
SPECIALS_CATEGORIES = [
    ("1-E5BEE36E", "/shop/specials/half-price/fruit-veg",            "Produce"),
    ("1_D5A2236",  "/shop/specials/half-price/poultry-meat-seafood",  "Meat"),
    ("1_6E4F4E4",  "/shop/specials/half-price/dairy-eggs-fridge",     "Dairy"),
    ("1_DEB537E",  "/shop/specials/half-price/bakery",                "Bakery"),
    ("1_717445A",  "/shop/specials/half-price/snacks-confectionery",  "Snacks"),
    ("1_5AF3A0A",  "/shop/specials/half-price/drinks",                "Beverages"),
    ("1_39FD49C",  "/shop/specials/half-price/pantry",                "Pantry"),
    ("1_ACA2FC2",  "/shop/specials/half-price/freezer",               "Frozen"),
    ("1_8D61DD6",  "/shop/specials/half-price/beauty",                "Personal Care"),
    ("1_894D0A8",  "/shop/specials/half-price/personal-care",         "Personal Care"),
    ("1_2432B58",  "/shop/specials/half-price/cleaning-maintenance",  "Household"),
    ("1_717A94B",  "/shop/specials/half-price/baby",                  "Household"),
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


def _build_scenario(cat_id: str, url_path: str) -> str:
    """
    Build a ScrapingBee js_scenario JSON string that:
      - Waits 5 s for Akamai challenge + Angular bootstrap
      - Calls the browse API via fetch() from the authenticated browser context
      - Stores the JSON response in <script id="wdata" type="application/json">
      - Waits 8 s for the fetch to resolve

    js_scenario expects a plain JSON string (NOT base64).
    """
    js = (
        "var d={"
        f"CategoryId:'{cat_id}',"
        "PageNumber:1,PageSize:36,SortType:'TraderRelevance',"
        f"Url:'{url_path}',"
        "FormatObject:{},IsSpecial:true"
        "};"
        "fetch('/apis/ui/browse/category',{"
        "method:'POST',"
        "headers:{'Content-Type':'application/json'},"
        "body:JSON.stringify(d)"
        "}).then(function(r){return r.text();})"
        ".then(function(t){"
        "var s=document.createElement('script');"
        "s.type='application/json';"
        "s.id='wdata';"
        "s.textContent=t;"
        "document.body.appendChild(s);"
        "});"
    )

    return json.dumps({
        "instructions": [
            {"wait": 5000},
            {"evaluate": js},
            {"wait": 8000},
        ]
    })


def _fetch_category(cat_id: str, url_path: str) -> Optional[dict]:
    slug = url_path.split("/")[-1]
    page_url = f"{BASE}{url_path}"

    r = requests.get(
        SCRAPINGBEE_URL,
        params={
            "api_key": SCRAPINGBEE_KEY,
            "url": page_url,
            "render_js": "true",
            "js_scenario": _build_scenario(cat_id, url_path),
        },
        timeout=120,
    )
    log.info(f"Woolworths {slug}: HTTP {r.status_code} len={len(r.text)}")

    if not r.ok:
        log.warning(f"Woolworths {slug}: ScrapingBee error {r.status_code}: {r.text[:200]}")
        return None

    soup = BeautifulSoup(r.text, "lxml")
    tag = soup.find("script", {"id": "wdata", "type": "application/json"})
    if not tag or not tag.string:
        log.warning(f"Woolworths {slug}: wdata script tag not found — fetch may not have completed")
        return None

    try:
        return json.loads(tag.string)
    except Exception as e:
        log.warning(f"Woolworths {slug}: JSON parse error: {e} — snippet: {(tag.string or '')[:200]}")
        return None


def scrape() -> tuple[list[ProductRecord], Optional[str]]:
    if not SCRAPINGBEE_KEY:
        return [], "SCRAPINGBEE_API_KEY not set"

    now = datetime.datetime.utcnow().isoformat()
    seen: set[int] = set()
    products: list[ProductRecord] = []
    last_error: Optional[str] = None

    for cat_id, url_path, default_cat in SPECIALS_CATEGORIES:
        slug = url_path.split("/")[-1]
        try:
            data = _fetch_category(cat_id, url_path)
            if data is None:
                last_error = f"No data for {slug}"
                time.sleep(1)
                continue

            total = data.get("TotalRecordCount", 0)
            bundles = data.get("Bundles") or []
            items = [p for b in bundles for p in (b.get("Products") or [])]
            log.info(f"Woolworths {slug}: total={total} items={len(items)}")

            for item in items:
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
            last_error = f"Error on {slug}: {e}"
            log.warning(f"Woolworths {slug} exception: {e}")

        time.sleep(1)

    return (products, None) if products else ([], last_error or "No Woolworths products found")
