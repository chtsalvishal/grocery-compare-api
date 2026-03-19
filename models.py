from pydantic import BaseModel
from typing import Optional


class SpecialProduct(BaseModel):
    name: str
    category: str = "Weekly Specials"
    colesPrice: Optional[float] = None
    wooliesPrice: Optional[float] = None
    aldiPrice: Optional[float] = None
    colesWasPrice: Optional[float] = None
    wooliesWasPrice: Optional[float] = None
    aldiWasPrice: Optional[float] = None
    unit: str = "ea"
    imageUrl: Optional[str] = None
    lastUpdated: str = ""


class SyncStatus(BaseModel):
    store: str
    status: str          # "ok" | "error" | "running"
    productsFound: int = 0
    error: Optional[str] = None


class SyncResult(BaseModel):
    total: int
    stores: list[SyncStatus]
    lastSyncedAt: str
