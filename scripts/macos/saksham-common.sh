#!/usr/bin/env bash

# Shared helpers for the clickable macOS Saksham launchers.

set -uo pipefail

SAKSHAM_MACOS_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SAKSHAM_REPO_ROOT="${SAKSHAM_REPO_ROOT:-$(cd "$SAKSHAM_MACOS_SCRIPT_DIR/../.." && pwd)}"
SAKSHAM_BACKEND_DIR="$SAKSHAM_REPO_ROOT/backend"
SAKSHAM_FRONTEND_DIR="$SAKSHAM_REPO_ROOT/frontend"
SAKSHAM_RUNTIME_DIR="${SAKSHAM_RUNTIME_DIR:-$HOME/.saksham/run}"
SAKSHAM_LOG_DIR="${SAKSHAM_LOG_DIR:-$HOME/.saksham/logs}"
SAKSHAM_LAUNCHER_LOG="$SAKSHAM_LOG_DIR/launcher-macos.log"
SAKSHAM_BACKEND_LOG="$SAKSHAM_LOG_DIR/backend.log"
SAKSHAM_FRONTEND_LOG="$SAKSHAM_LOG_DIR/frontend.log"
SAKSHAM_BACKEND_PID_FILE="$SAKSHAM_RUNTIME_DIR/backend.pid"
SAKSHAM_FRONTEND_PID_FILE="$SAKSHAM_RUNTIME_DIR/frontend.pid"
SAKSHAM_BACKEND_URL="http://127.0.0.1:8420"
SAKSHAM_FRONTEND_URL="http://127.0.0.1:5173"

mkdir -p "$SAKSHAM_RUNTIME_DIR" "$SAKSHAM_LOG_DIR"

saksham_timestamp() {
    date '+%Y-%m-%d %H:%M:%S'
}

saksham_log() {
    local level="$1"
    shift
    local line="[$(saksham_timestamp)] [$level] $*"
    printf '%s\n' "$line"
    printf '%s\n' "$line" >> "$SAKSHAM_LAUNCHER_LOG"
}

saksham_info() {
    saksham_log INFO "$@"
}

saksham_warn() {
    saksham_log WARN "$@" >&2
}

saksham_error() {
    saksham_log ERROR "$@" >&2
}

saksham_pause_on_error() {
    if [[ -t 0 && "${SAKSHAM_NO_PAUSE:-0}" != "1" ]]; then
        printf '\nPress Return to close this window. '
        read -r _
    fi
}

saksham_require_file() {
    local path="$1"
    local recovery="$2"
    if [[ ! -f "$path" ]]; then
        saksham_error "Required file is missing: $path"
        saksham_error "$recovery"
        return 1
    fi
}

saksham_require_executable() {
    local path="$1"
    local recovery="$2"
    if [[ ! -x "$path" ]]; then
        saksham_error "Required executable is missing: $path"
        saksham_error "$recovery"
        return 1
    fi
}

saksham_dotenv_value() {
    local file="$1"
    local key="$2"
    local value
    value="$(sed -n "s/^${key}=//p" "$file" | tail -n 1 | tr -d '\r')"
    if [[ ${#value} -ge 2 ]]; then
        case "$value" in
            \"*\") value="${value#\"}"; value="${value%\"}" ;;
            \'*\') value="${value#\'}"; value="${value%\'}" ;;
        esac
    fi
    printf '%s' "$value"
}

saksham_http_status() {
    local url="$1"
    local bearer="${2:-}"
    local timeout="${3:-5}"
    if [[ -n "$bearer" ]]; then
        # Feed credentials over stdin so they never appear in process arguments or logs.
        printf 'silent\nshow-error\noutput = "/dev/null"\nwrite-out = "%%{http_code}"\nmax-time = %s\nheader = "Authorization: Bearer %s"\nurl = "%s"\n' \
            "$timeout" "$bearer" "$url" | curl --config - 2>/dev/null || true
    else
        curl -sS -o /dev/null -w '%{http_code}' --max-time "$timeout" "$url" 2>/dev/null || true
    fi
}

saksham_wait_for_http() {
    local label="$1"
    local url="$2"
    local expected="$3"
    local timeout="$4"
    local bearer="${5:-}"
    local started="$SECONDS"
    local status
    while (( SECONDS - started < timeout )); do
        status="$(saksham_http_status "$url" "$bearer" 5)"
        if [[ "$status" == "$expected" ]]; then
            saksham_info "$label is ready."
            return 0
        fi
        sleep 2
    done
    saksham_error "$label did not become ready at $url within ${timeout}s."
    return 1
}

saksham_listener_pid() {
    local port="$1"
    lsof -tiTCP:"$port" -sTCP:LISTEN 2>/dev/null | head -n 1
}

saksham_pid_command() {
    local pid="$1"
    ps -p "$pid" -o command= 2>/dev/null || true
}

saksham_pid_cwd() {
    local pid="$1"
    lsof -a -p "$pid" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p' | head -n 1
}

saksham_parent_pid() {
    local pid="$1"
    ps -p "$pid" -o ppid= 2>/dev/null | tr -cd '0-9'
}

saksham_adopt_listener() {
    local name="$1"
    local port="$2"
    local pid_file="$3"
    local marker="$4"
    local expected_cwd="$5"
    local pid command cwd parent attempts owned_pid

    if [[ -f "$pid_file" ]]; then
        if saksham_managed_pid "$pid_file" "$marker" "$expected_cwd" >/dev/null; then
            return 0
        fi
        saksham_warn "Replacing a stale $name PID record."
        rm -f "$pid_file"
    fi
    pid="$(saksham_listener_pid "$port")"
    attempts=0
    owned_pid=""
    while [[ -n "$pid" && "$pid" != "1" && $attempts -lt 4 ]]; do
        command="$(saksham_pid_command "$pid")"
        cwd="$(saksham_pid_cwd "$pid")"
        if [[ "$command" == *"$marker"* && "$cwd" == "$expected_cwd" ]]; then
            owned_pid="$pid"
        fi
        parent="$(saksham_parent_pid "$pid")"
        [[ -n "$parent" && "$parent" != "$pid" ]] || break
        pid="$parent"
        attempts=$((attempts + 1))
    done
    if [[ -n "$owned_pid" ]]; then
        printf '%s\n' "$owned_pid" > "$pid_file"
        saksham_info "Adopted the existing $name process (PID $owned_pid)."
        return 0
    fi
    saksham_warn "$name is healthy but was not started by this launcher; Stop Saksham will leave it running."
}

saksham_managed_pid() {
    local pid_file="$1"
    local marker="$2"
    local expected_cwd="$3"
    local pid command cwd
    [[ -f "$pid_file" ]] || return 1
    pid="$(tr -cd '0-9' < "$pid_file")"
    [[ -n "$pid" ]] || return 1
    kill -0 "$pid" 2>/dev/null || return 1
    command="$(saksham_pid_command "$pid")"
    cwd="$(saksham_pid_cwd "$pid")"
    [[ "$command" == *"$marker"* && "$cwd" == "$expected_cwd" ]] || return 1
    printf '%s' "$pid"
}

saksham_start_process() {
    local name="$1"
    local pid_file="$2"
    local working_dir="$3"
    local service_log="$4"
    shift 4
    local pid

    (
        cd "$working_dir" || exit 1
        nohup "$@" >> "$service_log" 2>&1 < /dev/null &
        pid=$!
        printf '%s\n' "$pid" > "${pid_file}.tmp"
        mv "${pid_file}.tmp" "$pid_file"
    ) || {
        saksham_error "Could not launch $name."
        return 1
    }
    saksham_info "Started $name; logs: $service_log"
}

saksham_stop_process() {
    local name="$1"
    local pid_file="$2"
    local marker="$3"
    local expected_cwd="$4"
    local pid started

    if [[ ! -f "$pid_file" ]]; then
        saksham_info "$name was not started by the Saksham launcher."
        return 0
    fi
    pid="$(saksham_managed_pid "$pid_file" "$marker" "$expected_cwd" || true)"
    if [[ -z "$pid" ]]; then
        saksham_warn "Refusing to stop $name because its PID is stale or belongs to another command."
        rm -f "$pid_file"
        return 1
    fi

    kill -TERM "$pid" 2>/dev/null || true
    started="$SECONDS"
    while kill -0 "$pid" 2>/dev/null && (( SECONDS - started < 20 )); do
        sleep 1
    done
    if kill -0 "$pid" 2>/dev/null; then
        saksham_warn "$name did not stop gracefully; terminating launcher-owned PID $pid."
        kill -KILL "$pid" 2>/dev/null || true
    fi
    rm -f "$pid_file"
    saksham_info "$name stopped."
}

saksham_parse_active_meetings() {
    local backend_python="$1"
    "$backend_python" -c '
import json, sys
try:
    meetings = json.load(sys.stdin).get("meetings", [])
except Exception:
    meetings = []
for meeting in meetings:
    state = str(meeting.get("capture_state", ""))
    if state in {"recording", "stopping", "processing"}:
        title = meeting.get("title", "Untitled meeting")
        print(f"{state}: {title}")
' 2>/dev/null || true
}

saksham_active_meetings() {
    local backend_python="$1"
    local payload
    payload="$(curl -sS --max-time 8 "$SAKSHAM_BACKEND_URL/api/meetings?limit=100" 2>/dev/null || true)"
    [[ -n "$payload" ]] || return 0
    printf '%s' "$payload" | saksham_parse_active_meetings "$backend_python"
}

saksham_backend_remote_ready() {
    local backend_python="$1"
    local payload
    payload="$(curl -sS --max-time 10 "$SAKSHAM_BACKEND_URL/health" 2>/dev/null || true)"
    [[ -n "$payload" ]] || return 1
    printf '%s' "$payload" | "$backend_python" -c '
import json, sys
try:
    health = json.load(sys.stdin)
except Exception:
    raise SystemExit(1)
meeting = health.get("meeting_intelligence", {})
raise SystemExit(0 if health.get("status") == "healthy" and meeting.get("available") else 1)
' >/dev/null 2>&1
}
