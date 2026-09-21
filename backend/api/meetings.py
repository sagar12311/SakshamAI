"""Meeting intelligence REST API."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterator, Optional

from fastapi import APIRouter, File, Form, Header, HTTPException, Query, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from core.meeting_service import MeetingCaptureBusy, get_meeting_service


router = APIRouter()


class ProjectsRootUpdate(BaseModel):
    path: str = Field(min_length=1)


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=160)


class MeetingCreate(BaseModel):
    project_id: str
    title: str = Field(default="Client meeting", max_length=240)
    consent_confirmed: bool
    expected_speaker_count: Optional[int] = Field(default=None, ge=2, le=10)
    voice_memory_consent_confirmed: bool = False
    voice_memory_consent_scope: Optional[str] = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class MeetingUpdate(BaseModel):
    title: Optional[str] = Field(default=None, min_length=1, max_length=240)


class MomUpdate(BaseModel):
    content: str = Field(min_length=1)


class SpeakerUpdate(BaseModel):
    display_name: str = Field(min_length=1, max_length=120)


class RememberSpeakerRequest(BaseModel):
    display_name: str = Field(min_length=1, max_length=120)
    consent_confirmed: bool


class MeetingReprocess(BaseModel):
    expected_speaker_count: Optional[int] = Field(default=None, ge=2, le=10)
    automatic_speaker_count: bool = False


class PrivateCommandRequest(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    anchor_ms: int = Field(default=0, ge=0)
    selected_segment_ids: list[str] = Field(default_factory=list, max_length=50)


def _not_found(kind: str) -> HTTPException:
    return HTTPException(status_code=404, detail=f"{kind} not found")


@router.get("/projects-root")
async def get_projects_root():
    service = get_meeting_service()
    return {"path": str(service.projects_root)}


@router.put("/projects-root")
async def update_projects_root(request: ProjectsRootUpdate):
    try:
        path = await get_meeting_service().set_projects_root(request.path)
        return {"path": str(path)}
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@router.get("/projects")
async def list_projects():
    return {"projects": await get_meeting_service().list_projects()}


@router.post("/projects", status_code=201)
async def create_project(request: ProjectCreate):
    try:
        return {"project": await get_meeting_service().create_project(request.name)}
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@router.get("")
async def list_meetings(
    project_id: Optional[str] = None,
    limit: int = Query(default=100, ge=1, le=500),
):
    return {"meetings": await get_meeting_service().list_meetings(project_id, limit)}


@router.post("", status_code=201)
async def create_meeting(request: MeetingCreate):
    try:
        meeting = await get_meeting_service().create_meeting(
            request.project_id,
            request.title,
            consent_confirmed=request.consent_confirmed,
            expected_speaker_count=request.expected_speaker_count,
            voice_memory_consent_confirmed=request.voice_memory_consent_confirmed,
            voice_memory_consent_scope=request.voice_memory_consent_scope,
            metadata=request.metadata,
        )
        return {"meeting": get_meeting_service().public_meeting(meeting)}
    except KeyError as error:
        raise _not_found("Project") from error
    except MeetingCaptureBusy as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@router.get("/{meeting_id}")
async def get_meeting(meeting_id: str):
    try:
        return await get_meeting_service().get_detail(meeting_id)
    except KeyError as error:
        raise _not_found("Meeting") from error


@router.patch("/{meeting_id}")
async def update_meeting(meeting_id: str, request: MeetingUpdate):
    service = get_meeting_service()
    meeting = await service.store.get_meeting(meeting_id)
    if not meeting:
        raise _not_found("Meeting")
    updates = request.model_dump(exclude_none=True)
    updated = await service.store.update_meeting(meeting_id, **updates)
    return {"meeting": service.public_meeting(updated)}


@router.post("/{meeting_id}/stop")
async def stop_meeting(meeting_id: str):
    try:
        service = get_meeting_service()
        meeting = await service.stop_meeting(meeting_id)
        return {"meeting": service.public_meeting(meeting)}
    except KeyError as error:
        raise _not_found("Meeting") from error


@router.post("/{meeting_id}/retry")
async def retry_processing(meeting_id: str):
    try:
        service = get_meeting_service()
        meeting = await service.retry_processing(meeting_id)
        return {"meeting": service.public_meeting(meeting)}
    except KeyError as error:
        raise _not_found("Meeting") from error
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.post("/{meeting_id}/reprocess")
async def reprocess_meeting(meeting_id: str, request: MeetingReprocess):
    try:
        meeting = await get_meeting_service().retry_processing(
            meeting_id,
            request.expected_speaker_count,
            automatic_speaker_count=request.automatic_speaker_count,
        )
        return {"meeting": get_meeting_service().public_meeting(meeting)}
    except KeyError as error:
        raise _not_found("Meeting") from error
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.post("/{meeting_id}/private-commands")
async def submit_private_command(meeting_id: str, request: PrivateCommandRequest):
    try:
        return await get_meeting_service().submit_private_command(
            meeting_id,
            request.text,
            anchor_ms=request.anchor_ms,
            selected_segment_ids=request.selected_segment_ids,
        )
    except KeyError as error:
        raise _not_found("Meeting") from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


def _file_chunks(path: Path, start: int, length: int) -> Iterator[bytes]:
    remaining = length
    with path.open("rb") as handle:
        handle.seek(start)
        while remaining > 0:
            chunk = handle.read(min(256 * 1024, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk


def parse_byte_range(range_header: Optional[str], size: int) -> tuple[int, int, int]:
    if size <= 0:
        raise ValueError("Audio file is empty")
    start, end, status_code = 0, size - 1, 200
    if not range_header:
        return start, end, status_code
    if not range_header.startswith("bytes=") or "," in range_header:
        raise ValueError("Invalid audio byte range")
    value = range_header[6:].strip()
    left, right = value.split("-", 1)
    if not left:
        suffix = int(right)
        if suffix <= 0:
            raise ValueError("Invalid audio suffix range")
        start = max(0, size - suffix)
    else:
        start = int(left)
        end = int(right) if right else end
    if start < 0 or start >= size or end < start:
        raise ValueError("Audio byte range is not satisfiable")
    return start, min(end, size - 1), 206


@router.get("/{meeting_id}/audio")
async def stream_meeting_audio(
    meeting_id: str,
    range_header: Optional[str] = Header(default=None, alias="Range"),
):
    service = get_meeting_service()
    meeting = await service.store.get_meeting(meeting_id)
    if not meeting:
        raise _not_found("Meeting")
    availability = service.audio_availability(meeting)
    if not availability["available"] or not meeting.get("audio_path"):
        raise HTTPException(status_code=404, detail="Meeting audio is unavailable or expired")

    audio_path = Path(str(meeting["audio_path"])).resolve()
    private_root = (service.settings.saksham_home / "meetings" / meeting_id).resolve()
    if not audio_path.is_relative_to(private_root) or audio_path.name != "audio.wav":
        raise HTTPException(status_code=403, detail="Meeting audio path is invalid")
    size = audio_path.stat().st_size
    try:
        start, end, status_code = parse_byte_range(range_header, size)
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=416,
            detail="Audio byte range is not satisfiable",
            headers={"Content-Range": f"bytes */{size}"},
        )

    length = max(0, end - start + 1)
    headers = {
        "Accept-Ranges": "bytes",
        "Content-Length": str(length),
        "Cache-Control": "private, no-store",
    }
    if status_code == 206:
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    return StreamingResponse(
        _file_chunks(audio_path, start, length),
        status_code=status_code,
        media_type="audio/wav",
        headers=headers,
    )


@router.put("/{meeting_id}/mom")
async def save_mom(meeting_id: str, request: MomUpdate):
    try:
        return {"mom": await get_meeting_service().save_mom(meeting_id, request.content)}
    except KeyError as error:
        raise _not_found("Meeting") from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@router.post("/{meeting_id}/mom/finalize")
async def finalize_mom(meeting_id: str, request: MomUpdate):
    try:
        content = request.content.replace("**Status:** Draft", "**Status:** Final", 1)
        return {"mom": await get_meeting_service().save_mom(meeting_id, content, final=True)}
    except KeyError as error:
        raise _not_found("Meeting") from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@router.post("/{meeting_id}/mom/regenerate")
async def regenerate_mom(meeting_id: str):
    try:
        return {"mom": await get_meeting_service().regenerate_mom(meeting_id)}
    except KeyError as error:
        raise _not_found("Meeting") from error
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.patch("/{meeting_id}/speakers/{cluster_label}")
async def rename_speaker(meeting_id: str, cluster_label: str, request: SpeakerUpdate):
    try:
        mapping = await get_meeting_service().rename_speaker(
            meeting_id,
            cluster_label,
            request.display_name,
        )
        return {"speaker": mapping}
    except KeyError as error:
        raise _not_found("Meeting") from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@router.post("/{meeting_id}/speakers/{cluster_label}/remember")
async def remember_speaker(
    meeting_id: str,
    cluster_label: str,
    request: RememberSpeakerRequest,
):
    try:
        profile = await get_meeting_service().remember_speaker(
            meeting_id,
            cluster_label,
            request.display_name,
            request.consent_confirmed,
        )
        return {"profile": profile}
    except KeyError as error:
        raise _not_found("Meeting") from error
    except (ValueError, RuntimeError) as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@router.get("/voice-profiles/list")
async def list_voice_profiles(include_revoked: bool = False):
    profiles = await get_meeting_service().store.list_voice_profiles(include_revoked)
    return {"profiles": profiles}


@router.post("/voice-profiles/enroll-owner", status_code=201)
async def enroll_owner_voice(
    display_name: str = Form(...),
    consent_confirmed: bool = Form(...),
    audio_samples: list[UploadFile] = File(...),
):
    try:
        audio = [await item.read() for item in audio_samples]
        profile = await get_meeting_service().profiles.enroll(
            display_name,
            "owner",
            audio,
            consent_confirmed=consent_confirmed,
        )
        return {"profile": profile}
    except (ValueError, RuntimeError) as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@router.delete("/voice-profiles/{profile_id}")
async def revoke_voice_profile(profile_id: str):
    store = get_meeting_service().store
    profile = next(
        (
            item
            for item in await store.list_voice_profiles(include_revoked=True)
            if item["id"] == profile_id
        ),
        None,
    )
    revoked = await store.revoke_voice_profile(profile_id)
    if not revoked:
        raise _not_found("Voice profile")
    await store.add_voice_profile_event(
        profile_id=profile_id,
        meeting_id=None,
        event_type="profile_revoked",
        consent_scope=None,
        metadata={"display_name": profile.get("display_name") if profile else None},
    )
    return {"revoked": True}


@router.delete("/{meeting_id}")
async def delete_meeting(meeting_id: str, delete_note: bool = False):
    try:
        deleted = await get_meeting_service().delete_meeting(meeting_id, delete_note)
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    if not deleted:
        raise _not_found("Meeting")
    return {"deleted": True}
