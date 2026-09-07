"""MongoDB infrastructure: the shared client holder and per-aggregate repositories."""

from app.infrastructure.mongodb.client import MongoClientHolder

__all__ = ["MongoClientHolder"]
