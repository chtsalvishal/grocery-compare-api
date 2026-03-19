"""
GroceryCompare Backend API
--------------------------
Provides a single REST endpoint consumed by the Android app.
A background scheduler syncs all three retailers once daily.

Endpoints:
  GET /api/health          — liveness probe
  GET /api/specials        — all current specials (optionally filtered)
  POST /api/sync           — trigger an immediate sync (admin use)
  GET /api/sync/status     — last sync result
"""

import datetime
import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import asynccontextmanager

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

from database import get_all_products, upsert_products, clear_store_prices, merge_products
from models import SpecialProduct, SyncResult, SyncStatus
import scrapers.woolworths as wools_scraper
import scrapers.coles as coles_scraper
import scrapers.aldi as aldi_scraper

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Sync logic
# ---------------------------------------------------------------------------

_last_sync: SyncResult | None = None


def run_sync() -> SyncResult:
    global _last_sync
    log.info("Starting parallel sync for all retailers...")

    scrapers = [
        ("Woolworths", wools_scraper.scrape),
        ("Coles",      coles_scraper.scrape),
        ("Aldi",       aldi_scraper.scrape),
    ]

    # ── Phase 1: scrape all stores in parallel ──────────────────────────────
    scrape_results: dict[str, tuple[list, str | None]] = {}

    def _scrape(store: str, fn):
        log.info(f"Scraping {store}...")
        try:
            return store, fn()
        except Exception as e:
            log.exception(f"Unhandled error scraping {store}")
            return store, ([], str(e))

    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {pool.submit(_scrape, s, fn): s for s, fn in scrapers}
        for future in as_completed(futures):
            store, result = future.result()
            scrape_results[store] = result

    # ── Phase 2: write to DB serially (SQLite is single-writer) ─────────────
    statuses: list[SyncStatus] = []
    total = 0

    for store, _ in scrapers:          # preserve insertion order
        records, error = scrape_results.get(store, ([], "scrape did not complete"))
        if error and not records:
            log.warning(f"{store} failed: {error}")
            statuses.append(SyncStatus(store=store, status="error", error=error))
            continue

        store_key = store.lower().replace("woolworths", "woolies")
        clear_store_prices(store_key)
        upsert_products(records)

        log.info(f"{store}: {len(records)} products saved. Partial error: {error}")
        statuses.append(SyncStatus(store=store, status="ok", productsFound=len(records)))
        total += len(records)

    # ── Phase 3: cross-store merge ───────────────────────────────────────────
    try:
        merged = merge_products()
        log.info(f"merge_products: {merged} cross-store merges performed")
    except Exception as e:
        log.warning(f"merge_products failed (non-fatal): {e}")

    result = SyncResult(
        total=total,
        stores=statuses,
        lastSyncedAt=datetime.datetime.utcnow().isoformat(),
    )
    _last_sync = result
    log.info(f"Sync complete. Total products: {total}")
    return result


# ---------------------------------------------------------------------------
# App lifecycle — scheduler
# ---------------------------------------------------------------------------

scheduler = BackgroundScheduler()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Run initial sync in background so port binds immediately
    log.info("Scheduling initial sync in background thread...")
    threading.Thread(target=run_sync, daemon=True).start()

    # Schedule daily sync at 06:00 UTC (4 PM AEST, after supermarkets post new catalogues)
    scheduler.add_job(run_sync, "cron", hour=6, minute=0, id="daily_sync")
    scheduler.start()
    log.info("Scheduler started — daily sync at 06:00 UTC")

    yield

    scheduler.shutdown()
    log.info("Scheduler stopped")


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(
    title="GroceryCompare API",
    description="Australian supermarket price comparison backend",
    version="2.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/api/health")
def health():
    return {"status": "ok", "timestamp": datetime.datetime.utcnow().isoformat()}


@app.get("/api/specials", response_model=list[SpecialProduct])
def get_specials(
    category: str | None = Query(default=None, description="Filter by category name"),
    store: str | None = Query(default=None, description="Filter: coles | woolies | aldi"),
    q: str | None = Query(default=None, description="Search product name"),
    sort: str | None = Query(
        default="multi_store",
        description=(
            "Sort order: multi_store (default) | price_asc | price_desc | savings | az"
        ),
    ),
):
    """
    Return all current specials from the DB.
    Optionally filter by category, store, or name query.
    Only returns products that have at least one active price.

    Sort options:
      - multi_store  Products available at 2+ stores first, then alphabetically (default)
      - price_asc    Cheapest first (lowest active price)
      - price_desc   Most expensive first (highest active price)
      - savings      Biggest price savings (was_price - now_price) first
      - az           Alphabetical A→Z
    """
    records = get_all_products()

    results: list[SpecialProduct] = []
    for r in records:
        # At least one price must be set
        if not any([r.coles_price, r.woolies_price, r.aldi_price]):
            continue

        # Store filter
        if store:
            s = store.lower()
            if s == "coles" and not r.coles_price:
                continue
            if s in ("woolies", "woolworths") and not r.woolies_price:
                continue
            if s == "aldi" and not r.aldi_price:
                continue

        # Category filter
        if category and r.category.lower() != category.lower():
            continue

        # Name search
        if q and q.lower() not in r.name.lower():
            continue

        results.append(
            SpecialProduct(
                name=r.name,
                category=r.category or "Weekly Specials",
                colesPrice=r.coles_price,
                wooliesPrice=r.woolies_price,
                aldiPrice=r.aldi_price,
                colesWasPrice=r.coles_was_price,
                wooliesWasPrice=r.woolies_was_price,
                aldiWasPrice=r.aldi_was_price,
                unit=r.unit or "ea",
                imageUrl=r.image_url,
                lastUpdated=r.last_updated or "",
            )
        )

    # Helper: lowest active price for a product
    def _min_price(p: SpecialProduct) -> float:
        prices = [x for x in [p.colesPrice, p.wooliesPrice, p.aldiPrice] if x]
        return min(prices) if prices else 0.0

    def _max_price(p: SpecialProduct) -> float:
        prices = [x for x in [p.colesPrice, p.wooliesPrice, p.aldiPrice] if x]
        return max(prices) if prices else 0.0

    def _max_saving(p: SpecialProduct) -> float:
        saving = 0.0
        pairs = [
            (p.colesPrice, p.colesWasPrice),
            (p.wooliesPrice, p.wooliesWasPrice),
            (p.aldiPrice, p.aldiWasPrice),
        ]
        for now, was in pairs:
            if now and was and was > now:
                saving = max(saving, was - now)
        return saving

    def _store_count(p: SpecialProduct) -> int:
        return sum(1 for x in [p.colesPrice, p.wooliesPrice, p.aldiPrice] if x)

    sort_key = (sort or "multi_store").lower()

    if sort_key == "price_asc":
        results.sort(key=lambda p: (_min_price(p), p.name.lower()))
    elif sort_key == "price_desc":
        results.sort(key=lambda p: (-_max_price(p), p.name.lower()))
    elif sort_key == "savings":
        results.sort(key=lambda p: (-_max_saving(p), p.name.lower()))
    elif sort_key == "az":
        results.sort(key=lambda p: p.name.lower())
    else:
        # Default: multi_store — products at more stores first, then alphabetically
        results.sort(key=lambda p: (-_store_count(p), p.name.lower()))

    return results


@app.post("/api/sync", response_model=SyncResult)
def trigger_sync():
    """Manually trigger a sync. Intended for admin/testing use."""
    result = run_sync()
    return result


@app.get("/api/sync/status", response_model=SyncResult | None)
def sync_status():
    """Return the result of the last sync, or null if none has run yet."""
    if _last_sync is None:
        raise HTTPException(status_code=404, detail="No sync has run yet")
    return _last_sync


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)
