# Saksham AI — Complete Architecture Document

> **Purpose**: This document provides a complete, self-contained architectural reference for the Saksham AI project. It is designed so that any AI system (e.g., ChatGPT) can read this single file and have full context to understand, modify, debug, or extend the codebase.

---

## 1. Project Identity

| Field | Value |
|---|---|
| **Name** | Saksham AI |
| **Tagline** | Your Personal Jarvis — Cognitive Intelligence System |
| **Version** | 0.1.0 |
| **Platform** | macOS (local-first) |
| **LLM Provider** | LM Studio (local, OpenAI-compatible API; `localhost` by default, remote endpoint optional) |
| **Voice** | Always-on ambient listening with wake word detection |
| **Frontend** | Electron + React + Vite (TypeScript) |
| **Backend** | Python 3.11+ / FastAPI / WebSocket |
| **Architecture Pattern** | Multi-Agent Cognitive Bus (event-driven pub/sub) |

---

## 2. High-Level Architecture

```
┌────────────────────────────────────────────────────────────────────┐
│                        ELECTRON SHELL                              │
│  ┌──────────────────────────────────────────────────────────────┐  │
│  │           REACT FRONTEND (Vite, port 5173)                   │  │
│  │                                                              │  │
│  │  ┌─────────┐  ┌──────────────┐  ┌────────────────────────┐  │  │
│  │  │TitleBar  │  │ Chat (REST)  │  │ Voice (WebSocket)      │  │  │
│  │  │         │  │ POST /api/*  │  │ ws://localhost:8420/ws  │  │  │
│  │  └─────────┘  └──────┬───────┘  │  /voice                │  │  │
│  │                      │          └───────────┬────────────┘  │  │
│  └──────────────────────┼──────────────────────┼──────────────┘  │
│                         │ (Vite proxy)         │                  │
└─────────────────────────┼──────────────────────┼──────────────────┘
                          │                      │
                          ▼                      ▼
┌─────────────────────────────────────────────────────────────────────┐
│                    FASTAPI BACKEND (port 8420)                       │
│                                                                     │
│  ┌─────────────────────────────────────────────────────────────┐   │
│  │                     REST API LAYER                           │   │
│  │  /api/chat/message   /api/system/*   /api/modes/*            │   │
│  │  /api/documents/*    /api/memory/*   /api/tasks/*            │   │
│  └──────────────────────────┬──────────────────────────────────┘   │
│                              │                                     │
│  ┌───────────────────────────▼─────────────────────────────────┐   │
│  │                    COGNITION BUS                              │   │
│  │          (Async Priority Queue + Pub/Sub)                    │   │
│  │                                                              │   │
│  │  MessageTypes: USER_INPUT, AGENT_RESPONSE, TASK_START,       │   │
│  │    TASK_COMPLETE, PLAN_CREATED, MEMORY_STORE,                │   │
│  │    MEMORY_RECALL, MAC_COMMAND, PERMISSION_REQUEST, etc.      │   │
│  └──┬──────┬──────┬──────┬──────┬──────┬───────────────────────┘   │
│     │      │      │      │      │      │                           │
│     ▼      ▼      ▼      ▼      ▼      ▼                           │
│  ┌──────┐┌──────┐┌──────┐┌──────┐┌──────┐┌──────┐                 │
│  │PLAN- ││EXEC- ││GUARD-││MEM-  ││OBSER-││STRAT-│                 │
│  │NER   ││UTOR  ││IAN   ││ORY   ││VER   ││EGIST │                 │
│  │      ││      ││      ││      ││      ││      │                 │
│  │Brain ││Hands ││Safety││Memory││Eyes  ││Advis-│                 │
│  └──┬───┘└──┬───┘└──────┘└──┬───┘└──────┘└──────┘                 │
│     │       │               │                                      │
│     ▼       ▼               ▼                                      │
│  ┌──────┐ ┌──────────┐  ┌──────────┐                              │
│  │ LLM  │ │ Mac      │  │ ChromaDB │                              │
│  │Client│ │ Control  │  │ Vector   │                              │
│  │      │ │ (AS/JXA) │  │ Store    │                              │
│  └──┬───┘ └──────────┘  └──────────┘                              │
│     │                                                              │
└─────┼──────────────────────────────────────────────────────────────┘
      │ (HTTP via local endpoint or optional remote tunnel)
      ▼
┌─────────────────┐
│   LM Studio     │
│ (localhost      │
│  :1234)         │
│                 │
│ Model:          │
│ openai/         │
│ gpt-oss-20b     │
└─────────────────┘
```

---

## 3. Directory Structure

```
saksham/
├── backend/                          # Python FastAPI backend
│   ├── main.py                       # App entry point, lifespan, WebSocket endpoints
│   ├── config.py                     # Pydantic Settings (loads .env)
│   ├── .env                          # Environment variables (LLM URL, keys, ports)
│   ├── pyproject.toml                # Python dependencies
│   │
│   ├── api/                          # REST API routers (FastAPI)
│   │   ├── __init__.py
│   │   ├── chat.py                   # POST /api/chat/message, GET /api/chat/history/{id}
│   │   ├── documents.py             # POST /api/documents/upload (file text extraction)
│   │   ├── memory.py                # GET/POST /api/memory (CRUD, search)
│   │   ├── modes.py                 # GET/POST /api/modes (assist/execute/autonomous/shadow)
│   │   ├── system.py                # POST /api/system/app|terminal|browser
│   │   └── tasks.py                 # CRUD for tasks (placeholder)
│   │
│   ├── agents/                       # Multi-agent cognitive system
│   │   ├── __init__.py               # initialize_all_agents() entry point
│   │   ├── base_agent.py             # Abstract BaseAgent (think/act pattern)
│   │   ├── planner_agent.py          # "The Brain" — intent understanding, plan creation
│   │   ├── executor_agent.py         # "The Hands" — Mac OS action execution
│   │   ├── guardian_agent.py         # "The Conscience" — safety, risk assessment
│   │   ├── memory_agent.py           # "The Memory" — vector search, recall
│   │   ├── observer_agent.py         # "The Eyes" — execution monitoring
│   │   └── strategist_agent.py       # "The Advisor" — pattern analysis, suggestions
│   │
│   ├── core/                         # Core infrastructure
│   │   ├── __init__.py
│   │   ├── cognition_bus.py          # Central event bus (PriorityQueue + pub/sub)
│   │   ├── connection_manager.py     # WebSocket connection manager (multi-channel)
│   │   ├── llm_client.py             # Unified LLM client (OpenAI-compatible + Whisper + TTS)
│   │   ├── voice_processor.py        # Wake word detection, VAD, session management
│   │   ├── chat_store.py             # In-memory chat history store
│   │   └── document_processor.py     # Text extraction (code, PDF, text files)
│   │
│   ├── mac/                          # macOS system integration
│   │   ├── __init__.py               # Public API exports
│   │   ├── applescript_bridge.py     # Async AppleScript execution + templates
│   │   ├── app_controller.py         # Open/close/focus apps with alias resolution
│   │   ├── browser_controller.py     # Chrome/Safari tab & navigation control
│   │   ├── file_controller.py        # File CRUD operations
│   │   ├── terminal_executor.py      # Shell command execution (sandboxed)
│   │   ├── jxa.py                    # JavaScript for Automation (volume, dark mode)
│   │   ├── productivity.py           # System TTS, stop-speaking, etc.
│   │   ├── screen.py                 # Screen info utilities
│   │   └── apps.json                 # Custom app alias registry (persisted)
│   │
│   ├── memory/                       # Memory subsystem
│   │   ├── __init__.py
│   │   ├── vector_store.py           # ChromaDB wrapper (store/search/delete/update)
│   │   └── embeddings.py             # SentenceTransformer embeddings (all-MiniLM-L6-v2)
│   │
│   └── models/                       # Local model files
│       └── piper/                    # Piper TTS ONNX model
│           └── en_US-lessac-medium.onnx
│
├── frontend/                         # Electron + React + Vite frontend
│   ├── package.json                  # Node dependencies and scripts
│   ├── vite.config.ts                # Vite config (proxy to backend)
│   ├── tsconfig.json                 # TypeScript config
│   ├── tsconfig.electron.json        # Electron-specific TS config
│   ├── index.html                    # HTML entry point
│   │
│   ├── electron/                     # Electron main process
│   │   ├── main.ts                   # Window creation, tray, shortcuts, mic permissions
│   │   └── preload.ts                # contextBridge API (IPC security layer)
│   │
│   └── src/                          # React application source
│       ├── main.tsx                   # React entry point
│       ├── App.tsx                    # Root component (chat + voice + mode)
│       ├── App.css                    # Global app styles
│       ├── index.css                  # Base CSS reset/variables
│       │
│       ├── components/
│       │   ├── Chat/
│       │   │   ├── Chat.tsx           # Chat UI (messages, input, file upload)
│       │   │   └── Chat.css
│       │   ├── Voice/
│       │   │   ├── VoiceButton.tsx     # Voice toggle button (unused in ambient mode)
│       │   │   ├── VoiceButton.css
│       │   │   ├── AmbientIndicator.tsx # Mic status indicator
│       │   │   └── AmbientIndicator.css
│       │   ├── Mode/
│       │   │   ├── ModeIndicator.tsx   # Operating mode dropdown
│       │   │   └── ModeIndicator.css
│       │   └── TitleBar/
│       │       ├── TitleBar.tsx        # Custom Electron title bar
│       │       └── TitleBar.css
│       │
│       ├── hooks/
│       │   └── useAmbientListening.ts  # Core voice hook (WebSocket + AudioContext + VAD)
│       │
│       └── utils/
│           └── audioFeedback.ts        # Audio feedback tones (WebAudio)
│
└── README.md
```

---

## 4. Configuration (config.py + .env)

All configuration is managed via Pydantic `BaseSettings` which loads from `.env`:

### Key Environment Variables

| Variable | Default | Description |
|---|---|---|
| `LLM_BASE_URL` | `http://localhost:1234/v1` | LM Studio API endpoint (default local OpenAI-compatible server) |
| `LLM_MODEL` | `openai/gpt-oss-20b` | Model identifier in LM Studio |
| `LLM_API_KEY` | `lm-studio` | API key (dummy for local LM Studio) |
| `LLM_TEMPERATURE` | `0.7` | LLM temperature |
| `LLM_MAX_TOKENS` | `4096` | Maximum tokens per completion |
| `HOST` | `127.0.0.1` | Backend bind address |
| `PORT` | `8420` | Backend port |
| `VOICE_ENABLED` | `true` | Enable voice processing |
| `WHISPER_MODEL` | `base` | Whisper model size (tiny/base/small/medium/large) |
| `TTS_PROVIDER` | `edge` | TTS engine: `chatterbox` (local MPS or remote CUDA), `edge`, `piper`, `openai`, or `system` |
| `TTS_VOICE` | `onyx` | OpenAI TTS voice name |
| `EDGE_TTS_VOICE` | `en-US-GuyNeural` | Edge TTS voice name |
| `PIPER_MODEL_PATH` | `models/piper/en_US-lessac-medium.onnx` | Local Piper ONNX model |
| `CHATTERBOX_TTS_URL` | `http://127.0.0.1:8100` | Private Chatterbox Turbo service URL |
| `CHATTERBOX_TTS_API_KEY` | (set in .env) | Shared bearer token for the TTS service |
| `CHATTERBOX_TTS_FALLBACK` | `none` | Optional alternate provider; `none` preserves one consistent voice on failures |
| `CHATTERBOX_TTS_AUTOSTART` | `true` | Start and supervise bundled Chatterbox when its URL is local loopback |
| `CHATTERBOX_TTS_STARTUP_TIMEOUT_SECONDS` | `120` | Maximum wait for local model load and warm-up |
| `DEFAULT_MODE` | `assist` | Operating mode (assist/execute/autonomous/shadow) |
| `MAC_AUTOMATION_ENABLED` | `true` | Enable macOS AppleScript/JXA control |
| `REQUIRE_CONFIRMATION_FOR_DESTRUCTIVE` | `true` | Guardian safety gate |
| `OPENAI_TTS_API_KEY` | (set in .env) | Separate OpenAI key for TTS only |
| `SEARCH_PROVIDER` | `duckduckgo` | Web search provider for web_search action |

### Derived Paths

- `~/.saksham/` — Base data directory
- `~/.saksham/memory/` — ChromaDB persistence directory
- `~/.saksham/logs/audit.log` — Guardian audit log

---

## 5. Backend Deep Dive

### 5.1 Application Lifecycle (`main.py`)

```python
# Startup sequence (lifespan context manager):
1. Load settings
2. Start or reuse bundled local Chatterbox and wait for warm-up
3. Subscribe global handlers (AGENT_RESPONSE → store + broadcast + TTS)
4. Initialize all 6 agents (PlannerAgent, ExecutorAgent, GuardianAgent, MemoryAgent, ObserverAgent, StrategistAgent)
5. Start CognitionBus message processing loop
6. Start VoiceProcessor ambient listening

# Shutdown:
1. Stop VoiceProcessor
2. Stop CognitionBus
3. Stop Chatterbox only when this backend launched it
```

**WebSocket Endpoints:**
- `GET /ws` — Generic WebSocket for text chat
- `GET /ws/voice` — Dedicated voice WebSocket (binary audio in/out + JSON control messages)

**REST Endpoints:**
- `GET /` — Health check basic info
- `GET /health` — Detailed health (bus running, agents active, connections count)
- `GET /api/voice/buffer` — Get ambient listening buffer
- `POST /api/voice/save-memory` — Save ambient buffer to long-term memory

### 5.2 The Cognition Bus (`core/cognition_bus.py`)

The Cognition Bus is the **central nervous system** of Saksham. It is an **async priority queue with pub/sub routing**.

#### Message Types (Enum)

| Type | Description |
|---|---|
| `USER_INPUT` | Text from user (chat or voice transcription) |
| `USER_VOICE` | Raw voice audio from user |
| `AGENT_REQUEST` | Inter-agent request |
| `AGENT_RESPONSE` | Agent's text response to user |
| `AGENT_AUDIO_CHUNK` | Streaming audio chunk from agent |
| `PLAN_CREATED` | Planner created a multi-step plan |
| `PLAN_STEP` / `PLAN_COMPLETE` | Plan execution lifecycle |
| `TASK_START` / `TASK_PROGRESS` / `TASK_COMPLETE` / `TASK_FAILED` | Task execution lifecycle |
| `OBSERVATION` / `ERROR_DETECTED` | Observer events |
| `MEMORY_STORE` / `MEMORY_RECALL` | Memory operations |
| `MODE_CHANGE` | Operating mode switch |
| `PERMISSION_REQUEST` / `PERMISSION_GRANTED` / `PERMISSION_DENIED` | Guardian safety gate |
| `MAC_COMMAND` / `MAC_RESULT` | macOS actions |

#### Message Structure (`CognitionMessage`)

```python
@dataclass
class CognitionMessage:
    type: MessageType           # What kind of message
    payload: dict[str, Any]     # Data payload
    source: str                 # Who sent it (agent name or "user")
    target: Optional[str]       # Direct target agent, or None for broadcast
    correlation_id: str         # UUID for request-response tracking
    timestamp: datetime
    priority: int               # 1 (highest) to 10 (lowest)
    requires_response: bool     # If True, publisher awaits response via Future
```

#### Routing Logic

1. **Targeted messages** (`target` is set): Routed directly to the named agent's handler as an `asyncio.create_task()` (non-blocking).
2. **Broadcast messages** (`target` is None): Delivered to all subscribers of that `MessageType`.
3. **Request-Response**: When `requires_response=True`, the publisher creates an `asyncio.Future` and awaits it (180s timeout). The receiving agent calls `bus.respond(correlation_id, result)` to resolve the future.

#### Key Methods

| Method | Description |
|---|---|
| `process_message(text)` | Convenience: Creates `USER_INPUT` message targeting `planner`, requires response |
| `publish(message)` | Add message to priority queue; optionally wait for response |
| `subscribe(type, handler)` | Register a callback for a message type |
| `register_agent(name, handler)` | Register an agent for direct targeting |
| `respond(correlation_id, result)` | Resolve a pending Future |

### 5.3 Multi-Agent System

All agents extend `BaseAgent` which provides:
- `think(message) → dict` — Analyze the incoming message (abstract)
- `act(thought) → Any` — Execute based on analysis result (abstract)
- `ask_llm(prompt, ...)` — Query the LLM with the agent's system prompt
- `send_to_agent(target, type, payload, requires_response)` — Inter-agent communication
- `broadcast(type, payload)` — Broadcast to all listeners

#### Agent: **Planner** (name: `planner`)

**Role**: "The Brain" — understands user intent and creates executable plans.

**Message Flow (text chat):**
```
User types "hello" in frontend
    → POST /api/chat/message
    → bus.process_message("hello")
    → CognitionMessage(type=USER_INPUT, target="planner", requires_response=True)
    → PlannerAgent._handle_message()
        1. Queries MemoryAgent for context (MEMORY_RECALL to "memory")
        2. Sends prompt to LLM with system prompt + user input
        3. Parses JSON response from LLM
        4. If is_simple_response: publishes AGENT_RESPONSE, returns payload
        5. If has plan: asks GuardianAgent for permission
           - If Guardian rejects: saves pending plan, returns confirmation request
           - If Guardian approves: speaks acknowledgement, sends plan to ExecutorAgent
        6. Returns result to HTTP response via bus.respond()
```

**LLM Output Format** (JSON):
```json
{
    "understood_intent": "Open Safari",
    "is_simple_response": false,
    "response": "Opening Safari for you.",
    "plan": [
        {
            "action": "open_app",
            "target": "Safari",
            "description": "Open Safari browser",
            "parameters": {}
        }
    ]
}
```

**Available Actions in Plans:**
`open_app`, `close_app`, `minimize_app`, `maximize_app`, `terminal_command`, `browser_navigate`, `web_search`, `speak_response`, `system_control`, `add_app_alias`

**Confirmation Flow**: Planner stores `_pending_plan` and `_awaiting_confirmation`. If user says "yes"/"go ahead", it executes the stored plan. If "no"/"cancel", it clears the state.

**Task Completion Hook**: Planner subscribes to `TASK_COMPLETE` and `TASK_FAILED`. When execution finishes, it generates a natural language summary using the LLM (with a clean non-JSON system prompt) and publishes it as `AGENT_RESPONSE`.

---

#### Agent: **Executor** (name: `executor`)

**Role**: "The Hands" — executes macOS actions.

**Initialization**: Lazily imports and instantiates Mac controllers:
- `AppController` — open/close/focus apps
- `TerminalExecutor` — shell commands
- `FileController` — file CRUD
- `BrowserController` — navigate/interact with browsers
- `JXAController` — system settings (volume, dark mode)

**Plan Execution Flow:**
```
Receives TASK_START with plan[]
    → For each step:
        1. Broadcast TASK_PROGRESS
        2. Execute the step (dispatch by action type)
        3. On failure: broadcast ERROR_DETECTED, continue
    → Broadcast TASK_COMPLETE or TASK_FAILED with all results
```

**Action Implementations:**

| Action | Implementation |
|---|---|
| `open_app` | `AppController.open_app()` → AppleScript `tell app to activate` |
| `close_app` | Graceful quit → Force kill fallback |
| `minimize_app` | `Cmd+M` keystroke via System Events |
| `maximize_app` | Click zoom button or `Ctrl+Cmd+F` |
| `terminal_command` | `TerminalExecutor.execute()` → `asyncio.create_subprocess_shell()` |
| `browser_navigate` | `BrowserController.navigate()` → AppleScript `open location` |
| `web_search` | DuckDuckGo HTML scraping (with `wttr.in` intercept for weather) |
| `system_control` | `JXAController` → `set_volume`, `set_dark_mode`, `open_url` |
| `add_app_alias` | `AppController.add_alias()` → persists to `mac/apps.json` |

---

#### Agent: **Guardian** (name: `guardian`)

**Role**: "The Conscience" — safety gate for all actions.

**Risk Assessment** (0-10 scale):

| Level | Description | Example |
|---|---|---|
| 0 | Safe | `open_app` |
| 2 | Low risk | Simple terminal command |
| 5 | Medium | `file_operation` write/delete, `rm`, `mv`, `git push` |
| 6 | Moderate | `close_app` |
| 7 | Needs confirmation | `sudo`, `system_control`, `chmod` |
| 8 | Dangerous | Keywords: `delete`, `kill`, `terminate` |
| 9 | Very dangerous | Protected paths: `/`, `/System`, `/Library` |
| 10 | Blocked | `rm -rf`, `sudo rm`, `mkfs`, `dd if=` |

**Decision Logic:**
- Risk ≥ 10 → **Block** (never execute)
- Risk ≥ 7 or (≥5 in assist mode) → **Requires confirmation** (unless autonomous mode)
- Risk < threshold → **Approve**

All decisions are logged to `~/.saksham/logs/audit.log` as JSON lines.

---

#### Agent: **Memory** (name: `memory`)

**Role**: "The Memory" — semantic storage and retrieval.

**Storage Backends** (cascading):
1. **ChromaDB** (primary) — persistent vector database at `~/.saksham/memory/`
2. **JSONL files** (fallback) — simple keyword search in `~/.saksham/memory/{type}.jsonl`

**Embeddings**: `all-MiniLM-L6-v2` via `SentenceTransformer` (local, no API needed).

**Memory Types**: `general`, `user`, `project`, `task`, `skill`

**Operations:**
- `MEMORY_STORE` → Store text with metadata and type tag
- `MEMORY_RECALL` → Semantic search, returns top-N results by cosine similarity

---

#### Agent: **Observer** (name: `observer`)

**Role**: "The Eyes" — monitors execution in real-time.

Subscribes to: `TASK_START`, `TASK_PROGRESS`, `TASK_COMPLETE`, `TASK_FAILED`, `ERROR_DETECTED`

Tracks active executions in `_active_executions` dict. On failure, notifies the Strategist for pattern analysis.

---

#### Agent: **Strategist** (name: `strategist`)

**Role**: "The Advisor" — detects patterns and suggests improvements.

Subscribes to: `OBSERVATION`, `TASK_COMPLETE`

Capabilities:
- Failure analysis (asks LLM for root cause)
- Repetition detection (suggests automation after 3+ repetitions)
- Proactive suggestions

---

### 5.4 LLM Client (`core/llm_client.py`)

Unified client wrapping `AsyncOpenAI`:

| Method | Description |
|---|---|
| `chat(messages, tools, ...)` | Standard chat completion |
| `complete(messages, system_prompt, ...)` | Wrapper for agents (adds system prompt) |
| `stream(messages, ...)` | Streaming chat completion (yields chunks) |
| `transcribe(audio_data, language)` | Speech-to-text (Cloud Whisper or local faster-whisper) |
| `speak(text, voice)` | OpenAI TTS (legacy, voice_processor now handles TTS) |

**Critical Configuration:**
- **Timeout**: `httpx.Timeout(120.0, connect=10.0)` — long timeout for slower reasoning models
- **Transcription Strategy**: If API key starts with `lm-studio` → local `faster-whisper`. If real OpenAI key → cloud Whisper API.

### 5.5 Voice Processor (`core/voice_processor.py`)

**Always-on ambient listening pipeline:**

```
Frontend Mic → WebSocket /ws/voice (binary WAV chunks)
    → Backend VoiceProcessor.process_audio_chunk()
        1. Noise reduction (noisereduce library, stationary)
        2. Cooldown check (0.8s after Saksham finishes speaking)
        3. Size validation (minimum 3000 bytes)
        4. Transcribe (faster-whisper, local)
        5. Garbage filter (reject single words, filler phrases)
        6. Barge-in detection (stop phrases while speaking)
        7. Wake word / Session check:
           - Wake word detected → start session → process
           - Session active → extend session → process
           - Conversation mode → always process
           - None → ignore
        8. Publish USER_INPUT to CognitionBus targeting "planner"
```

**Wake Word Patterns**: `saksham`, `hey saksham`, `hi saksham`, `hello`, `hi`, `hey` (and phonetic variants)

**Session Management:**
- Session starts on wake word detection
- Auto-extends when Saksham responds (30s timeout)
- Ends on stop phrases ("bye", "stop", "wait") or silence timeout

**TTS Provider Chain** (with fallbacks):
1. **Piper** (local ONNX, <100ms) → falls back if `synthesize_stream_raw` not available
2. **Edge TTS** (free Microsoft cloud, ~200ms)
3. **OpenAI TTS** (best quality, ~1-3s)
4. **Mac System TTS** (last resort via `say` command)

### 5.6 Connection Manager (`core/connection_manager.py`)

Multi-channel WebSocket manager:
- Channels: `default` (chat), `voice`
- Methods: `broadcast_json()`, `broadcast_bytes()` (for audio)
- Auto-cleanup of dead connections

### 5.7 Global Response Handler (`main.py`)

When **any agent** publishes `AGENT_RESPONSE`:
1. Store in `ChatStore` (in-memory history)
2. Broadcast JSON to all `chat` and `voice` WebSocket clients
3. If `speak=True`: Generate TTS audio → broadcast binary to `voice` clients
4. Extend voice session

---

## 6. Frontend Deep Dive

### 6.1 Electron Shell (`electron/main.ts`)

- **Frameless window** with `titleBarStyle: 'hiddenInset'`
- **System tray** icon with show/hide/quit menu
- **Global shortcut**: `Cmd+Shift+S` to toggle visibility
- **Auto-grants microphone permission** (critical for voice)
- **Preload script** exposes `window.saksham` API via `contextBridge`
- Dev mode loads `http://localhost:5173`, production loads `dist/index.html`

### 6.2 React App (`src/App.tsx`)

**State:**
- `mode`: Operating mode (`assist`/`execute`/`autonomous`/`shadow`)
- `messages[]`: Chat history (merged from local state + polling)
- `ambientEnabled`: Voice on/off toggle

**Communication Patterns:**

1. **Text Chat** (REST):
   ```
   User types → POST /api/chat/message → JSON response → add to messages
   ```
   Uses Vite proxy: `/api` → `http://localhost:8420`

2. **Voice** (WebSocket):
   ```
   useAmbientListening hook → ws://localhost:8420/ws/voice
   Sends: WAV binary chunks
   Receives: JSON (transcription, response) + binary (TTS audio)
   ```

3. **History Polling** (1s interval):
   ```
   GET /api/chat/history/default → smart merge with deduplication
   ```
   This catches async responses (e.g., task completion results that arrive after the initial HTTP response).

**Deduplication**: Messages are deduplicated by ID match or content+timestamp proximity (<2s for local, <5s for history merge).

### 6.3 Ambient Listening Hook (`hooks/useAmbientListening.ts`)

The most complex frontend component. Implements:

1. **Microphone Capture**: Raw audio at 16kHz, mono, no browser noise suppression
2. **Voice Activity Detection (VAD)**: Custom RMS-based detection
   - `SILENCE_THRESHOLD = 0.02`
   - `MIN_SPEECH_FRAMES = 5` (~1.3s minimum speech)
   - `MIN_CONSECUTIVE_SPEECH = 2` (debounce noise spikes)
3. **WAV Encoding**: Client-side Float32 → PCM16 WAV encoding
4. **WebSocket Management**: Auto-reconnect on disconnect (3s delay)
5. **Audio Playback Queue**: Sequential playback with echo prevention (500ms deaf period)
6. **Audio Format Detection**: Checks WAV header (RIFF) vs MP3 for correct playback

**State Exposed:**
- `isListening`, `hasPermission`, `isSpeaking`, `isUserSpeaking`, `audioLevel`, `transcriptionBuffer`

### 6.4 Chat Component (`components/Chat/Chat.tsx`)

- Message list with `framer-motion` animations
- Text input with send button
- **File upload**: Sends to `/api/documents/upload`, extracted text becomes hidden context in the chat message
- Suggestion buttons for quick actions

### 6.5 Vite Configuration

```typescript
proxy: {
    '/api': { target: 'http://localhost:8420', changeOrigin: true },
    '/ws':  { target: 'ws://localhost:8420', ws: true }
}
```

---

## 7. Data Flow: Complete Request Lifecycle

### 7.1 Text Chat Flow

```
1. User types "open safari" → Chat.handleSubmit()
2. POST /api/chat/message { message: "open safari" }
3. ChatStore.add_message("user", "open safari")
4. CognitionBus.process_message("open safari")
   → Creates CognitionMessage(USER_INPUT, target="planner", requires_response=True)
5. PlannerAgent._handle_message()
   a. Queries MemoryAgent for relevant context → (empty for new sessions)
   b. Builds prompt with system prompt + user input + available actions
   c. Calls LLM (AsyncOpenAI → LM Studio over the configured OpenAI-compatible endpoint)
   d. Parses JSON: { understood_intent: "Open Safari", plan: [{action: "open_app", target: "Safari"}] }
6. PlannerAgent.act()
   a. Sends PERMISSION_REQUEST to GuardianAgent
   b. Guardian assesses risk: open_app → level 0 → approved
   c. Publishes AGENT_RESPONSE("Opening Safari for you.", speak=True)
   d. Sends TASK_START to ExecutorAgent
7. ExecutorAgent._execute_plan()
   a. Broadcasts TASK_PROGRESS
   b. Calls AppController.open_app("Safari") → osascript -e 'tell application "Safari" to activate'
   c. Broadcasts TASK_COMPLETE
8. PlannerAgent._handle_task_complete()
   a. Generates natural language summary via LLM
   b. Publishes AGENT_RESPONSE("Safari is now open.", speak=True)
9. Global handler (main.py):
   a. Stores response in ChatStore
   b. Broadcasts JSON to all WebSocket clients (chat + voice channels)
   c. Generates TTS audio (Edge TTS) → broadcasts binary to voice clients
10. Frontend polls /api/chat/history/default → picks up new messages
11. HTTP response returns to original POST: { response: "Opening Safari for you.", type: "response" }
```

### 7.2 Voice Flow

```
1. User speaks "Hey Saksham, what's the weather in Pune?"
2. Frontend VAD detects speech, buffers Float32 samples
3. On silence: encodes WAV, sends binary via WebSocket to /ws/voice
4. VoiceProcessor.process_audio_chunk()
   a. Noise reduction (noisereduce)
   b. Transcribes via faster-whisper → "hey saksham, what's the weather in pune"
   c. Wake word "saksham" detected → starts session
   d. Publishes USER_INPUT to bus targeting "planner"
5. Same agent flow as text (Planner → Guardian → Executor)
6. Executor._web_search("weather in pune")
   a. Intercepts weather query → calls wttr.in API
   b. Returns: "Pune: Partly cloudy, 32°C. Feels like 35°C. Wind 12 km/h"
7. Planner gets TASK_COMPLETE, generates natural response via LLM
8. AGENT_RESPONSE broadcast → TTS generated → binary audio sent to voice WebSocket
9. Frontend plays audio through speaker queue
```

---

## 8. Operating Modes

| Mode | Behavior |
|---|---|
| **assist** | Default. Suggests actions, waits for user approval via Guardian for risky ops (risk ≥ 5) |
| **execute** | Executes after brief confirmation (higher risk threshold) |
| **autonomous** | Executes within defined boundaries, reports after. Guardian only blocks risk ≥ 10 |
| **shadow** | Observes silently, learns patterns, suggests improvements |

Mode is changed via `POST /api/modes/switch` → broadcasts `MODE_CHANGE` on the CognitionBus → GuardianAgent updates its threshold.

---

## 9. Security Model

1. **No direct database binding to frontend** — All data flows through API endpoints
2. **Guardian Agent** — Reviews every plan before execution. Blocks `rm -rf`, `sudo rm`, etc.
3. **Audit Logging** — All permission decisions logged to `~/.saksham/logs/audit.log`
4. **Protected Paths** — `/`, `/System`, `/Library`, `/Applications`, `/Users`, `~` blocked from file operations
5. **Electron Context Isolation** — `nodeIntegration: false`, `contextIsolation: true`, preload script bridges
6. **API Key Security** — Uses Pydantic `SecretStr` for all sensitive values
7. **CORS** — Currently `allow_origins=["*"]` for development (should be restricted in production)
8. **Confirmation Flow** — Destructive actions require explicit user confirmation ("yes"/"go ahead")

---

## 10. Dependencies

### Backend (Python)

| Package | Purpose |
|---|---|
| `fastapi` + `uvicorn` | Web framework + ASGI server |
| `openai` (AsyncOpenAI) | LLM client (OpenAI-compatible, works with LM Studio) |
| `chromadb` | Vector database for semantic memory |
| `sentence-transformers` | Local embeddings (`all-MiniLM-L6-v2`) |
| `faster-whisper` | Local speech-to-text |
| `edge-tts` | Free Microsoft TTS |
| `piper-tts` | Local ONNX TTS (fastest) |
| `noisereduce` | Audio noise reduction |
| `soundfile` + `numpy` | Audio processing |
| `httpx` | HTTP client (for web search, custom timeouts) |
| `beautifulsoup4` | HTML parsing (DuckDuckGo scraping) |
| `pypdf` | PDF text extraction |
| `pydantic` + `pydantic-settings` | Config management |
| `loguru` | Structured logging |
| `pyobjc-framework-*` | macOS native integration |

### Frontend (Node.js)

| Package | Purpose |
|---|---|
| `react` + `react-dom` | UI framework |
| `vite` + `@vitejs/plugin-react` | Build tool + HMR |
| `electron` + `electron-builder` | Desktop app shell |
| `framer-motion` | Animations |
| `lucide-react` | Icons |
| `typescript` | Type safety |
| `concurrently` + `wait-on` | Dev workflow |

---

## 11. Running the Project

### Prerequisites
- macOS (required for AppleScript/JXA)
- Python 3.11+ with venv
- Node.js 18+
- LM Studio running locally or reachable over your configured network endpoint with a loaded model
- Microphone permissions granted

### Start Backend
```bash
cd backend
source .venv/bin/activate
python main.py
# Runs on http://127.0.0.1:8420
```

### Start Frontend (Dev)
```bash
cd frontend
npm run dev          # Vite dev server on http://localhost:5173
# OR
npm run electron:dev  # Full Electron + Vite + TypeScript watch
```

### Start Frontend (Production)
```bash
cd frontend
npm run electron:build  # Builds .app bundle
```

---

## 12. Known Issues & Gotchas

1. **LLM Timeout**: The 20B reasoning model can take 15-60s. The `AsyncOpenAI` client timeout is set to 120s and the CognitionBus response timeout is 180s to accommodate this. If you see "Request timed out", check LM Studio is running and the model is loaded.

2. **Piper TTS**: The `synthesize_stream_raw` method may not be available in all Piper versions. Falls back to Edge TTS.

3. **History Polling**: Frontend polls `/api/chat/history/default` every 1 second. This is intentional to catch async task completion responses. It's not a bug — the Planner returns an "acknowledgement" immediately, and the actual result is broadcast later when execution completes.

4. **File Upload URL**: The Chat component hardcodes `http://localhost:8420/api/documents/upload` instead of using the Vite proxy relative path. Should be `/api/documents/upload`.

5. **Empty `content` from Reasoning Models**: Models like `gpt-oss-20b` return thinking in a `reasoning` field and the answer in `content`. If `max_tokens` is too low, all tokens are consumed by reasoning and `content` is empty. Ensure `max_tokens` is set to at least 4096.

6. **Python 3.9 Compatibility**: The `pyproject.toml` specifies `>=3.11` but the actual venv has Python 3.9. The code uses `list[str]` type hints (3.9+ with `__future__` annotations, but some files don't import it). May need `from __future__ import annotations` in affected files.

7. **Conversation Mode**: `conversation_mode` is `True` by default in `VoiceProcessor`, which means ALL speech is processed without needing a wake word. This can cause unintended activations from background noise.

---

## 13. Extension Points

### Adding a New Agent
1. Create `agents/new_agent.py` extending `BaseAgent`
2. Implement `think()` and `act()`
3. Add to `agents/__init__.py` → `initialize_all_agents()`
4. Subscribe to relevant `MessageType` events in `initialize()`

### Adding a New Action
1. Add the action name to the Planner's system prompt (`planner_agent.py`, line ~283)
2. Add handler in `ExecutorAgent._execute_step()` dispatch
3. Implement the handler method

### Adding a New API Endpoint
1. Create router in `api/` directory
2. Register in `main.py`: `app.include_router(new_router, prefix="/api/new", tags=[...])`
3. Add Vite proxy if needed in `vite.config.ts`

### Switching LLM Provider
1. Update `.env`: `LLM_BASE_URL`, `LLM_MODEL`, `LLM_API_KEY`
2. For Cloud OpenAI: Set `LLM_BASE_URL=` (empty → default), `LLM_API_KEY=sk-...`
3. For Ollama: Set `LLM_BASE_URL=http://localhost:11434/v1`

---

## 14. File-by-File Reference

### Backend Core Files

| File | Lines | Key Classes/Functions | Responsibility |
|---|---|---|---|
| `main.py` | 303 | `app`, `lifespan()`, `voice_websocket()` | FastAPI app, WebSocket endpoints, global response handler |
| `config.py` | 107 | `Settings`, `get_settings()` | Pydantic settings from .env |
| `core/cognition_bus.py` | 338 | `CognitionBus`, `CognitionMessage`, `MessageType` | Event bus with priority queue routing |
| `core/llm_client.py` | 325 | `LLMClient`, `get_llm_client()` | OpenAI-compatible LLM calls, Whisper, TTS |
| `core/voice_processor.py` | 448 | `VoiceProcessor`, `get_voice_processor()` | Audio processing, wake word, VAD, session mgmt, TTS |
| `core/connection_manager.py` | 96 | `ConnectionManager` | Multi-channel WebSocket management |
| `core/chat_store.py` | 36 | `ChatStore`, `get_chat_store()` | In-memory chat history |
| `core/document_processor.py` | 85 | `DocumentProcessor` | Text extraction from code/PDF files |

### Agent Files

| File | Lines | Agent Name | Role |
|---|---|---|---|
| `agents/base_agent.py` | 186 | (abstract) | BaseAgent with think/act pattern |
| `agents/planner_agent.py` | 651 | `planner` | Intent understanding, plan creation, LLM interaction |
| `agents/executor_agent.py` | 497 | `executor` | macOS action execution |
| `agents/guardian_agent.py` | 245 | `guardian` | Safety assessment, permission gate |
| `agents/memory_agent.py` | 273 | `memory` | Vector storage, semantic recall |
| `agents/observer_agent.py` | 118 | `observer` | Execution monitoring |
| `agents/strategist_agent.py` | 127 | `strategist` | Pattern analysis, suggestions |

### Mac Control Files

| File | Lines | Key Class | Responsibility |
|---|---|---|---|
| `mac/applescript_bridge.py` | 237 | `run_applescript()`, `AppleScriptTemplates` | Async AppleScript execution |
| `mac/app_controller.py` | 208 | `AppController` | App lifecycle with alias system |
| `mac/browser_controller.py` | ~250 | `BrowserController` | Browser tab/navigation control |
| `mac/terminal_executor.py` | ~150 | `TerminalExecutor` | Shell command execution |
| `mac/file_controller.py` | ~200 | `FileController` | File CRUD |
| `mac/jxa.py` | 66 | `JXAController` | System settings via JavaScript for Automation |
| `mac/productivity.py` | ~300 | `ProductivityController` | System TTS, stop speaking |

### Frontend Core Files

| File | Lines | Key Components | Responsibility |
|---|---|---|---|
| `electron/main.ts` | 170 | `createWindow()`, `createTray()` | Electron main process |
| `electron/preload.ts` | 56 | `window.saksham` API | Secure IPC bridge |
| `src/App.tsx` | 267 | `App()` | Root component, state management |
| `src/hooks/useAmbientListening.ts` | 444 | `useAmbientListening()` | Voice capture, WebSocket, VAD, audio playback |
| `src/components/Chat/Chat.tsx` | 205 | `Chat()` | Chat UI with file upload |
| `src/components/Voice/AmbientIndicator.tsx` | ~30 | `AmbientIndicator()` | Mic status indicator |
| `src/components/Mode/ModeIndicator.tsx` | ~50 | `ModeIndicator()` | Mode dropdown |
| `src/components/TitleBar/TitleBar.tsx` | ~40 | `TitleBar()` | Custom Electron title bar |

---

## 15. API Reference

### Chat API (`/api/chat`)

| Method | Path | Body | Response |
|---|---|---|---|
| `POST` | `/api/chat/message` | `{ message: string, context?: string, conversation_id?: string }` | `{ response: string, conversation_id: string, type: string, speak: bool }` |
| `GET` | `/api/chat/history/{conversation_id}` | — | `{ conversation_id: string, messages: Message[] }` |

### Documents API (`/api/documents`)

| Method | Path | Body | Response |
|---|---|---|---|
| `POST` | `/api/documents/upload` | multipart/form-data `file` | `{ filename: string, text: string, type: "file_content" }` |

### Modes API (`/api/modes`)

| Method | Path | Body | Response |
|---|---|---|---|
| `GET` | `/api/modes/current` | — | `{ mode: string, description: string }` |
| `GET` | `/api/modes/available` | — | `{ modes: [{name, description}] }` |
| `POST` | `/api/modes/switch` | `{ mode: string }` | `{ switched: bool, mode: string, description: string }` |

### System API (`/api/system`)

| Method | Path | Body | Response |
|---|---|---|---|
| `POST` | `/api/system/app` | `{ app_name: string, action: "open"\|"close" }` | `{ success: bool, ... }` |
| `POST` | `/api/system/terminal` | `{ command: string, cwd?: string }` | `{ stdout, stderr, return_code }` |
| `POST` | `/api/system/browser` | `{ action: string, url?: string, browser?: string }` | varies |
| `GET` | `/api/system/frontmost` | — | `{ app: string }` |
| `POST` | `/api/system/notify` | `title, message` (query params) | `{ sent: true }` |

### Memory API (`/api/memory`)

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/memory/` | List memories (placeholder) |
| `GET` | `/api/memory/search?query=...` | Semantic search (placeholder) |
| `POST` | `/api/memory/` | Store memory (placeholder) |
| `DELETE` | `/api/memory/{id}` | Delete memory |
| `GET` | `/api/memory/stats` | Storage statistics |

### WebSocket Endpoints

| Path | Protocol | Description |
|---|---|---|
| `ws://localhost:8420/ws` | JSON text | Generic chat WebSocket |
| `ws://localhost:8420/ws/voice` | Binary + JSON | Voice: sends WAV audio chunks, receives TTS audio + transcription JSON |

---

*Document generated by Antigravity AI on 2026-08-09. Reflects codebase state at this date.*
