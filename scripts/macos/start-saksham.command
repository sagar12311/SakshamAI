#!/usr/bin/env bash

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=saksham-common.sh
source "$SCRIPT_DIR/saksham-common.sh"

start_saksham() {
    local backend_env="$SAKSHAM_BACKEND_DIR/.env"
    local backend_python="$SAKSHAM_BACKEND_DIR/.venv/bin/python"
    local frontend_vite="$SAKSHAM_FRONTEND_DIR/node_modules/.bin/vite"
    local remote_worker_url worker_key llm_base_url llm_key llm_models_url chatterbox_url
    local status listener

    saksham_info "Starting Saksham on macOS."
    saksham_require_file "$backend_env" "Configure backend/.env before using the launcher." || return 1
    saksham_require_executable "$backend_python" "Install the backend virtual environment first." || return 1
    saksham_require_executable "$frontend_vite" "Run npm install in the frontend directory first." || return 1

    remote_worker_url="$(saksham_dotenv_value "$backend_env" MEETING_INTELLIGENCE_REMOTE_URL)"
    worker_key="$(saksham_dotenv_value "$backend_env" MEETING_INTELLIGENCE_API_KEY)"
    llm_base_url="$(saksham_dotenv_value "$backend_env" LLM_BASE_URL)"
    llm_key="$(saksham_dotenv_value "$backend_env" LLM_API_KEY)"
    chatterbox_url="$(saksham_dotenv_value "$backend_env" CHATTERBOX_TTS_URL)"

    if [[ -z "$remote_worker_url" || -z "$worker_key" ]]; then
        saksham_error "Remote Meeting Intelligence URL or API key is missing from backend/.env."
        return 1
    fi
    if [[ -z "$llm_base_url" ]]; then
        saksham_error "LLM_BASE_URL is missing from backend/.env."
        return 1
    fi
    llm_models_url="${llm_base_url%/}/models"

    if [[ -d /Applications/Tailscale.app ]]; then
        open -gja Tailscale >/dev/null 2>&1 || true
    fi
    if ! saksham_wait_for_http \
        "RTX Meeting Intelligence" \
        "${remote_worker_url%/}/health" \
        200 \
        10 \
        "$worker_key"; then
        saksham_warn "RTX Meeting Intelligence is unavailable; starting with the Mac fallback."
    fi
    saksham_wait_for_http "LM Studio" "$llm_models_url" 200 90 "$llm_key" || {
        saksham_error "The PC LLM endpoint is unavailable. Run the Windows server launcher."
        return 1
    }

    status="$(saksham_http_status "$SAKSHAM_BACKEND_URL/health" "" 5)"
    if [[ "$status" == "200" ]]; then
        saksham_info "Reusing the healthy Saksham backend on port 8420."
        saksham_adopt_listener \
            "Saksham backend" \
            8420 \
            "$SAKSHAM_BACKEND_PID_FILE" \
            "-m uvicorn main:app" \
            "$SAKSHAM_BACKEND_DIR"
    else
        listener="$(saksham_listener_pid 8420)"
        if [[ -n "$listener" ]]; then
            saksham_error "Port 8420 is occupied by an unhealthy process (PID $listener)."
            saksham_error "Stop that process before trying the launcher again."
            return 1
        fi
        saksham_start_process \
            "Saksham backend" \
            "$SAKSHAM_BACKEND_PID_FILE" \
            "$SAKSHAM_BACKEND_DIR" \
            "$SAKSHAM_BACKEND_LOG" \
            "$backend_python" -m uvicorn main:app --reload --host 127.0.0.1 --port 8420 || return 1
        saksham_wait_for_http "Saksham backend" "$SAKSHAM_BACKEND_URL/health" 200 210 || {
            saksham_stop_process \
                "Saksham backend" \
                "$SAKSHAM_BACKEND_PID_FILE" \
                "-m uvicorn main:app" \
                "$SAKSHAM_BACKEND_DIR" || true
            saksham_error "Inspect $SAKSHAM_BACKEND_LOG"
            return 1
        }
    fi

    if ! saksham_backend_remote_ready "$backend_python"; then
        saksham_error "The backend started, but neither RTX nor Mac Meeting Intelligence is available."
        saksham_error "Inspect $SAKSHAM_BACKEND_LOG"
        return 1
    fi
    if [[ -n "$chatterbox_url" ]]; then
        saksham_wait_for_http \
            "Chatterbox TTS" \
            "${chatterbox_url%/}/health" \
            200 \
            15 || {
                saksham_error "The backend is running, but Chatterbox TTS is unavailable."
                saksham_error "Inspect $SAKSHAM_BACKEND_LOG and $HOME/.saksham/logs/chatterbox.log"
                return 1
            }
    fi

    status="$(saksham_http_status "$SAKSHAM_FRONTEND_URL" "" 5)"
    if [[ "$status" == "200" ]]; then
        saksham_info "Reusing the healthy Saksham frontend on port 5173."
        saksham_adopt_listener \
            "Saksham frontend" \
            5173 \
            "$SAKSHAM_FRONTEND_PID_FILE" \
            ".bin/vite" \
            "$SAKSHAM_FRONTEND_DIR"
    else
        listener="$(saksham_listener_pid 5173)"
        if [[ -n "$listener" ]]; then
            saksham_error "Port 5173 is occupied by an unhealthy process (PID $listener)."
            return 1
        fi
        saksham_start_process \
            "Saksham frontend" \
            "$SAKSHAM_FRONTEND_PID_FILE" \
            "$SAKSHAM_FRONTEND_DIR" \
            "$SAKSHAM_FRONTEND_LOG" \
            "$frontend_vite" --host 127.0.0.1 --port 5173 || return 1
        saksham_wait_for_http "Saksham frontend" "$SAKSHAM_FRONTEND_URL" 200 60 || {
            saksham_stop_process \
                "Saksham frontend" \
                "$SAKSHAM_FRONTEND_PID_FILE" \
                ".bin/vite" \
                "$SAKSHAM_FRONTEND_DIR" || true
            saksham_error "Inspect $SAKSHAM_FRONTEND_LOG"
            return 1
        }
    fi

    saksham_info "Saksham is ready at $SAKSHAM_FRONTEND_URL"
    if [[ "${SAKSHAM_NO_OPEN:-0}" != "1" ]]; then
        open "$SAKSHAM_FRONTEND_URL"
    fi
}

if ! start_saksham; then
    saksham_pause_on_error
    exit 1
fi
