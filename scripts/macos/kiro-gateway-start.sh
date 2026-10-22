#!/bin/bash
# Start an existing local gateway and verify readiness without rebuilding it.
# Uses only Bash, curl, Python's standard library, and an installed Podman CLI.
set -u
umask 077
export PATH="/opt/podman/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:${PATH:-}"

MACHINE_NAME="${KIRO_GATEWAY_MACHINE:-podman-machine-default}"
CONNECTION_NAME="${KIRO_GATEWAY_CONNECTION:-podman-machine-default-root}"
CONTAINER_NAME="${KIRO_GATEWAY_CONTAINER:-kiro-gateway}"
HEALTH_URL="${KIRO_GATEWAY_HEALTH_URL:-http://127.0.0.1:8000/health}"
LOG_FILE="${KIRO_GATEWAY_START_LOG:-/tmp/kiro-gateway-start.log}"
LOCK_DIR="${KIRO_GATEWAY_START_LOCK:-$HOME/Library/Caches/kiro-gateway/start.lock}"
PODMAN_BIN="${KIRO_GATEWAY_PODMAN_BIN:-podman}"
CURL_BIN="${KIRO_GATEWAY_CURL_BIN:-/usr/bin/curl}"
PYTHON_BIN="${KIRO_GATEWAY_PYTHON_BIN:-python3}"
COMMAND_TIMEOUT="${KIRO_GATEWAY_COMMAND_TIMEOUT:-10}"
START_TIMEOUT="${KIRO_GATEWAY_START_TIMEOUT:-90}"
STARTUP_TIMEOUT="${KIRO_GATEWAY_STARTUP_TIMEOUT:-180}"
READY_ATTEMPTS="${KIRO_GATEWAY_READY_ATTEMPTS:-30}"
HEALTH_ATTEMPTS="${KIRO_GATEWAY_HEALTH_ATTEMPTS:-20}"
POLL_INTERVAL="${KIRO_GATEWAY_POLL_INTERVAL:-2}"

log() {
    printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >> "$LOG_FILE"
}

fail() {
    log "ERROR: $*"
    exit 1
}

# Check configuration before starting any processes or modifying the lock.
for value in "$COMMAND_TIMEOUT" "$START_TIMEOUT" "$STARTUP_TIMEOUT" "$READY_ATTEMPTS" "$HEALTH_ATTEMPTS"; do
    case "$value" in ''|*[!0-9]*) fail "Timeouts and attempt limits must be positive integers." ;; esac
    [[ "$value" -gt 0 ]] || fail "Timeouts and attempt limits must be positive integers."
done
case "$POLL_INTERVAL" in ''|*[!0-9]*) fail "The poll interval must be a nonnegative integer." ;; esac
command -v "$PYTHON_BIN" >/dev/null 2>&1 || fail "Python 3 is required for bounded startup checks."
command -v "$PODMAN_BIN" >/dev/null 2>&1 || fail "Podman was not found; check its installed path."
command -v "$CURL_BIN" >/dev/null 2>&1 || fail "curl was not found."

# The parent directory is private. Concurrent launchd/manual invocations share one lock.
mkdir -p "$(dirname "$LOCK_DIR")" || fail "Cannot create the startup lock directory."
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
    owner="$(cat "$LOCK_DIR/pid" 2>/dev/null || true)"
    case "$owner" in
        ''|*[!0-9]*) ;;
        0) owner="" ;;
        *)
            if kill -0 "$owner" 2>/dev/null; then
                log "Another startup check is already running."
                exit 0
            fi
            ;;
    esac
    # A second launcher can arrive before the first one finishes writing its PID.
    if [[ -z "$owner" || "$owner" == *[!0-9]* ]]; then
        if ! "$PYTHON_BIN" -c '
import pathlib
import sys
import time
try:
    age = time.time() - pathlib.Path(sys.argv[1]).stat().st_mtime
except OSError:
    sys.exit(1)
sys.exit(0 if age >= int(sys.argv[2]) else 1)
' "$LOCK_DIR" "$STARTUP_TIMEOUT"; then
            log "Another startup check is acquiring the lock."
            exit 0
        fi
    fi
    # Remove only our known lock files; never recursively delete an unexpected directory.
    rm -f "$LOCK_DIR/pid"
    rmdir "$LOCK_DIR" 2>/dev/null || fail "Cannot clear the stale startup lock."
    mkdir "$LOCK_DIR" 2>/dev/null || fail "Another startup check acquired the lock."
fi
printf '%s\n' "$$" > "$LOCK_DIR/pid"
cleanup() {
    rm -f "$LOCK_DIR/pid"
    rmdir "$LOCK_DIR" 2>/dev/null || true
}
trap cleanup EXIT
trap 'exit 1' HUP INT TERM
DEADLINE=$((SECONDS + STARTUP_TIMEOUT))

run_bounded() {
    local limit="$1" remaining
    shift
    remaining=$((DEADLINE - SECONDS))
    [[ "$remaining" -gt 0 ]] || return 124
    [[ "$limit" -le "$remaining" ]] || limit="$remaining"
    "$PYTHON_BIN" -c '
import subprocess
import sys
try:
    result = subprocess.run(sys.argv[2:], timeout=int(sys.argv[1]), check=False)
except subprocess.TimeoutExpired:
    sys.exit(124)
except OSError:
    sys.exit(127)
else:
    sys.exit(result.returncode)
' "$limit" "$@"
}

podman_check() {
    run_bounded "$COMMAND_TIMEOUT" "$PODMAN_BIN" --connection "$CONNECTION_NAME" "$@"
}

healthy() {
    local response
    response="$(run_bounded "$COMMAND_TIMEOUT" "$CURL_BIN" --noproxy '*' \
        --fail --silent --show-error --connect-timeout 1 --max-time 3 \
        --max-filesize 16384 "$HEALTH_URL" 2>/dev/null)" || return 1
    printf '%s' "$response" | "$PYTHON_BIN" -c '
import json
import sys
try:
    payload = json.load(sys.stdin)
except (ValueError, UnicodeError):
    sys.exit(1)
sys.exit(0 if isinstance(payload, dict) and payload.get("status") == "healthy" else 1)
'
}

# Healthy periodic checks are quiet and do not restart running workloads.
if healthy; then
    exit 0
fi
log "Starting gateway readiness check (machine=$MACHINE_NAME, container=$CONTAINER_NAME)."
state="$(run_bounded "$COMMAND_TIMEOUT" "$PODMAN_BIN" machine inspect "$MACHINE_NAME" --format '{{.State}}' 2>/dev/null)" \
    || fail "Cannot inspect machine $MACHINE_NAME. Check podman machine list."
case "$state" in
    running|starting) log "Podman machine is $state; waiting for its connection." ;;
    stopped)
        log "Starting Podman machine $MACHINE_NAME."
        # Podman Desktop may race this startup; readiness below is the final check.
        if ! run_bounded "$START_TIMEOUT" "$PODMAN_BIN" machine start "$MACHINE_NAME" >> "$LOG_FILE" 2>&1; then
            log "Machine start did not finish successfully; checking whether another launcher started it."
        fi
        ;;
    *) fail "Unexpected machine state: $state. Check Podman Desktop." ;;
esac

ready=false
for ((attempt = 0; attempt < READY_ATTEMPTS && SECONDS < DEADLINE; attempt++)); do
    if podman_check info --format '{{.Host.Hostname}}' >/dev/null 2>&1; then
        ready=true
        break
    fi
    sleep "$POLL_INTERVAL"
done
[[ "$ready" == true ]] || fail "Podman connection $CONNECTION_NAME is not ready; launchd will retry."
podman_check container exists "$CONTAINER_NAME" >/dev/null 2>&1 \
    || fail "Container $CONTAINER_NAME was not found. Restore the existing gateway container before retrying."
running="$(podman_check inspect --format '{{.State.Running}}' "$CONTAINER_NAME" 2>/dev/null)" \
    || fail "Cannot inspect container $CONTAINER_NAME."
if [[ "$running" != true ]]; then
    log "Starting existing container $CONTAINER_NAME."
    podman_check start "$CONTAINER_NAME" >> "$LOG_FILE" 2>&1 \
        || fail "Container startup failed; inspect podman logs $CONTAINER_NAME."
fi

for ((attempt = 0; attempt < HEALTH_ATTEMPTS && SECONDS < DEADLINE; attempt++)); do
    if healthy; then
        log "Gateway is healthy. Startup completed successfully."
        exit 0
    fi
    sleep "$POLL_INTERVAL"
done
fail "Gateway did not become healthy at $HEALTH_URL; launchd will retry."
