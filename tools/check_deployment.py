"""Validate operator config without printing or transmitting its values."""
import argparse
import json
import re
from pathlib import Path
from urllib.parse import urlparse
from uuid import UUID


def validate(path: Path, public: bool) -> list[str]:
    if not path.is_file():
        return ["Deployment env file is missing; copy deploy/.env.example into a private env file."]
    env = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, separator, value = line.partition("=")
        if not separator:
            return ["Invalid env syntax: use one NAME=value per line."]
        env[name.strip()] = value.strip().strip("'\"")
    errors = []

    def require(name):
        value = env.get(name, "")
        if not value or any(word in value for word in ("replace-with", "your-project", "example.com")):
            errors.append(f"Configure {name} with a real value.")
        return value

    url = urlparse(require("SUPABASE_URL"))
    if url.scheme != "https" or not url.hostname or url.username or url.password:
        errors.append("SUPABASE_URL must use HTTPS without embedded credentials.")
    password = require("POSTGRES_PASSWORD")
    if not re.fullmatch(r"[A-Za-z0-9_-]{32,}", password):
        errors.append("POSTGRES_PASSWORD must contain at least 32 random URL-safe characters.")
    if env.get("BETA_INVITE_ONLY", "true").lower() != "true":
        errors.append("Keep BETA_INVITE_ONLY=true for the first home-server deployment.")
    try:
        users = json.loads(env.get("BETA_USER_IDS", "[]"))
        if not isinstance(users, list) or not users:
            raise ValueError()
        for user in users:
            UUID(user)
    except (ValueError, TypeError, AttributeError):
        errors.append("BETA_USER_IDS must contain at least one invited Supabase user UUID.")
    try:
        origins = json.loads(env.get("CORS_ORIGINS", "[]"))
        if not isinstance(origins, list) or not origins:
            raise ValueError()
        for origin in origins:
            parsed = urlparse(origin)
            if parsed.scheme != "https" or not parsed.hostname or parsed.path or parsed.query or parsed.fragment or parsed.username:
                raise ValueError()
    except (ValueError, TypeError, AttributeError):
        errors.append("CORS_ORIGINS must be a nonempty JSON list of exact HTTPS origins.")
    for name in ("DAILY_TOKEN_LIMIT", "DAILY_GLOBAL_TOKEN_LIMIT", "DAILY_MEETING_MINUTES", "DAILY_GLOBAL_MEETING_MINUTES"):
        try:
            if int(env.get(name, "0")) <= 0:
                raise ValueError()
        except ValueError:
            errors.append(f"{name} must be a positive integer.")
    if env.get("HOSTED_ENABLED", "false").lower() == "true":
        for name in ("HOSTED_LLM_BASE_URL", "HOSTED_LLM_API_KEY", "HOSTED_LLM_MODEL", "MEETING_WORKER_URL", "MEETING_WORKER_API_KEY"):
            require(name)
    if public:
        require("CLOUDFLARE_TUNNEL_TOKEN")
    return errors


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=Path("deploy/.env"))
    parser.add_argument("--public", action="store_true", help="Also require tunnel configuration")
    args = parser.parse_args()
    problems = validate(args.env_file, args.public)
    for problem in problems:
        print(f"BLOCKED: {problem}")
    if not problems:
        print("Configuration checks passed. This does not verify live accounts, DNS, or worker isolation.")
    raise SystemExit(bool(problems))
