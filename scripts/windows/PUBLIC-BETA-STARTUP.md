# One-click Windows public-beta startup

Prerequisites: configured public-beta `deploy/.env`, Docker Desktop/WSL2,
LM Studio with its `lms` CLI and downloaded model, and ngrok installed and
authenticated as the current Windows user. Complete the initial inference test first.

Add `NGROK_PUBLIC_URL=https://YOUR-ASSIGNED-NAME.ngrok-free.dev` to the private
`deploy/.env` (no trailing slash). Keep the same origin in Cloudflare Pages.
Keep model credentials in the private environment file and the ngrok token in
ngrok's existing user configuration. Do not put tokens in the launcher.

Double-click `Start-Public-Beta.cmd` in the repository root. You can create a
Windows desktop shortcut to this file. The launcher starts Docker if needed,
starts only the public gateway/database, checks login enforcement, starts LM
Studio if needed, reuses the loaded model or loads the configured one, and
starts the configured ngrok endpoint with local request inspection disabled.
It does not build/pull new versions or run migrations beyond normal gateway
startup. It does not start the legacy personal launcher, Tailscale routes, or
the meeting worker. Meeting startup remains a separate follow-up.

Keep the ngrok window open. If a matching ngrok agent is already running, the
launcher reuses it after a health check. It never kills another agent or deletes
database volumes. A second concurrent launcher is rejected.

This is an interactive, one-click launcher, **not a Windows boot service**.
After reboot, sign in and double-click it. Docker/LM Studio and ngrok must remain
running. Sleep, power loss, or an internet outage can still interrupt service.
Automatic sign-in, firewall changes, and privileged startup tasks are not installed.

Acceptance checks on Windows: run with Docker/LM Studio stopped; run with them
already running; launch twice; verify a wrong model key or missing ngrok token
fails clearly; verify `/ready` returns 200 and unauthenticated `/v1/projects`
returns 401 through Pages; send and reload a chat from mobile data. Reboot,
sign in, launch again, and verify saved chats persist. Do not stop personal
services for testing if they are in use.
