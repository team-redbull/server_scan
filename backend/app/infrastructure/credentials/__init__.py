"""Resolves a collector's login/endpoint from environment configuration.

No `Manager` document is read to decide where to connect. See `env.py`.
"""

from app.infrastructure.credentials.env import EnvConnectionResolver, configured_manager_types

__all__ = ["EnvConnectionResolver", "configured_manager_types"]
