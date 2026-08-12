"""Minimal MongoDB repository for the `managers` collection.

Same rationale as `site_repository.py`: managers are a small, human-curated
reference collection, so a plain `list_all()` is the right shape for Phase
1 rather than cursor pagination.
"""

from __future__ import annotations

from typing import Any

from pymongo.asynchronous.collection import AsyncCollection

from app.domain.models.manager import Manager
from app.infrastructure.mongodb.client import MongoClientHolder
from app.infrastructure.mongodb.indexes import MANAGERS_COLLECTION

_Document = dict[str, Any]


class MongoManagerRepository:
    def __init__(self, mongo: MongoClientHolder) -> None:
        self._mongo = mongo

    @property
    def _collection(self) -> AsyncCollection[_Document]:
        return self._mongo.db[MANAGERS_COLLECTION]

    async def upsert(self, manager: Manager) -> Manager:
        doc = manager.model_dump(by_alias=True, mode="json")
        await self._collection.replace_one({"_id": manager.id}, doc, upsert=True)
        return manager

    async def get_by_id(self, manager_id: str) -> Manager | None:
        doc = await self._collection.find_one({"_id": manager_id})
        if doc is None:
            return None
        return Manager.model_validate(doc)

    async def list_all(self) -> list[Manager]:
        docs = await self._collection.find({}).to_list(length=None)
        return [Manager.model_validate(doc) for doc in docs]
