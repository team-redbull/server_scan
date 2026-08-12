"""Minimal MongoDB repository for the `sites` collection.

No cursor pagination — sites are a small, human-curated reference
collection (dozens, not thousands), so `list_all()` returning every
document is the right shape for Phase 1. Revisit if that assumption ever
stops holding.
"""

from __future__ import annotations

from typing import Any

from pymongo.asynchronous.collection import AsyncCollection

from app.domain.models.site import Site
from app.infrastructure.mongodb.client import MongoClientHolder
from app.infrastructure.mongodb.indexes import SITES_COLLECTION

_Document = dict[str, Any]


class MongoSiteRepository:
    def __init__(self, mongo: MongoClientHolder) -> None:
        self._mongo = mongo

    @property
    def _collection(self) -> AsyncCollection[_Document]:
        return self._mongo.db[SITES_COLLECTION]

    async def upsert(self, site: Site) -> Site:
        doc = site.model_dump(by_alias=True, mode="json")
        await self._collection.replace_one({"_id": site.id}, doc, upsert=True)
        return site

    async def get_by_id(self, site_id: str) -> Site | None:
        doc = await self._collection.find_one({"_id": site_id})
        if doc is None:
            return None
        return Site.model_validate(doc)

    async def list_all(self) -> list[Site]:
        docs = await self._collection.find({}).to_list(length=None)
        return [Site.model_validate(doc) for doc in docs]
