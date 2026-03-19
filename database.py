from sqlalchemy import create_engine, Column, String, Float, text
from sqlalchemy.orm import DeclarativeBase, Session
from typing import Optional
import datetime
import re
import difflib

engine = create_engine("sqlite:///specials.db", connect_args={"check_same_thread": False})


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
    with Session(engine) as session:
        col_map = {"coles": "coles_price", "woolies": "woolies_price", "aldi": "aldi_price"}
        col = col_map.get(store.lower())
        if col:
            session.execute(text(f"UPDATE products SET {col} = NULL"))
            session.commit()


def merge_products():
    """
    Cross-store product matching.

    Groups all products by normalised name (lowercase, strip brand store suffixes,
    strip size tokens like 500g/1L/2kg). Pairs with >80% name similarity are merged
    into a single record that carries prices from all matching stores.

    When merging two records the one with the longer (more detailed) name wins.
    The loser record is deleted from the DB; the winner record is updated with the
    additional store prices.

    Returns a count of how many merges were performed.
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
        # Collapse whitespace
        n = re.sub(r"\s+", " ", n).strip()
        return n

    with Session(engine) as session:
        all_records: list[ProductRecord] = session.query(ProductRecord).all()

        # Build list of (normalised_name, record)
        normed = [(_normalise(r.name), r) for r in all_records]

        merged_count = 0
        # Track which primary keys have already been consumed in a merge
        consumed: set[str] = set()

        for i, (norm_i, rec_i) in enumerate(normed):
            if rec_i.name in consumed:
                continue
            for j in range(i + 1, len(normed)):
                norm_j, rec_j = normed[j]
                if rec_j.name in consumed:
                    continue

                ratio = difflib.SequenceMatcher(None, norm_i, norm_j).ratio()
                if ratio < 0.80:
                    continue

                # Decide winner = longer (more detailed) name
                if len(rec_j.name) > len(rec_i.name):
                    winner, loser = rec_j, rec_i
                else:
                    winner, loser = rec_i, rec_j

                # Merge loser's prices into winner where winner has none
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

                # Use the best category (non-generic wins)
                if winner.category in ("Weekly Specials", None) and loser.category not in ("Weekly Specials", None):
                    winner.category = loser.category

                # Re-fresh winner in session and delete loser
                db_winner = session.get(ProductRecord, winner.name)
                db_loser = session.get(ProductRecord, loser.name)
                if db_winner and db_loser:
                    db_winner.coles_price = winner.coles_price
                    db_winner.coles_was_price = winner.coles_was_price
                    db_winner.woolies_price = winner.woolies_price
                    db_winner.woolies_was_price = winner.woolies_was_price
                    db_winner.aldi_price = winner.aldi_price
                    db_winner.aldi_was_price = winner.aldi_was_price
                    db_winner.image_url = winner.image_url
                    db_winner.category = winner.category
                    session.delete(db_loser)
                    consumed.add(loser.name)
                    merged_count += 1

        session.commit()

    return merged_count


def upsert_products(records: list[ProductRecord]):
    """Merge incoming records into the DB, updating prices per store."""
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
