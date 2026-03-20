# GroceryCompare Backend API

A FastAPI backend that scrapes weekly specials from Australian supermarkets (Woolworths, Coles, Aldi) and exposes a REST API consumed by the GroceryCompare Android app.

---

## Architecture Diagram

```
                        ┌─────────────────────────────────────────────────────────┐
                        │                  RENDER.COM (Cloud Host)                │
                        │                                                         │
   ┌──────────────┐     │   ┌─────────────────────────────────────────────────┐  │
   │  APScheduler │     │   │                   main.py                       │  │
   │  06:00 UTC   │─────┼──▶│   FastAPI App  +  Sync Orchestrator             │  │
   │  daily cron  │     │   └──────────┬──────────────────────────────────────┘  │
   └──────────────┘     │              │                                          │
                        │              │  ThreadPoolExecutor (max_workers=3)      │
                        │    ┌─────────┼──────────────────────┐                  │
                        │    │         │                       │                  │
                        │    ▼         ▼                       ▼                  │
                        │  ┌────────┐ ┌────────────────┐ ┌─────────┐            │
                        │  │aldi.py │ │ woolworths.py   │ │coles.py │            │
                        │  │scraper │ │    scraper      │ │ scraper │            │
                        │  └───┬────┘ └───────┬─────────┘ └────┬────┘            │
                        │      │              │                 │                 │
                        └──────┼──────────────┼─────────────────┼─────────────────┘
                               │              │                 │
                               │              │ ScrapingBee     │
                               │              │ JS Scenario     │
                               ▼              ▼                 ▼
                        ┌──────────┐  ┌──────────────┐  ┌──────────┐
                        │   Aldi   │  │  ScrapingBee  │  │  Coles   │
                        │ Website  │  │  (Chrome bot  │  │ Website  │
                        │(direct   │  │   bypass)     │  │(direct   │
                        │  HTTP)   │  └──────┬────────┘  │  HTTP)   │
                        └──────────┘         │           └──────────┘
                                             ▼
                                    ┌─────────────────┐
                                    │   Woolworths    │
                                    │    Website      │
                                    │ (Akamai WAF +   │
                                    │  Angular SPA)   │
                                    └─────────────────┘

                        ┌─────────────────────────────────────────────────────────┐
                        │                  RENDER.COM (continued)                 │
                        │                                                         │
                        │   After scraping completes (parallel):                  │
                        │                                                         │
                        │   Phase 2 — Serial DB writes (SQLite single-writer)     │
                        │   ┌─────────┐   ┌─────────┐   ┌─────────┐             │
                        │   │ Woolies │   │  Coles  │   │  Aldi   │             │
                        │   │ records │──▶│ records │──▶│ records │──▶ ...       │
                        │   └─────────┘   └─────────┘   └─────────┘             │
                        │                      │                                  │
                        │                      ▼                                  │
                        │            ┌──────────────────┐                        │
                        │            │   database.py    │                        │
                        │            │  upsert_products │                        │
                        │            │  clear_store_    │                        │
                        │            │    prices        │                        │
                        │            └────────┬─────────┘                        │
                        │                     │                                   │
                        │   Phase 3 — Fuzzy cross-store merge                    │
                        │            merge_products()                             │
                        │            (difflib 80% similarity)                    │
                        │                     │                                   │
                        │                     ▼                                   │
                        │            ┌──────────────────┐                        │
                        │            │   specials.db    │                        │
                        │            │  (SQLite)        │                        │
                        │            │                  │                        │
                        │            │  products table  │                        │
                        │            │  ─ name (PK)     │                        │
                        │            │  ─ coles_price   │                        │
                        │            │  ─ woolies_price │                        │
                        │            │  ─ aldi_price    │                        │
                        │            │  ─ *_was_price   │                        │
                        │            │  ─ category      │                        │
                        │            └────────┬─────────┘                        │
                        │                     │                                   │
                        │            ┌────────▼─────────┐                        │
                        │            │  GET /api/       │                        │
                        │            │  specials        │◀── POST /api/sync      │
                        │            │  (filter, sort)  │◀── GET /api/health     │
                        │            └────────┬─────────┘    GET /api/sync/status│
                        └─────────────────────┼───────────────────────────────────┘
                                              │
                                              │  JSON over HTTPS
                                              ▼
                        ┌─────────────────────────────────────────────────────────┐
                        │               Android App (GroceryCompare)              │
                        │                                                         │
                        │  SyncWorker (WorkManager)                               │
                        │       │                                                 │
                        │       ▼                                                 │
                        │  MasterCatalogueRepository                              │
                        │       │  replaceAll() — full DB replace on each sync    │
                        │       ▼                                                 │
                        │  Room Database (local SQLite)                           │
                        │       │                                                 │
                        │       ▼                                                 │
                        │  HomeViewModel — filter, sort, category                 │
                        │       │                                                 │
                        │       ▼                                                 │
                        │  HomeScreen (Jetpack Compose)                           │
                        │  ┌──────────────────────────────────────────┐          │
                        │  │  Product cards — side-by-side price      │          │
                        │  │  comparison across Coles / Woolies / Aldi│          │
                        │  │  with BEST PRICE badge + savings banner  │          │
                        │  └──────────────────────────────────────────┘          │
                        └─────────────────────────────────────────────────────────┘
```

### Data flow summary

1. **Trigger** — APScheduler fires daily at 06:00 UTC, or the user taps Sync in the app (`POST /api/sync`)
2. **Scrape** — All 3 stores scraped in parallel. Woolworths runs 12 category fetches concurrently via ScrapingBee (real Chrome browser to bypass Akamai WAF + Angular SPA). Coles and Aldi use direct HTTP.
3. **Write** — Results written to SQLite serially (one store at a time). Each store's price column is cleared first so stale specials are removed.
4. **Merge** — `merge_products()` fuzzy-matches the same product across stores (80% name similarity) and collapses them into one record with prices from all matching stores.
5. **Serve** — `GET /api/specials` returns the unified product list with optional filtering by store, category, or name, and sorting by price, savings, or multi-store availability.
6. **Android** — `SyncWorker` polls the API, writes results to a local Room database, and the UI renders price comparisons in real time.

---

## What it does

- Scrapes weekly/half-price specials from Woolworths, Coles, and Aldi
- Normalises products into a unified schema with per-store prices and "was" prices
- Fuzzy-merges the same product across stores (e.g. "Coca-Cola 1.25L" from Coles and Woolworths becomes one record with both prices)
- Runs a full sync automatically every day at 06:00 UTC (4 PM AEST)
- Exposes a REST API for the Android app to fetch and display price comparisons

### Supported retailers

| Store | Source | Method |
|-------|--------|--------|
| Woolworths | `/shop/specials/half-price/*` (12 categories) | ScrapingBee JS scenario (bypasses Akamai WAF) |
| Coles | `/on-special` category pages | Direct HTTP + BeautifulSoup |
| Aldi | `/products/lower-prices` + `/products/super-savers` | Direct HTTP + BeautifulSoup |

### Product categories

Products are classified into: `Produce`, `Meat`, `Dairy`, `Bakery`, `Snacks`, `Beverages`, `Pantry`, `Frozen`, `Personal Care`, `Household`, `Weekly Specials`

---

## API Endpoints

### `GET /api/health`
Liveness probe.

```json
{ "status": "ok", "timestamp": "2026-03-20T06:00:00" }
```

---

### `GET /api/specials`
Returns all current specials. Supports filtering and sorting.

**Query parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `category` | string | Filter by category (e.g. `Dairy`, `Weekly Specials`) |
| `store` | string | Filter by store: `coles`, `woolies`, `aldi` |
| `q` | string | Search product name (case-insensitive substring) |
| `sort` | string | Sort order (see below) |

**Sort options:**

| Value | Description |
|-------|-------------|
| `multi_store` | Products available at 2+ stores first, then A-Z (default) |
| `price_asc` | Cheapest first |
| `price_desc` | Most expensive first |
| `savings` | Biggest dollar saving (was - now) first |
| `az` | Alphabetical A-Z |

**Example response:**

```json
[
  {
    "name": "Coca-Cola 1.25L",
    "category": "Beverages",
    "colesPrice": 2.50,
    "wooliesPrice": 2.75,
    "aldiPrice": null,
    "colesWasPrice": 4.00,
    "wooliesWasPrice": 4.50,
    "aldiWasPrice": null,
    "unit": "1.25L",
    "imageUrl": "https://...",
    "lastUpdated": "2026-03-20T06:00:00"
  }
]
```

---

### `POST /api/sync`
Triggers an immediate full sync of all three stores. Returns the sync result.

```json
{
  "total": 1243,
  "lastSyncedAt": "2026-03-20T06:00:00",
  "stores": [
    { "store": "Woolworths", "status": "ok", "productsFound": 412 },
    { "store": "Coles",      "status": "ok", "productsFound": 526 },
    { "store": "Aldi",       "status": "ok", "productsFound": 305 }
  ]
}
```

---

### `GET /api/sync/status`
Returns the result of the last completed sync. Returns `404` if no sync has run yet.

---

## Architecture

```
main.py              — FastAPI app, scheduler, sync orchestration
database.py          — SQLite via SQLAlchemy, upsert + fuzzy merge logic
models.py            — Pydantic response models
scrapers/
  coles.py           — Coles scraper (direct HTTP)
  woolworths.py      — Woolworths scraper (ScrapingBee JS scenario)
  aldi.py            — Aldi scraper (direct HTTP)
render.yaml          — Render deployment config
```

### Sync pipeline

1. **Parallel scrape** — all 3 stores scraped concurrently (`ThreadPoolExecutor(max_workers=3)`). Woolworths fetches its 12 categories concurrently too (`max_workers=4`).
2. **Serial DB writes** — SQLite is single-writer; results are written store-by-store after all scraping is done.
3. **Cross-store merge** — `merge_products()` uses `difflib` 80% similarity matching to merge the same product across stores into one record with prices from all matching stores.

---

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `SCRAPINGBEE_API_KEY` | Yes (for Woolworths) | ScrapingBee API key. Woolworths uses Akamai bot detection; ScrapingBee runs a real Chrome browser to bypass it. Without this key, Woolworths scraping is skipped. |
| `PORT` | Set by Render | Port to bind on. Defaults to `8000` locally. |

---

## Local Development

### Prerequisites

- Python 3.11+
- A [ScrapingBee](https://www.scrapingbee.com/) API key (free tier: 1,000 credits/month ~ 16 syncs)

### Setup

```bash
# Clone the repo
git clone https://github.com/chtsalvishal/grocery-compare-api.git
cd grocery-compare-api

# Install dependencies
pip install -r requirements.txt

# Set your ScrapingBee key
export SCRAPINGBEE_API_KEY=your_key_here   # macOS/Linux
$env:SCRAPINGBEE_API_KEY="your_key_here"  # Windows PowerShell

# Run the server
python main.py
# or
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

The server starts on `http://localhost:8000`. On startup it immediately kicks off a background sync.

### Test the API

```bash
# Health check
curl http://localhost:8000/api/health

# Get all specials
curl http://localhost:8000/api/specials

# Filter by store and category
curl "http://localhost:8000/api/specials?store=woolies&category=Weekly%20Specials"

# Search by name, sort by savings
curl "http://localhost:8000/api/specials?q=coca-cola&sort=savings"

# Trigger a manual sync
curl -X POST http://localhost:8000/api/sync

# Check last sync status
curl http://localhost:8000/api/sync/status
```

---

## Deployment (Render)

The repo includes a `render.yaml` for one-click deploy to [Render](https://render.com).

### Steps

1. Push this repo to GitHub.
2. In Render, create a new **Web Service** and connect the GitHub repo — or use **Blueprint** to auto-deploy from `render.yaml`.
3. Add the environment variable:
   - Key: `SCRAPINGBEE_API_KEY`
   - Value: your ScrapingBee API key
4. Deploy. Render will run `pip install -r requirements.txt` then start with:
   ```
   uvicorn main:app --host 0.0.0.0 --port $PORT
   ```
5. After deploy, the server binds immediately and starts a background sync. The first sync takes 3–5 minutes (ScrapingBee JS scenarios take ~13 seconds per Woolworths category).

### Automatic daily sync

The scheduler runs a full sync every day at **06:00 UTC** (4 PM AEST), after supermarkets post their new weekly catalogues.

To force a re-sync at any time: `POST /api/sync` or tap **Sync** in the Android app.

---

## ScrapingBee credits usage

| Store | Credits per sync |
|-------|-----------------|
| Woolworths | 60 (5 credits × 12 categories) |
| Coles | 0 (direct HTTP) |
| Aldi | 0 (direct HTTP) |
| **Total** | **~60 credits/sync** |

Free tier: 1,000 credits/month = ~16 full syncs/month.

---

## Database

SQLite file: `specials.db` (created automatically on first run).

Schema (`products` table):

| Column | Type | Description |
|--------|------|-------------|
| `name` | TEXT (PK) | Product name (primary key — used for deduplication) |
| `category` | TEXT | Product category |
| `coles_price` | REAL | Current Coles price |
| `woolies_price` | REAL | Current Woolworths price |
| `aldi_price` | REAL | Current Aldi price |
| `coles_was_price` | REAL | Previous Coles price (before special) |
| `woolies_was_price` | REAL | Previous Woolworths price |
| `aldi_was_price` | REAL | Previous Aldi price |
| `unit` | TEXT | Pack size (e.g. `500g`, `1.25L`, `ea`) |
| `image_url` | TEXT | Product image URL |
| `last_updated` | TEXT | ISO timestamp of last update |

Before each sync, the target store's price column is set to `NULL` so stale products that no longer appear in specials are cleared automatically.
