#!/usr/bin/env bash

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=saksham-common.sh
source "$SCRIPT_DIR/saksham-common.sh"

stop_saksham() {
    local backend_python="$SAKSHAM_BACKEND_DIR/.venv/bin/python"
    local active

    saksham_info "Stopping launcher-owned Saksham services on macOS."
    if [[ "$(saksham_http_status "$SAKSHAM_BACKEND_URL/health" "" 5)" == "200" ]]; then
        active="$(saksham_active_meetings "$backend_python")"
        if [[ -n "$active" ]]; then
            saksham_error "Saksham cannot stop while meeting work is unfinished:"
            printf '%s\n' "$active" >&2
            saksham_error "Stop the recording and wait for processing to complete, then try again."
            return 1
        fi
    fi

    saksham_stop_process \
        "Saksham frontend" \
        "$SAKSHAM_FRONTEND_PID_FILE" \
        ".bin/vite" \
        "$SAKSHAM_FRONTEND_DIR" || return 1
    saksham_stop_process \
        "Saksham backend" \
        "$SAKSHAM_BACKEND_PID_FILE" \
        "-m uvicorn main:app" \
        "$SAKSHAM_BACKEND_DIR" || return 1
    saksham_info "Mac Saksham services are stopped. You can now use the Windows Stop shortcut."
}

if ! stop_saksham; then
    saksham_pause_on_error
    exit 1
fi
