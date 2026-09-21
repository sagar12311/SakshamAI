# Hosted beta deployment

The public beta runs inference on a Saksham-operated home server. The only
publicly routed service is the Gateway; never route the existing desktop backend,
LM Studio, Chatterbox, or Meeting Intelligence worker directly through Cloudflare.

## Deploy

1. Create a Supabase project and configure email/password authentication.
2. Create a named Cloudflare Tunnel and route `api.saksham.ai` to the
   `cloudflared` service. Do not use a Quick Tunnel for production.
3. Copy `deploy/.env.example` to `deploy/.env`, generate unique passwords and
   worker keys, and set the real Supabase URL.
4. From `deploy`, run `docker compose -f docker-compose.public.yml up -d --build`.
5. Configure the website to send Supabase access tokens to
   `https://api.saksham.ai/v1/*`.

## Safety properties

- The compose file does not deploy the desktop backend, so its automation and
  system-control endpoints are not reachable by public users.
- Postgres stores only per-user/day token and meeting-duration counters.
- BYOK keys are accepted only in the request header and are not persisted.
- BYOK provider URLs must be HTTPS and match `ALLOWED_BYOK_HOSTS`, preventing
  users from turning the gateway into an internal-network proxy.
- The meeting endpoint requires consent and applies an upload-size plus daily
  meeting-time limit before forwarding content to the private worker.
