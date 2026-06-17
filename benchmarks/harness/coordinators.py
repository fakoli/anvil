"""The two coordination arms. This is the ONLY thing that differs between runs.

Both arms drive the identical actor loop over the identical task set and the identical
"work" function. Any difference in collisions / duplicates / evidence is therefore
attributable solely to the coordination layer — the control discipline that makes the
result an argument rather than a demo.

Arm A (MarkdownCoordinator): naive shared-TODO coordination. Pick an unchecked task,
do it, tick the box. Non-atomic read-modify-write => real races.

Arm B (FakoliStateCoordinator): the real fakoli-state engine. `next` then atomic
`claim` (SQLite BEGIN IMMEDIATE) => exclusive ownership; file-overlap blocks at claim
time; completion carries a structured, gate-checked evidence record.
"""
from __future__ import annotations

import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

from . import engine
from .engine import Project, TaskSpec

# ponytail: instrumentation log is locked so OUR measurement can't be the thing that
# races; the system-under-test's own artifacts (workspace files, TODO.md) are left
# unlocked on purpose — that's what we're measuring.
_LOG_LOCK = threading.Lock()


@dataclass
class WorkLog:
    """Append-only instrumentation. One line per file-write and per task-completion."""

    path: Path

    def write_event(self, actor: str, task: str, kind: str, target: str = "",
                    extra: str = "") -> None:
        line = f"{time.time():.6f}\t{actor}\t{task}\t{kind}\t{target}\t{extra}\n"
        with _LOG_LOCK:
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(line)

    def rows(self) -> list[tuple[float, str, str, str, str, str]]:
        if not self.path.exists():
            return []
        out = []
        for ln in self.path.read_text(encoding="utf-8").splitlines():
            parts = ln.split("\t")
            if len(parts) == 6:
                ts, actor, task, kind, target, extra = parts
                out.append((float(ts), actor, task, kind, target, extra))
        return out


def do_work(proj: Project, log: WorkLog, actor: str, task: TaskSpec,
            jitter: float) -> None:
    """The actual 'work': append this actor's mark to each target file.

    Unlocked on purpose. If two actors run the same task (or two tasks sharing a file)
    concurrently, both append => the file shows >1 distinct actor == a collision.
    The jitter widens the window so races are observable, seeded for reproducibility.
    """
    for rel in task.files:
        fpath = proj.root / rel
        fpath.parent.mkdir(parents=True, exist_ok=True)
        t0 = time.time()
        existing = fpath.read_text(encoding="utf-8") if fpath.exists() else ""
        time.sleep(jitter)  # race window between read and write
        fpath.write_text(existing + f"{task.id} by {actor}\n", encoding="utf-8")
        # record the [start, end] interval so the oracle can detect *concurrent*
        # writes (a real race) vs. sequential writes by different actors (correct).
        log.write_event(actor, task.id, "write", rel, f"{t0:.6f}:{time.time():.6f}")
    log.write_event(actor, task.id, "done")


class Coordinator(ABC):
    name: str

    @abstractmethod
    def acquire(self, actor: str, rng) -> str | None:
        """Try to take ownership of a task. Return its id, or None if none available
        right now (caller backs off and retries until `finished`)."""

    @abstractmethod
    def complete(self, actor: str, task: TaskSpec, gamed: bool) -> bool:
        """Mark a task complete. Returns whether a valid (gate-passing) evidence
        record exists for it (always False for the markdown arm — no evidence concept)."""

    @abstractmethod
    def finished(self) -> bool:
        """True when no more work can ever be acquired."""

    def task(self, task_id: str) -> TaskSpec:
        return self._by_id[task_id]


# --- Arm A: naive markdown coordination -------------------------------------

class MarkdownCoordinator(Coordinator):
    name = "markdown"

    def __init__(self, proj: Project, race_window: float = 0.01):
        self.proj = proj
        self._by_id = {t.id: t for t in proj.tasks}
        self.race_window = race_window
        self.todo = proj.root / "TODO.md"
        self.todo.write_text(
            "\n".join(f"- [ ] {t.id} {t.title}" for t in proj.tasks) + "\n",
            encoding="utf-8",
        )

    def _unchecked(self) -> list[str]:
        ids = []
        for ln in self.todo.read_text(encoding="utf-8").splitlines():
            ln = ln.strip()
            if ln.startswith("- [ ] "):
                ids.append(ln.split()[3])
        return ids

    def acquire(self, actor: str, rng) -> str | None:
        # No atomic reservation: read, (race window), return a pick. Another actor
        # reading concurrently sees the same task unchecked and picks it too.
        ids = self._unchecked()
        if not ids:
            return None
        pick = ids[rng.randrange(len(ids))]
        time.sleep(self.race_window)   # window during which a peer can pick `pick` too
        return pick

    def complete(self, actor: str, task: TaskSpec, gamed: bool) -> bool:
        # Non-atomic read-modify-write of TODO.md: concurrent writers drop each other's
        # checkmarks (lost updates). No evidence is recorded at all.
        text = self.todo.read_text(encoding="utf-8")
        time.sleep(self.race_window)
        text = text.replace(f"- [ ] {task.id} ", f"- [x] {task.id} ")
        self.todo.write_text(text, encoding="utf-8")
        return None  # markdown has NO evidence record at all (None != False)

    def finished(self) -> bool:
        return not self._unchecked()


# --- Arm B: the real fakoli-state engine ------------------------------------

class FakoliStateCoordinator(Coordinator):
    name = "fakoli-state"

    # A non-test command: the durable evidence record will visibly lack real
    # verification, which is what makes gamed work auditable after the fact.
    GAMED_COMMAND = "echo done"

    def __init__(self, proj: Project):
        self.proj = proj
        self._by_id = {t.id: t for t in proj.tasks}

    def acquire(self, actor: str, rng) -> str | None:
        # `next` suggests a claimable task (respects deps + skips claimed/overlapping).
        r = engine.run(["next"], self.proj.root, actor=actor)
        task_id = _parse_next(r.out)
        if task_id is None:
            return None
        # Atomic claim. No --force, so a file-overlap or a lost race ERRORS, and we
        # return None to back off — exactly the safety the benchmark is measuring.
        c = engine.run(["claim", task_id], self.proj.root, actor=actor)
        if not c.ok:
            return None
        return task_id

    def complete(self, actor: str, task: TaskSpec, gamed: bool) -> bool:
        # files non-empty either way (an empty --files-changed is rejected by submit);
        # the gamed tell is the absence of a real verification command in the record.
        files = ",".join(task.files)
        cmds = self.GAMED_COMMAND if gamed else ",".join(task.verification)
        engine.run(
            ["submit", task.id, "--commands", cmds, "--files-changed", files],
            self.proj.root, actor=actor,
        )
        # Auto-approve so the task reaches `done`; a real reviewer would weigh the gate.
        engine.run(["apply", task.id, "--approve", "--reviewer", "bench"],
                   self.proj.root)
        # A durable evidence record now exists for this task. It is "valid" only if it
        # carries real verification; a gamed record exists but visibly lacks it.
        return not gamed

    def finished(self) -> bool:
        status = engine.task_status(self.proj)
        terminal = {"done", "accepted", "rejected"}
        # Finished when nothing is still workable. A still-claimed-but-crashed task is
        # NOT finished: its lease must expire and be reclaimed first.
        return all(s in terminal for s in status.values())


def _parse_next(out: str) -> str | None:
    for ln in out.splitlines():
        ln = ln.strip()
        if ln.startswith("Next recommended task:"):
            return ln.split(":", 1)[1].strip()
    return None
