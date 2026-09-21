"""
Saksham AI - FastAPI Application Entry Point
Main server with WebSocket support for real-time voice and chat
"""

import asyncio
import json
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from loguru import logger

from config import get_settings
from api import chat, tasks, memory, system, modes, documents, integrations, learning, meetings, security
from core.cognition_bus import get_cognition_bus
from core.connection_manager import ConnectionManager
from core.voice_processor import get_voice_processor
from core.chat_store import get_chat_store
from core.chatterbox_service import get_chatterbox_service_manager
from core.meeting_audio import AudioSequenceGap
from core.meeting_service import get_meeting_service
from core.meeting_intelligence_service import get_meeting_intelligence_service_manager
from core.procedural_learning import observe_verified_success
from core.state_store import get_state_store
from core.cognition_bus import CognitionMessage, MessageType
from core.origin_policy import is_trusted_browser_origin
from core.admin_auth import configure_admin_voice_verifier, get_admin_auth_service
from core.headless_audio import HeadlessAudioRuntime
from agents import initialize_all_agents
from memory.nova_memory import get_nova_memory


settings = get_settings()

# Global instances - use getters to ensure singleton pattern
cognition_bus = get_cognition_bus()
connection_manager = ConnectionManager()
voice_processor = get_voice_processor()
chatterbox_service = get_chatterbox_service_manager()
meeting_service = get_meeting_service()
meeting_intelligence_service = get_meeting_intelligence_service_manager()
headless_audio = HeadlessAudioRuntime(voice_processor)


async def handle_agent_response(message: CognitionMessage) -> None:
    """Store agent responses for polling and broadcast audio"""
    payload = dict(message.payload)
    conversation_id = str(payload.get("conversation_id", "default"))
    if conversation_id.startswith("meeting:"):
        payload["speak"] = False
        payload["audio_suppressed"] = "meeting_private_console"
        await meeting_service.record_private_agent_response(
            conversation_id.removeprefix("meeting:"),
            payload,
        )
        return
    if payload.get("speak", False) and await meeting_service.has_active_capture():
        payload["speak"] = False
        payload["audio_suppressed"] = "meeting_capture_active"
    text = payload.get("text", "")
    spoken_text = payload.get("spoken_text") or text
    voice_channel = f"voice:{conversation_id}"
    
    sensitive_admin = bool(payload.get("sensitive_admin_activation"))
    if not sensitive_admin:
        logger.info(f"💾 Storing agent response: {text[:50]}...")
        await get_chat_store().add_message(
            role="assistant", content=text, conversation_id=conversation_id,
            msg_type=payload.get("type", "response"), speak=payload.get("speak", False),
            spoken_text=spoken_text, tone=payload.get("tone", "neutral"),
            untrusted_web_derived=bool(payload.get("untrusted_web_derived")),
        )
    if not sensitive_admin:
        await voice_processor.record_assistant_turn(text, conversation_id)
    
    # Broadcast Text to all clients (Push update)
    # Important: Both "chat" (Web UI) and "voice" (Voice UI) need to know the text response
    # This triggers the 'onResponse' callback in the frontend
    if not sensitive_admin:
        await connection_manager.broadcast_json(payload, channel="chat")
    await connection_manager.broadcast_json(payload, channel=voice_channel)
    
    # Broadcast Audio if speak is True
    if payload.get("speak", False) and spoken_text and spoken_text.strip():
        try:
            logger.info("🎤 Generating TTS for response...")
            audio = await voice_processor.text_to_speech(
                spoken_text,
                tone=payload.get("tone", "neutral"),
            )
            if audio:
                await connection_manager.broadcast_bytes(audio, channel=voice_channel)
            
            # IMPORTANT: Extend session after speaking so user can reply without wake word
            voice_processor.extend_session()
            logger.info("🔄 Session extended after response - user can reply without wake word")
        except Exception as e:
            logger.error(f"TTS Broadcast Error: {e}")


async def handle_audio_chunk(message: CognitionMessage) -> None:
    """Handle streaming audio chunks from agent"""
    payload = message.payload
    conversation_id = str(payload.get("conversation_id", "default"))
    if conversation_id.startswith("meeting:"):
        return
    if await meeting_service.has_active_capture():
        return
    text = payload.get("text", "")
    
    if text:
        try:
            # Generate Audio for this chunk (sentence level)
            audio = await voice_processor.text_to_speech(
                text,
                tone=payload.get("tone", "neutral"),
            )
            if audio:
                 await connection_manager.broadcast_bytes(
                     audio,
                     channel=f"voice:{conversation_id}",
                 )
        except Exception as e:
            logger.error(f"TTS Streaming Error: {e}")


async def handle_task_progress(message: CognitionMessage) -> None:
    """Persist executor progress for Meeting Mode and the Command Center."""
    payload = dict(message.payload)
    conversation_id = str(payload.get("conversation_id", "default"))
    if conversation_id.startswith("meeting:"):
        await meeting_service.record_private_action_progress(
            conversation_id.removeprefix("meeting:"),
            payload,
        )
        return
    task_id = str(payload.get("task_id") or "")
    execution_id = str(payload.get("execution_id") or "")
    if not task_id or not execution_id:
        return

    store = get_state_store()
    total_steps = max(1, int(payload.get("total_steps") or 1))
    current_step = max(1, int(payload.get("step") or 1))
    progress = min(100, round((current_step - 1) / total_steps * 100, 2))
    try:
        await store.update_execution(
            task_id,
            execution_id,
            {
                "status": "running",
                "progress": progress,
                "current_step_id": str(current_step),
            },
        )
        task = await store.get_task(task_id)
        if task:
            steps = []
            for index, original in enumerate(task.get("steps", []), start=1):
                step = dict(original) if isinstance(original, dict) else {"description": str(original)}
                if index < current_step:
                    step["status"] = "completed"
                elif index == current_step:
                    step["status"] = "running"
                else:
                    step["status"] = "pending"
                steps.append(step)
            await store.update_task(task_id, {"steps": steps, "status": "running"})
        await _broadcast_command_center_task(task_id)
    except Exception as error:
        logger.warning(f"Command Center progress persistence failed: {error}")


async def _broadcast_command_center_task(task_id: str) -> None:
    """Push a fresh task snapshot; polling remains a safe reconnect fallback."""
    snapshot = await get_state_store().get_task_snapshot(task_id)
    if snapshot:
        await connection_manager.broadcast_json(
            {"type": "command_center_task", "task": snapshot},
            channel="chat",
        )


async def handle_plan_created(message: CognitionMessage) -> None:
    """Create the durable Command Center task before any executor is invoked."""
    payload = dict(message.payload)
    task_id = str(payload.get("task_id") or "")
    conversation_id = str(payload.get("conversation_id") or "default")
    if not task_id or conversation_id.startswith("meeting:"):
        return

    raw_steps = payload.get("steps") or []
    steps = []
    for index, raw_step in enumerate(raw_steps, start=1):
        step = dict(raw_step) if isinstance(raw_step, dict) else {"description": str(raw_step)}
        step.setdefault("id", str(index))
        step.setdefault("status", "pending")
        steps.append(step)
    title = str(payload.get("intent") or "Saksham task").strip() or "Saksham task"
    store = get_state_store()
    try:
        await store.create_task(
            title=title,
            description=f"Planned from conversation {conversation_id}",
            status="planning",
            steps=steps,
            task_id=task_id,
        )
        await store.record_task_event(
            task_id,
            "plan.created",
            "Saksham created an execution plan",
            data={"total_steps": len(steps)},
        )
        await _broadcast_command_center_task(task_id)
    except Exception as error:
        logger.warning(f"Command Center plan persistence failed: {error}")


async def handle_task_approval_requested(message: CognitionMessage) -> None:
    """Persist a Guardian decision as an auditable user affirmation request."""
    payload = dict(message.payload)
    task_id = str(payload.get("task_id") or "")
    conversation_id = str(payload.get("conversation_id") or "default")
    if not task_id or conversation_id.startswith("meeting:"):
        return

    approval = dict(payload.get("approval") or {})
    level = str(approval.get("approval_level") or "confirm")
    store = get_state_store()
    try:
        if level not in {"confirm", "admin"}:
            await store.update_task(task_id, {"status": "blocked"})
            await store.record_task_event(
                task_id,
                "plan.blocked",
                str(approval.get("reason") or "Guardian blocked this plan"),
                level="error",
            )
        else:
            approval_record = await store.request_task_approval(
                task_id,
                level=level,
                summary=str(approval.get("reason") or "Review this plan before it runs"),
                action={
                    "risk": approval.get("max_risk"),
                    "plan": payload.get("plan") or [],
                },
                approval_id=str(payload.get("approval_id") or "") or None,
            )
            if level == "admin":
                await store.update_task(task_id, {"status": "awaiting_admin_activation"})
        await _broadcast_command_center_task(task_id)
    except Exception as error:
        logger.warning(f"Command Center approval persistence failed: {error}")


async def handle_task_terminal(message: CognitionMessage) -> None:
    """Persist the executor's verified completion result and step outcomes."""
    payload = dict(message.payload)
    task_id = str(payload.get("task_id") or "")
    execution_id = str(payload.get("execution_id") or "")
    conversation_id = str(payload.get("conversation_id") or "default")
    if not task_id or not execution_id or conversation_id.startswith("meeting:"):
        return

    cancelled = bool(payload.get("cancelled"))
    success = bool(payload.get("success"))
    status = "cancelled" if cancelled else "completed" if success else "failed"
    results = payload.get("results") or []
    error = None
    for item in results:
        if not isinstance(item, dict) or item.get("success"):
            continue
        nested_result = item.get("result")
        nested_error = nested_result.get("error") if isinstance(nested_result, dict) else None
        error = str(item.get("error") or nested_error or "Action did not complete")
        break
    store = get_state_store()
    completion_persisted = False
    try:
        await store.update_execution(
            task_id,
            execution_id,
            {
                "status": status,
                "progress": 100 if status == "completed" else 0,
                "result": {"results": results},
                "error": error,
            },
        )
        task = await store.get_task(task_id)
        if task:
            outcomes = {int(item.get("step", 0)): item for item in results if isinstance(item, dict)}
            steps = []
            for index, original in enumerate(task.get("steps", []), start=1):
                step = dict(original) if isinstance(original, dict) else {"description": str(original)}
                outcome = outcomes.get(index)
                if outcome and outcome.get("success"):
                    step["status"] = "completed"
                    step["evidence"] = "Verified by executor"
                elif outcome and outcome.get("cancelled"):
                    step["status"] = "skipped"
                elif outcome:
                    step["status"] = "failed"
                    step["evidence"] = str(outcome.get("error") or "Execution did not complete")
                steps.append(step)
            await store.update_task(task_id, {"steps": steps, "status": status})
        completion_persisted = status == "completed"
        await _broadcast_command_center_task(task_id)
    except Exception as error:
        logger.warning(f"Command Center completion persistence failed: {error}")

    # Learning is a best-effort observation after the durable execution has
    # completed.  It never changes the executor outcome, planner state, or
    # what can run next; only verified routine plans become review candidates.
    if completion_persisted:
        try:
            persisted_task = await store.get_task(task_id)
            persisted_execution = await store.get_execution(execution_id)
            if persisted_task and persisted_execution:
                observation = await observe_verified_success(
                    store,
                    task=persisted_task,
                    execution=persisted_execution,
                )
                if observation.created:
                    await _broadcast_command_center_task(task_id)
        except Exception as error:
            logger.warning(f"Procedural learning observation failed: {error}")


async def handle_task_cancellation_requested(message: CognitionMessage) -> None:
    """Persist cancellations issued conversationally before the executor sees them."""
    payload = dict(message.payload)
    task_id = str(payload.get("task_id") or "")
    if not task_id:
        return
    try:
        await get_state_store().request_task_cancellation(task_id, reason=payload.get("reason"))
        await _broadcast_command_center_task(task_id)
    except Exception as error:
        logger.warning(f"Command Center cancellation persistence failed: {error}")


async def handle_user_turn_committed(message: CognitionMessage) -> None:
    """Persist and expose the complete merged turn to connected clients."""
    payload = message.payload
    text = payload.get("text", "")
    conversation_id = payload.get("conversation_id", "default")
    if not text:
        return

    await get_chat_store().add_message(
        role="user",
        content=text,
        conversation_id=conversation_id,
        nlu=payload.get("nlu", {}),
        fragments=payload.get("fragments", []),
    )
    await connection_manager.broadcast_json({
        "type": "transcription",
        "text": text,
        "should_respond": True,
        "session_active": True,
        "turn_committed": True,
        "fragment_count": payload.get("fragment_count", 1),
        "nlu": payload.get("nlu", {}),
    }, channel=f"voice:{conversation_id}")
    await connection_manager.broadcast_json({
        "type": "status",
        "text": "Thinking...",
    }, channel=f"voice:{conversation_id}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Application lifespan manager.
    Handles startup and shutdown events.
    """
    # Startup
    state_store = get_state_store()
    await state_store.initialize()
    # Configure the admin voice verifier once at startup. It remains
    # unavailable unless an explicitly private worker and long API key are set.
    if settings.admin_voice_enabled and not configure_admin_voice_verifier():
        logger.warning("Admin voice activation disabled: private worker settings are incomplete or invalid")
    await get_nova_memory().initialize()
    await meeting_service.initialize()
    meeting_service.set_event_sink(
        lambda meeting_id, payload: connection_manager.broadcast_json(
            payload,
            channel=f"meeting:{meeting_id}",
        )
    )
    current_mode = await state_store.get_mode(settings.default_mode)

    logger.info(f"🚀 Starting {settings.app_name} v{settings.app_version}")
    logger.info(f"🎤 Voice enabled: {settings.voice_enabled}")
    logger.info(f"🖥️  Mac automation: {settings.mac_automation_enabled}")
    logger.info(f"📊 Mode: {current_mode}")

    if settings.tts_enabled and settings.tts_provider == "chatterbox":
        await chatterbox_service.start()
    await meeting_intelligence_service.start()
    
    # Subscribe to responses for persistence
    cognition_bus.subscribe(MessageType.AGENT_RESPONSE, handle_agent_response)
    cognition_bus.subscribe(MessageType.AGENT_AUDIO_CHUNK, handle_audio_chunk)
    cognition_bus.subscribe(MessageType.PLAN_CREATED, handle_plan_created)
    cognition_bus.subscribe(MessageType.TASK_APPROVAL_REQUESTED, handle_task_approval_requested)
    cognition_bus.subscribe(MessageType.TASK_PROGRESS, handle_task_progress)
    cognition_bus.subscribe(MessageType.TASK_COMPLETE, handle_task_terminal)
    cognition_bus.subscribe(MessageType.TASK_FAILED, handle_task_terminal)
    cognition_bus.subscribe(MessageType.TASK_CANCELLATION_REQUESTED, handle_task_cancellation_requested)
    cognition_bus.subscribe(MessageType.USER_TURN_COMMITTED, handle_user_turn_committed)
    
    # Initialize agents
    try:
        agents = await initialize_all_agents()
        logger.info(f"🤖 Initialized {len(agents)} agents")
    except Exception as e:
        logger.warning(f"Agent initialization skipped: {e}")
    
    await cognition_bus.start()
    
    # Start voice processor
    if settings.voice_enabled:
        await voice_processor.start_listening()
    if settings.headless_mode and settings.voice_enabled:
        await headless_audio.start()
    
    try:
        yield
    finally:
        logger.info("🛑 Shutting down Saksham AI...")
        await voice_processor.stop_listening()
        await headless_audio.stop()
        await cognition_bus.stop()
        await meeting_service.shutdown()
        await meeting_intelligence_service.stop()
        await chatterbox_service.stop()


app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description="Saksham AI - Your Personal Cognitive Intelligence System",
    lifespan=lifespan,
)

# CORS remains deliberately narrow even in development: a random browser tab
# must not be able to drive the loopback assistant or read its private data.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type"],
)


@app.middleware("http")
async def reject_untrusted_browser_origin(request: Request, call_next):
    """Reject explicit foreign browser origins before they reach local APIs."""
    origin = request.headers.get("origin")
    if origin and not is_trusted_browser_origin(origin, settings.cors_origins):
        logger.warning(f"Rejected loopback API request from untrusted origin: {origin[:200]}")
        return JSONResponse(status_code=403, content={"detail": "Untrusted browser origin"})
    return await call_next(request)


async def _require_trusted_websocket_origin(websocket: WebSocket) -> bool:
    """WebSockets bypass CORS, so require an exact trusted browser origin."""
    origin = websocket.headers.get("origin")
    if is_trusted_browser_origin(origin, settings.cors_origins):
        return True
    logger.warning(f"Rejected WebSocket from untrusted origin: {str(origin)[:200]}")
    await websocket.close(code=4403, reason="Untrusted browser origin")
    return False

# Include API routers
app.include_router(chat.router, prefix="/api/chat", tags=["Chat"])
app.include_router(tasks.router, prefix="/api/tasks", tags=["Tasks"])
app.include_router(learning.router, prefix="/api/learning", tags=["Procedural Learning"])
app.include_router(memory.router, prefix="/api/memory", tags=["Memory"])
app.include_router(system.router, prefix="/api/system", tags=["System Control"])
app.include_router(modes.router, prefix="/api/modes", tags=["Operating Modes"])
app.include_router(documents.router, prefix="/api/documents", tags=["Documents"])
app.include_router(integrations.router, prefix="/api/integrations", tags=["Integration Lab"])
app.include_router(meetings.router, prefix="/api/meetings", tags=["Meeting Intelligence"])
app.include_router(security.router, prefix="/api/security", tags=["Security"])


@app.get("/")
async def root():
    """Health check and basic info"""
    current_mode = await get_state_store().get_mode(settings.default_mode)
    return {
        "name": settings.app_name,
        "version": settings.app_version,
        "status": "operational",
        "mode": current_mode,
        "voice_enabled": settings.voice_enabled,
        "ambient_listening": voice_processor.is_listening,
        "meeting_capture_active": await meeting_service.has_active_capture(),
    }


@app.get("/health")
async def health():
    """Detailed health check"""
    return {
        "status": "healthy",
        "cognition_bus": cognition_bus.is_running,
        "agents_active": cognition_bus.active_agents,
        "connections": connection_manager.active_connections_count,
        "voice_active": voice_processor.is_listening,
        "chatterbox_tts": chatterbox_service.status,
        "meeting_intelligence": await meeting_service.intelligence.health(),
        "meeting_intelligence_service": meeting_intelligence_service.status,
        "meeting_capture_active": await meeting_service.has_active_capture(),
    }


@app.websocket("/ws/meetings/{meeting_id}")
async def meeting_websocket(websocket: WebSocket, meeting_id: str):
    """Receive sequenced PCM16 frames and publish meeting intelligence events."""
    if not await _require_trusted_websocket_origin(websocket):
        return
    meeting = await meeting_service.store.get_meeting(meeting_id)
    if not meeting:
        await websocket.close(code=4404, reason="Meeting not found")
        return
    await connection_manager.connect(websocket, channel=f"meeting:{meeting_id}")
    meeting_service.client_connected(meeting_id)
    await connection_manager.send_json(
        {
            "type": "meeting_ready",
            "meeting": meeting,
            "ack": int(meeting["last_sequence"]),
        },
        websocket,
    )
    try:
        while True:
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                break
            payload = message.get("bytes")
            if payload:
                if len(payload) < 6:
                    await connection_manager.send_json(
                        {"type": "recoverable_error", "stage": "capture", "message": "Invalid audio frame"},
                        websocket,
                    )
                    continue
                sequence = int.from_bytes(payload[:4], "big", signed=False)
                try:
                    result = await meeting_service.append_audio(meeting_id, sequence, payload[4:])
                    await connection_manager.send_json(
                        {"type": "audio_ack", **result},
                        websocket,
                    )
                except AudioSequenceGap as error:
                    await connection_manager.send_json(
                        {"type": "audio_nack", "expected": error.expected, "received": error.received},
                        websocket,
                    )
                except Exception as error:
                    await connection_manager.send_json(
                        {"type": "recoverable_error", "stage": "capture", "message": str(error)},
                        websocket,
                    )
                continue
            text_payload = message.get("text")
            if text_payload:
                try:
                    control = json.loads(text_payload)
                except json.JSONDecodeError:
                    continue
                if control.get("type") == "stop":
                    await meeting_service.stop_meeting(meeting_id)
                elif control.get("type") == "ping":
                    await connection_manager.send_json({"type": "pong"}, websocket)
    except WebSocketDisconnect:
        pass
    finally:
        connection_manager.disconnect(websocket)
        meeting_service.client_disconnected(meeting_id)


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """
    WebSocket endpoint for real-time communication.
    Handles text chat and sends responses.
    """
    if not await _require_trusted_websocket_origin(websocket):
        return
    await connection_manager.connect(websocket)
    try:
        while True:
            data = await websocket.receive()
            
            if "text" in data:
                message = data["text"]
                response = await cognition_bus.process_message(message)
                await websocket.send_json(response)
                
    except WebSocketDisconnect:
        connection_manager.disconnect(websocket)
        logger.info("Client disconnected from WebSocket")


@app.websocket("/ws/voice")
async def voice_websocket(websocket: WebSocket):
    """
    Dedicated WebSocket for always-on voice streaming.
    Real-time transcription with wake word detection.
    """
    if not await _require_trusted_websocket_origin(websocket):
        return
    if await meeting_service.has_active_capture():
        await websocket.close(code=4409, reason="Meeting Mode has exclusive microphone access")
        return
    conversation_id = websocket.query_params.get("conversation_id", "default")
    await connection_manager.connect(websocket, channel=f"voice:{conversation_id}")
    audio_queue: asyncio.Queue[tuple[int, bytes]] = asyncio.Queue()
    audio_sequence = 0
    logger.info(f"🎤 Voice client connected (conversation={conversation_id})")

    async def process_audio_queue() -> None:
        while True:
            sequence, audio_chunk = await audio_queue.get()
            try:
                result = await voice_processor.process_audio_chunk(
                    audio_chunk,
                    conversation_id=conversation_id,
                    sequence=sequence,
                )

                if result.get("playback_action") == "stop":
                    await connection_manager.send_json({
                        "type": "playback_control",
                        "action": "stop",
                        "reason": "user_interruption",
                    }, websocket)

                if result.get("admin_activation") is not None:
                    activation_result = dict(result.get("admin_activation") or {})
                    activation_result.pop("capability", None)
                    await connection_manager.send_json({
                        "type": "admin_activation",
                        "result": activation_result,
                    }, websocket)
                if result.get("transcription") or result.get("session_active") is not None:
                    await connection_manager.send_json({
                        "type": "transcription",
                        "text": result.get("transcription", ""),
                        "merged_text": result.get("merged_transcription", ""),
                        "pending_turn": result.get("pending_turn", False),
                        "fragment_count": result.get("fragment_count", 0),
                        "should_respond": result.get("should_respond", False),
                        "session_active": result.get("session_active", False),
                        "timestamp": asyncio.get_event_loop().time(),
                    }, websocket)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.error(f"Voice audio worker error: {error}")
            finally:
                audio_queue.task_done()

    worker_task = asyncio.create_task(process_audio_queue())

    try:
        while True:
            # Receive data - text (control) or bytes (audio)
            data = await websocket.receive()
            
            # Check for disconnect message
            if data.get("type") == "websocket.disconnect":
                logger.info("🎤 Voice client disconnect signal received")
                break
            
            if "bytes" in data and data["bytes"]:
                if await meeting_service.has_active_capture():
                    await websocket.close(code=4409, reason="Meeting Mode has exclusive microphone access")
                    break
                audio_chunk = data["bytes"]
                audio_sequence += 1
                logger.info(
                    f"🎤 Queued voice audio chunk #{audio_sequence} "
                    f"({len(audio_chunk)} bytes)"
                )
                await voice_processor.note_audio_received(conversation_id, audio_sequence)
                await audio_queue.put((audio_sequence, audio_chunk))
            
            elif "text" in data and data["text"]:
                # Process Control Message
                try:
                    import json
                    message = json.loads(data["text"])
                    if message.get("type") == "set_conversation_mode":
                        enabled = message.get("enabled", False)
                        await voice_processor.set_conversation_mode(enabled)
                    elif message.get("type") == "playback_state":
                        voice_processor.set_playback_state(
                            bool(message.get("speaking", False)),
                            conversation_id,
                        )
                    elif message.get("type") == "speech_activity":
                        await voice_processor.note_speech_activity(
                            conversation_id,
                            bool(message.get("speaking", False)),
                        )
                except Exception as e:
                    logger.error(f"Control message error: {e}")

                
    except WebSocketDisconnect:
        connection_manager.disconnect(websocket)
        logger.info("🎤 Voice client disconnected")
    except Exception as e:
        logger.error(f"Voice WebSocket error: {e}")
        connection_manager.disconnect(websocket)
    finally:
        voice_processor.set_playback_state(False, conversation_id)
        connection_manager.disconnect(websocket)
        worker_task.cancel()
        await asyncio.gather(worker_task, return_exceptions=True)
        await voice_processor.flush_conversation(conversation_id)


@app.get("/api/voice/buffer")
async def get_ambient_buffer():
    """Get the current ambient listening buffer"""
    return {
        "buffer": voice_processor.get_ambient_buffer(),
        "is_listening": voice_processor.is_listening,
    }


@app.post("/api/voice/save-memory")
async def save_ambient_to_memory():
    """Save current ambient buffer to long-term memory"""
    await voice_processor.save_ambient_to_memory()
    return {"saved": True}


if __name__ == "__main__":
    import uvicorn
    
    uvicorn.run(
        "main:app",
        host=settings.host,
        port=settings.port,
        reload=settings.debug,
        log_level=settings.log_level.lower(),
    )
