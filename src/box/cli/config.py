"""Configuration command implementation."""

from __future__ import annotations

from pathlib import Path

from box.config.repository import ConfigRepository


def show(repository: ConfigRepository) -> int:
    """Print active configuration in a stable human-readable format."""
    config = repository.load()
    print(f"preferred_runtime: {config.preferred_runtime or '(none)'}")
    print(f"prefer_sdk: {str(config.prefer_sdk).lower()}")
    print("allowed_game_roots:")
    for root in config.allowed_game_roots:
        print(f"  - {root}")
    return 0


def set_value(repository: ConfigRepository, key: str, value: str) -> int:
    """Update one supported configuration value."""
    if key == "allowed-game-root":
        repository.add_allowed_root(Path(value))
    elif key == "preferred-runtime":
        repository.set_preferred_runtime(None if value == "none" else value)
    else:
        raise ValueError("supported keys: allowed-game-root, preferred-runtime")
    return show(repository)
