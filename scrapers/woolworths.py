"""
Woolworths specials scraper.

Strategy: Search the product API across grocery category keywords, collect all
results where IsOnSpecial=True. Deduplicates by stockcode.
"""

import time
import datetime
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
    "Referer": f"{BASE}/",
    "X-Requested-With": "XMLHttpRequest",
    "Content-Type": "application/json",
}

# Broad search terms that cover all major grocery sections
SEARCH_TERMS = [
    "milk", "cheese", "yogurt", "butter", "eggs",
    "chicken", "beef", "pork", "lamb", "sausage", "bacon",
    "bread", "cereal", "pasta", "rice", "oil", "sauce",
    "chips", "chocolate", "biscuit", "coffee", "juice",
    "frozen", "ice cream", "pizza",
    "shampoo", "toothpaste", "detergent", "nappy",
    "apple", "banana", "tomato", "potato", "salad",
]


def _categorise(name: str) -> str:
    n = name.lower()
    if any(w in n for w in ["milk", "cheese", "yogurt", "butter", "cream", "egg"]):
        return "Dairy"
    if any(w in n for w in ["chicken", "beef", "pork", "lamb", "mince", "steak", "sausage", "bacon", "meat"]):
        return "Meat"
    if any(w in n for w in ["apple", "banana", "tomato", "onion", "potato", "carrot", "lettuce", "salad", "fruit", "veg"]):
        return "Produce"
    if any(w in n for w in ["bread", "roll", "cake", "cookie", "pastry", "donut", "muffin"]):
        return "Bakery"
    if any(w in n for w in ["pasta", "rice", "flour", "sugar", "oil", "sauce", "cereal", "canned", "tinned"]):
        return "Pantry"
    if any(w in n for w in ["frozen", "ice cream", "pizza"]):
        return "Frozen"
    if any(w in n for w in ["water", "juice", "drink", "soda", "beer", "wine", "coffee", "tea"]):
        return "Beverages"
    if any(w in n for w in ["chip", "chocolate", "biscuit", "snack", "lolly", "candy"]):
        return "Snacks"
    if any(w in n for w in ["shampoo", "toothpaste", "deodorant", "soap", "body wash"]):
        return "Personal Care"
    if any(w in n for w in ["cleaning", "detergent", "paper", "tissue", "bin", "nappy"]):
        return "Household"
    return "Weekly Specials"


def scrape(max_terms: int = len(SEARCH_TERMS)) -> tuple[list[ProductRecord], Optional[str]]:
    session = requests.Session()
    session.headers.update(HEADERS)

    # Warmup — establishes Akamai session cookies
    try:
        session.get(f"{BASE}/", timeout=30)
        time.sleep(1.5)
    except Exception as e:
        return [], f"Warmup failed: {e}"

    now = datetime.datetime.utcnow().isoformat()
    seen: set[int] = set()          # deduplicate by Woolworths Stockcode
    products: list[ProductRecord] = []
    last_error: Optional[str] = None

    for term in SEARCH_TERMS[:max_terms]:
        try:
            resp = session.post(
                f"{BASE}/apis/ui/Search/products",
                json={
                    "SearchTerm": term,
                    "PageSize": 36,
                    "PageNumber": 1,
                    "SortType": "Relevance",
                },
                timeout=30,
            )

            if not resp.ok:
                last_error = f"HTTP {resp.status_code} for term '{term}'"
                time.sleep(1)
                continue

            bundles = resp.json().get("Products") or []
            for bundle in bundles:
                for item in bundle.get("Products") or []:
                    if not item.get("IsOnSpecial"):
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
                    img_tag = item.get("MediumImageTag") or ""
                    unit = item.get("PackageSize") or item.get("CupMeasure") or "ea"

                    products.append(ProductRecord(
                        name=name,
                        category=_categorise(name),
                        woolies_price=float(price),
                        woolies_was_price=float(was) if was else None,
                        unit=unit,
                        image_url=f"{IMG_BASE}{img_tag}" if img_tag else None,
                        last_updated=now,
                    ))

        except Exception as e:
            last_error = f"Error on term '{term}': {e}"

        time.sleep(0.6)

    return products, last_error if not products else None
