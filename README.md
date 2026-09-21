# Saksham AI

Saksham is an experimental, local-first AI assistant for macOS. It combines a
FastAPI backend with a React/Electron client, voice interaction, optional
meeting intelligence, and opt-in macOS automation.

> **Beta software.** Review every action before enabling system control. The
> public configuration runs locally and keeps privileged capabilities disabled.

## Quick start: local/self-hosted

Prerequisites: macOS, Python 3.11+, Node.js 18+, and optionally an
OpenAI-compatible local model server such as [LM Studio](https://lmstudio.ai/).

```bash
git clone https://github.com/NEFEX-Technologies/saksham.git
cd saksham/backend
python -m venv .venv
source .venv/bin/activate
pip install -e .
cp .env.example .env
python -m uvicorn main:app --host 127.0.0.1 --port 8420
```

In another terminal, run `npm ci` and `npm run dev` from `frontend`, then open
`http://127.0.0.1:5173`. Electron development is available through
`npm run electron:dev`.

## Safety and advanced features

The source includes Chat, Meetings, optional local voice services, and optional
macOS automation. Automation, hands-free/admin activation, and commerce
features are disabled in `backend/.env.example`. Enable them only on a machine
you own after reviewing the relevant source and safeguards.

Optional Chatterbox and Meeting Intelligence services must bind to loopback or
a private network. Do not expose ports `8420`, `8100`, `8110`, or a model server
directly to the public internet. Model weights are not redistributed; some
third-party models require accepting their upstream terms.

## Hosted beta

A hosted beta may provide authenticated access to a separately deployed,
quota-limited inference gateway. It never exposes the maintainers' model,
meeting-worker, or private-network endpoints. Hosted access is optional; this
repository remains usable with your own local model endpoint.

## Documentation

- [Launchers](scripts/README.md)
- [Chatterbox TTS](services/chatterbox_tts/README.md)
- [Meeting Intelligence](services/meeting_intelligence/README.md)
- [Security policy](SECURITY.md)
- [Third-party and model notices](THIRD_PARTY_NOTICES.md)

## Contributing and license

Please read [CONTRIBUTING.md](CONTRIBUTING.md) and our
[Code of Conduct](CODE_OF_CONDUCT.md). Saksham is licensed under
[Apache-2.0](LICENSE).
