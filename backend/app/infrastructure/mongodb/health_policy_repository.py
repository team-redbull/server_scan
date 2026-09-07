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
    """MongoDB-backed store for health policies."""

    def __init__(self, mongo: MongoClientHolder) -> None:
        """
        Store the shared Mongo client holder.

        Args:
            mongo (MongoClientHolder): The connected client holder.
        """
        self._mongo = mongo

    @property
    def _collection(self) -> AsyncCollection[_Document]:
        return self._mongo.db[HEALTH_POLICIES_COLLECTION]

    async def upsert(self, policy: HealthPolicy) -> HealthPolicy:
        """
        Replace-or-insert a policy by `_id`.

        Args:
            policy (HealthPolicy): The policy to persist.

        Returns:
            HealthPolicy: The same policy, for chaining.
        """
        doc = policy.model_dump(by_alias=True, mode="json")
        await self._collection.replace_one({"_id": policy.id}, doc, upsert=True)
        return policy

    async def get_by_id(self, policy_id: str) -> HealthPolicy | None:
        """
        Look up one policy by its id.

        Args:
            policy_id (str): The policy's id.

        Returns:
            HealthPolicy | None: The policy, or None if not found.
        """
        doc = await self._collection.find_one({"_id": policy_id})
        if doc is None:
            return None
        return HealthPolicy.model_validate(doc)

    async def get_by_name(self, name: str) -> HealthPolicy | None:
        """
        Look up one policy by its unique name.

        Args:
            name (str): The policy's name.

        Returns:
            HealthPolicy | None: The policy, or None if not found.
        """
        doc = await self._collection.find_one({"name": name})
        if doc is None:
            return None
        return HealthPolicy.model_validate(doc)

    async def list_all(self, *, enabled_only: bool = False) -> list[HealthPolicy]:
        """
        List every policy.

        Args:
            enabled_only (bool): If True, only return enabled policies.

        Returns:
            list[HealthPolicy]: All matching policies.
        """
        query: dict[str, object] = {"enabled": True} if enabled_only else {}
        docs = await self._collection.find(query).to_list(length=None)
        return [HealthPolicy.model_validate(doc) for doc in docs]

    async def delete(self, policy_id: str) -> bool:
        """
        Delete one policy by its id.

        Args:
            policy_id (str): The policy's id.

        Returns:
            bool: True if a policy was deleted, False if none matched.
        """
        result = await self._collection.delete_one({"_id": policy_id})
        return result.deleted_count > 0
