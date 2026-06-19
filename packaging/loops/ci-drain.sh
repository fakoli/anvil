#!/bin/sh
# ci-drain.sh — drain the Anvil ready queue until empty.
#
# Documented skeleton. The loop CONDITION is the seam; the loop BODY is the
# governed per-task flow. Adapt the body to your harness, keep the condition.
#
# THE SEAM — `anvil next -q`:
#   exit 0  a task is ready (the loop runs the body)
#   exit 3  the queue is empty (the loop stops cleanly; this is success)
#   exit !0 (other) a real error (state dir missing, backend broken) — PROPAGATED
#
# Anvil's durable, leased state makes this safe to run as cron/CI and safe to
# run CONCURRENTLY: single-winner leases mean two drainers never claim the same
# task, and evidence gating means a step cannot fake "done".
set -eu

# Drain until the queue is empty (exit 3), but PROPAGATE any real error instead
# of masking it as a clean drain. Using `if` (not a bare `while cmd`) is what
# lets us tell exit 3 apart from exit 1/2: a bare `while` would stop on BOTH and
# then report success. The `if` condition also keeps `set -e` from tripping.
while true; do
	if anvil next -q; then
		:                          # a task is ready -> run the body below
	else
		rc=$?
		[ "$rc" -eq 3 ] && break   # empty queue -> clean stop (success)
		exit "$rc"                 # real error -> propagate, do NOT mask as drained
	fi

	# --- per-task body: claim -> packet -> work -> submit --evidence -> apply ---

	# 1. Claim the recommended task (single-winner lease + file-conflict check).
	task="$(anvil next --json | sed -n 's/.*"id"[^"]*"\([^"]*\)".*/\1/p' | head -n1)"
	[ -n "$task" ] || break
	anvil claim "$task"

	# 2. Fetch the work packet — the contract that teaches the steps,
	#    acceptance criteria, files in scope, and verification commands.
	anvil packet "$task"

	# 3. Do the work. Replace this with your executor (a codex/claude run, a
	#    script, etc.). It must implement the packet's acceptance criteria.
	#    e.g.  codex exec "Implement $task per .anvil/packets/$task.md"

	# 4. Submit completion evidence (the typed proof gate). `--commands` and
	#    `--files-changed` ARE the evidence; this auto-releases the claim and
	#    moves the task to needs_review.
	anvil submit "$task" \
		--commands "REPLACE_WITH_VERIFICATION_COMMANDS" \
		--files-changed "REPLACE_WITH_FILES_CHANGED"

	# 5. Apply the gate. `--strict` refuses --approve when required evidence is
	#    missing, so a step can't ship unverified.
	anvil apply "$task" --approve --strict
done

# `anvil next -q` exited 3: the queue is drained. (A real error would have
# exited above with its own non-zero code.)
echo "queue drained"
