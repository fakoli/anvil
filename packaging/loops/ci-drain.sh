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

# Concurrency identity (#103): give THIS drainer a distinct default actor so two
# drainers on one machine/user don't collapse to the SAME signing-key
# fingerprint — which would let a second drainer RENEW the first's lease instead
# of conflicting, defeating single-winner leasing. $$ is unique per process and
# every `anvil` call below inherits it, so they all resolve one consistent
# actor. Override by exporting ANVIL_ACTOR (a pinned name) or ANVIL_SESSION_ID
# yourself before running.
export ANVIL_SESSION_ID="${ANVIL_SESSION_ID:-drain-$$}"

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

	# --- per-task body: claim -> packet -> work -> capture proof -> submit -> apply ---

	# 1. Claim the recommended task (single-winner lease + file-conflict check).
	task="$(anvil next --json | sed -n 's/.*"id"[^"]*"\([^"]*\)".*/\1/p' | head -n1)"
	[ -n "$task" ] || break
	# Concurrency: another drainer may have claimed this task between `next` and
	# here. Skip to the next task instead of aborting the whole loop (`set -e`
	# would otherwise exit on a lost lease). `next` won't re-recommend a claimed
	# task, so this can't spin. The claim id and actor bind step 4's command
	# proof to this claim.
	claimed="$(anvil claim "$task" --json)" || continue
	claim="$(printf '%s\n' "$claimed" | sed -n 's/.*"claim": {"id": "\([^"]*\)".*/\1/p')"
	actor="$(printf '%s\n' "$claimed" | sed -n 's/.*"claimed_by": "\([^"]*\)".*/\1/p')"
	[ -n "$claim" ] && [ -n "$actor" ] || { echo "could not read the claim for $task" >&2; exit 1; }

	# 2. Fetch the work packet — the contract that teaches the steps,
	#    acceptance criteria, files in scope, and verification commands.
	anvil packet "$task"

	# 3. Do the work. Replace this with your executor (a codex/claude run, a
	#    script, etc.). It must implement the packet's acceptance criteria.
	#    e.g.  codex exec "Implement $task per .anvil/packets/$task.md"

	# 4. Run the packet's verification command and record its command proof.
	#    A bare shell has no hooks, so nothing captures the proof for you, and
	#    step 6 refuses approval without it. ANVIL_CLAIM_ID binds the proof to
	#    this claim; without it the capture still exits 0 but proves nothing.
	#    The command string must be the one the packet lists.
	code=0
	REPLACE_WITH_VERIFICATION_COMMAND > verify.log 2>&1 || code=$?
	ANVIL_CLAIM_ID="$claim" anvil hook capture-evidence \
		--command "REPLACE_WITH_VERIFICATION_COMMAND" \
		--exit-code "$code" --stdout-file verify.log --actor "$actor"

	# 5. Submit completion evidence: what you ran and what you changed. This
	#    releases the claim and moves the task to needs_review.
	anvil submit "$task" \
		--commands "REPLACE_WITH_VERIFICATION_COMMAND" \
		--files-changed "REPLACE_WITH_FILES_CHANGED"

	# 6. Apply the gate. `--strict` refuses --approve until every required check
	#    has a matching, passing command proof, so a step can't ship unverified.
	#    This approves the loop's own work; drop the line to leave tasks at
	#    needs_review for a person.
	anvil apply "$task" --approve --strict
done

# `anvil next -q` exited 3: the queue is drained. (A real error would have
# exited above with its own non-zero code.)
echo "queue drained"
