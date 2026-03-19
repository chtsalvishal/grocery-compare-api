import os
from sqlalchemy import create_engine, Column, String, Float, text
from sqlalchemy.orm import DeclarativeBase, Session
from typing import Optional
import datetime

_db_path = os.environ.get("DB_PATH", "specials.db")
engine = create_engine(f"sqlite:///{_db_path}", connect_args={"check_same_thread": False})


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
