# Chatterbox Turbo Service

Saksham sends text plus a conversational tone to this private HTTP service and receives WAV audio. The same API supports two deployments:

- Native Apple Silicon using Metal (`mps`)
- A Linux NVIDIA server using CUDA

```text
Saksham -> http://127.0.0.1:8100 -> Chatterbox on Apple Metal
Saksham -> Tailscale -> Chatterbox on an NVIDIA CUDA server
```

## Option A: Native Apple Silicon

This is the simplest way to make Chatterbox part of Saksham on the current M3 Mac. Run it as a native Python process; Docker Desktop cannot expose the Apple GPU to a Linux container as MPS.

The current Mac has 16 GB of unified memory. Turbo should fit, but Saksham, Whisper, Electron, and Chatterbox share that memory, and the MacBook Air is fanless. Treat local MPS as a measured deployment: test generation latency and memory pressure before making it the permanent default.

### 1. Install Python and dependencies

Chatterbox requires a newer Python than the system Python 3.9 currently installed on this Mac. The setup uses the existing `uv` installation to download a self-contained Python 3.11, avoiding changes to the system Python:

```bash
cd services/chatterbox_tts
./setup_macos.sh
```

The setup script creates an isolated `.venv` using a `uv`-managed Python and installs Chatterbox Turbo, PyTorch, and the service dependencies. It does not change Saksham's backend environment.

### 2. Configure and start

```bash
cp .env.macos.example .env
openssl rand -hex 32
```

Put the generated token in `TTS_API_KEY`. The default Mac configuration uses:

```dotenv
TTS_DEVICE=mps
TTS_REQUIRE_CUDA=false
TTS_VOICE_ID=saksham
```

Start the service directly when testing it in isolation:

```bash
./run_macos.sh
```

Once Saksham is configured with the local URL, starting the backend starts this
service automatically and waits for its model warm-up. It reuses a service that
is already healthy and will not start a second process when port 8100 is occupied.

The first start downloads the Chatterbox Turbo model into the ignored `.cache/huggingface` directory and can take several minutes. It also generates a short warm-up utterance before reporting healthy, moving Metal's first-inference cost out of the first conversation. The startup probe fails rather than silently dropping to slow CPU inference if Metal is unavailable.

### 3. Verify local speech

In another terminal:

```bash
curl http://127.0.0.1:8100/health
curl --fail --request POST http://127.0.0.1:8100/v1/audio/speech \
  --header "Authorization: Bearer your-generated-token" \
  --header "Content-Type: application/json" \
  --data '{"input":"Hi Sagar. I am ready when you are.","voice":"saksham","tone":"warm","response_format":"wav"}' \
  --output chatterbox-test.wav
afplay chatterbox-test.wav
```

The health response should show `device: "mps"` and `mps_available: true` under `accelerator`.

On the current M3 MacBook Air, the first un-warmed request took 18.9 seconds for 2.56 seconds of audio. A subsequent request took 5.07 seconds for 3.56 seconds of audio, and an end-to-end Saksham request completed in 3.16 seconds. This is usable for local/private speech, but the RTX CUDA deployment remains the better target for low-latency conversation.

## Option B: NVIDIA CUDA Server

### Requirements

- A Linux host with the RTX 5060 Ti and a recent NVIDIA driver
- Docker Engine with Docker Compose
- NVIDIA Container Toolkit configured for Docker
- Tailscale on both the Mac and GPU server
- Enough free VRAM alongside the LLM

The image deliberately uses PyTorch 2.7.1 with CUDA 12.8 rather than Chatterbox's default Torch 2.6 pin. Torch 2.6 is too old for RTX 50-series Blackwell GPUs. The service also runs a real CUDA tensor operation during startup so an incompatible wheel fails immediately.

### 1. Configure the GPU server

From the repository checkout on the GPU server:

```bash
cd services/chatterbox_tts
cp .env.example .env
openssl rand -hex 32
```

Put the generated token in `TTS_API_KEY`. The service rejects missing, placeholder, or shorter-than-32-character keys. Set `TTS_BIND_ADDRESS` to the GPU server's Tailscale IPv4 address so the service is not exposed on every network interface:

```dotenv
TTS_API_KEY=your-generated-token
TTS_BIND_ADDRESS=100.x.y.z
TTS_PORT=8100
```

Do not forward port 8100 from the public internet. The API key protects requests, while the Tailscale-only bind limits network exposure.

### 2. Choose the voice

Leave `TTS_VOICE_PROMPT` empty to start with Chatterbox Turbo's bundled voice.

For a stable Saksham voice, place a clean, single-speaker WAV file in `voices/saksham.wav` and configure:

```dotenv
TTS_VOICE_ID=saksham
TTS_VOICE_PROMPT=/voices/saksham.wav
```

Use roughly 8 to 12 seconds of natural speech with little noise, music, reverb, or long silence. Only clone a voice when you have the speaker's permission. Chatterbox requires more than five seconds of reference audio.

### 3. Build and start

```bash
docker compose build
docker compose up -d
docker compose logs -f chatterbox-tts
```

The first build downloads the CUDA runtime and Python dependencies. The first service start also downloads and caches the Chatterbox model in the `chatterbox-model-cache` Docker volume, so it can take several minutes.

Check the GPU and model state:

```bash
curl http://100.x.y.z:8100/health
nvidia-smi
```

For an RTX 5060 Ti, `/health` should report CUDA 12.8 and compute capability `12.0` under `accelerator`. Test synthesis on the server before connecting Saksham:

```bash
curl --fail --request POST http://100.x.y.z:8100/v1/audio/speech \
  --header "Authorization: Bearer your-generated-token" \
  --header "Content-Type: application/json" \
  --data '{"input":"Hi Sagar. I am ready when you are.","voice":"saksham","tone":"warm","response_format":"wav"}' \
  --output chatterbox-test.wav
```

## Connect Saksham

For native MPS, set these values in `backend/.env`:

```dotenv
TTS_ENABLED=true
TTS_PROVIDER=chatterbox
CHATTERBOX_TTS_URL=http://127.0.0.1:8100
CHATTERBOX_TTS_API_KEY=your-generated-token
CHATTERBOX_TTS_VOICE=saksham
CHATTERBOX_TTS_TIMEOUT_SECONDS=45
CHATTERBOX_TTS_FALLBACK=none
CHATTERBOX_TTS_AUTOSTART=true
CHATTERBOX_TTS_STARTUP_TIMEOUT_SECONDS=120
```

For the NVIDIA server, use the same settings with its Tailscale address:

```dotenv
TTS_ENABLED=true
TTS_PROVIDER=chatterbox
CHATTERBOX_TTS_URL=http://100.x.y.z:8100
CHATTERBOX_TTS_API_KEY=your-generated-token
CHATTERBOX_TTS_VOICE=saksham
CHATTERBOX_TTS_TIMEOUT_SECONDS=45
CHATTERBOX_TTS_FALLBACK=none
CHATTERBOX_TTS_AUTOSTART=false
```

Restart the backend after changing `.env`. With `CHATTERBOX_TTS_FALLBACK=none`,
Saksham keeps showing the text response but skips speech if Chatterbox fails, so
it never surprises the user with a different voice. An alternate provider can
still be selected explicitly for deployments that value availability over voice
consistency.

## Tone and natural delivery

Saksham sends its NLU-derived tone with every request. The service maps that tone
to a narrow, conservative temperature range and resets `TTS_SEED` before each
generation. This preserves one recognizable voice while allowing emotional
prosody rather than inserting fake laughter or sighs. Chatterbox Turbo supports
native tags such as `[laugh]`, `[chuckle]`, and `[cough]`; those should only be
added later by a deliberate dialogue policy when they fit the conversation.

Natural delivery also depends on the text sent to TTS. Saksham's spoken-response path removes Markdown, shortens research-style answers, and asks the planner for conversational phrasing instead of reading search output verbatim.

## Operations

Keep one Uvicorn worker. The service serializes inference on the accelerator to avoid competing generations and memory spikes. For product-scale traffic, run one service replica per GPU and place a private queue or load balancer in front of the replicas.

If the LLM and Chatterbox share the 5060 Ti, watch `nvidia-smi` while both are active. An out-of-memory failure means the LLM quantization or context cache must be reduced, some LLM layers must be offloaded, or TTS must move to another GPU.

Useful commands:

```bash
docker compose ps
docker compose logs --tail=200 chatterbox-tts
docker compose restart chatterbox-tts
docker compose pull
docker compose build --pull
```

Upstream references: [Chatterbox](https://github.com/resemble-ai/chatterbox), [PyTorch MPS](https://docs.pytorch.org/docs/stable/notes/mps.html), and [PyTorch 2.7](https://pytorch.org/blog/pytorch-2-7/).
