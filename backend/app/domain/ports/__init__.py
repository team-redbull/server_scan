"""Ports: the interfaces the domain depends on and infrastructure implements."""

from app.domain.ports.provider import ProviderServer, ServerInventoryProvider
from app.domain.ports.repository import Page, ServerRepository

__all__ = ["Page", "ProviderServer", "ServerInventoryProvider", "ServerRepository"]
