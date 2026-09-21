# Saksham Meeting Intelligence

Private ASR, speaker diarization, voice-embedding, and admin voice-verification worker. Models load lazily, jobs run one at a time, and CUDA jobs are refused when free VRAM is below `MEETING_GPU_MIN_FREE_MB`; this keeps LM Studio online.

Generate a shared API key with `python3 -c 'import secrets; print(secrets.token_urlsafe(32))'` and put the same value in the backend and worker environment files. The worker fails closed when this key is missing or left as the example placeholder.

## macOS fallback

```bash
./setup_macos.sh
cp .env.example .env
./run_macos.sh
```

Install `ffmpeg` first (`brew install ffmpeg`) if it is not already available. For fully offline operation, set each model variable to a pre-downloaded local directory before starting the worker.
When the Saksham backend starts this worker, it injects the shared API key automatically; the local `.env` is only needed for `HF_TOKEN` and optional model overrides. Accept the Community-1 conditions before setting the token. Never paste the token into chat or commit the `.env` file.

## NVIDIA server

1. Accept the conditions for `pyannote/speaker-diarization-community-1` on Hugging Face.
2. Set `HF_TOKEN` and the same random API key used by Saksham.
3. Copy this `meeting_intelligence` directory and the repository's `scripts/windows` directory to the RTX host, then run `C:\Saksham\scripts\windows\Start-Saksham-Server.ps1`. It validates the two required worker secrets and runs `docker compose up --build -d`. The image fetches the official AASIST architecture and pretrained weights at the pinned commit in the Dockerfile.
4. Configure `MEETING_INTELLIGENCE_REMOTE_URL`, `ADMIN_VOICE_WORKER_URL`, and `ADMIN_VOICE_ENABLED=true` in the Saksham backend.
5. Check the authenticated `/health` response. `capabilities.admin_voice_auth` must be `true` before a dangerous task can proceed. This field now becomes true only after the transcript, speaker-embedding, and anti-spoof models have all loaded.

The worker exposes `/health`, `/v1/transcribe`, `/v1/diarize`, `/v1/embed`, and `/v1/admin/verify`. Pyannote telemetry is disabled by default. Downloaded models remain in the local Docker volume, image, or Hugging Face cache for offline use.

Admin verification combines the random spoken challenge, speaker similarity, PIN verification on the Mac, and an AASIST bona-fide speech score. AASIST is a useful anti-spoofing baseline trained on ASVspoof 2019 Logical Access; it is not a guarantee against every physical replay or future voice-cloning attack. Dangerous actions therefore remain fail-closed on any worker or verification uncertainty.
