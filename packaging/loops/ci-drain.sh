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

# Anvil's JSON is read with a JSON parser rather than text matching: an actor
# may contain characters JSON escapes, and the proof below binds to the decoded
# value. python3 when it is on PATH, otherwise the interpreter uv manages (uv is
# Anvil's one prerequisite). Resolved HERE, before any claim, so a missing
# interpreter stops the drainer cleanly instead of stranding a claimed task.
if command -v python3 > /dev/null 2>&1; then
	json_python() { python3 "$@"; }
elif command -v uv > /dev/null 2>&1; then
	json_python() { uv run --quiet --no-project python "$@"; }
else
	echo "ci-drain.sh needs python3 or uv on PATH to read Anvil's JSON output" >&2
	exit 1
fi

# Read one field of the claim that `anvil claim --json` returned.
claim_field() {
	printf '%s' "$claimed" | json_python -c 'import json, sys; print(json.load(sys.stdin)["data"]["claim"][sys.argv[1]])' "$1"
}

# Run ONE verification command and record its command proof against the claim.
# Call it once for EVERY verification command the packet lists, with the command
# string exactly as the packet gives it: the gate wants a matching, passing
# proof for each. A bare shell has no hooks, so nothing else captures the proof.
# ANVIL_CLAIM_ID binds it to this claim; without it the capture still exits 0
# but proves nothing. The output goes to its own temporary file, so concurrent
# drainers in one checkout never read each other's.
# A failing command stops the drainer HERE, before `submit` would release the
# claim: the failed proof is kept, and the task stays claimed until the lease
# expires or you `anvil release` it, instead of landing in needs_review broken.
verify() {
	log="$(mktemp)"
	code=0
	sh -c "$1" > "$log" 2>&1 || code=$?
	ANVIL_CLAIM_ID="$claim" anvil hook capture-evidence \
		--command "$1" --exit-code "$code" --stdout-file "$log" --actor "$actor"
	rm -f "$log"
	if [ "$code" -ne 0 ]; then
		echo "verification failed (exit $code) for $task, claim $claim still held: $1" >&2
		exit "$code"
	fi
}

# Stop unless every command proof the task requires was really recorded under
# this claim. `capture-evidence` exits 0 whatever happens: with capture switched
# off in config.yaml it records nothing, and a capture that does not match the
# claim lands in the orphan buffer. Its exit code proves neither. The preflight
# reads what was recorded and lists each required proof with `satisfied`. Checked
# before `submit` for the same reason as above: the claim is still held, so the
# task can be fixed and re-run rather than left in needs_review without proof.
require_proofs() {
	preflight="$(anvil evidence-preflight "$task" --json)" || {
		printf '%s\n' "$preflight" >&2
		exit 1
	}
	missing="$(printf '%s' "$preflight" | json_python -c 'import json, sys; print("\n".join(p["command"] for p in json.load(sys.stdin)["data"]["required_command_proofs"] if not p["satisfied"]))')"
	if [ -n "$missing" ]; then
		echo "no passing command proof under claim $claim for $task, claim still held; not submitting:" >&2
		printf '%s\n' "$missing" >&2
		exit 1
	fi
}

# Drain until the queue is empty (exit 3), but PROPAGATE any real error instead
# of masking it as a clean drain. Using `if` (not a bare `while cmd`) is what
# lets us tell exit 3 apart from exit 1/2: a bare `while` would stop on BOTH and
# then report success. The `if` condition also keeps `set -e` from tripping.
refused=""   # the task whose claim was refused on the previous pass, if any
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
	# would otherwise exit on a lost lease). `next` won't re-recommend a task
	# someone else holds, so a lost race cannot spin. Every other refusal (an
	# unapproved PRD, an actor mismatch) repeats on each pass: `next` hands the
	# same task straight back. Stop when that happens instead of looping forever.
	# In --json mode the refusal is printed on stdout, so show it here; nothing
	# else will. The claim id and owner bind step 4's command proofs to this claim.
	if claimed="$(anvil claim "$task" --json)"; then
		refused=""
	else
		printf '%s\n' "$claimed" >&2
		if [ "$task" = "$refused" ]; then
			echo "claim refused twice in a row for $task; stopping" >&2
			exit 1
		fi
		refused="$task"
		continue
	fi
	claim="$(claim_field id)"
	actor="$(claim_field claimed_by)"

	# 2. Fetch the work packet — the contract that teaches the steps,
	#    acceptance criteria, files in scope, and verification commands.
	anvil packet "$task"

	# 3. Do the work. Replace this with your executor (a codex/claude run, a
	#    script, etc.). It must implement the packet's acceptance criteria.
	#    e.g.  codex exec "Implement $task per .anvil/packets/$task.md"

	# 4. Run every verification command the packet lists and record a command
	#    proof for each: one `verify` line per command. Step 6 refuses approval
	#    while any of them is missing, so `require_proofs` checks that they all
	#    landed while the claim is still held.
	verify "REPLACE_WITH_VERIFICATION_COMMAND"
	# verify "REPLACE_WITH_ANOTHER_VERIFICATION_COMMAND"
	require_proofs

	# 5. Submit completion evidence: what you ran (one --commands per command
	#    verified above) and what you changed. This releases the claim and moves
	#    the task to needs_review.
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
