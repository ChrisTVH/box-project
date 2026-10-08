"""Minimal type stubs for PyGObject (gi.repository).

The runtime comes from the system apt packages, which ship no types.
Each namespace box-gui uses is stubbed as a submodule returning Any
for every attribute, so gi calls are explicitly untyped instead of
silently Unknown while staying valid in type expressions.
"""
