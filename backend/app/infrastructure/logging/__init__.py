"""Structured logging setup: configures structlog + stdlib logging as one pipeline."""

from app.infrastructure.logging.config import configure_logging

__all__ = ["configure_logging"]
