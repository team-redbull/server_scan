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
    """MongoDB-backed store for site reference documents."""

    def __init__(self, mongo: MongoClientHolder) -> None:
        """
        Store the shared Mongo client holder.

        Args:
            mongo (MongoClientHolder): The connected client holder.
        """
        self._mongo = mongo

    @property
    def _collection(self) -> AsyncCollection[_Document]:
        return self._mongo.db[SITES_COLLECTION]

    async def upsert(self, site: Site) -> Site:
        """
        Replace-or-insert a site by `_id`.

        Args:
            site (Site): The site to persist.

        Returns:
            Site: The same site, for chaining.
        """
        doc = site.model_dump(by_alias=True, mode="json")
        await self._collection.replace_one({"_id": site.id}, doc, upsert=True)
        return site

    async def get_by_id(self, site_id: str) -> Site | None:
        """
        Look up one site by its id.

        Args:
            site_id (str): The site's id.

        Returns:
            Site | None: The site, or None if not found.
        """
        doc = await self._collection.find_one({"_id": site_id})
        if doc is None:
            return None
        return Site.model_validate(doc)

    async def list_all(self) -> list[Site]:
        """
        List every site.

        Returns:
            list[Site]: All sites in the collection.
        """
        docs = await self._collection.find({}).to_list(length=None)
        return [Site.model_validate(doc) for doc in docs]
