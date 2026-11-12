# Qwen and PersonaPlex trial

This development branch targets the owner's account only. Do not expose the
voice worker, PersonaPlex, or LM Studio directly to the internet. Do not enable
the public voice frontend until local tests below pass.

## Mac: Qwen

Install LM Studio from its official site. Download `Qwen3-4B-Instruct-2507` in a
4-bit GGUF (Q4_K_M) and load it with 8192 context and one parallel request. This
is 4B, not GPT-OSS 20B. Record the exact LM Studio model identifier from `/v1/models`.
Use a dedicated API token and enable authentication. Keep the token private.

Run a local prompt set: (1) explain a concept, (2) compare two choices, (3) recall
the user explicitly prefers roasted makhana and Tuesday, (4) identify uncertainty,
and (5) produce a concise summary without treating assistant guesses as user facts.
Record answer quality, first-token delay, total latency, RAM pressure, and swap.
Restart the Mac and verify the model server can be restored before any live trial.

For Windows to reach the Mac, use a Tailscale-only address and restrict the Mac
firewall and tailnet ACL to the Windows host. LM Studio's network binding may also
listen on the LAN: require authentication and verify a LAN client cannot access it.
Do not enable a public tunnel or port forwarding. Follow LM Studio's current
server security guidance: https://lmstudio.ai/docs/developer/core/server/settings

## Windows: voice and gateway

Follow `personaplex-connection.md` to start 4-bit PersonaPlex and the `small.en`
CPU worker separately. In the private `deploy/.env`, set `HOSTED_ENABLED=true`,
`HOSTED_LLM_BASE_URL` to the Mac's private Tailscale URL including `/v1`,
`HOSTED_LLM_API_KEY` to the LM Studio token, and `HOSTED_LLM_MODEL` to the exact
loaded model identifier. Set `VOICE_ENABLED=true`, configure the worker secret,
and set `BETA_INVITE_ONLY=true` and `BETA_USER_IDS` to only the owner's UUID.
Never print or paste secrets into logs or chat. The gateway is the only public
entry point; the LAN model and worker endpoints remain private.

The same private config enables hosted chat and Qwen summary generation. The
single text slot is independent from the single voice slot. Meeting processing
still shares the text/meeting slot and should remain off in this trial.

Test Qwen alone; PersonaPlex alone with GPT-OSS unloaded; then both together.
During simultaneous inference, record first-token and total text time, voice
stuttering and interruption responsiveness, Windows `nvidia-smi`, and memory
pressure on both machines. Check behavior if the Mac or worker disconnects. An
unavailable Qwen must not erase a saved transcript or prevent voice from resuming.

## Local app and review

Use a local HTTPS frontend with a configured Supabase test session; set the public
voice build flag and matching gateway origin/CORS. A normal localhost secure context
may be used for development; ensure the gateway websocket Origin matches exactly.
Verify audio playback, microphone permissions, echo cancellation, interruption,
pause, transcript persistence, explicit remember/forget, summary review/approval,
resume, reload, deletion, and cross-account isolation. The microphone does not
require typing. A saved draft is never sent to PersonaPlex before approval.

When a conversation is paused, Qwen is requested for a draft. If unavailable,
the transcript stays saved and the UI shows an error; retry from the memory panel.
Remembering or forgetting a passage invalidates both summaries. Forget removes
the remembered marker, not the underlying transcript; recent transcript text may
still enter model context. Delete the conversation to remove application records.
Approved summaries are user-reviewed context, not a claim of model-level learning.

Only after local acceptance, deploy gateway first and frontend second. The public
beta stays invite-only with only your UUID admitted; use existing Pages/ngrok
gateway routing. Verify authentication, origins, WebSocket connectivity and
quotas from another network before considering additional users.
