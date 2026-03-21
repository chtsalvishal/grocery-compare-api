from sqlalchemy import create_engine, Column, String, Float
from sqlalchemy.orm import DeclarativeBase, Session
from typing import Optional
import datetime
import os
import re
import threading
from collections import defaultdict
from rapidfuzz import fuzz

_db_write_lock = threading.Lock()

_db_url = os.environ.get("DATABASE_URL", "sqlite:///specials.db")
engine = create_engine(_db_url, connect_args={"check_same_thread": False})


class Base(DeclarativeBase):
    pass


class ProductRecord(Base):
    __tablename__ = "products"

    name = Column(String, primary_key=True)
    category = Column(String, default="Weekly Specials")
    coles_price = Column(Float, nullable=True)
    woolies_price = Column(Float, nullable=True)
    aldi_price = Column(Float, nullable=True)
    coles_was_price = Column(Float, nullable=True)
    woolies_was_price = Column(Float, nullable=True)
    aldi_was_price = Column(Float, nullable=True)
    unit = Column(String, default="ea")
    image_url = Column(String, nullable=True)
    last_updated = Column(String)


Base.metadata.create_all(engine)


def get_all_products() -> list[ProductRecord]:
    with Session(engine) as session:
        return session.query(ProductRecord).order_by(ProductRecord.name).all()


def clear_store_prices(store: str):
    """Zero out prices for one store before re-syncing so stale data is removed."""
    col_map = {
        "coles":   ProductRecord.coles_price,
        "woolies": ProductRecord.woolies_price,
        "aldi":    ProductRecord.aldi_price,
    }
    col_attr = col_map.get(store.lower())
    if col_attr is None:
        return
    with _db_write_lock:
        with Session(engine) as session:
            session.query(ProductRecord).update({col_attr: None}, synchronize_session=False)
            session.commit()


def merge_products():
    """
    Cross-store product matching — optimised from O(n²) to near O(n).

    Optimisations applied:
      1. Category bucketing   — only compare products in the same category
      2. Length pre-filter    — skip pairs whose length ratio < 0.6 (can't reach 0.80 similarity)
      3. Token pre-filter     — skip pairs that share no word tokens of 4+ chars
      4. rapidfuzz            — C-extension string similarity, ~100x faster than difflib
    """
    _SIZE_RE = re.compile(
        r"\b\d+(\.\d+)?\s*(kg|g|ml|l|ltr|litre|litres|pk|pack|pcs|pieces|x\d+)\b",
        re.IGNORECASE,
    )
    _STORE_WORDS = re.compile(
        r"\b(woolworths|woolies|coles|aldi)\b", re.IGNORECASE
    )

    def _normalise(name: str) -> str:
        n = name.lower()
        n = _STORE_WORDS.sub("", n)
        n = _SIZE_RE.sub("", n)
        n = re.sub(r"\s+", " ", n).strip()
        return n

    def _tokens(norm: str) -> set[str]:
        return {w for w in norm.split() if len(w) >= 4}

    with _db_write_lock:
        with Session(engine) as session:
            all_records: list[ProductRecord] = session.query(ProductRecord).all()

            # Fix 1: bucket by category so we only compare within-category pairs
            buckets: dict[str, list[tuple[str, set[str], ProductRecord]]] = defaultdict(list)
            for r in all_records:
                norm = _normalise(r.name)
                buckets[r.category or "Weekly Specials"].append((norm, _tokens(norm), r))

            merged_count = 0
            consumed: set[str] = set()

            for category, items in buckets.items():
                for i, (norm_i, tok_i, rec_i) in enumerate(items):
                    if rec_i.name in consumed:
                        continue
                    len_i = len(norm_i)

                    for j in range(i + 1, len(items)):
                        norm_j, tok_j, rec_j = items[j]
                        if rec_j.name in consumed:
                            continue

                        # Fix 2: length pre-filter — if lengths differ too much, ratio < 0.80 is guaranteed
                        len_j = len(norm_j)
                        if len_i == 0 or len_j == 0:
                            continue
                        if min(len_i, len_j) / max(len_i, len_j) < 0.6:
                            continue

                        # Fix 3: token pre-filter — must share at least one meaningful word
                        if tok_i and tok_j and tok_i.isdisjoint(tok_j):
                            continue

                        # Fix 4: rapidfuzz — ~100x faster than difflib.SequenceMatcher
                        if fuzz.ratio(norm_i, norm_j) < 80:
                            continue

                        # Decide winner = longer (more detailed) name
                        winner, loser = (rec_j, rec_i) if len(rec_j.name) > len(rec_i.name) else (rec_i, rec_j)

                        if loser.coles_price is not None and winner.coles_price is None:
                            winner.coles_price = loser.coles_price
                            winner.coles_was_price = loser.coles_was_price
                        if loser.woolies_price is not None and winner.woolies_price is None:
                            winner.woolies_price = loser.woolies_price
                            winner.woolies_was_price = loser.woolies_was_price
                        if loser.aldi_price is not None and winner.aldi_price is None:
                            winner.aldi_price = loser.aldi_price
                            winner.aldi_was_price = loser.aldi_was_price
                        if loser.image_url and not winner.image_url:
                            winner.image_url = loser.image_url
                        if winner.category in ("Weekly Specials", None) and loser.category not in ("Weekly Specials", None):
                            winner.category = loser.category

                        db_winner = session.get(ProductRecord, winner.name)
                        db_loser  = session.get(ProductRecord, loser.name)
                        if db_winner and db_loser:
                            db_winner.coles_price     = winner.coles_price
                            db_winner.coles_was_price = winner.coles_was_price
                            db_winner.woolies_price     = winner.woolies_price
                            db_winner.woolies_was_price = winner.woolies_was_price
                            db_winner.aldi_price     = winner.aldi_price
                            db_winner.aldi_was_price = winner.aldi_was_price
                            db_winner.image_url = winner.image_url
                            db_winner.category  = winner.category
                            session.delete(db_loser)
                            consumed.add(loser.name)
                            merged_count += 1

            session.commit()

    return merged_count


def upsert_products(records: list[ProductRecord]):
    """Merge incoming records into the DB, updating prices per store."""
    with _db_write_lock:
        with Session(engine) as session:
            for p in records:
                existing = session.get(ProductRecord, p.name)
                if existing:
                    # Only overwrite the fields that came in non-null
                    if p.coles_price is not None:
                        existing.coles_price = p.coles_price
                        existing.coles_was_price = p.coles_was_price
                    if p.woolies_price is not None:
                        existing.woolies_price = p.woolies_price
                        existing.woolies_was_price = p.woolies_was_price
                    if p.aldi_price is not None:
                        existing.aldi_price = p.aldi_price
                        existing.aldi_was_price = p.aldi_was_price
                    if p.unit and p.unit != "ea":
                        existing.unit = p.unit
                    if p.image_url:
                        existing.image_url = p.image_url
                    existing.last_updated = p.last_updated
                else:
                    session.add(p)
            session.commit()
