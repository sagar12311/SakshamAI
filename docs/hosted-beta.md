# Hosted beta deployment

The public beta runs inference on a Saksham-operated home server. The only
publicly routed service is the Gateway; never route the existing desktop backend,
LM Studio, Chatterbox, or Meeting Intelligence worker directly through Cloudflare.

## Deploy

Deployment requires operator access to Supabase, Cloudflare, and DNS for
`saksham.ai`. Do not paste private keys into issues, chat, or browser build
variables. This repository contains templates, not live production secrets.

### 1. Supabase

Create/select a Free project, enable email/password authentication and email
confirmation, and use an asymmetric JWT signing key (ES256 or RS256). Legacy
HS256 projects must migrate signing keys before using this gateway's JWKS
validation. Disable anonymous sign-in. Set the Auth Site URL to
`https://saksham.ai` and allow only your intended confirmation/redirect URLs.

Copy the project URL and **publishable** key (or legacy **anon** key) into the
frontend build settings below. Never use the service-role/secret key there.
The gateway validates sessions using public signing keys; it needs no Supabase
service-role key.

Create your test account and add its UUID from Supabase Auth Users to the
private `BETA_USER_IDS` JSON list. The initial deployment is invite-only:
registration alone does not grant GPU or BYOK gateway access.

### 2. Cloudflare Pages frontend

Connect the `sagar12311/SakshamAI` repository using these settings:

| Setting | Value |
| --- | --- |
| Production branch | `public-beta` |
| Root directory | `frontend` |
| Node version | `24` (`NODE_VERSION` build variable) |
| Build command | `npm run build:public` |
| Output directory | `dist-public` |
| `VITE_SUPABASE_URL` | Your Supabase HTTPS project URL |
| `VITE_SUPABASE_ANON_KEY` | Your publishable/anon key |
| `VITE_SAKSHAM_GATEWAY_URL` | `https://api.saksham.ai` |

The dedicated public entry does not import the desktop app or its local API
proxies. The build fails when configuration is missing or a privileged
Supabase key is detected. `VITE_PUBLIC_BETA` is not needed by this build.
No tunnel tokens, model keys, or production env files belong in Pages.

Add `saksham.ai` as a Pages custom domain after reviewing the preview. If using
another domain or a custom Supabase hostname, update both the gateway's exact
`CORS_ORIGINS` and `frontend/public-beta/_headers` connect-src policy before
rebuilding. Do not allow arbitrary preview origins access to your gateway.

### 3. Home gateway (local validation first)

Create a private `deploy/.env` from the example. Generate a random URL-safe
Postgres password (e.g. `openssl rand -hex 32`). Compose constructs the database
URL from this password; it passes each service only its own configuration.
Point workers at a dedicated beta instance with separate credentials and no
personal data directories. Keep `HOSTED_ENABLED=false` initially.

From the repository root:

```bash
python3 tools/check_deployment.py --env-file deploy/.env
docker compose --env-file deploy/.env -f deploy/docker-compose.public.yml up -d --build --wait
curl --fail http://127.0.0.1:8080/ready
```

This starts only Postgres and the gateway, bound to loopback. Keep one gateway
replica and one Uvicorn worker: the concurrency gate is per process and only
coordinates requests through this gateway, not personal or other GPU jobs.
Choose quotas after measuring your GPU; the defaults are starting limits,
not a hardware safety guarantee.

### 4. Tunnel and first invited-user test

For a domain-free beta, Tailscale Funnel can expose the loopback-bound gateway
on HTTPS port 443. Configure Cloudflare Pages production variables
`SAKSHAM_GATEWAY_ORIGIN` with the stable Funnel HTTPS origin and
`VITE_SAKSHAM_GATEWAY_URL` with the Pages site origin. The Pages Function under
`/v1/*` forwards only gateway API requests. The gateway still verifies every
account token, and the Mac must remain online. Keep the private GPU and
Postgres ports off Funnel. A quick `trycloudflare.com` tunnel is for temporary
testing and should not be used as the production API address.

To check the Funnel route and unauthenticated boundary, run:

```bash
tailscale funnel status --json
curl --fail https://YOUR-FUNNEL-NAME.ts.net/ready
curl -i https://YOUR-FUNNEL-NAME.ts.net/v1/projects  # expect 401
```

The browser calls `/v1/*` on its own Pages origin, so users do not need the
Tailscale client or a browser connection to the Funnel hostname.

Create a named, remotely managed Cloudflare Tunnel. Its published application
route must be `api.saksham.ai` → **HTTP `gateway:8080`**. The tunnel connector
is `cloudflared`; the destination service is `gateway`, not `cloudflared` or
`localhost`. Do not route private worker/model ports or private subnets.
Put the connector token only in your private deployment env file.

```bash
python3 tools/check_deployment.py --env-file deploy/.env --public
docker compose --env-file deploy/.env -f deploy/docker-compose.public.yml --profile public up -d --build --wait
```

Sign in as the invited user and test BYOK with your own provider key. Once the
dedicated inference endpoints are verified, set `HOSTED_ENABLED=true` privately
and recreate the gateway. Confirm a non-invited account gets 403, a request
without a session gets 401, and personal/worker routes get 404. Keep beta access
invite-only until ingress rate limiting and load testing are complete; upload
length checks alone are not full denial-of-service protection.

To stop public traffic while keeping local services running:

```bash
docker compose --env-file deploy/.env -f deploy/docker-compose.public.yml --profile public stop cloudflared
```

References: [Cloudflare Pages build settings](https://developers.cloudflare.com/pages/configuration/build-configuration/),
[Tunnel tokens](https://developers.cloudflare.com/tunnel/reference/tunnel-tokens/),
[Supabase JWT signing keys](https://supabase.com/docs/guides/auth/signing-keys).

## Meeting Intelligence

The public Meeting Intelligence screen is intentionally stateless: a signed-in
user explicitly confirms participant consent, records from the browser
microphone in real time, stops the recording, and receives a transcript. There
is no file picker or recording library. Audio remains in browser memory while
recording and is sent once to the gateway only after stop; it is then cleared
from the browser. This is live capture, not streaming transcription. It does
not expose the desktop Meeting API, projects, speaker profiles, diarization,
voice verification, or private commands.

- **Hosted mode** receives a browser-produced mono PCM16 WAV after the user
  stops capture. The gateway limits the final request to 25 MiB and 15 minutes
  by default, uses one shared hosted GPU job
  at a time across Chat and Meetings, and enforces a per-user daily duration
  quota.
- **BYOK mode** uses a session-only key with the allowlisted
  OpenAI-compatible `/audio/transcriptions` adapter. It does not consume the
  hosted GPU quota, but the final live-capture buffer still passes through the
  authenticated gateway to reach the provider.
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
(cd frontend && npm ci && npm run build:public && npm test -- --run)
bash tools/smoke-public.sh
```

The frontend command requires the three public build variables listed above.
The Docker smoke test builds the real gateway image on a disposable private
network, checks readiness and unauthenticated access, and exercises quotas
against real Postgres (including fresh accounts, global rollback, and concurrent
reservations). It uses no GPU, tunnel, production secrets, or host mounts and
removes only its own containers afterward. CI also builds the separate public
frontend with fake public configuration.

## Safety properties

- The compose file does not deploy the desktop backend, so its automation and
  system-control endpoints are not reachable by public users.
- Postgres stores usage counters and account-owned projects, conversations, and
  message history. Chats are retained until the user deletes them; deleting a
  project keeps its chats. Provider API keys and meeting audio are never stored
  in this database. This is server-side storage, not end-to-end encryption.
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
