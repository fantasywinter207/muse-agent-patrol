"""Retired JSON helper boundary. No whole-state writer is exposed in v3.

Use patrol.index and component index boundaries. Historical source remains in Git.
Legacy clients calling common.save_state fail closed rather than overwrite cursors.
"""
from patrol.index import main

__all__ = ["main"]
