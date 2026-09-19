"""No-I/O orchestration layer shared by CLI and graphical front ends."""

from __future__ import annotations

from box.api.cleanup import CleanupCatalog, CleanupItem, RemovalResult
from box.api.diagnose import DiagnoseResult
from box.api.inspect import Inspection
from box.api.interaction import ConsoleInteraction, Interaction
from box.api.uninstall import full_wipe_data
from box.config.models import AppConfig
from box.config.repository import ConfigRepository
from box.models import GameInfo, RuntimeInfo, RuntimeSpec
from box.paths import AppPaths

__all__ = [
    "AppConfig",
    "AppPaths",
    "CleanupCatalog",
    "CleanupItem",
    "ConfigRepository",
    "ConsoleInteraction",
    "DiagnoseResult",
    "GameInfo",
    "Inspection",
    "Interaction",
    "RemovalResult",
    "RuntimeInfo",
    "RuntimeSpec",
    "full_wipe_data",
]
