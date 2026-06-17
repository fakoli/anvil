"""fakoli-state.planning — PRD template parser and rule-based scoring engine.

Public surface (lazy on the LLM path):
- ``template`` — deterministic markdown PRD parser; no LLM, no I/O.
- ``scoring``  — six-dimension rule-based scorer; no LLM, no I/O.
- ``llm``      — Phase 7 LLM provider abstraction (Protocol + Anthropic impl
  + Recorded test double).  Augmentation only — never required.

The ``llm`` submodule imports the ``anthropic`` SDK at module load.  To keep
the deterministic CLI path free of that import cost, ``llm`` is NOT eagerly
imported here.  Callers explicitly do ``from fakoli_state.planning.llm
import ...`` only when they need it (which the CLI helper does inside the
``--use-llm`` branch).
"""

from __future__ import annotations

import fakoli_state.planning.scoring as scoring
import fakoli_state.planning.template as template

__all__ = ["template", "scoring"]
