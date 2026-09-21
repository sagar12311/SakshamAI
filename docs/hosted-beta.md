# Hosted beta deployment

The public beta runs inference on a Saksham-operated home server. The only
publicly routed service is the Gateway; never route the existing desktop backend,
LM Studio, Chatterbox, or Meeting Intelligence worker directly through Cloudflare.

## Deploy

1. Create a Supabase project and configure email/password authentication.
   Require confirmed email addresses. Before opening sign-up broadly, enable
   CAPTCHA or use an invite/allowlist so a home GPU cannot be exhausted through
   bulk account creation.
2. Create a named Cloudflare Tunnel and route `api.saksham.ai` to the
   `cloudflared` service. Do not use a Quick Tunnel for production.
3. Copy `deploy/.env.example` to `deploy/.env`, generate unique passwords and
   worker keys, and set the real Supabase URL.
4. From `deploy`, run `docker compose -f docker-compose.public.yml up -d --build`.
5. Configure the website to send Supabase access tokens to
   `https://api.saksham.ai/v1/*`.

## Meeting Intelligence

The public Meeting Intelligence screen is intentionally stateless: a signed-in
user uploads one recording, explicitly confirms participant consent, receives a
transcript, and the browser clears the selected file after completion. It does
not expose the desktop Meeting API, live capture, projects, speaker profiles,
diarization, or voice verification.

- **Hosted mode** accepts only a valid mono PCM16 WAV file. The gateway limits
  uploads to 25 MiB and 15 minutes by default, uses one shared hosted GPU job
  at a time across Chat and Meetings, and enforces a per-user daily duration
  quota.
- **BYOK mode** uses a session-only key with the allowlisted
  OpenAI-compatible `/audio/transcriptions` adapter. It does not consume the
  hosted GPU quota, but the file still passes through the authenticated gateway
  to reach the provider.
- Audio travels browser → Cloudflare → Gateway → private worker/provider.
  The gateway does not retain it deliberately; the hosted worker uses a
  temporary file during processing. Document any additional retention imposed
  by your cloud, auth, or provider configuration before inviting users.

Never expose the private worker's `/v1/diarize`, `/v1/embed`, or
`/v1/admin/verify` routes.

## Validate before deployment

The Gateway Docker image uses Python 3.12. From the repository root, run:

```bash
python3.12 -m venv .gateway-venv
.gateway-venv/bin/pip install -r gateway/requirements-dev.txt
PYTHONPATH=gateway .gateway-venv/bin/pytest gateway/tests -q
(cd frontend && npm ci && npm run build && npm test -- --run)
```

## Safety properties

- The compose file does not deploy the desktop backend, so its automation and
  system-control endpoints are not reachable by public users.
- Postgres stores only per-user/day token and meeting-duration counters.
- BYOK keys are accepted only in the request header and are not persisted.
- BYOK provider URLs must be HTTPS and match `ALLOWED_BYOK_HOSTS`, preventing
  users from turning the gateway into an internal-network proxy.
- The meeting endpoint requires consent and applies an upload-size plus daily
  meeting-time limit before forwarding content to the private worker. It also
  validates hosted WAV audio, permits one hosted job at a time, and refunds the
  reservation when the worker fails.
- Hosted Chat and Meeting limits apply both per user and across the whole beta.
  Set `DAILY_GLOBAL_TOKEN_LIMIT` and `DAILY_GLOBAL_MEETING_MINUTES` low enough
  for the capacity of the home GPU; this aggregate limit remains effective even
  if many users register accounts.
