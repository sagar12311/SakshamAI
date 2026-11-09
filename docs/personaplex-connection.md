# Saksham + PersonaPlex: first connected voice beta

## Status and behavior

This branch adds a voice workspace, authenticated gateway WebSocket, private WSL
audio adapter, CPU `small.en` transcription, and PostgreSQL voice history.
It has not been tested end-to-end on the Windows GPU host or deployed to Pages.

The browser sends mono PCM16 at 24 kHz. The gateway authenticates a Supabase token
in the first WebSocket message (not the URL), checks Origin and conversation
ownership, then connects to the private adapter using a separate shared secret.
The adapter translates PCM/Opus for the existing PersonaPlex server at port 8998.
Speech replies stay native speech-to-speech; ASR runs separately on CPU for logs.

Transcripts are finalized in short chunks. They can arrive out of speaking order,
and generated assistant text may include words interrupted before playback.
No recording files are written by the adapter; raw audio is transient in memory.
Gateway/worker access logging is disabled in the commands below. PersonaPlex itself
may print prompts in its terminal: do not publish or retain those logs with personal
details. Deleting a conversation removes live database events, not existing backups.

Pause stops microphone input and waits for transcript saving. Check a user caption
and choose **Remember this detail** to include it in the next starting prompt.
To correct a detail, forget the old one, say the correction, pause, and remember the
correct caption. Assistant captions cannot become confirmed memories. Context
includes at most 16 events, prioritizing up to eight confirmed passages. Passages
are bounded to 400 characters in model context; this is not unlimited recall.
There is no dynamic in-session prompt injection. Resume reconnects and can take
time while PersonaPlex prepares the new prompt. No second text LLM is loaded.

## Windows/WSL setup (after syncing this branch)

Keep the normal text model and Meeting worker off during initial validation.
Only gateway/PostgreSQL, the existing tunnel, PersonaPlex, and this adapter are needed.

In Ubuntu, install the adapter into the separate ASR environment:

```bash
cd /mnt/c/SakshamPublicBeta/app
~/saksham-voice-benchmark/.venv-asr/bin/python -m pip install -r services/voice/requirements.txt --timeout 120 --retries 10 --resume-retries 10
```

This pins PyAV 16.0.1 and sphn 0.2.1. Do **not** install these requirements into
PersonaPlex's `.venv`: its codec Python API differs. Their on-wire audio is Ogg/Opus.

Generate a shared secret locally:

```bash
python3 -c 'import secrets; print(secrets.token_urlsafe(48))'
```

Store it as `VOICE_WORKER_KEY` in the private Windows `deploy/.env` and enter the
same value into the adapter terminal using a hidden prompt. Never paste it in chat.
Configure the gateway's private `.env`:

```dotenv
VOICE_ENABLED=true
VOICE_WORKER_URL=ws://host.docker.internal:8999/stream
VOICE_WORKER_KEY=YOUR_GENERATED_SECRET
VOICE_MAX_SESSION_SECONDS=600
CORS_ORIGINS=["https://sakshampublicbeta.pages.dev"]
```

Preserve any other allowed origins you already need. The session ceiling includes
model preparation; pause before reaching it to flush the final audio. Beta access
still uses your existing authenticated user allowlist.

Ubuntu terminal 1: start the already-tested PersonaPlex runtime:

```bash
cd ~/saksham-voice-benchmark
source .venv/bin/activate
PYTHONPATH="$PWD/runtime-4bit${PYTHONPATH:+:$PYTHONPATH}" python -m moshi.server --host 127.0.0.1 --port 8998 --quantize-4bit
```

Close any existing direct PersonaPlex browser session so the model is available.

Ubuntu terminal 2: start the adapter:

```bash
cd /mnt/c/SakshamPublicBeta/app
read -rsp 'Voice worker secret: ' VOICE_WORKER_KEY
export VOICE_WORKER_KEY
~/saksham-voice-benchmark/.venv-asr/bin/python -m uvicorn worker:app --app-dir services/voice --host 127.0.0.1 --port 8999 --no-access-log --ws-max-size 65536
```

First start downloads `small.en` only if it isn't already cached. Both processes
must remain running. Do not tunnel ports 8998 or 8999 directly.

In PowerShell, rebuild the gateway (this does not start the text model):

```powershell
cd C:\SakshamPublicBeta\app
docker compose --env-file deploy\.env -f deploy\docker-compose.public.yml up -d --build gateway
```

Verify the Docker-to-WSL adapter path without displaying the secret:

```powershell
docker compose --env-file deploy\.env -f deploy\docker-compose.public.yml exec gateway python -c "import os,httpx; r=httpx.get('http://host.docker.internal:8999/health',headers={'Authorization':'Bearer '+os.environ['VOICE_WORKER_KEY']},timeout=10); print(r.status_code,r.text)"
```

Expected: HTTP 200, `asr_ready: true`. This checks the adapter, not GPU inference.
If connection is refused, stop here and investigate Windows/WSL localhost
forwarding. Do not solve it by exposing the raw model or adapter to the internet.

## Frontend rollout and GPU acceptance

Deploy this branch's gateway before its frontend. The Pages function must preserve
WebSocket upgrades; this branch includes that change. Build the frontend with
`VITE_SAKSHAM_VOICE_ENABLED=true`. Keep its existing Supabase and gateway values.
Only the public UI flag belongs in Pages; never add the worker secret there.

The new build permits microphone use on its own origin and allows `wss` to the
configured gateway. Use the same HTTPS Pages origin listed in gateway CORS.

1. Sign in, select **Voice**, accept microphone/transcript consent, and Start talking.
2. Confirm live speech, interruptions and normal latency through the entire tunnel.
3. Say the snack/day, pause, and verify saved user and assistant captions.
4. Remember the correct user passage. Resume and ask for the snack and day.
5. Reload the page, reopen Voice history, and verify the saved transcript.
6. Test another account: it must not see or access the first account's history.
7. Test a second simultaneous session: it should report busy, not queue on the GPU.
8. Unplug/reconnect networking; confirm clear failure and slot release. Recent
   unflushed speech may be lost on abrupt departure. Use Pause before navigation.

Keep this opt-in until these Windows/tunnel checks pass. Turning `VOICE_ENABLED`
and the UI flag off disables the feature while preserving existing transcripts.

## Local verification

Gateway tests use disposable PostgreSQL and a simulated private worker. Worker
tests use real sphn/PyAV audio conversion with simulated PersonaPlex and ASR text;
they do not load a model. Frontend tests cover consent, errors, auth transport and
pause/microphone cleanup. None substitutes for the Windows/GPU acceptance above.

Protocol reference: https://github.com/NVIDIA/personaplex/blob/main/moshi/moshi/server.py
WebSocket proxy reference: https://developers.cloudflare.com/workers/runtime-apis/websockets/
