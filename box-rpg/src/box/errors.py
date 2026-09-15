"""Domain errors presented by the command-line interface."""


class BoxError(Exception):
    """Base error for an expected box-rpg failure."""


class ConfigurationError(BoxError):
    """Raised when configuration is missing or invalid."""


class GameValidationError(BoxError):
    """Raised when a game directory is unsupported or disallowed."""


class RuntimeError(BoxError):
    """Raised when an NW.js runtime cannot be managed or used."""


class LaunchError(BoxError):
    """Raised when a launch session cannot be created or started."""
