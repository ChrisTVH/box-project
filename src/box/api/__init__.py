"""No-I/O orchestration layer shared by CLI and graphical front ends."""

from __future__ import annotations

from box.api.diagnose import DiagnoseResult
from box.api.inspect import Inspection
from box.api.interaction import ConsoleInteraction, Interaction
from box.config.models import AppConfig
from box.config.repository import ConfigRepository
from box.models import GameInfo, RuntimeInfo, RuntimeSpec

__all__ = [
    "AppConfig",
    "ConfigRepository",
    "ConsoleInteraction",
    "DiagnoseResult",
    "GameInfo",
    "Inspection",
    "Interaction",
    "RuntimeInfo",
    "RuntimeSpec",
]
