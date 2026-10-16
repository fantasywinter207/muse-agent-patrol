#!/usr/bin/env bash
# Reference Phase 3 HATCH Hook, not installed/enabled by this repository.
# Hook: Python Light Tick + independent action-queue compensation + Muse wake.
# Requires prior host approval for credentialed polling and authorized writes.
# Fits the current ~60 s Hook script budget (25 s light + 25 s drain, plus
# shell/SQLite overhead); long readbacks may require another compensation turn.
set -euo pipefail
source "${HATCH_HOOK_RUNTIME:?host Hook runtime required}"

REPO="${CREWQUAY_REPO:?repo required}"
DB="${CREWQUAY_DB:?sqlite path required}"
CONFIG="${CREWQUAY_CONFIG:?config required}"
LOCK="${CREWQUAY_HOOK_LOCK:-${HOME}/hooks/state/crewquay-phase3-hook.lock}"
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
    --config "$CONFIG" --db "$DB" outbox-drain --once --lane chat \
    --limit 3 >/dev/null 2>&1) || DRAIN_EXIT=$?
  log "phase3 executor compensation exit=${DRAIN_EXIT}"
else
  log "phase3 hook skipped: prior invocation still active"
fi
exec 9>&-

# Query AFTER the tick, not before: a new next_attempt_at might be newer
# than the Hook starting timestamp.
NOW="$(date +%s)"
if [ ! -f "$DB" ]; then
  silent "sqlite unavailable" '{"status":"unavailable"}'
  exit 0
fi

COUNT="$(sqlite3 -cmd ".timeout 5000" "file:${DB}?mode=ro" \
  "SELECT count(*) FROM wakeup_requests
   WHERE status IN ('pending','retry_wait')
   AND next_attempt_at <= ${NOW};" 2>/dev/null)" || {
  silent "wakeup sqlite read failed" '{"status":"read_failed"}'
  exit 0
}
if [[ "$COUNT" =~ ^[0-9]+$ ]] && [ "$COUNT" -gt 0 ]; then
  wake "authorized decision work pending" "{\"pending\":${COUNT}}"
else
  silent "no due decision work" '{"pending":0}'
fi
