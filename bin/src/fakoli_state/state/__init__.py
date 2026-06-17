"""fakoli-state.state — entity models and pure state-machine transitions.

Public surface:
- ``models`` — Pydantic v2 models for every entity; import from here first.
- ``transitions`` — pure transition functions; they never do I/O.

The backend (sqlite.py) and schema (schema.py) are Wave 2 / welder's scope and
are intentionally absent from this re-export.
"""

from __future__ import annotations

from fakoli_state.state import models, transitions

__all__ = ["models", "transitions"]
