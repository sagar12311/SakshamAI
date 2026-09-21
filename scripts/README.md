# Saksham launchers

These optional launchers start a self-hosted Saksham installation. They are
deliberately local-first: model and meeting-worker services must remain on a
loopback or private network. Do not expose LM Studio, Chatterbox, or Meeting
Intelligence ports directly to the public internet.

## One-time Windows installation

Copy the repository to a location such as `C:\Saksham`, set
`SAKSHAM_SERVER_ROOT` if using another location, then run this once in
PowerShell:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
& 'C:\Saksham\launcher\Install-Saksham-Shortcuts.ps1'
```

This creates **Start Saksham Server** and **Stop Saksham Server** on the Windows
desktop. The Start shortcut:

1. Connects Tailscale when it is installed. Set `SAKSHAM_TAILSCALE_IP` only if
   you want to pin the expected private address.
2. Builds/starts the CUDA Meeting Intelligence worker and verifies its admin anti-spoof model.
3. Starts LM Studio headlessly, loads `openai/gpt-oss-20b`, and serves port `1234`.
4. Ensures private Tailscale forwarding for ports `8110` and `1234`.

The Stop shortcut waits for the meeting worker to become idle, unloads LM Studio,
stops the worker and Docker Desktop, then takes Tailscale offline. Its persistent
Serve configuration resumes on the next Start.

If `lms` is not found, open LM Studio once and install its command-line integration.
The launcher never logs the worker API key from `C:\Saksham\meeting_intelligence\.env`.

## One-time Mac installation

Run:

```bash
cd "/path/to/saksham"
./scripts/macos/install-shortcuts.command
```

This creates **Start Saksham** and **Stop Saksham** on the Mac desktop. Click the
Windows Start shortcut first, followed by Mac Start. Mac Start verifies the remote
worker and LLM, starts Uvicorn and Chatterbox through the backend, starts Vite, and
opens `http://127.0.0.1:5173` only after the complete stack is healthy.

Shutdown is the reverse: Mac Stop first, then Windows Stop. Mac Stop refuses to
continue while a meeting is recording, stopping, or processing.

## Diagnostics

- Windows launcher: `C:\Saksham\logs\launcher-windows.log`
- Mac launcher: `~/.saksham/logs/launcher-macos.log`
- Mac backend: `~/.saksham/logs/backend.log`
- Mac frontend: `~/.saksham/logs/frontend.log`

Advanced overrides are optional:

- `SAKSHAM_SERVER_ROOT` changes the Windows root from `C:\Saksham`.
- `SAKSHAM_TAILSCALE_IP` changes the expected server address.
- `SAKSHAM_REPO_ROOT` points Mac launchers at another checkout.
- `SAKSHAM_NO_OPEN=1` prevents Mac Start from opening the browser.
