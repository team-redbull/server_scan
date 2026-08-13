"""MongoDB implementation for the `health_policies` collection.

Small, human-curated collection (dozens, not thousands) — same rationale
as `site_repository.py`/`manager_repository.py`: `list_all()` returning
every document is the right shape, no cursor pagination needed.
"""

from __future__ import annotations

from typing import Any

from pymongo.asynchronous.collection import AsyncCollection

from app.domain.models.health_policy import HealthPolicy
from app.infrastructure.mongodb.client import MongoClientHolder
from app.infrastructure.mongodb.indexes import HEALTH_POLICIES_COLLECTION

_Document = dict[str, Any]


class MongoHealthPolicyRepository:
    def __init__(self, mongo: MongoClientHolder) -> None:
        self._mongo = mongo

    @property
    def _collection(self) -> AsyncCollection[_Document]:
        return self._mongo.db[HEALTH_POLICIES_COLLECTION]

    async def upsert(self, policy: HealthPolicy) -> HealthPolicy:
        doc = policy.model_dump(by_alias=True, mode="json")
        await self._collection.replace_one({"_id": policy.id}, doc, upsert=True)
        return policy

    async def get_by_id(self, policy_id: str) -> HealthPolicy | None:
        doc = await self._collection.find_one({"_id": policy_id})
        if doc is None:
            return None
        return HealthPolicy.model_validate(doc)

    async def get_by_name(self, name: str) -> HealthPolicy | None:
        doc = await self._collection.find_one({"name": name})
        if doc is None:
            return None
        return HealthPolicy.model_validate(doc)

    async def list_all(self, *, enabled_only: bool = False) -> list[HealthPolicy]:
        query: dict[str, object] = {"enabled": True} if enabled_only else {}
        docs = await self._collection.find(query).to_list(length=None)
        return [HealthPolicy.model_validate(doc) for doc in docs]

    async def delete(self, policy_id: str) -> bool:
        result = await self._collection.delete_one({"_id": policy_id})
        return result.deleted_count > 0
