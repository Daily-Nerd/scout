"""scout: candidate finder for the Agent Memory Atlas."""

from __future__ import annotations

import importlib.metadata

try:
    __version__ = importlib.metadata.version("scout")
except importlib.metadata.PackageNotFoundError:  # pragma: no cover
    __version__ = "0.0.0"
