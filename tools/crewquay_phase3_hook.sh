#!/usr/bin/env bash
# Reference HATCH Hook, not installed/enabled by this repository.
# Hook: Light Tick + independent action compensation + fenced Muse wake.
# Requires prior host approval for credentialed polling and authorized writes.
# Fits the current ~60 s Hook script budget (25 s light + 25 s drain, plus
# shell/SQLite overhead); long readbacks may require another compensation turn.
set -euo pipefail
source "${HATCH_HOOK_RUNTIME:?host Hook runtime required}"

REPO="${CREWQUAY_REPO:?repo required}"
DB="${CREWQUAY_DB:?sqlite path required}"
CONFIG="${CREWQUAY_CONFIG:?config required}"
LOCK="${CREWQUAY_HOOK_LOCK:-${HOME}/hooks/state/crewquay-phase3-hook.lock}"
HOOK_OWNER="${CREWQUAY_HOOK_OWNER:-crewquay-hatch-hook}"
mkdir -p "$(dirname "$LOCK")"

# This is separate from the stable CLI .tick.lock: never hold the CLI lock
# around both CLI invocations. Each invocation releases it before returning.
exec 9>"$LOCK"
if flock -n 9; then
  # No raw stdout/stderr in Hook logs; external content or stack traces could
  # contain secrets. The CLI itself already stores structured run reports.
  TICK_EXIT=0
  (cd "$REPO" && timeout 25s python3 -m patrol \
    --config "$CONFIG" --db "$DB" tick --light >/dev/null 2>&1) || TICK_EXIT=$?
  log "phase3 light tick exit=${TICK_EXIT}"

  # Pure Python Executor, not a Muse worker. Only verified, authorized actions
  # can POST. An unknown remote result is readback-checked, never blindly resent.
  DRAIN_EXIT=0
  (cd "$REPO" && timeout 25s python3 -m patrol \
    --config "$CONFIG" --db "$DB" outbox-drain --once --lane any \
    --limit 3 >/dev/null 2>&1) || DRAIN_EXIT=$?
  log "phase3 executor compensation exit=${DRAIN_EXIT}"
else
  log "phase3 hook skipped: prior invocation still active"
  silent "previous Hook turn is still active" '{"status":"busy"}'
  exit 0
fi
exec 9>&-

# The ONLY wake dispatch source is the preexisting durable wakeup_requests.
# Lease a specific due intent BEFORE calling the external host's wake().
# No model is started here; the Muse worker separately runs schedule-next.
if [ ! -f "$DB" ]; then
  silent "sqlite unavailable" '{"status":"unavailable"}'
  exit 0
fi
PREPARED=""
if ! PREPARED="$(cd "$REPO" && timeout 8s python3 -m patrol \
    --config "$CONFIG" --db "$DB" host-wakeup-prepare \
    --owner "$HOOK_OWNER" 2>/dev/null)"; then
  silent "wakeup reservation unavailable" '{"status":"deferred"}'
  exit 0
fi
DECISION="$(printf '%s' "$PREPARED" | python3 -c \
  'import json,sys
d=json.load(sys.stdin)
print("wake" if d.get("should_wake") is True and
      d.get("status")=="reserved" and d.get("lease_version") else "silent")' \
  2>/dev/null)" || {
  silent "invalid wakeup handoff" '{"status":"invalid"}'
  exit 0
}
if [ "$DECISION" = "wake" ]; then
  # The host must explicitly acknowledge delivery with wakeup-ack and a
  # verifiable receipt. Return/exit from wake() is NOT proof of acceptance.
  # An unacknowledged lease expires to unknown, never blind redelivery.
  wake "durable CrewQuay decision work reserved" "$PREPARED"
else
  silent "no due unleased wakeup intent" '{"status":"idle"}'
fi
