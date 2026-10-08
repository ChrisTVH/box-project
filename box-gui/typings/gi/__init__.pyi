"""Minimal stub for gi (PyGObject introspection entry point).

The runtime comes from the system apt packages, which ship no type
information. Only require_version() is used by box-gui, so this is all
the static checker may assume here; see repository/__init__.pyi.
"""


def require_version(namespace: str, version: str) -> None: ...
