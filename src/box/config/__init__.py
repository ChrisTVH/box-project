"""Configuration persistence."""

from box.config.models import AppConfig
from box.config.repository import ConfigRepository

__all__ = ["AppConfig", "ConfigRepository"]
