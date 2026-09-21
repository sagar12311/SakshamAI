"""Private meeting intelligence worker for ASR, diarization, and speaker embeddings."""

from __future__ import annotations

import asyncio
import hmac
import json
import os
import tempfile
import threading
import wave
from pathlib import Path
from typing import Optional

os.environ.setdefault("PYANNOTE_METRICS_ENABLED", "0")

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, UploadFile


API_KEY = os.getenv("MEETING_INTELLIGENCE_API_KEY", "")
DEVICE = os.getenv("MEETING_INTELLIGENCE_DEVICE", "auto")
DEFAULT_ASR_MODEL = os.getenv("MEETING_ASR_MODEL", "large-v3-turbo")
DIARIZATION_MODEL = os.getenv(
    "MEETING_DIARIZATION_MODEL",
    "pyannote/speaker-diarization-community-1",
)
EMBEDDING_MODEL = os.getenv(
    "MEETING_EMBEDDING_MODEL",
    "pyannote/wespeaker-voxceleb-resnet34-LM",
)
HF_TOKEN = os.getenv("HF_TOKEN", "")
GPU_MIN_FREE_MB = int(os.getenv("MEETING_GPU_MIN_FREE_MB", "4500"))
ADMIN_VOICE_AUTH_ENABLED = os.getenv("ADMIN_VOICE_AUTH_ENABLED", "false").lower() == "true"
ADMIN_ANTISPOOF_MODEL = os.getenv("ADMIN_ANTISPOOF_MODEL", "")
ADMIN_ANTISPOOF_ARCHITECTURE = os.getenv("ADMIN_ANTISPOOF_ARCHITECTURE", "")
ADMIN_SPEAKER_THRESHOLD = float(os.getenv("ADMIN_SPEAKER_THRESHOLD", "0.82"))
ADMIN_LIVENESS_THRESHOLD = float(os.getenv("ADMIN_LIVENESS_THRESHOLD", "0.90"))

app = FastAPI(title="Saksham Meeting Intelligence", version="0.1.0")


async def authorize(authorization: Optional[str] = Header(default=None)) -> None:
    if not API_KEY or API_KEY.startswith("replace-") or len(API_KEY) < 24:
        raise HTTPException(status_code=503, detail="Meeting worker API key is not securely configured")
    supplied = authorization or ""
    if not hmac.compare_digest(supplied, f"Bearer {API_KEY}"):
        raise HTTPException(status_code=401, detail="Invalid meeting intelligence API key")


class Models:
    def __init__(self) -> None:
        self.asr = {}
        self.diarization = None
        self.embedding = None
        self.admin_antispoof = None
        self.admin_antispoof_error = ""
        self.admin_voice_error = ""
        self._admin_model_lock = threading.Lock()
        self._lock = None

    @property
    def lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    @staticmethod
    def resolved_device() -> str:
        if DEVICE != "auto":
            return DEVICE
        try:
            import torch

            if torch.cuda.is_available():
                return "cuda"
            if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
                return "mps"
        except Exception:
            pass
        return "cpu"

    def ensure_capacity(self) -> None:
        if self.resolved_device() != "cuda":
            return
        try:
            import torch

            free_bytes, _ = torch.cuda.mem_get_info()
            free_mb = free_bytes // (1024 * 1024)
            if free_mb < GPU_MIN_FREE_MB:
                raise HTTPException(
                    status_code=503,
                    detail=f"GPU busy: {free_mb} MB free; {GPU_MIN_FREE_MB} MB required",
                )
        except HTTPException:
            raise
        except Exception:
            # Capacity probing is advisory; model loading still reports real failures.
            return

    def get_asr(self, model_name: str):
        selected = model_name or DEFAULT_ASR_MODEL
        if selected in self.asr:
            return self.asr[selected]
        from faster_whisper import WhisperModel

        device = self.resolved_device()
        if device == "mps":
            # CTranslate2 has no MPS backend; use optimized CPU inference on Apple Silicon.
            device = "cpu"
        compute_type = "float16" if device == "cuda" else "int8"
        self.asr[selected] = WhisperModel(selected, device=device, compute_type=compute_type)
        return self.asr[selected]

    def get_diarization(self):
        if self.diarization is not None:
            return self.diarization
        import torch
        from pyannote.audio import Pipeline

        source = Path(DIARIZATION_MODEL).expanduser()
        origin = str(source) if source.exists() else DIARIZATION_MODEL
        kwargs = {"token": HF_TOKEN} if HF_TOKEN and not source.exists() else {}
        pipeline = Pipeline.from_pretrained(origin, **kwargs)
        device = self.resolved_device()
        if device == "cuda":
            pipeline.to(torch.device("cuda"))
        elif device == "mps":
            pipeline.to(torch.device("mps"))
        self.diarization = pipeline
        return pipeline

    def get_embedding(self):
        if self.embedding is not None:
            return self.embedding
        import torch
        from pyannote.audio import Inference, Model

        source = Path(EMBEDDING_MODEL).expanduser()
        origin = str(source) if source.exists() else EMBEDDING_MODEL
        kwargs = {"token": HF_TOKEN} if HF_TOKEN and not source.exists() else {}
        model = Model.from_pretrained(origin, **kwargs)
        inference = Inference(model, window="whole")
        device = self.resolved_device()
        if device in {"cuda", "mps"}:
            inference.to(torch.device(device))
        self.embedding = inference
        return inference

    def get_admin_antispoof(self):
        if self.admin_antispoof is not None:
            return self.admin_antispoof
        with self._admin_model_lock:
            if self.admin_antispoof is not None:
                return self.admin_antispoof
            if not ADMIN_ANTISPOOF_MODEL or not ADMIN_ANTISPOOF_ARCHITECTURE:
                raise RuntimeError("Admin anti-spoof model is not configured")
            from admin_antispoof import AASISTVerifier

            try:
                self.admin_antispoof = AASISTVerifier(
                    ADMIN_ANTISPOOF_ARCHITECTURE,
                    ADMIN_ANTISPOOF_MODEL,
                    device=self.resolved_device(),
                )
                self.admin_antispoof_error = ""
            except Exception as exc:
                self.admin_antispoof_error = type(exc).__name__
                raise
            return self.admin_antispoof

    def admin_voice_ready(self) -> bool:
        """Warm every model used by the fail-closed admin ceremony.

        Reporting only an anti-spoof model here would produce a misleading
        healthy status when the later transcript or speaker verification model
        could not load.  The startup launcher therefore waits for all three.
        """
        if not ADMIN_VOICE_AUTH_ENABLED or not module_available("pyannote.audio"):
            self.admin_voice_error = "admin_voice_dependencies_unavailable"
            return False
        try:
            self.ensure_capacity()
            self.get_asr(DEFAULT_ASR_MODEL)
            self.get_embedding()
            self.get_admin_antispoof()
            self.admin_voice_error = ""
            return True
        except Exception as exc:
            self.admin_voice_error = type(exc).__name__
            return False


models = Models()


def module_available(name: str) -> bool:
    try:
        __import__(name)
        return True
    except Exception:
        return False


def save_upload(audio: bytes) -> str:
    handle = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    try:
        handle.write(audio)
        handle.close()
        with wave.open(handle.name, "rb") as wav:
            if wav.getnchannels() != 1 or wav.getsampwidth() != 2:
                raise HTTPException(status_code=400, detail="Expected mono PCM16 WAV audio")
        return handle.name
    except Exception:
        try:
            os.unlink(handle.name)
        except FileNotFoundError:
            pass
        raise


def load_waveform(path: str) -> dict:
    """Decode validated PCM16 WAV without relying on TorchCodec native libraries."""
    import numpy as np
    import torch

    with wave.open(path, "rb") as wav:
        sample_rate = wav.getframerate()
        samples = np.frombuffer(
            wav.readframes(wav.getnframes()),
            dtype="<i2",
        ).astype(np.float32)
    waveform = torch.from_numpy(samples / 32768.0).unsqueeze(0)
    return {"waveform": waveform, "sample_rate": sample_rate}


@app.get("/health")
async def health(_: None = Depends(authorize)):
    admin_voice_ready = await asyncio.to_thread(models.admin_voice_ready)
    return {
        "status": "healthy",
        "busy": models.lock.locked(),
        "device": models.resolved_device(),
        "capabilities": {
            "transcription": module_available("faster_whisper"),
            "diarization": module_available("pyannote.audio"),
            "embedding": module_available("pyannote.audio"),
            "admin_voice_auth": admin_voice_ready,
        },
        "models_loaded": {
            "asr": list(models.asr),
            "diarization": models.diarization is not None,
            "embedding": models.embedding is not None,
            "admin_antispoof": models.admin_antispoof is not None,
        },
        "admin_voice_error": models.admin_voice_error or None,
    }


@app.post("/v1/transcribe")
async def transcribe(
    audio: UploadFile = File(...),
    offset_ms: int = Form(default=0),
    model: str = Form(default=""),
    language: str = Form(default="auto"),
    final: bool = Form(default=False),
    _: None = Depends(authorize),
):
    if models.lock.locked():
        raise HTTPException(status_code=409, detail="Worker is busy")
    payload = await audio.read()
    path = save_upload(payload)
    try:
        async with models.lock:
            models.ensure_capacity()

            def run():
                asr = models.get_asr(model or DEFAULT_ASR_MODEL)
                segments, info = asr.transcribe(
                    path,
                    language=None if language == "auto" else language,
                    beam_size=5 if final else 3,
                    vad_filter=True,
                    word_timestamps=True,
                    condition_on_previous_text=final,
                )
                results = []
                for segment in segments:
                    words = [
                        {
                            "start_ms": offset_ms + round((word.start or 0) * 1000),
                            "end_ms": offset_ms + round((word.end or 0) * 1000),
                            "word": word.word,
                            "probability": word.probability,
                        }
                        for word in (segment.words or [])
                    ]
                    results.append(
                        {
                            "start_ms": offset_ms + round(segment.start * 1000),
                            "end_ms": offset_ms + round(segment.end * 1000),
                            "text": segment.text.strip(),
                            "confidence": None,
                            "words": words,
                        }
                    )
                return results, info.language, info.language_probability

            segments, detected_language, language_probability = await asyncio.to_thread(run)
            return {
                "segments": segments,
                "language": detected_language,
                "language_probability": language_probability,
                "model": model or DEFAULT_ASR_MODEL,
                "device": models.resolved_device(),
            }
    finally:
        os.unlink(path)


@app.post("/v1/diarize")
async def diarize(
    audio: UploadFile = File(...),
    num_speakers: Optional[int] = Form(default=None, ge=1, le=20),
    min_speakers: Optional[int] = Form(default=None, ge=1, le=20),
    max_speakers: Optional[int] = Form(default=None, ge=1, le=20),
    _: None = Depends(authorize),
):
    if min_speakers and max_speakers and min_speakers > max_speakers:
        raise HTTPException(status_code=422, detail="min_speakers cannot exceed max_speakers")
    if models.lock.locked():
        raise HTTPException(status_code=409, detail="Worker is busy")
    payload = await audio.read()
    path = save_upload(payload)
    try:
        async with models.lock:
            models.ensure_capacity()

            def run():
                def annotation_turns(annotation):
                    if hasattr(annotation, "itertracks"):
                        iterator = (
                            (turn, speaker)
                            for turn, _, speaker in annotation.itertracks(yield_label=True)
                        )
                    else:
                        iterator = iter(annotation)
                    return [
                        {
                            "start_ms": round(turn.start * 1000),
                            "end_ms": round(turn.end * 1000),
                            "speaker": str(speaker),
                        }
                        for turn, speaker in iterator
                    ]

                options = {
                    key: value
                    for key, value in {
                        "num_speakers": num_speakers,
                        "min_speakers": min_speakers,
                        "max_speakers": max_speakers,
                    }.items()
                    if value is not None
                }
                output = models.get_diarization()(load_waveform(path), **options)
                regular_annotation = getattr(output, "speaker_diarization", output)
                regular_turns = annotation_turns(regular_annotation)
                exclusive_annotation = getattr(output, "exclusive_speaker_diarization", None)
                selected_annotation = (
                    exclusive_annotation if exclusive_annotation is not None else regular_annotation
                )
                turns = annotation_turns(selected_annotation)

                events = {}
                for turn in regular_turns:
                    events.setdefault(turn["start_ms"], []).append((turn["speaker"], True))
                    events.setdefault(turn["end_ms"], []).append((turn["speaker"], False))
                active = set()
                previous = None
                overlaps = []
                for timestamp in sorted(events):
                    if previous is not None and timestamp > previous and len(active) > 1:
                        if overlaps and overlaps[-1]["end_ms"] == previous:
                            overlaps[-1]["end_ms"] = timestamp
                        else:
                            overlaps.append({"start_ms": previous, "end_ms": timestamp})
                    for speaker, starting in events[timestamp]:
                        if starting:
                            active.add(speaker)
                        else:
                            active.discard(speaker)
                    previous = timestamp
                return turns, overlaps

            turns, overlap_ranges = await asyncio.to_thread(run)
            return {
                "turns": turns,
                "overlap_ranges": overlap_ranges,
                "model": DIARIZATION_MODEL,
                "device": models.resolved_device(),
                "speaker_count": len({turn["speaker"] for turn in turns}),
                "requested_speaker_count": num_speakers,
            }
    finally:
        os.unlink(path)


@app.post("/v1/embed")
async def embed(
    audio: UploadFile = File(...),
    _: None = Depends(authorize),
):
    if models.lock.locked():
        raise HTTPException(status_code=409, detail="Worker is busy")
    payload = await audio.read()
    path = save_upload(payload)
    try:
        with wave.open(path, "rb") as wav:
            duration = wav.getnframes() / max(1, wav.getframerate())
        async with models.lock:
            models.ensure_capacity()

            def run():
                result = models.get_embedding()(load_waveform(path))
                values = result.reshape(-1).tolist()
                return [float(value) for value in values]

            embedding = await asyncio.to_thread(run)
            return {
                "embedding": embedding,
                "duration_seconds": duration,
                "model": EMBEDDING_MODEL,
                "device": models.resolved_device(),
            }
    finally:
        os.unlink(path)


@app.post("/v1/admin/verify")
async def verify_admin_voice(
    audio: UploadFile = File(...),
    challenge_id: str = Form(..., min_length=8, max_length=128),
    reference_embedding: str = Form(default=""),
    _: None = Depends(authorize),
):
    """Verify an admin challenge without persisting audio or credentials."""
    if not ADMIN_VOICE_AUTH_ENABLED:
        raise HTTPException(status_code=503, detail="Admin voice authentication is disabled")
    if not ADMIN_ANTISPOOF_MODEL:
        raise HTTPException(status_code=503, detail="Admin anti-spoof model is not configured")
    if not ADMIN_ANTISPOOF_ARCHITECTURE:
        raise HTTPException(status_code=503, detail="Admin anti-spoof architecture is not configured")
    try:
        reference = [float(value) for value in json.loads(reference_embedding)]
    except (TypeError, ValueError, json.JSONDecodeError):
        raise HTTPException(status_code=422, detail="A valid reference voice embedding is required")
    if not reference:
        raise HTTPException(status_code=422, detail="A valid reference voice embedding is required")
    if models.lock.locked():
        raise HTTPException(status_code=409, detail="Worker is busy")
    payload = await audio.read()
    path = save_upload(payload)
    try:
        async with models.lock:
            models.ensure_capacity()

            def run():
                import torch

                waveform = load_waveform(path)
                asr = models.get_asr(DEFAULT_ASR_MODEL)
                segments, _ = asr.transcribe(
                    path,
                    language=None,
                    beam_size=3,
                    vad_filter=True,
                    word_timestamps=False,
                    condition_on_previous_text=False,
                )
                transcript = " ".join(segment.text.strip() for segment in segments if segment.text.strip())
                probe = models.get_embedding()(waveform).reshape(-1).float()
                known = torch.tensor(reference, dtype=probe.dtype).reshape(-1)
                if probe.numel() != known.numel() or probe.numel() == 0:
                    speaker_score = 0.0
                else:
                    speaker_score = float(torch.nn.functional.cosine_similarity(probe, known, dim=0).item())
                liveness_score = models.get_admin_antispoof().score(waveform)
                return transcript, speaker_score, liveness_score, probe.detach().cpu().tolist()

            transcript, speaker_score, liveness_score, speaker_embedding = await asyncio.to_thread(run)
            return {
                "challenge_id": challenge_id,
                "transcript": transcript,
                "speaker_score": round(speaker_score, 4),
                "liveness_score": round(liveness_score, 4),
                "speaker_embedding": speaker_embedding,
                "required_speaker_score": ADMIN_SPEAKER_THRESHOLD,
                "required_liveness_score": ADMIN_LIVENESS_THRESHOLD,
                "model_version": f"{EMBEDDING_MODEL}|{Path(ADMIN_ANTISPOOF_MODEL).name}",
            }
    finally:
        os.unlink(path)
