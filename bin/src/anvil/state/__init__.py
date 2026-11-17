"""anvil.state — entity models and pure state-machine transitions.

Public surface:
- ``models`` — Pydantic v2 models for every entity; import from here first.
- ``transitions`` — pure transition functions; they never do I/O.

The backend (sqlite.py) and schema (schema.py) are Wave 2 / welder's scope and
are intentionally absent from this re-export.

Both submodules load on first access, not at package import. The
file-inspection worker lives in this package and starts on every backend open;
importing Pydantic there would dominate its runtime.
"""

from __future__ import annotations

import importlib
from types import ModuleType

__all__ = ["models", "transitions"]


def __getattr__(name: str) -> ModuleType:
    if name in __all__:
        return importlib.import_module(f"{__name__}.{name}")
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
