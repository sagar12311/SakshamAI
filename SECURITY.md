# Security policy

Saksham can interact with local applications and may process sensitive audio,
documents, and screen context. Treat it as trusted local software, not as a
publicly exposed web service.

## Reporting a vulnerability

Do not open a public issue for a suspected vulnerability or exposed secret.
Contact the maintainers privately at **security@nefex.tech** with reproduction
steps, impact, and affected version. We will acknowledge reports within seven
days.

## Safe defaults

- Bind the backend and optional workers to loopback or a private network.
- Keep `MAC_AUTOMATION_ENABLED`, `ADMIN_VOICE_ENABLED`,
  `HANDS_FREE_MODE`, and commerce features disabled until explicitly reviewed.
- Use unique generated tokens for optional worker services; never reuse or
  commit them.
- Obtain participant consent before recording meetings.
